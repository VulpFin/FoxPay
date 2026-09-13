import json
import hashlib
import hmac
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .ledger import transaction_balances
from .services import record_webhook
from .models import (
    APIKey,
    Customer,
    IdempotencyRecord,
    LedgerTransaction,
    Merchant,
    MerchantWebhookEndpoint,
    PaymentAttempt,
    PaymentIntent,
    ProviderConfig,
    ProviderCredential,
    ProviderEvent,
    Refund,
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
            crypto_addresses={"BTC": "bc1qtestaddress"},
        )
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

    def test_openapi_reports_api_title_and_request_id_header(self):
        response = self.client.get(reverse("payments:openapi"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["title"], "Fox Pay API")
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
