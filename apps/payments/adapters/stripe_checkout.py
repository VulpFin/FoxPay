from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.urls import reverse

from apps.payments.models import PaymentAttempt, PaymentIntent, ProviderConfig
from .base import Capability, PaymentProviderAdapter, ProviderAdapterError


STRIPE_ADAPTERS = {"stripe", "stripe_checkout"}


def stripe_module():
    try:
        import stripe
    except ImportError as exc:
        raise ImproperlyConfigured("Install the stripe package to use the Stripe Checkout adapter.") from exc
    return stripe


def object_value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def object_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return {}


class StripeCheckoutAdapter(PaymentProviderAdapter):
    provider = "stripe"
    capabilities = [
        Capability.CARD,
        Capability.DEBIT,
        Capability.WALLET,
        Capability.HOSTED_CHECKOUT,
        Capability.WEBHOOKS,
        Capability.IDEMPOTENCY,
        Capability.MULTICURRENCY,
    ]

    def credential(self, name, fallback_setting=""):
        if self.provider_config:
            credential = (
                self.provider_config.credentials.filter(name=name, revoked_at__isnull=True)
                .order_by("-last_rotated_at")
                .first()
            )
            if credential:
                return credential.reveal_secret()
        return getattr(settings, fallback_setting, "") if fallback_setting else ""

    def secret_key(self):
        value = self.credential("secret_key", "STRIPE_SECRET_KEY")
        if not value:
            raise ImproperlyConfigured("Stripe Checkout requires a secret_key provider credential or STRIPE_SECRET_KEY.")
        return value

    def create_checkout_session(self, request, intent, payload):
        stripe = stripe_module()
        config = self.provider_config.settings if self.provider_config else {}
        payment_method_types = config.get("payment_method_types", ["card"])
        success_url = intent.success_url or request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[intent.client_secret]))
        cancel_url = intent.cancel_url or request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[intent.client_secret]))
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=self.provider_config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=self.provider,
            status=PaymentAttempt.STATUS_PENDING,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Customer completes card or debit payment on Stripe-hosted Checkout.",
        )
        metadata = {
            "foxpay_payment_intent": intent.public_id,
            "foxpay_attempt_id": str(attempt.id),
            "foxpay_merchant_id": str(intent.merchant.uuid),
        }
        if intent.reference:
            metadata["foxpay_reference"] = intent.reference
        payment_intent_data = {"metadata": metadata}
        if intent.capture_strategy == PaymentIntent.CAPTURE_MANUAL:
            payment_intent_data["capture_method"] = "manual"

        params = {
            "mode": "payment",
            "client_reference_id": intent.public_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items": [
                {
                    "price_data": {
                        "currency": intent.currency.lower(),
                        "unit_amount": intent.amount,
                        "product_data": {"name": intent.description or "Fox Pay payment"},
                    },
                    "quantity": 1,
                }
            ],
            "metadata": metadata,
            "payment_intent_data": payment_intent_data,
        }
        if payment_method_types:
            params["payment_method_types"] = payment_method_types
        if config.get("automatic_tax"):
            params["automatic_tax"] = {"enabled": True}
        if intent.customer and intent.customer.email:
            params["customer_email"] = intent.customer.email

        try:
            session = stripe.checkout.Session.create(
                **params,
                api_key=self.secret_key(),
                idempotency_key=f"foxpay:{intent.public_id}:{attempt.id}",
            )
        except Exception as exc:
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.provider_status = "create_failed"
            attempt.failure_category = exc.__class__.__name__
            attempt.failure_code = getattr(exc, "code", "") or getattr(exc, "user_message", "")[:80]
            attempt.save(update_fields=["status", "provider_status", "failure_category", "failure_code", "updated_at"])
            raise ProviderAdapterError(f"Stripe Checkout session creation failed: {exc}") from exc

        session_dict = object_to_dict(session)
        attempt.status = PaymentAttempt.STATUS_ACTION_REQUIRED
        attempt.provider_reference = object_value(session, "id", "")
        attempt.provider_status = object_value(session, "status", "")
        attempt.checkout_url = object_value(session, "url", "")
        attempt.provider_response_metadata = {
            "stripe_session_id": attempt.provider_reference,
            "stripe_payment_intent_id": object_value(session, "payment_intent", ""),
            "stripe_payment_status": object_value(session, "payment_status", ""),
            "livemode": bool(object_value(session, "livemode", False)),
            "mode": object_value(session, "mode", "payment"),
            "expires_at": object_value(session, "expires_at", None),
        }
        if session_dict.get("currency"):
            attempt.provider_response_metadata["currency"] = session_dict["currency"]
        attempt.save(
            update_fields=[
                "status",
                "provider_reference",
                "provider_status",
                "checkout_url",
                "provider_response_metadata",
                "updated_at",
            ]
        )
        return attempt

    create_attempt = create_checkout_session


def stripe_provider_config_query(provider=""):
    query = Q(adapter__in=STRIPE_ADAPTERS) | (Q(adapter="") & Q(provider__in=STRIPE_ADAPTERS))
    configs = ProviderConfig.objects.filter(kind=ProviderConfig.KIND_CARD, is_active=True).filter(query)
    if provider and provider != "stripe":
        configs = configs.filter(provider=provider)
    return configs.order_by("priority", "created_at")


def webhook_secret_candidates(provider=""):
    for config in stripe_provider_config_query(provider):
        credential = (
            config.credentials.filter(name="webhook_secret", revoked_at__isnull=True)
            .order_by("-last_rotated_at")
            .first()
        )
        if credential:
            yield config, credential.reveal_secret()
    if settings.STRIPE_WEBHOOK_SECRET:
        yield None, settings.STRIPE_WEBHOOK_SECRET


def construct_stripe_event(request, provider=""):
    stripe = stripe_module()
    signature = request.headers.get("Stripe-Signature", "")
    if not signature:
        raise ProviderAdapterError("Missing Stripe-Signature header.")
    candidates = list(webhook_secret_candidates(provider))
    if not candidates:
        raise ImproperlyConfigured("No Stripe webhook_secret provider credential or STRIPE_WEBHOOK_SECRET is configured.")
    last_error = None
    for config, secret in candidates:
        try:
            event = stripe.Webhook.construct_event(request.body, signature, secret)
            return config, object_to_dict(event) or event
        except Exception as exc:
            last_error = exc
    raise ProviderAdapterError(f"Stripe webhook signature verification failed: {last_error}") from last_error


def normalize_stripe_event(event):
    event_type = event.get("type", "unknown")
    data_object = event.get("data", {}).get("object", {})
    metadata = data_object.get("metadata") or {}
    status = ""
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        status = "paid" if data_object.get("payment_status") == "paid" else "processing"
    elif event_type == "checkout.session.async_payment_failed":
        status = "failed"
    elif event_type == "checkout.session.expired":
        status = "expired"
    else:
        status = data_object.get("status", "")

    return {
        "id": event.get("id", ""),
        "type": event_type,
        "payment_intent": metadata.get("foxpay_payment_intent") or data_object.get("client_reference_id", ""),
        "foxpay_attempt_id": metadata.get("foxpay_attempt_id", ""),
        "foxpay_method": PaymentAttempt.METHOD_CARD,
        "status": status,
        "provider_reference": data_object.get("id", ""),
        "stripe_payment_intent_id": data_object.get("payment_intent", ""),
        "stripe_payment_status": data_object.get("payment_status", ""),
        "livemode": data_object.get("livemode", False),
        "raw": event,
    }
