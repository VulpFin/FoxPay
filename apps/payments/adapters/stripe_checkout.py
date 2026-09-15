from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.urls import reverse

from apps.payments.models import PaymentAttempt, PaymentIntent, ProviderConfig
from .base import Capability, PaymentProviderAdapter, ProviderAdapterError, ProviderOperationResult, ProviderRequestError


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
        Capability.REFUNDS,
        Capability.PARTIAL_REFUNDS,
        Capability.DISPUTES,
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
        if self.provider_config and self.provider_config.connection_id and self.provider_config.connection.authorization_method == "stripe_connect":
            from apps.payments.stripe_connect import platform_secret_key

            return platform_secret_key(self.provider_config.environment)
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

        price_data = {
            "currency": intent.currency.lower(),
            "unit_amount": intent.amount,
            "product_data": {"name": intent.description or "Fox Pay payment"},
        }
        if config.get("automatic_tax"):
            price_data["tax_behavior"] = config.get("tax_behavior", "exclusive")
            if config.get("tax_code"):
                price_data["product_data"]["tax_code"] = config["tax_code"]

        params = {
            "mode": "payment",
            "client_reference_id": intent.public_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items": [
                {
                    "price_data": price_data,
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
            stripe_account = None
            if self.provider_config and self.provider_config.connection_id and self.provider_config.connection.authorization_method == "stripe_connect":
                connection = self.provider_config.connection
                if connection.status != connection.STATUS_ACTIVE or connection.revoked_at or not connection.external_account_id:
                    raise ProviderAdapterError("Stripe connection is not active.")
                stripe_account = connection.external_account_id
            session = stripe.checkout.Session.create(
                **params,
                api_key=self.secret_key(),
                idempotency_key=f"foxpay:{intent.public_id}:{attempt.id}",
                **({"stripe_account": stripe_account} if stripe_account else {}),
            )
        except Exception as exc:
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.provider_status = "create_failed"
            attempt.failure_category = exc.__class__.__name__
            attempt.failure_code = str(getattr(exc, "code", ""))[:80]
            attempt.save(update_fields=["status", "provider_status", "failure_category", "failure_code", "updated_at"])
            raise ProviderAdapterError("Stripe Checkout session creation failed.") from exc

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

    def _refund_scope(self):
        stripe_account = None
        if self.provider_config:
            self.provider_config.refresh_from_db(fields=["is_active", "connection"])
            if not self.provider_config.is_active:
                raise ProviderRequestError("Stripe routing is disabled.", code="route_disabled")
            connection = self.provider_config.connection
            if connection and connection.authorization_method == "stripe_connect":
                connection.refresh_from_db(fields=["status", "revoked_at", "external_account_id"])
                if connection.status != connection.STATUS_ACTIVE or connection.revoked_at or not connection.external_account_id:
                    raise ProviderRequestError("Stripe connection is not active.", code="connection_inactive")
                stripe_account = connection.external_account_id
        return stripe_account

    def _refund_reference(self, refund):
        attempt = refund.payment_attempt
        metadata = attempt.provider_response_metadata or {}
        payment_intent_id = metadata.get("stripe_payment_intent_id", "")
        charge_id = metadata.get("stripe_charge_id", "")
        if not payment_intent_id and str(attempt.provider_reference).startswith("pi_"):
            payment_intent_id = attempt.provider_reference
        if not charge_id and str(attempt.provider_reference).startswith("ch_"):
            charge_id = attempt.provider_reference
        if not payment_intent_id and not charge_id:
            raise ProviderRequestError("Stripe payment reference is unavailable.", code="payment_reference_missing")
        return payment_intent_id, charge_id

    @staticmethod
    def _refund_result(data, *, expected_amount, expected_currency):
        amount = object_value(data, "amount")
        currency = str(object_value(data, "currency", "")).upper()
        if amount != expected_amount or currency != expected_currency.upper():
            raise ProviderRequestError(
                "Stripe returned refund details that did not match the request.",
                code="refund_details_mismatch",
                ambiguous=True,
            )
        provider_status = str(object_value(data, "status", ""))[:80]
        status = {
            "succeeded": "succeeded",
            "pending": "pending",
            "requires_action": "pending",
            "failed": "failed",
            "canceled": "canceled",
        }.get(provider_status, "pending")
        return ProviderOperationResult(
            status=status,
            provider_reference=str(object_value(data, "id", ""))[:160],
            provider_status=provider_status,
            safe_metadata={
                "failure_reason": str(object_value(data, "failure_reason", ""))[:80],
                "pending_reason": str(object_value(data, "pending_reason", ""))[:80],
            },
            failure_code=str(object_value(data, "failure_reason", ""))[:80] if status == "failed" else "",
            failure_category="provider_declined" if status == "failed" else "",
        )

    def refund(self, refund):
        stripe = stripe_module()
        stripe_account = self._refund_scope()
        payment_intent_id, charge_id = self._refund_reference(refund)
        params = {
            "amount": refund.amount,
            "metadata": {
                "foxpay_refund_id": refund.public_id,
                "foxpay_payment_intent": refund.payment_intent.public_id,
            },
        }
        if payment_intent_id:
            params["payment_intent"] = payment_intent_id
        else:
            params["charge"] = charge_id
        if refund.reason in {"duplicate", "fraudulent", "requested_by_customer"}:
            params["reason"] = refund.reason
        try:
            result = stripe.Refund.create(
                **params,
                api_key=self.secret_key(),
                idempotency_key=refund.provider_idempotency_key,
                **({"stripe_account": stripe_account} if stripe_account else {}),
            )
        except ProviderRequestError:
            raise
        except Exception as exc:
            name = exc.__class__.__name__
            ambiguous = name in {"APIConnectionError", "APIError", "RateLimitError"} or "Timeout" in name
            raise ProviderRequestError(
                "Stripe refund request failed.",
                code=getattr(exc, "code", "") or name,
                ambiguous=ambiguous,
            ) from exc
        return self._refund_result(result, expected_amount=refund.amount, expected_currency=refund.currency)

    def retrieve_refund(self, refund):
        if not refund.provider_refund_id:
            return self.refund(refund)
        stripe = stripe_module()
        stripe_account = self._refund_scope()
        try:
            result = stripe.Refund.retrieve(
                refund.provider_refund_id,
                api_key=self.secret_key(),
                **({"stripe_account": stripe_account} if stripe_account else {}),
            )
        except ProviderRequestError:
            raise
        except Exception as exc:
            raise ProviderRequestError(
                "Stripe refund lookup failed.",
                code=getattr(exc, "code", "") or exc.__class__.__name__,
                ambiguous=True,
            ) from exc
        return self._refund_result(result, expected_amount=refund.amount, expected_currency=refund.currency)


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
    }
