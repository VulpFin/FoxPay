"""PayPal Checkout (Orders v2) as a hosted card/wallet route.

PayPal is a hosted provider like Stripe Checkout: Fox Pay creates an order,
sends the customer to PayPal's approval page, and settles the attempt when
PayPal says the money moved. Fox Pay never sees card details.

One PayPal-specific wrinkle worth knowing: approval is not payment. An order
that a customer approves still has to be captured, so the webhook receiver
captures on CHECKOUT.ORDER.APPROVED and only settles the attempt once a capture
has actually completed.

Credentials come from `ProviderCredential` rows (`client_id`, `client_secret`,
`webhook_id`) and fall back to the PAYPAL_* settings.
"""
import json
import time
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.payments.models import PaymentAttempt
from apps.payments.templatetags.foxpay_money import THREE_DECIMAL as ISO_THREE_DECIMAL, ZERO_DECIMAL as ISO_ZERO_DECIMAL

from .base import Capability, PaymentProviderAdapter, ProviderAdapterError

PAYPAL_ADAPTERS = {"paypal", "paypal_checkout"}
# Fox Pay counts minor units the way ISO does; PayPal quotes decimal strings
# and refuses decimals outright for a few currencies. Both have to be honoured,
# or a Hungarian order is quoted a hundred times over.
PAYPAL_NO_DECIMALS = {"HUF", "JPY", "TWD"}
LIVE = "https://api-m.paypal.com"
SANDBOX = "https://api-m.sandbox.paypal.com"
TIMEOUT = 20


def iso_places(currency):
    currency = str(currency).upper()
    return 0 if currency in ISO_ZERO_DECIMAL else 3 if currency in ISO_THREE_DECIMAL else 2


def paypal_places(currency):
    return 0 if str(currency).upper() in PAYPAL_NO_DECIMALS else iso_places(currency)


def minor_to_value(amount, currency):
    """Fox Pay minor units -> the decimal string PayPal wants."""
    places = iso_places(currency)
    quoted = Decimal(int(amount)) / (10 ** places)
    return f"{quoted:.{paypal_places(currency)}f}"


def capture_amount(resource):
    """(minor units, currency) for a PayPal amount, or (None, "") if absent.

    PayPal quotes money as a decimal string; Fox Pay counts minor units. This
    is what lets a receiver check that the money that moved is the money that
    was asked for.
    """
    amount = (resource or {}).get("amount") or {}
    value = amount.get("value")
    currency = str(amount.get("currency_code", "")).upper()
    if value is None or not currency:
        return None, ""
    try:
        quoted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None, ""
    return int((quoted * (10 ** iso_places(currency))).to_integral_value()), currency


def api_base(environment=""):
    env = (environment or getattr(settings, "PAYPAL_ENV", "") or "sandbox").lower()
    return LIVE if env == "live" else SANDBOX


class PayPalCheckoutAdapter(PaymentProviderAdapter):
    provider = "paypal"
    capabilities = [
        Capability.CARD,
        Capability.WALLET,
        Capability.HOSTED_CHECKOUT,
        Capability.WEBHOOKS,
        Capability.MULTICURRENCY,
    ]

    # -- configuration ---------------------------------------------------
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

    def client_id(self):
        value = self.credential("client_id", "PAYPAL_CLIENT_ID")
        if not value:
            raise ImproperlyConfigured("PayPal requires a client_id provider credential or PAYPAL_CLIENT_ID.")
        return value

    def client_secret(self):
        value = self.credential("client_secret", "PAYPAL_CLIENT_SECRET")
        if not value:
            raise ImproperlyConfigured("PayPal requires a client_secret provider credential or PAYPAL_CLIENT_SECRET.")
        return value

    def webhook_id(self):
        return self.credential("webhook_id", "PAYPAL_WEBHOOK_ID")

    def base_url(self):
        environment = getattr(self.provider_config, "environment", "") if self.provider_config else ""
        settings_env = (self.provider_config.settings or {}).get("paypal_env", "") if self.provider_config else ""
        return api_base(settings_env or ("live" if environment == "live" else ""))

    # -- transport -------------------------------------------------------
    def _token(self):
        import base64

        auth = base64.b64encode(f"{self.client_id()}:{self.client_secret()}".encode()).decode()
        request = urllib.request.Request(
            f"{self.base_url()}/v1/oauth2/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read()).get("access_token", "")
        except urllib.error.HTTPError as exc:
            raise ProviderAdapterError(f"PayPal rejected these credentials ({exc.code}).")
        except Exception as exc:
            raise ProviderAdapterError(f"PayPal is unreachable ({exc.__class__.__name__}).")

    def call(self, method, path, body=None, token=""):
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url()}{path}",
            data=payload,
            method=method,
            headers={"Authorization": f"Bearer {token or self._token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except ValueError:
                return exc.code, {}
        except Exception as exc:
            raise ProviderAdapterError(f"PayPal is unreachable ({exc.__class__.__name__}).")

    # -- creating the attempt --------------------------------------------
    def create_checkout_session(self, request, intent, payload):
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=self.provider_config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=self.provider_config.provider if self.provider_config else self.provider,
            status=PaymentAttempt.STATUS_PENDING,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Customer approves the payment on PayPal, then Fox Pay captures it.",
        )
        return_url = intent.success_url or request.build_absolute_uri("/")
        cancel_url = intent.cancel_url or return_url
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": intent.public_id,
                "custom_id": intent.public_id,
                "invoice_id": f"{intent.public_id}:{attempt.id}",
                "description": (intent.description or "Fox Pay payment")[:127],
                "amount": {
                    "currency_code": intent.currency.upper(),
                    "value": minor_to_value(intent.amount, intent.currency),
                },
            }],
            "application_context": {
                "brand_name": (self.provider_config.display_name if self.provider_config else "") or "Fox Pay",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        status, data = self.call("POST", "/v2/checkout/orders", body)
        if status not in (200, 201):
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.provider_status = "create_failed"
            attempt.failure_category = "paypal_create_order"
            attempt.failure_code = str(data.get("name", ""))[:80]
            attempt.save(update_fields=["status", "provider_status", "failure_category", "failure_code", "updated_at"])
            raise ProviderAdapterError(f"PayPal order creation failed ({status}).")

        approval = ""
        for link in data.get("links", []):
            if link.get("rel") in ("approve", "payer-action"):
                approval = link.get("href", "")
                break
        attempt.status = PaymentAttempt.STATUS_ACTION_REQUIRED
        attempt.provider_reference = data.get("id", "")
        attempt.provider_status = data.get("status", "")
        attempt.checkout_url = approval
        attempt.provider_response_metadata = {
            "paypal_order_id": attempt.provider_reference,
            "paypal_status": attempt.provider_status,
            "environment": "live" if self.base_url() == LIVE else "sandbox",
        }
        attempt.save(update_fields=["status", "provider_reference", "provider_status", "checkout_url",
                                    "provider_response_metadata", "updated_at"])
        if not approval:
            raise ProviderAdapterError("PayPal did not return an approval link.")
        return attempt

    create_attempt = create_checkout_session

    # -- settlement -------------------------------------------------------
    def capture(self, paypal_order_id):
        """Approval is not payment: this is what takes the money."""
        status, data = self.call("POST", f"/v2/checkout/orders/{paypal_order_id}/capture", {})
        if status in (200, 201):
            return data
        if status == 422 and any(d.get("issue") == "ORDER_ALREADY_CAPTURED" for d in (data.get("details") or [])):
            return self.retrieve(paypal_order_id)
        raise ProviderAdapterError(f"PayPal capture failed ({status}).")

    def retrieve(self, paypal_order_id):
        status, data = self.call("GET", f"/v2/checkout/orders/{paypal_order_id}")
        if status != 200:
            raise ProviderAdapterError(f"PayPal order lookup failed ({status}).")
        return data

    def settlement_event(self, order, event_id=""):
        """A completed capture inside an order, shaped like the event for it.

        Capturing hands back the whole order rather than a webhook, but the
        money has moved just the same, so the receiver can settle from it
        without waiting for PayPal to tell us twice. Empty when nothing in the
        order actually captured.
        """
        for unit in order.get("purchase_units") or []:
            for capture in (unit.get("payments") or {}).get("captures") or []:
                if capture.get("status") != "COMPLETED":
                    continue
                resource = dict(capture)
                resource["custom_id"] = capture.get("custom_id") or unit.get("custom_id", "")
                resource["invoice_id"] = capture.get("invoice_id") or unit.get("invoice_id", "")
                return {
                    "id": event_id or f"capture:{capture.get('id', '')}",
                    "event_type": "PAYMENT.CAPTURE.COMPLETED",
                    "resource": resource,
                }
        return {}

    def verify_webhook(self, request):
        """PayPal checks its own signature; we hand the headers back to it.

        With no webhook id configured there is nothing to check against, so the
        answer is no - an unverifiable webhook is refused, never assumed.
        """
        webhook_id = self.webhook_id()
        if not webhook_id:
            return False
        required = ("Paypal-Transmission-Id", "Paypal-Transmission-Time", "Paypal-Cert-Url",
                    "Paypal-Auth-Algo", "Paypal-Transmission-Sig")
        if any(not request.headers.get(header) for header in required):
            return False
        try:
            event = json.loads(request.body)
        except (UnicodeDecodeError, ValueError):
            return False
        status, data = self.call("POST", "/v1/notifications/verify-webhook-signature", {
            "transmission_id": request.headers.get("Paypal-Transmission-Id"),
            "transmission_time": request.headers.get("Paypal-Transmission-Time"),
            "cert_url": request.headers.get("Paypal-Cert-Url"),
            "auth_algo": request.headers.get("Paypal-Auth-Algo"),
            "transmission_sig": request.headers.get("Paypal-Transmission-Sig"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        })
        return status == 200 and data.get("verification_status") == "SUCCESS"

    def normalize_webhook(self, payload):
        """PayPal's event -> the shape record_webhook() understands."""
        event_type = payload.get("event_type", "")
        resource = payload.get("resource") or {}
        reference = resource.get("custom_id") or ""
        invoice = resource.get("invoice_id") or ""
        if not reference and resource.get("purchase_units"):
            unit = resource["purchase_units"][0]
            reference = unit.get("custom_id", "")
            invoice = unit.get("invoice_id", invoice)
        attempt_id = ""
        if ":" in invoice:
            reference = reference or invoice.split(":", 1)[0]
            attempt_id = invoice.split(":", 1)[1]

        status = {
            "PAYMENT.CAPTURE.COMPLETED": "succeeded",
            "CHECKOUT.ORDER.APPROVED": "approved",
            "PAYMENT.CAPTURE.DENIED": "failed",
            "PAYMENT.CAPTURE.REVERSED": "failed",
            "CHECKOUT.ORDER.VOIDED": "canceled",
        }.get(event_type, "")
        normalized = {
            "id": payload.get("id", ""),
            "type": event_type,
            "payment_intent": reference,
            "status": status,
            "provider_reference": resource.get("id", ""),
            "foxpay_method": PaymentAttempt.METHOD_CARD,
            "transaction_id": resource.get("id", ""),
        }
        if attempt_id.isdigit():
            normalized["foxpay_attempt_id"] = int(attempt_id)
        return normalized
