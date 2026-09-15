import requests
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from apps.payments.models import PaymentAttempt

from .base import Capability, PaymentProviderAdapter, ProviderAdapterError


SQUARE_API_VERSION = "2026-08-19"


class SquareCheckoutAdapter(PaymentProviderAdapter):
    provider = "square"
    capabilities = [
        Capability.CARD,
        Capability.DEBIT,
        Capability.WALLET,
        Capability.HOSTED_CHECKOUT,
        Capability.WEBHOOKS,
        Capability.IDEMPOTENCY,
    ]

    def create_checkout_session(self, request, intent, payload):
        config = self.provider_config
        if not config or config.adapter_name != "square":
            raise ImproperlyConfigured("Square checkout requires a Square provider config.")
        location_id = config.settings.get("location_id", "")
        connection = config.connection
        token = ""
        if connection and connection.authorization_method == "square_oauth":
            from apps.payments.square_oauth import refresh_connection

            if not refresh_connection(connection):
                raise ImproperlyConfigured("Square seller authorization must be reconnected.")
            connection.refresh_from_db()
            if connection.status != connection.STATUS_ACTIVE or connection.revoked_at:
                raise ImproperlyConfigured("Square seller authorization is not active.")
            token = connection.access_token()
        else:
            credential = config.credentials.filter(name="access_token", revoked_at__isnull=True).order_by("-last_rotated_at").first()
            token = credential.reveal_secret() if credential else ""
        if not location_id or not token:
            raise ImproperlyConfigured("Square checkout requires a location_id and access_token.")
        if config.settings.get("location_currency") and intent.currency.upper() != config.settings["location_currency"].upper():
            raise ProviderAdapterError("Square location does not support this currency.")
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=self.provider,
            status=PaymentAttempt.STATUS_PENDING,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Complete payment on Square-hosted checkout.",
        )
        redirect_url = intent.success_url or request.build_absolute_uri(
            reverse("payments:foxpay_checkout", args=[intent.client_secret])
        )
        body = {
            "idempotency_key": f"foxpay-{attempt.uuid}",
            "quick_pay": {
                "name": intent.description or "Fox Pay payment",
                "price_money": {"amount": intent.amount, "currency": intent.currency},
                "location_id": location_id,
            },
            "checkout_options": {"redirect_url": redirect_url, "allow_tipping": False},
            "payment_note": f"FoxPay {intent.public_id}",
        }
        if intent.customer and intent.customer.email:
            body["pre_populated_data"] = {"buyer_email": intent.customer.email}
        endpoint = "https://connect.squareupsandbox.com" if config.environment == "test" else "https://connect.squareup.com"
        try:
            response = requests.post(
                f"{endpoint}/v2/online-checkout/payment-links",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Square-Version": SQUARE_API_VERSION,
                },
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            payment_link = result.get("payment_link") if isinstance(result, dict) else None
            payment_link = payment_link if isinstance(payment_link, dict) else {}
            link_id = payment_link.get("id", "")
            order_id = payment_link.get("order_id", "")
            url = payment_link.get("url", "")
            parsed_url = urlsplit(url) if isinstance(url, str) else None
            if not link_id or not order_id or not parsed_url or parsed_url.scheme != "https" or parsed_url.hostname not in {"square.link", "sandbox.square.link"}:
                raise ValueError("Square returned an incomplete payment link.")
        except (requests.RequestException, ValueError, TypeError) as exc:
            if connection and connection.authorization_method == "square_oauth":
                response = getattr(exc, "response", None)
                if response is not None and getattr(response, "status_code", 0) in {401, 403}:
                    try:
                        errors = response.json().get("errors", [])
                    except (AttributeError, TypeError, ValueError):
                        errors = []
                    error_code = next(
                        (item.get("code") for item in errors if isinstance(item, dict) and isinstance(item.get("code"), str)),
                        "UNAUTHORIZED",
                    )
                    from apps.payments.square_oauth import mark_connection_unhealthy

                    mark_connection_unhealthy(connection, error_code)
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.provider_status = "create_failed"
            attempt.failure_category = exc.__class__.__name__
            attempt.save(update_fields=["status", "provider_status", "failure_category", "updated_at"])
            raise ProviderAdapterError("Square payment link creation failed.") from exc
        attempt.status = PaymentAttempt.STATUS_ACTION_REQUIRED
        attempt.provider_reference = link_id
        attempt.provider_status = "open"
        attempt.checkout_url = url
        attempt.provider_response_metadata = {"square_order_id": order_id, "square_location_id": location_id}
        attempt.save(update_fields=["status", "provider_reference", "provider_status", "checkout_url", "provider_response_metadata", "updated_at"])
        return attempt

    create_attempt = create_checkout_session
