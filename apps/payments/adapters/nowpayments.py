import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.urls import reverse

from apps.payments.models import PaymentAttempt
from .base import Capability, PaymentProviderAdapter, ProviderAdapterError


API_BASE_URLS = {
    "test": "https://api.sandbox.nowpayments.io/v1",
    "live": "https://api.nowpayments.io/v1",
}


class NowPaymentsAuthorizationError(ProviderAdapterError):
    pass


def provider_secret(config, name):
    credential = (
        config.credentials.filter(name=name, revoked_at__isnull=True)
        .order_by("-last_rotated_at")
        .first()
    )
    return credential.reveal_secret() if credential else ""


def canonical_ipn(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_ipn(payload, signature, secret):
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), canonical_ipn(payload), hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def invoice_order_id(intent, attempt):
    return f"foxpay:{intent.public_id}:{attempt.id}"


def create_invoice_request(api_key, environment, payload):
    base_url = API_BASE_URLS.get(environment)
    if not base_url:
        raise ProviderAdapterError("Unsupported NOWPayments environment.")
    request = Request(
        f"{base_url}/invoice",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            data = json.load(response)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise NowPaymentsAuthorizationError("NOWPayments rejected the seller authorization.") from exc
        raise ProviderAdapterError(f"NOWPayments invoice request returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise ProviderAdapterError("NOWPayments invoice request could not be completed.") from exc
    except ValueError as exc:
        raise ProviderAdapterError("NOWPayments returned an invalid invoice response.") from exc
    if not isinstance(data, dict) or not data.get("id") or not data.get("invoice_url"):
        raise ProviderAdapterError("NOWPayments did not return an invoice ID and URL.")
    parsed_url = urlsplit(str(data["invoice_url"]))
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"nowpayments.io", "sandbox.nowpayments.io"}:
        raise ProviderAdapterError("NOWPayments returned an invalid invoice URL.")
    return data


class NowPaymentsAdapter(PaymentProviderAdapter):
    provider = "nowpayments"
    capabilities = [Capability.CRYPTO, Capability.STABLECOIN, Capability.HOSTED_CHECKOUT, Capability.WEBHOOKS]

    def create_invoice(self, request, intent, payload):
        config = self.provider_config
        if not config:
            raise ProviderAdapterError("NOWPayments requires a provider configuration.")
        config.refresh_from_db(fields=["is_active", "connection"])
        if not config.is_active:
            raise ProviderAdapterError("NOWPayments provider routing is disabled.")
        connection = config.connection
        if connection and connection.authorization_method == "nowpayments_credentials":
            connection.refresh_from_db(fields=["status", "revoked_at", "authorization_method"])
            if connection.status != connection.STATUS_ACTIVE or connection.revoked_at:
                raise ProviderAdapterError("NOWPayments seller authorization is not active.")
        if intent.currency != "USD":
            raise ProviderAdapterError("NOWPayments currently supports USD-priced Fox Pay invoices only.")
        api_key = provider_secret(config, "api_key")
        ipn_secret = provider_secret(config, "ipn_secret")
        if not api_key or not ipn_secret:
            raise ProviderAdapterError("NOWPayments requires api_key and ipn_secret credentials.")

        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CRYPTO,
            rail=PaymentAttempt.METHOD_CRYPTO,
            provider=config.provider,
            status=PaymentAttempt.STATUS_PENDING,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Customer completes payment on NOWPayments hosted checkout.",
        )
        fallback_url = request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[intent.client_secret]))
        invoice_payload = {
            "price_amount": float(Decimal(intent.amount) / Decimal(100)),
            "price_currency": "usd",
            "order_id": invoice_order_id(intent, attempt),
            "order_description": intent.description or "Fox Pay payment",
            "ipn_callback_url": request.build_absolute_uri(
                reverse("payments:nowpayments_ipn", args=[intent.merchant.slug, config.provider])
            ),
            "success_url": intent.success_url or fallback_url,
            "cancel_url": intent.cancel_url or fallback_url,
        }
        pay_currency = config.settings.get("pay_currency")
        if pay_currency:
            invoice_payload["pay_currency"] = str(pay_currency).lower()
        try:
            invoice = create_invoice_request(api_key, config.environment, invoice_payload)
        except ProviderAdapterError as exc:
            if connection and isinstance(exc, NowPaymentsAuthorizationError):
                from apps.payments.nowpayments_connection import mark_connection_unhealthy

                mark_connection_unhealthy(connection, config, "api_key_rejected")
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.provider_status = "create_failed"
            attempt.save(update_fields=["status", "provider_status", "updated_at"])
            raise

        attempt.provider_reference = str(invoice["id"])
        attempt.checkout_url = str(invoice["invoice_url"])
        attempt.status = PaymentAttempt.STATUS_ACTION_REQUIRED
        attempt.provider_status = "waiting"
        attempt.provider_response_metadata = {"nowpayments_order_id": invoice_payload["order_id"]}
        attempt.save(update_fields=[
            "provider_reference", "checkout_url", "status", "provider_status",
            "provider_response_metadata", "updated_at",
        ])
        return attempt

    create_attempt = create_invoice


def ipn_attempt(config, payload):
    order_id = str(payload.get("order_id", ""))
    parts = order_id.split(":")
    if len(parts) != 3 or parts[0] != "foxpay" or not parts[2].isdigit():
        return None
    return PaymentAttempt.objects.select_related("intent").filter(
        id=int(parts[2]),
        intent__public_id=parts[1],
        provider_config=config,
        method=PaymentAttempt.METHOD_CRYPTO,
        provider_response_metadata__nowpayments_order_id=order_id,
    ).first()


def validate_ipn_amount(attempt, payload):
    if str(payload.get("price_currency", "")).upper() != attempt.currency:
        return False
    try:
        price = Decimal(str(payload["price_amount"]))
        expected = Decimal(attempt.amount) / Decimal(100)
    except (InvalidOperation, KeyError, TypeError):
        return False
    return price == expected


def normalize_ipn(attempt, payload):
    provider_status = str(payload.get("payment_status", "")).lower()
    status = {"failed": "failed", "expired": "expired"}.get(provider_status, "processing")
    if provider_status == "finished":
        try:
            paid = Decimal(str(payload["actually_paid"]))
            quoted = Decimal(str(payload["pay_amount"]))
            if quoted > 0 and paid >= quoted:
                status = "paid"
        except (InvalidOperation, KeyError, TypeError):
            pass
    return {
        "id": f"np:{hashlib.sha256(canonical_ipn(payload)).hexdigest()}",
        "type": f"nowpayments.payment.{provider_status}",
        "payment_intent": attempt.intent.public_id,
        "foxpay_attempt_id": str(attempt.id),
        "foxpay_method": PaymentAttempt.METHOD_CRYPTO,
        "status": status,
        "provider_reference": attempt.provider_reference,
        "transaction_id": str(payload.get("payment_id", "")),
        "provider_status": provider_status[:80],
        "price_amount": str(payload.get("price_amount", ""))[:80],
        "price_currency": str(payload.get("price_currency", ""))[:16],
        "pay_amount": str(payload.get("pay_amount", ""))[:80],
        "actually_paid": str(payload.get("actually_paid", ""))[:80],
        "pay_currency": str(payload.get("pay_currency", ""))[:32],
    }
