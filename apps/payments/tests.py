import json
import hashlib
import hmac
import base64
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.test import RequestFactory
from django.urls import reverse

from .adapters.nowpayments import canonical_ipn
from .adapters.square_webhooks import verify_square_signature
from .adapters.square_checkout import SquareCheckoutAdapter
from .adapters.paypal_checkout import PayPalCheckoutAdapter, capture_amount, minor_to_value
from .adapters.stripe_methods import create_setup_checkout, detach_saved_method, record_setup_checkout
from .ledger import transaction_balances
from .services import record_webhook
from .services import APIError, get_or_create_customer
from .models import (
    APIKey,
    Customer,
    IdempotencyRecord,
    LedgerTransaction,
    Merchant,
    MerchantAllowedReturnOrigin,
    MerchantWebhookEndpoint,
    PaymentAttempt,
    PaymentIntent,
    PaymentMethodReference,
    ProviderConfig,
    ProviderCredential,
    ProviderCustomerReference,
    ProviderEvent,
    Refund,
    SubscriptionReference,
    WebhookDelivery,
)


class FakeStripeSession:
    def __init__(self, **kwargs):
        self._data = kwargs

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict_recursive(self):
        return dict(self._data)


class FakeStripeCheckoutSession:
    last_kwargs = None

    @classmethod
    def create(cls, **kwargs):
        cls.last_kwargs = kwargs
        return FakeStripeSession(
            id="cs_test_123",
            url="https://checkout.stripe.com/c/pay/cs_test_123",
            status="open",
            payment_status="unpaid",
            payment_intent="pi_test_123",
            livemode=False,
            mode="payment",
            expires_at=1893456000,
        )


class FakeStripeWebhook:
    event = None

    @classmethod
    def construct_event(cls, payload, sig_header, secret):
        if sig_header != "valid-signature" or secret != "whsec_test":
            raise ValueError("bad signature")
        return cls.event


class FakeStripe:
    class checkout:
        Session = FakeStripeCheckoutSession

    Webhook = FakeStripeWebhook


class PaymentIntentAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="owner@example.com", email="owner@example.com", password="test-pass")
        self.merchant = Merchant.objects.create(
            owner=self.user,
            name="VulpFin",
            slug="vulpfin",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
            allow_legacy_provider_configs=True,
            crypto_addresses={"BTC": "bc1qtestaddress"},
        )
        MerchantAllowedReturnOrigin.objects.create(merchant=self.merchant, origin="https://merchant.example")
        _, self.raw_key = APIKey.issue(self.merchant, "Test key")

    def test_create_payment_intent_with_card_and_crypto_options(self):
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(
                {
                    "amount": 2500,
                    "currency": "USD",
                    "description": "Test checkout",
                    "payment_methods": ["card", "crypto"],
                    "crypto_asset_amount": "0.001",
                }
            ),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["object"], "payment_intent")
        self.assertEqual(payload["amount"], 2500)
        self.assertEqual(len(payload["payment_options"]), 2)
        self.assertTrue(payload["foxpay_checkout_url"].endswith(f"/pay/{payload['client_secret']}/"))

    def test_create_payment_intent_can_attach_customer(self):
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(
                {
                    "amount": 2500,
                    "currency": "USD",
                    "payment_methods": ["card"],
                    "customer": {"external_id": "cust_1001", "email": "paying@example.com", "name": "Paying Customer"},
                }
            ),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Customer.objects.get().external_id, "cust_1001")
        self.assertIsNotNone(response.json()["customer"])

    def test_idempotency_key_returns_existing_payment_intent(self):
        body = json.dumps(
            {
                "amount": 2500,
                "currency": "USD",
                "payment_methods": ["card"],
            }
        )

        first = self.client.post(
            reverse("payments:payment_intents"),
            data=body,
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            HTTP_IDEMPOTENCY_KEY="order-1001",
        )
        second = self.client.post(
            reverse("payments:payment_intents"),
            data=body,
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            HTTP_IDEMPOTENCY_KEY="order-1001",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(PaymentIntent.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)

    def test_scope_is_required_for_payment_creation(self):
        _, read_only_key = APIKey.issue(self.merchant, "Read only", scopes=["payments:read"])

        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 2500, "currency": "USD"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=read_only_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "missing_scope")

    def test_create_payment_intent_uses_multiple_provider_configs_per_method(self):
        ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="card-primary",
            adapter="mock",
            priority=10,
        )
        ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="card-secondary",
            adapter="hosted",
            priority=20,
            settings={"checkout_url": "https://backup-card.example/checkout"},
        )
        ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CRYPTO,
            provider="crypto-primary",
            adapter="manual",
            priority=10,
            settings={"addresses": {"BTC": "bc1qprimary"}},
        )
        ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CRYPTO,
            provider="crypto-secondary",
            adapter="manual",
            priority=20,
            settings={"addresses": {"BTC": "bc1qsecondary"}},
        )

        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(
                {
                    "amount": 2500,
                    "currency": "USD",
                    "payment_methods": ["card", "crypto"],
                    "crypto_asset_amount": "0.001",
                }
            ),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 201)
        options = response.json()["payment_options"]
        self.assertEqual([option["provider"] for option in options], ["card-primary", "card-secondary", "crypto-primary", "crypto-secondary"])
        self.assertEqual(options[2]["crypto_invoice"]["address"], "bc1qprimary")
        self.assertEqual(options[3]["crypto_invoice"]["address"], "bc1qsecondary")

    @patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=FakeStripe)
    def test_stripe_checkout_provider_creates_hosted_session(self, _stripe_module):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="stripe-primary",
            adapter="stripe",
            priority=10,
            settings={"payment_method_types": ["card"], "automatic_tax": True, "tax_behavior": "exclusive"},
        )
        credential = ProviderCredential(provider_config=config, name="secret_key")
        credential.set_secret("sk_test_example")
        credential.save()

        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(
                {
                    "amount": 4200,
                    "currency": "USD",
                    "description": "Stripe test checkout",
                    "payment_methods": ["card"],
                    "success_url": "https://merchant.example/success",
                    "cancel_url": "https://merchant.example/cancel",
                    "customer": {"external_id": "cust_2001", "email": "buyer@example.com"},
                }
            ),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 201)
        option = response.json()["payment_options"][0]
        self.assertEqual(option["provider"], "stripe-primary")
        self.assertEqual(option["checkout_url"], "https://checkout.stripe.com/c/pay/cs_test_123")
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["mode"], "payment")
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["payment_method_types"], ["card"])
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["line_items"][0]["price_data"]["unit_amount"], 4200)
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["line_items"][0]["price_data"]["tax_behavior"], "exclusive")
        self.assertTrue(FakeStripeCheckoutSession.last_kwargs["automatic_tax"]["enabled"])
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["customer_email"], "buyer@example.com")
        self.assertEqual(FakeStripeCheckoutSession.last_kwargs["api_key"], "sk_test_example")

    @patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=FakeStripe)
    def test_stripe_webhook_marks_checkout_payment_succeeded(self, _stripe_module):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="stripe-primary",
            adapter="stripe",
            priority=10,
        )
        secret = ProviderCredential(provider_config=config, name="webhook_secret")
        secret.set_secret("whsec_test")
        secret.save()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD")
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider="stripe-primary",
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=1000,
            currency="USD",
        )
        FakeStripeWebhook.event = {
            "id": "evt_stripe_1001",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "payment_status": "paid",
                    "payment_intent": "pi_test_123",
                    "client_reference_id": intent.public_id,
                    "livemode": False,
                    "metadata": {
                        "foxpay_payment_intent": intent.public_id,
                        "foxpay_attempt_id": str(attempt.id),
                    },
                }
            },
        }

        first = self.client.post(
            reverse("payments:stripe_webhook_provider", args=["stripe-primary"]),
            data=b'{"id":"evt_stripe_1001"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="valid-signature",
        )
        second = self.client.post(
            reverse("payments:stripe_webhook_provider", args=["stripe-primary"]),
            data=b'{"id":"evt_stripe_1001"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="valid-signature",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        self.assertEqual(attempt.provider_reference, "cs_test_123")
        self.assertEqual(ProviderEvent.objects.count(), 1)

    @patch("apps.payments.adapters.nowpayments.create_invoice_request")
    def test_nowpayments_hosted_invoice_requires_signed_matching_finished_ipn(self, create_invoice):
        self.assertEqual(canonical_ipn({"z": 1, "a": {"b": 2, "a": 3}}), b'{"a":{"a":3,"b":2},"z":1}')
        create_invoice.return_value = {
            "id": "123456",
            "invoice_url": "https://nowpayments.io/payment/?iid=123456",
        }
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CRYPTO,
            provider="nowpayments-primary",
            adapter="nowpayments",
            settings={"pay_currency": "btc"},
        )
        for name, value in (("api_key", "np_test_key"), ("ipn_secret", "np_test_secret")):
            credential = ProviderCredential(provider_config=config, name=name)
            credential.set_secret(value)
            credential.save()

        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 2500, "currency": "USD", "payment_methods": ["crypto"]}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 201)
        attempt = PaymentAttempt.objects.get(intent__public_id=response.json()["id"])
        self.assertEqual(attempt.checkout_url, create_invoice.return_value["invoice_url"])
        request_payload = create_invoice.call_args.args[2]
        self.assertEqual(request_payload["price_amount"], 25)
        self.assertEqual(request_payload["pay_currency"], "btc")
        self.assertEqual(request_payload["ipn_callback_url"], "http://testserver/api/v1/webhooks/nowpayments/vulpfin/nowpayments-primary/")

        ipn_url = reverse("payments:nowpayments_ipn", args=["vulpfin", "nowpayments-primary"])
        payload = {
            "payment_id": 998877,
            "payment_status": "partially_paid",
            "order_id": request_payload["order_id"],
            "price_amount": 25,
            "price_currency": "usd",
            "pay_amount": 0.001,
            "actually_paid": 0.0005,
        }

        def send_ipn(signature="valid"):
            digest = hmac.new(b"np_test_secret", canonical_ipn(payload), hashlib.sha512).hexdigest()
            return self.client.post(
                ipn_url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_NOWPAYMENTS_SIG=digest if signature == "valid" else "invalid",
            )

        self.assertEqual(send_ipn("invalid").status_code, 401)
        self.assertEqual(send_ipn().status_code, 200)
        attempt.intent.refresh_from_db()
        self.assertNotEqual(attempt.intent.status, PaymentIntent.STATUS_SUCCEEDED)

        payload["payment_status"] = "confirmed"
        payload["actually_paid"] = 0.001
        self.assertEqual(send_ipn().status_code, 200)
        attempt.intent.refresh_from_db()
        self.assertNotEqual(attempt.intent.status, PaymentIntent.STATUS_SUCCEEDED)

        payload["payment_status"] = "finished"
        payload["actually_paid"] = 0.0005
        self.assertEqual(send_ipn().status_code, 200)
        attempt.intent.refresh_from_db()
        self.assertNotEqual(attempt.intent.status, PaymentIntent.STATUS_SUCCEEDED)

        payload["actually_paid"] = 0.001
        payload["price_amount"] = 24
        self.assertEqual(send_ipn().status_code, 422)
        payload["price_amount"] = 25
        self.assertEqual(send_ipn().status_code, 200)
        self.assertEqual(send_ipn().status_code, 200)
        attempt.intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        self.assertEqual(LedgerTransaction.objects.filter(payment_intent=attempt.intent, transaction_type="payment_succeeded").count(), 1)

    @override_settings(FOXPAY_ENV="live")
    def test_live_crypto_does_not_fall_back_to_unverified_manual_wallet(self):
        _, live_key = APIKey.issue(self.merchant, environment="live")
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 2500, "currency": "USD", "payment_methods": ["crypto"]}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=live_key,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("No available crypto providers", response.json()["error"]["message"])

    @override_settings(FOXPAY_ENV="live")
    @patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=FakeStripe)
    def test_live_default_checkout_uses_available_card_route(self, _stripe_module):
        _, live_key = APIKey.issue(self.merchant, environment="live")
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            environment="live",
            kind=ProviderConfig.KIND_CARD,
            provider="stripe-primary",
            adapter="stripe",
        )
        credential = ProviderCredential(provider_config=config, name="secret_key")
        credential.set_secret("sk_live_example")
        credential.save()

        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 2500, "currency": "USD"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=live_key,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual([option["provider"] for option in response.json()["payment_options"]], ["stripe-primary"])

    @override_settings(FOXPAY_ENV="live")
    def test_nowpayments_ipn_rejects_callbacks_until_secret_is_configured(self):
        call_command(
            "configure_nowpayments_provider",
            merchant="vulpfin",
            provider="nowpayments-primary",
            environment="live",
            stdout=StringIO(),
        )
        config = ProviderConfig.objects.get(provider="nowpayments-primary")
        self.assertFalse(config.is_active)

        response = self.client.post(
            reverse("payments:nowpayments_ipn", args=["vulpfin", "nowpayments-primary"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "provider_not_configured")

    def test_rejects_missing_api_key(self):
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 2500, "currency": "USD"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("request_id", response.json()["error"])

    def test_mock_card_checkout_can_mark_intent_succeeded(self):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD")
        intent.attempts.create(method="card", provider="mock", status="action_required")

        response = self.client.post(reverse("payments:mock_card_checkout", args=[intent.client_secret]), {"action": "approve"})
        self.client.post(reverse("payments:mock_card_checkout", args=[intent.client_secret]), {"action": "approve"})

        self.assertEqual(response.status_code, 200)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        tx = LedgerTransaction.objects.get(payment_intent=intent)
        self.assertEqual(transaction_balances(tx), (1000, 1000))

    def test_create_refund_records_ledger_and_is_idempotent(self):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD", status=PaymentIntent.STATUS_SUCCEEDED)
        intent.attempts.create(method="card", provider="mock", status="succeeded", amount=1000, currency="USD")

        first = self.client.post(
            reverse("payments:refunds", args=[intent.public_id]),
            data=json.dumps({"amount": 400, "reason": "requested_by_customer"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            HTTP_IDEMPOTENCY_KEY="refund-1001",
        )
        second = self.client.post(
            reverse("payments:refunds", args=[intent.public_id]),
            data=json.dumps({"amount": 400, "reason": "requested_by_customer"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            HTTP_IDEMPOTENCY_KEY="refund-1001",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Refund.objects.count(), 1)
        tx = LedgerTransaction.objects.get(refund__public_id=first.json()["id"])
        self.assertEqual(transaction_balances(tx), (400, 400))

    def test_live_stripe_refund_does_not_claim_success_without_provider_call(self):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="stripe-primary",
            adapter="stripe",
        )
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1000,
            currency="USD",
            status=PaymentIntent.STATUS_SUCCEEDED,
            environment="live",
        )
        intent.attempts.create(
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider="stripe",
            status=PaymentAttempt.STATUS_SUCCEEDED,
            amount=1000,
            currency="USD",
        )

        response = self.client.post(
            reverse("payments:refunds", args=[intent.public_id]),
            data=json.dumps({"amount": 400}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "provider_refund_unavailable")
        self.assertFalse(Refund.objects.filter(payment_intent=intent).exists())
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)

    def test_manual_capture_is_rejected_before_creating_provider_session(self):
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({
                "amount": 1000,
                "currency": "USD",
                "payment_methods": ["card"],
                "capture_strategy": "manual",
            }),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "manual_capture_unavailable")
        self.assertFalse(PaymentIntent.objects.exists())

    def test_expiring_one_card_option_keeps_another_available(self):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD")
        primary = intent.attempts.create(
            method=PaymentAttempt.METHOD_CARD,
            provider="stripe",
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
        )
        backup = intent.attempts.create(
            method=PaymentAttempt.METHOD_CARD,
            provider="backup",
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
        )

        record_webhook("stripe-primary", {
            "id": "evt_primary_expired",
            "type": "checkout.session.expired",
            "payment_intent": intent.public_id,
            "foxpay_attempt_id": str(primary.id),
            "foxpay_method": PaymentAttempt.METHOD_CARD,
            "status": "expired",
        })
        intent.refresh_from_db()
        backup.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_REQUIRES_PAYMENT)
        self.assertEqual(backup.status, PaymentAttempt.STATUS_ACTION_REQUIRED)

        record_webhook("backup", {
            "id": "evt_backup_paid",
            "type": "checkout.session.completed",
            "payment_intent": intent.public_id,
            "foxpay_attempt_id": str(backup.id),
            "foxpay_method": PaymentAttempt.METHOD_CARD,
            "status": "paid",
        })
        record_webhook("stripe-primary", {
            "id": "evt_primary_expired_late",
            "type": "checkout.session.expired",
            "payment_intent": intent.public_id,
            "foxpay_attempt_id": str(primary.id),
            "foxpay_method": PaymentAttempt.METHOD_CARD,
            "status": "expired",
        })
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(ProviderEvent.objects.get(provider_event_id="evt_primary_expired_late").normalized_event_type, "payment_attempt.expired")

    @override_settings(FOXPAY_WEBHOOK_SECRET="test-secret")
    def test_duplicate_provider_webhooks_are_idempotent(self):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD")
        intent.attempts.create(method="crypto", provider="manual", status="action_required", amount=1000, currency="USD")
        payload = {"id": "evt_1001", "type": "crypto.confirmed", "payment_intent": intent.public_id, "status": "confirmed"}
        body = json.dumps(payload)
        signature = hmac.new(b"test-secret", body.encode("utf-8"), hashlib.sha256).hexdigest()

        first = self.client.post(reverse("payments:crypto_webhook", args=["manual"]), data=body, content_type="application/json", HTTP_X_FOXPAY_SIGNATURE=signature)
        second = self.client.post(reverse("payments:crypto_webhook", args=["manual"]), data=body, content_type="application/json", HTTP_X_FOXPAY_SIGNATURE=signature)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ProviderEvent.objects.count(), 1)

    def test_provider_credentials_are_encrypted_at_rest(self):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="card-primary",
            adapter="hosted",
        )
        credential = ProviderCredential(provider_config=config, name="api_key")
        credential.set_secret("super-secret-value")
        credential.save()

        credential.refresh_from_db()
        self.assertNotIn("super-secret-value", credential.encrypted_value)
        self.assertEqual(credential.reveal_secret(), "super-secret-value")


class HealthCheckTests(TestCase):
    def test_healthz_reports_ok(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_readyz_checks_database_and_cache_without_details(self):
        response = self.client.get(reverse("readyz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready", "service": "foxpay"})

    @patch("foxpay.views.cache.set", side_effect=RuntimeError("redis detail must stay private"))
    def test_readyz_returns_a_redacted_service_unavailable_response(self, _set):
        response = self.client.get(reverse("readyz"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable", "service": "foxpay"})
        self.assertNotContains(response, "redis detail", status_code=503)

    def test_openapi_reports_api_title_and_request_id_header(self):
        response = self.client.get(reverse("payments:openapi"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["title"], "Fox Pay API")
        self.assertIn(
            "/api/v1/webhooks/stripe/connect/{environment}/",
            response.json()["paths"],
        )
        self.assertIn(
            "/api/v1/webhooks/square/oauth/{environment}/",
            response.json()["paths"],
        )
        self.assertIn(
            "/api/v1/webhooks/paypal/partner/{environment}/",
            response.json()["paths"],
        )
        self.assertTrue(response.headers["X-Request-ID"].startswith("req_"))


class BootstrapMerchantTests(TestCase):
    def test_bootstrap_imports_provider_configs_from_env(self):
        provider_configs = json.dumps(
            [
                {
                    "kind": "card",
                    "provider": "card-primary",
                    "adapter": "mock",
                    "display_name": "Primary card",
                    "priority": 10,
                    "settings": {},
                },
                {
                    "kind": "crypto",
                    "provider": "btc-backup",
                    "adapter": "manual",
                    "display_name": "Backup BTC",
                    "priority": 20,
                    "is_active": False,
                    "settings": {"addresses": {"BTC": "bc1qbackup"}},
                },
            ]
        )

        with patch.dict("os.environ", {"FOXPAY_PROVIDER_CONFIGS": provider_configs}):
            call_command(
                "bootstrap_merchant",
                name="VulpFin",
                email="admin@example.com",
                password="change-me-now",
                stdout=StringIO(),
            )

        self.assertEqual(ProviderConfig.objects.count(), 2)
        card_provider = ProviderConfig.objects.get(provider="card-primary")
        crypto_provider = ProviderConfig.objects.get(provider="btc-backup")
        self.assertEqual(card_provider.adapter, "mock")
        self.assertFalse(crypto_provider.is_active)
        self.assertEqual(crypto_provider.settings["addresses"]["BTC"], "bc1qbackup")

    def test_webhook_endpoint_secret_is_encrypted_and_revealable(self):
        User = get_user_model()
        user = User.objects.create_user(username="owner@example.com")
        merchant = Merchant.objects.create(owner=user, name="VulpFin", slug="vulpfin")

        endpoint, secret = MerchantWebhookEndpoint.create_with_secret(merchant, "https://merchant.example/webhook")

        endpoint.refresh_from_db()
        self.assertTrue(secret.startswith("whsec_"))
        self.assertNotIn(secret, endpoint.encrypted_secret)
        self.assertTrue(endpoint.matches_secret(secret))
        self.assertEqual(endpoint.reveal_secret(), secret)


@override_settings(FOXPAY_ENV="live")
class SquareWebhookTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="square-owner@example.com")
        self.merchant = Merchant.objects.create(owner=user, name="VulpFin", slug="vulpfin")
        self.url = reverse("payments:square_webhook", args=["vulpfin", "square-primary"])
        self.notification_url = f"https://foxpay.fyi{self.url}"

    def configure(self, provider="square-primary", key="sq-test-signature-key"):
        with patch.dict("os.environ", {"SQUARE_WEBHOOK_SIGNATURE_KEY": key}):
            call_command(
                "configure_square_webhook_provider",
                merchant="vulpfin",
                provider=provider,
                environment="live",
                stdout=StringIO(),
            )
        return ProviderConfig.objects.get(provider=provider)

    def signature(self, body, key="sq-test-signature-key", url=None):
        digest = hmac.new(
            key.encode("utf-8"),
            (url or self.notification_url).encode("utf-8") + body,
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def send_event(self, event, key="sq-test-signature-key", url=None, path=None):
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        return self.client.post(
            path or self.url,
            data=body,
            content_type="application/json",
            HTTP_X_SQUARE_HMACSHA256_SIGNATURE=self.signature(body, key=key, url=url),
        )

    def test_square_signature_matches_published_example(self):
        self.assertTrue(verify_square_signature(
            b'{"hello":"world"}',
            "2kRE5qRU2tR+tBGlDwMEw2avJ7QM4ikPYD/PJ3bd9Og=",
            "asdf1234",
            "https://example.com/webhook",
        ))
        self.assertFalse(verify_square_signature(
            b'{"hello":"world"}', "not-ascii-\u00e9", "asdf1234", "https://example.com/webhook"
        ))

    def test_receiver_exists_before_signature_key_but_rejects_posts(self):
        self.assertEqual(self.client.post(self.url, data="{}", content_type="application/json").status_code, 404)
        self.configure(key="")
        config = ProviderConfig.objects.get(provider="square-primary")
        self.assertFalse(config.is_active)
        self.assertEqual(config.settings["webhook_notification_url"], self.notification_url)
        self.assertEqual(self.client.get(self.url).json(), {"receiver": "square", "ready": False})
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "provider_not_configured")
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_signed_event_is_deduplicated_and_does_not_settle_intent(self):
        config = self.configure()
        credential = config.credentials.get(name="webhook_signature_key")
        self.assertNotIn("sq-test-signature-key", credential.encrypted_value)
        self.assertTrue(self.client.get(self.url).json()["ready"])
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD")
        event = {
            "event_id": "evt_square_1001",
            "type": "payment.updated",
            "merchant_id": "square-merchant-1",
            "data": {
                "type": "payment",
                "id": "square-payment-1",
                "object": {"payment": {
                    "status": "COMPLETED",
                    "order_id": "square-order-1",
                    "reference_id": intent.public_id,
                    "card_details": {"card": {"last_4": "1234", "fingerprint": "private-card-data"}},
                }},
            },
        }
        first = self.send_event(event)
        second = self.send_event(event)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"received": True, "recorded": True, "reconciled": False})
        self.assertEqual(second.json(), {"received": True, "recorded": False, "reconciled": False})
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 1)
        recorded = ProviderEvent.objects.get()
        self.assertEqual(recorded.merchant, self.merchant)
        self.assertEqual(recorded.payload["status"], "COMPLETED")
        self.assertEqual(recorded.payload["resource_id"], "square-payment-1")
        self.assertNotIn("private-card-data", json.dumps(recorded.payload))
        self.assertNotIn("1234", json.dumps(WebhookDelivery.objects.get().payload))
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)

    def test_wrong_signature_url_and_malformed_signed_json_are_rejected(self):
        self.configure()
        event = {"event_id": "evt_square_1002", "type": "payment.created"}
        self.assertEqual(self.send_event(event, key="wrong-key").status_code, 401)
        self.assertEqual(self.send_event(event, url=self.notification_url.rstrip("/")).status_code, 401)
        bad_body = b"not-json"
        response = self.client.post(
            self.url,
            data=bad_body,
            content_type="application/json",
            HTTP_X_SQUARE_HMACSHA256_SIGNATURE=self.signature(bad_body),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.send_event({"type": "payment.created"}).status_code, 400)
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_two_square_provider_routes_have_independent_keys(self):
        self.configure()
        self.configure(provider="square-backup", key="backup-signature-key")
        backup_path = reverse("payments:square_webhook", args=["vulpfin", "square-backup"])
        event = {"event_id": "evt_shared_id", "type": "payment.created"}
        self.assertEqual(self.send_event(event).status_code, 200)
        self.assertEqual(self.send_event(event, path=backup_path).status_code, 401)
        self.assertEqual(self.send_event(
            event,
            key="backup-signature-key",
            url=f"https://foxpay.fyi{backup_path}",
            path=backup_path,
        ).status_code, 200)
        self.assertEqual(ProviderEvent.objects.count(), 2)

    def test_command_rejects_nonmatching_notification_url(self):
        with self.assertRaises(CommandError):
            call_command(
                "configure_square_webhook_provider",
                merchant="vulpfin",
                environment="live",
                notification_url="https://foxpay.fyi/api/v1/webhooks/stripe/stripe-primary/",
                stdout=StringIO(),
            )
        self.assertEqual(ProviderConfig.objects.count(), 0)

    def test_square_checkout_and_signed_payment_reconcile_once(self):
        config = self.configure()
        with patch.dict("os.environ", {"SQUARE_ACCESS_TOKEN": "square-test-token"}):
            call_command(
                "configure_square_checkout_provider",
                merchant="vulpfin",
                environment="live",
                location_id="SQ_LOCATION_1",
                square_merchant_id="square-merchant-1",
                activate=True,
                stdout=StringIO(),
            )
        config.refresh_from_db()
        self.assertTrue(config.is_active)
        self.assertNotIn("square-test-token", config.credentials.get(name="access_token").encrypted_value)
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="live")
        response = type("SquareResponse", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"payment_link": {"id": "link-1", "order_id": "order-1", "url": "https://square.link/u/link-1"}},
        })()
        with patch("apps.payments.adapters.square_checkout.requests.post", return_value=response) as post:
            request = RequestFactory().get("/", secure=True, HTTP_HOST="testserver")
            attempt = SquareCheckoutAdapter(config).create_attempt(request, intent, {})
        self.assertEqual(attempt.checkout_url, "https://square.link/u/link-1")
        self.assertEqual(post.call_args.kwargs["json"]["quick_pay"]["price_money"], {"amount": 2500, "currency": "USD"})
        self.assertEqual(post.call_args.kwargs["json"]["quick_pay"]["location_id"], "SQ_LOCATION_1")
        event = {
            "event_id": "evt_square_paid_1",
            "type": "payment.updated",
            "merchant_id": "square-merchant-1",
            "data": {"type": "payment", "id": "payment-1", "object": {"payment": {
                "id": "payment-1", "order_id": "order-1", "location_id": "SQ_LOCATION_1",
                "status": "COMPLETED", "total_money": {"amount": 2500, "currency": "USD"},
                "card_details": {"card": {"fingerprint": "private-card-data"}},
            }}},
        }
        first = self.send_event(event)
        second = self.send_event(event)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["reconciled"])
        self.assertEqual(second.status_code, 200)
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        self.assertEqual(attempt.provider_reference, "payment-1")
        self.assertNotIn("private-card-data", json.dumps(list(WebhookDelivery.objects.values_list("payload", flat=True))))

    def test_square_webhook_rejects_wrong_amount_for_linked_order(self):
        config = self.configure()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="live")
        PaymentAttempt.objects.create(
            intent=intent, provider_config=config, method="card", provider="square-primary", amount=2500, currency="USD",
            provider_response_metadata={"square_order_id": "order-1", "square_location_id": "SQ_LOCATION_1"},
        )
        event = {"event_id": "evt_wrong_amount", "type": "payment.updated", "data": {"type": "payment", "object": {"payment": {
            "id": "payment-1", "order_id": "order-1", "location_id": "SQ_LOCATION_1", "status": "COMPLETED",
            "total_money": {"amount": 100, "currency": "USD"},
        }}}}
        self.assertFalse(self.send_event(event).json()["reconciled"])
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)


class CustomerIdentityAndSubscriptionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner", password="test-pass")
        self.merchant = Merchant.objects.create(owner=self.owner, name="VulpFin", slug="vulpfin", status=Merchant.STATUS_ACTIVE)
        _, self.key = APIKey.issue(self.merchant, scopes=["subscriptions:write"])
        self.subject = "00000000-0000-4000-8000-000000000001"

    def test_merchant_can_sync_subscription_for_verified_subject_mapping(self):
        payload = {
            "customer": {"external_id": "customer-1", "tg11_user_uuid": self.subject},
            "provider": "stripe",
            "provider_reference": "sub_123",
            "plan_name": "Fox plan",
            "status": "active",
            "amount": 1200,
            "currency": "USD",
            "current_period_end": "2027-01-01T00:00:00Z",
        }
        url = reverse("payments:subscription_references")
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=self.key)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(SubscriptionReference.objects.count(), 1)
        self.assertEqual(str(Customer.objects.get().tg11_user_uuid), self.subject)
        payload["status"] = "canceled"
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SubscriptionReference.objects.get().status, "canceled")

    def test_subscription_cannot_move_to_another_customer(self):
        url = reverse("payments:subscription_references")
        payload = {"customer": {"tg11_user_uuid": self.subject}, "provider": "stripe", "provider_reference": "sub_123", "plan_name": "Fox plan", "status": "active"}
        self.assertEqual(self.client.post(url, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=self.key).status_code, 201)
        payload["customer"]["tg11_user_uuid"] = "a25156f4-3d63-40f9-8f19-8fa0d32f87b5"
        self.assertEqual(self.client.post(url, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=self.key).status_code, 409)
        self.assertEqual(SubscriptionReference.objects.count(), 1)

    def test_customer_identity_conflicts_are_rejected(self):
        Customer.objects.create(merchant=self.merchant, external_id="customer-1", tg11_user_uuid=self.subject)
        Customer.objects.create(merchant=self.merchant, external_id="customer-2", tg11_user_uuid="a25156f4-3d63-40f9-8f19-8fa0d32f87b5")
        with self.assertRaises(APIError):
            get_or_create_customer(self.merchant, {"customer": {"external_id": "customer-1", "tg11_user_uuid": "a25156f4-3d63-40f9-8f19-8fa0d32f87b5"}})

    def test_subscription_sync_requires_scoped_key(self):
        _, read_key = APIKey.issue(self.merchant, scopes=["payments:read"])
        response = self.client.post(
            reverse("payments:subscription_references"),
            data=json.dumps({"customer": {"tg11_user_uuid": self.subject}, "provider": "stripe", "provider_reference": "sub_123", "plan_name": "Fox plan", "status": "active"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=read_key,
        )
        self.assertEqual(response.status_code, 403)


class StripeSavedMethodTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("owner")
        self.merchant = Merchant.objects.create(owner=user, name="VulpFin", slug="vulpfin")
        self.customer = Customer.objects.create(merchant=self.merchant, tg11_user_uuid="00000000-0000-4000-8000-000000000001")
        self.config = ProviderConfig.objects.create(
            merchant=self.merchant, kind="card", provider="stripe-primary", adapter="stripe", environment="test",
        )
        credential = ProviderCredential(provider_config=self.config, name="secret_key")
        credential.set_secret("sk_test_mock")
        credential.save()
        self.stripe = Mock()
        self.stripe.Customer.create.return_value = {"id": "cus_test_1", "livemode": False}
        self.stripe.checkout.Session.create.return_value = {
            "url": "https://checkout.stripe.com/c/pay/setup-1", "customer": "cus_test_1", "livemode": False,
        }
        self.stripe.SetupIntent.retrieve.return_value = {
            "status": "succeeded", "customer": "cus_test_1", "payment_method": "pm_test_1",
        }
        self.stripe.PaymentMethod.retrieve.return_value = {
            "id": "pm_test_1", "customer": "cus_test_1", "type": "card",
            "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
        }

    def test_setup_webhook_saves_only_masked_method_and_detaches(self):
        request = RequestFactory().post("/", secure=True, HTTP_HOST="testserver")
        with patch("apps.payments.adapters.stripe_methods.stripe_module", return_value=self.stripe):
            url = create_setup_checkout(request, self.config, self.customer)
            self.assertEqual(url, "https://checkout.stripe.com/c/pay/setup-1")
            self.assertEqual(ProviderCustomerReference.objects.get().provider_reference, "cus_test_1")
            event = {
                "id": "evt_setup_1", "type": "checkout.session.completed", "livemode": False,
                "data": {"object": {
                    "mode": "setup", "status": "complete", "customer": "cus_test_1",
                    "client_reference_id": str(self.customer.uuid), "setup_intent": "seti_test_1",
                    "metadata": {"foxpay_customer_uuid": str(self.customer.uuid), "foxpay_provider_config_id": str(self.config.pk)},
                }},
            }
            self.assertTrue(record_setup_checkout(self.config, event))
            self.assertTrue(record_setup_checkout(self.config, event))
            method = PaymentMethodReference.objects.get()
            self.assertEqual(method.display_metadata["last4"], "4242")
            self.assertEqual(WebhookDelivery.objects.count(), 1)
            self.assertNotIn("sk_test_mock", json.dumps(WebhookDelivery.objects.get().payload))
            detach_saved_method(method)
        self.stripe.PaymentMethod.detach.assert_called_once()
        self.assertEqual(PaymentMethodReference.objects.count(), 0)

    def test_setup_webhook_rejects_another_customer(self):
        ProviderCustomerReference.objects.create(provider_config=self.config, customer=self.customer, provider_reference="cus_test_1")
        event = {
            "id": "evt_setup_2", "type": "checkout.session.completed", "livemode": False,
            "data": {"object": {
                "mode": "setup", "status": "complete", "customer": "cus_someone_else",
                "client_reference_id": str(self.customer.uuid), "setup_intent": "seti_test_2",
                "metadata": {"foxpay_customer_uuid": str(self.customer.uuid), "foxpay_provider_config_id": str(self.config.pk)},
            }},
        }
        with patch("apps.payments.adapters.stripe_methods.stripe_module", return_value=self.stripe):
            self.assertFalse(record_setup_checkout(self.config, event))
        self.assertEqual(PaymentMethodReference.objects.count(), 0)


@override_settings(FOXPAY_ENV="live", PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="", PAYPAL_WEBHOOK_ID="")
class PayPalCheckoutTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="paypal-owner@example.com")
        self.merchant = Merchant.objects.create(
            owner=user,
            name="VulpFin",
            slug="vulpfin",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
            allow_legacy_provider_configs=True,
        )
        self.url = reverse("payments:paypal_webhook", args=["vulpfin", "paypal-primary"])

    def configure(self, webhook_id="WH-TEST-1", provider="paypal-primary"):
        env = {
            "PAYPAL_CLIENT_ID": "paypal-test-client",
            "PAYPAL_CLIENT_SECRET": "paypal-test-secret",
            "PAYPAL_WEBHOOK_ID": webhook_id,
        }
        with patch.dict("os.environ", env):
            call_command(
                "configure_paypal_provider",
                merchant="vulpfin",
                provider=provider,
                environment="live",
                paypal_env="live",
                activate=True,
                stdout=StringIO(),
            )
        return ProviderConfig.objects.get(provider=provider)

    def paid_order(self, intent, attempt, value="25.00", currency="USD"):
        return {
            "id": "PAYPAL-ORDER-1",
            "status": "COMPLETED",
            "purchase_units": [{
                "custom_id": intent.public_id,
                "invoice_id": f"{intent.public_id}:{attempt.id}",
                "payments": {"captures": [{
                    "id": "CAPTURE-1",
                    "status": "COMPLETED",
                    "amount": {"currency_code": currency, "value": value},
                }]},
            }],
        }

    def intent_and_attempt(self, config, amount=2500, currency="USD"):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=amount, currency=currency)
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            status=PaymentAttempt.STATUS_PENDING,
            amount=intent.amount,
            currency=intent.currency,
        )
        return intent, attempt

    def approval_event(self, intent, attempt, event_id="WH-EVT-1"):
        return {
            "id": event_id,
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": "PAYPAL-ORDER-1",
                "purchase_units": [{"custom_id": intent.public_id, "invoice_id": f"{intent.public_id}:{attempt.id}"}],
            },
        }

    def post(self, event):
        return self.client.post(self.url, data=json.dumps(event), content_type="application/json")

    def test_credentials_are_stored_encrypted_and_the_receiver_reports_ready(self):
        config = self.configure()
        self.assertTrue(config.is_active)
        self.assertEqual(config.settings["paypal_env"], "live")
        credential = config.credentials.get(name="client_secret")
        self.assertNotIn("paypal-test-secret", credential.encrypted_value)
        self.assertEqual(credential.reveal_secret(), "paypal-test-secret")
        self.assertEqual(self.client.get(self.url).json(), {"receiver": "paypal", "ready": True})

    def test_unknown_provider_and_missing_webhook_id_are_refused(self):
        self.assertEqual(self.post({"id": "x", "event_type": "y"}).status_code, 404)
        self.configure(webhook_id="")
        self.assertFalse(self.client.get(self.url).json()["ready"])
        response = self.post({"id": "x", "event_type": "y"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "provider_not_configured")
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_an_event_paypal_will_not_vouch_for_is_refused(self):
        self.configure()
        with patch.object(PayPalCheckoutAdapter, "verify_webhook", return_value=False):
            response = self.post({"id": "WH-EVT-9", "event_type": "PAYMENT.CAPTURE.COMPLETED"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(WebhookDelivery.objects.count(), 0)
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_an_approved_order_is_captured_and_settles_the_intent(self):
        config = self.configure()
        intent, attempt = self.intent_and_attempt(config)
        with patch.object(PayPalCheckoutAdapter, "verify_webhook", return_value=True):
            with patch.object(PayPalCheckoutAdapter, "capture", return_value=self.paid_order(intent, attempt)) as capture:
                response = self.post(self.approval_event(intent, attempt))
        capture.assert_called_once_with("PAYPAL-ORDER-1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["captured"])
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        self.assertEqual(attempt.provider_reference, "CAPTURE-1")

    def test_a_capture_for_the_wrong_amount_does_not_settle_the_intent(self):
        from .models import AuditLog

        config = self.configure()
        intent, attempt = self.intent_and_attempt(config)
        with patch.object(PayPalCheckoutAdapter, "verify_webhook", return_value=True):
            with patch.object(PayPalCheckoutAdapter, "capture", return_value=self.paid_order(intent, attempt, value="5.00")):
                response = self.post(self.approval_event(intent, attempt))
        self.assertEqual(response.status_code, 200)
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertNotEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        self.assertTrue(AuditLog.objects.filter(action="paypal.webhook.amount_mismatch").exists())

    def test_the_same_capture_event_only_settles_once(self):
        config = self.configure()
        intent, attempt = self.intent_and_attempt(config)
        event = {
            "id": "WH-EVT-CAP-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAPTURE-1",
                "custom_id": intent.public_id,
                "invoice_id": f"{intent.public_id}:{attempt.id}",
                "amount": {"currency_code": "USD", "value": "25.00"},
            },
        }
        with patch.object(PayPalCheckoutAdapter, "verify_webhook", return_value=True):
            first = self.post(event)
            second = self.post(event)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 2)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)

    def test_amounts_cross_paypal_in_the_units_each_side_uses(self):
        self.assertEqual(minor_to_value(2500, "USD"), "25.00")
        self.assertEqual(minor_to_value(2500, "JPY"), "2500")
        self.assertEqual(minor_to_value(250000, "HUF"), "2500")
        self.assertEqual(minor_to_value(2500, "KRW"), "2500")
        self.assertEqual(capture_amount({"amount": {"currency_code": "HUF", "value": "2500"}}), (250000, "HUF"))
        self.assertEqual(capture_amount({"amount": {"currency_code": "KRW", "value": "2500"}}), (2500, "KRW"))

    def test_capture_amounts_are_read_in_minor_units(self):
        self.assertEqual(capture_amount({"amount": {"currency_code": "usd", "value": "25.00"}}), (2500, "USD"))
        self.assertEqual(capture_amount({"amount": {"currency_code": "JPY", "value": "2500"}}), (2500, "JPY"))
        self.assertEqual(capture_amount({"amount": {"currency_code": "USD", "value": "not-money"}}), (None, ""))
        self.assertEqual(capture_amount({}), (None, ""))

    def test_an_active_paypal_provider_needs_both_halves_of_its_credentials(self):
        with patch.dict("os.environ", {"PAYPAL_CLIENT_ID": "only-the-id", "PAYPAL_CLIENT_SECRET": "", "PAYPAL_WEBHOOK_ID": ""}):
            with self.assertRaises(CommandError):
                call_command("configure_paypal_provider", merchant="vulpfin", environment="live", activate=True, stdout=StringIO())
        self.assertFalse(ProviderConfig.objects.filter(provider="paypal-primary").exists())
