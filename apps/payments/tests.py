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
from .models import (
    APIKey,
    Customer,
    IdempotencyRecord,
    LedgerTransaction,
    Merchant,
    MerchantWebhookEndpoint,
    PaymentIntent,
    ProviderConfig,
    ProviderCredential,
    ProviderEvent,
    Refund,
)


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
