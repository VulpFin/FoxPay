"""PayPal Checkout (Orders v2) as a hosted card/wallet route.

PayPal is a hosted provider like Stripe Checkout: Fox Pay creates an order,
sends the customer to PayPal's approval page, and settles the attempt when
PayPal says the money moved. Fox Pay never sees card details.

One PayPal-specific wrinkle worth knowing: approval is not payment. An order
that a customer approves still has to be captured, so the webhook receiver
captures on CHECKOUT.ORDER.APPROVED and only settles the attempt once a capture
has actually completed.

Seller partner connections use FoxPay's platform credentials and a seller-ID
auth assertion. Legacy operator-created routes can still read encrypted
`ProviderCredential` rows during migration.
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

from .base import Capability, PaymentProviderAdapter, ProviderAdapterError, ProviderOperationResult, ProviderRequestError

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
        Capability.IDEMPOTENCY,
        Capability.REFUNDS,
        Capability.PARTIAL_REFUNDS,
        Capability.DISPUTES,
    ]

    # -- configuration ---------------------------------------------------
    def partner_connection(self, *, require_active=True):
        config = self.provider_config
        if not config or not config.connection_id:
            return None
        config.refresh_from_db(fields=["is_active", "connection"])
        connection = config.connection
        if connection.authorization_method != "paypal_partner":
            return None
        connection.refresh_from_db(fields=["status", "revoked_at", "external_account_id", "authorization_method"])
        if require_active and (
            not config.is_active
            or connection.status != connection.STATUS_ACTIVE
            or connection.revoked_at
        ):
            raise ProviderAdapterError("PayPal seller authorization is not active.")
        return connection

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
        connection = self.partner_connection()
        if connection:
            from apps.payments.paypal_partner import partner_credentials

            return partner_credentials(connection.environment)[0]
        value = self.credential("client_id", "PAYPAL_CLIENT_ID")
        if not value:
            raise ImproperlyConfigured("PayPal requires a client_id provider credential or PAYPAL_CLIENT_ID.")
        return value

    def client_secret(self):
        connection = self.partner_connection()
        if connection:
            from apps.payments.paypal_partner import partner_credentials

            return partner_credentials(connection.environment)[1]
        value = self.credential("client_secret", "PAYPAL_CLIENT_SECRET")
        if not value:
            raise ImproperlyConfigured("PayPal requires a client_secret provider credential or PAYPAL_CLIENT_SECRET.")
        return value

    def webhook_id(self):
        connection = self.partner_connection(require_active=False)
        if connection:
            from apps.payments.paypal_partner import partner_webhook_id

            return partner_webhook_id(connection.environment)
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

    def call(self, method, path, body=None, token="", request_id=""):
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token or self._token()}", "Content-Type": "application/json"}
        connection = self.partner_connection()
        if connection:
            from apps.payments.paypal_partner import auth_assertion, partner_credentials

            client_id, _, _, attribution_id = partner_credentials(connection.environment)
            headers["PayPal-Auth-Assertion"] = auth_assertion(client_id, connection.external_account_id)
            headers["PayPal-Partner-Attribution-Id"] = attribution_id
        if request_id:
            headers["PayPal-Request-Id"] = request_id[:108]
        request = urllib.request.Request(
            f"{self.base_url()}{path}",
            data=payload,
            method=method,
            headers=headers,
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
        connection = self.partner_connection()
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
        if connection:
            body["purchase_units"][0]["payee"] = {"merchant_id": connection.external_account_id}
        status, data = self.call(
            "POST",
            "/v2/checkout/orders",
            body,
            request_id=f"foxpay-order-{attempt.uuid}",
        )
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
        parsed_approval = urllib.parse.urlsplit(approval) if isinstance(approval, str) else None
        allowed_hosts = {"www.paypal.com", "paypal.com"} if self.base_url() == LIVE else {"www.sandbox.paypal.com", "sandbox.paypal.com"}
        if approval and (
            not parsed_approval
            or parsed_approval.scheme != "https"
            or parsed_approval.hostname not in allowed_hosts
        ):
            approval = ""
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

    @staticmethod
    def _refund_result(data, refund):
        minor, currency = capture_amount(data)
        if minor != refund.amount or currency != refund.currency.upper():
            raise ProviderRequestError(
                "PayPal returned refund details that did not match the request.",
                code="refund_details_mismatch",
                ambiguous=True,
            )
        provider_status = str(data.get("status", ""))[:80]
        status = {
            "COMPLETED": "succeeded",
            "PENDING": "pending",
            "CANCELLED": "canceled",
            "FAILED": "failed",
        }.get(provider_status, "pending")
        return ProviderOperationResult(
            status=status,
            provider_reference=str(data.get("id", ""))[:160],
            provider_status=provider_status,
            failure_code="refund_failed" if status == "failed" else "",
            failure_category="provider_declined" if status == "failed" else "",
        )

    def refund(self, refund):
        capture_id = refund.payment_attempt.provider_reference
        if not capture_id:
            raise ProviderRequestError("PayPal capture ID is unavailable.", code="payment_reference_missing")
        body = {
            "amount": {
                "currency_code": refund.currency.upper(),
                "value": minor_to_value(refund.amount, refund.currency),
            },
            "custom_id": refund.public_id[:127],
        }
        if refund.reason:
            body["note_to_payer"] = refund.reason[:255]
        try:
            status, data = self.call(
                "POST",
                f"/v2/payments/captures/{capture_id}/refund",
                body,
                request_id=refund.provider_idempotency_key,
            )
        except ProviderAdapterError as exc:
            raise ProviderRequestError(
                "PayPal refund request outcome is not yet known.",
                code=exc.__class__.__name__,
                ambiguous="unreachable" in str(exc).lower(),
            ) from exc
        if status not in {200, 201}:
            raise ProviderRequestError(
                "PayPal rejected the refund request.",
                code=data.get("name", "") or f"http_{status}",
                ambiguous=status >= 500 or status == 429,
            )
        return self._refund_result(data, refund)

    def retrieve_refund(self, refund):
        if not refund.provider_refund_id:
            return self.refund(refund)
        try:
            status, data = self.call("GET", f"/v2/payments/refunds/{refund.provider_refund_id}")
        except ProviderAdapterError as exc:
            raise ProviderRequestError(
                "PayPal refund lookup is unavailable.",
                code=exc.__class__.__name__,
                ambiguous=True,
            ) from exc
        if status != 200:
            raise ProviderRequestError(
                "PayPal refund lookup failed.",
                code=data.get("name", "") or f"http_{status}",
                ambiguous=status >= 500 or status == 429,
            )
        return self._refund_result(data, refund)

    # -- settlement -------------------------------------------------------
    def capture(self, paypal_order_id):
        """Approval is not payment: this is what takes the money."""
        status, data = self.call(
            "POST",
            f"/v2/checkout/orders/{paypal_order_id}/capture",
            {},
            request_id=f"foxpay-capture-{paypal_order_id}",
        )
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
