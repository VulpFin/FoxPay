import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from tg11_auth.models import TG11IdentityLink

from .models import APIKey, Merchant, MerchantMembership, MerchantProviderConnection, PaymentAttempt, PaymentIntent, ProviderConfig, ProviderEvent, ProviderOnboardingSession
from .onboarding import begin_onboarding
from .stripe_connect import exchange_code, verify_account


class FakeCheckout:
    last_kwargs = None

    @classmethod
    def create(cls, **kwargs):
        cls.last_kwargs = kwargs
        return {
            "id": "cs_test_connected_1",
            "url": "https://checkout.stripe.com/c/pay/connected",
            "status": "open",
            "payment_status": "unpaid",
            "payment_intent": "pi_connected_1",
            "livemode": False,
            "mode": "payment",
        }


class FakeWebhook:
    event = None

    @classmethod
    def construct_event(cls, body, signature, secret):
        if signature != "valid" or secret != "whsec_connect_test":
            raise ValueError("Bad signature")
        return cls.event


class FakeStripe:
    class checkout:
        Session = FakeCheckout

    Webhook = FakeWebhook


class FakeOAuthStripe:
    class OAuth:
        token_result = None

        @classmethod
        def token(cls, **kwargs):
            return cls.token_result

    class Account:
        @classmethod
        def retrieve(cls, account_id, **kwargs):
            return {"id": account_id, "country": "US", "charges_enabled": True, "capabilities": {"card_payments": "active"}}


@override_settings(
    FOXPAY_STRIPE_CONNECT_ENABLED=True,
    STRIPE_TEST_SECRET_KEY="sk_test_platform",
    STRIPE_CONNECT_CLIENT_ID_TEST="ca_test_platform",
    STRIPE_CONNECT_WEBHOOK_SECRET_TEST="whsec_connect_test",
    STRIPE_CONNECT_WEBHOOK_SECRET_LIVE="whsec_connect_test",
)
class StripeConnectTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("seller", email="seller@example.com")
        self.other_user = User.objects.create_user("other", email="other@example.com")
        TG11IdentityLink.objects.create(user=self.user, subject=str(uuid.uuid4()), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other_user, subject=str(uuid.uuid4()), application="foxpay")
        self.merchant = Merchant.objects.create(owner=self.user, name="Seller", slug="seller", status="active", live_payments_enabled=True)
        self.other_merchant = Merchant.objects.create(owner=self.other_user, name="Other", slug="other", status="active")
        MerchantMembership.objects.create(merchant=self.merchant, user=self.user, role="owner")
        MerchantMembership.objects.create(merchant=self.other_merchant, user=self.other_user, role="owner")
        self.client.force_login(self.user)

    def recent_mfa(self):
        import time
        session = self.client.session
        session["foxpay_tg11_mfa"] = True
        session["foxpay_tg11_auth_time"] = int(time.time())
        session.save()

    def connection(self, *, status="active", account_id="acct_SellerTest"):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant, provider="stripe", environment="test", authorization_method="stripe_connect",
            external_account_id=account_id, status=status, granted_scopes=["read_write"],
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant, connection=connection, kind="card", provider="stripe-primary",
            adapter="stripe", environment="test", is_active=status == "active",
        )
        return connection, config

    def test_begin_and_callback_store_only_account_id_and_reject_replay(self):
        start = reverse("seller_stripe_connect", args=["seller", "test"])
        self.assertEqual(self.client.post(start).status_code, 403)
        self.recent_mfa()
        with patch("apps.payments.stripe_connect_views.authorize_url", return_value="https://connect.stripe.com/oauth/authorize") as authorize:
            response = self.client.post(start)
        self.assertEqual(response.status_code, 302)
        state = authorize.call_args.kwargs["state"]
        session = ProviderOnboardingSession.objects.get()
        self.assertNotIn(state, session.state_hash)
        with patch("apps.payments.stripe_connect_views.exchange_code", return_value="acct_SellerTest") as exchange, patch("apps.payments.stripe_connect_views.verify_account", return_value={"ready": True, "country": "US", "capabilities": ["card_payments"]}):
            callback = reverse("seller_stripe_callback", args=["seller", "test"])
            response = self.client.get(callback, {"state": state, "code": "ac_test"})
        self.assertEqual(response.status_code, 302)
        exchange.assert_called_once_with(environment="test", code="ac_test")
        connection = MerchantProviderConnection.objects.get()
        self.assertEqual(connection.external_account_id, "acct_SellerTest")
        self.assertEqual(connection.status, "active")
        self.assertFalse(connection.encrypted_access_token)
        self.assertFalse(connection.encrypted_refresh_token)
        config = ProviderConfig.objects.get()
        self.assertEqual(config.connection, connection)
        self.assertEqual(config.adapter, "stripe")
        self.assertEqual(self.client.get(callback, {"state": state, "code": "ac_test"}).status_code, 400)

    def test_callback_state_is_not_transferable_to_other_merchant(self):
        _, state = begin_onboarding(merchant=self.merchant, user=self.user, provider="stripe", environment="test")
        callback = reverse("seller_stripe_callback", args=["other", "test"])
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(callback, {"state": state, "code": "ac_test"}).status_code, 400)
        self.assertIsNone(ProviderOnboardingSession.objects.get().consumed_at)

    @patch("apps.payments.stripe_connect.stripe_module", return_value=FakeOAuthStripe)
    def test_oauth_response_mode_scope_and_account_are_verified(self, _stripe):
        FakeOAuthStripe.OAuth.token_result = {"stripe_user_id": "acct_SellerTest", "livemode": False, "scope": "read_write", "access_token": "do-not-store"}
        self.assertEqual(exchange_code(environment="test", code="ac_test"), "acct_SellerTest")
        self.assertTrue(verify_account(environment="test", account_id="acct_SellerTest")["ready"])
        for changed in ({"livemode": True}, {"scope": "read_only"}, {"stripe_user_id": "not-an-account"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                FakeOAuthStripe.OAuth.token_result = {"stripe_user_id": "acct_SellerTest", "livemode": False, "scope": "read_write", **changed}
                exchange_code(environment="test", code="ac_test")

    def test_connected_account_has_one_merchant_owner(self):
        self.connection()
        with self.assertRaises(IntegrityError), transaction.atomic():
            MerchantProviderConnection.objects.create(
                merchant=self.other_merchant, provider="stripe", environment="test", authorization_method="stripe_connect",
                external_account_id="acct_SellerTest", status="active",
            )

    @patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=FakeStripe)
    def test_checkout_uses_platform_key_and_connected_account_direct_charge(self, _stripe):
        connection, config = self.connection()
        _, raw = APIKey.issue(self.merchant, environment="test", scopes=["payments:write"])
        response = self.client.post(
            reverse("payments:payment_intents"), data=json.dumps({"amount": 1200, "currency": "USD", "payment_methods": ["card"]}),
            content_type="application/json", HTTP_X_FOXPAY_KEY=raw,
        )
        self.assertEqual(response.status_code, 201)
        kwargs = FakeCheckout.last_kwargs
        self.assertEqual(kwargs["api_key"], "sk_test_platform")
        self.assertEqual(kwargs["stripe_account"], connection.external_account_id)
        self.assertNotIn("application_fee_amount", kwargs)
        self.assertNotIn("application_fee_amount", kwargs["payment_intent_data"])
        self.assertEqual(PaymentAttempt.objects.get().provider_config, config)

    def _event(self, intent, attempt, *, event_id="evt_connected_1", account="acct_SellerTest", amount=1200, currency="usd", merchant_id=None):
        return {
            "id": event_id, "type": "checkout.session.completed", "account": account, "livemode": False,
            "data": {"object": {
                "id": attempt.provider_reference, "mode": "payment", "livemode": False,
                "payment_status": "paid", "amount_total": amount, "currency": currency,
                "client_reference_id": intent.public_id,
                "metadata": {
                    "foxpay_payment_intent": intent.public_id, "foxpay_attempt_id": str(attempt.pk),
                    "foxpay_merchant_id": merchant_id or str(self.merchant.uuid),
                },
                "card_details": {"fingerprint": "sensitive-card-fingerprint"},
            }},
        }

    @patch("apps.payments.stripe_connect_views.stripe_module", return_value=FakeStripe)
    def test_signed_webhook_reconciles_only_matching_connection_and_is_idempotent(self, _stripe):
        connection, config = self.connection()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1200, currency="USD", environment="test")
        attempt = PaymentAttempt.objects.create(intent=intent, provider_config=config, method="card", provider=config.provider, status="action_required", amount=1200, currency="USD", provider_reference="cs_test_connected_1")
        url = reverse("stripe_connect_webhook", args=["test"])
        def send(event):
            FakeWebhook.event = event
            return self.client.post(url, data=b'{"event":"test"}', content_type="application/json", HTTP_STRIPE_SIGNATURE="valid")
        base = self._event(intent, attempt)
        for changes in ({"amount": 999}, {"currency": "eur"}, {"merchant_id": str(self.other_merchant.uuid)}):
            with self.subTest(changes=changes):
                self.assertEqual(send(self._event(intent, attempt, **changes)).status_code, 422)
                intent.refresh_from_db()
                self.assertNotEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        wrong_account = self._event(intent, attempt, account="acct_Unknown")
        self.assertEqual(send(wrong_account).json(), {"received": True, "processed": False})
        FakeWebhook.event = base
        live_endpoint = reverse("stripe_connect_webhook", args=["live"])
        self.assertEqual(self.client.post(live_endpoint, data=b'{"event":"test"}', content_type="application/json", HTTP_STRIPE_SIGNATURE="valid").json(), {"received": True, "processed": True})
        self.assertEqual(send(base).json(), {"received": True, "processed": False})
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        event = ProviderEvent.objects.get()
        self.assertEqual(event.merchant, self.merchant)
        self.assertEqual(event.provider, f"stripe-connect:{connection.pk}")
        self.assertNotIn("sensitive-card-fingerprint", json.dumps(event.payload))
        FakeWebhook.event = base
        self.assertEqual(self.client.post(url, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="invalid").status_code, 401)

    @patch("apps.payments.stripe_connect_views.stripe_module", return_value=FakeStripe)
    def test_deauthorization_and_account_status_events_disable_route(self, _stripe):
        connection, config = self.connection(status="restricted")
        url = reverse("stripe_connect_webhook", args=["test"])
        FakeWebhook.event = {"id": "evt_account_updated", "type": "account.updated", "account": connection.external_account_id, "livemode": False, "data": {"object": {"id": connection.external_account_id, "charges_enabled": True, "capabilities": {"card_payments": "active"}, "country": "US"}}}
        self.assertEqual(self.client.post(url, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="valid").status_code, 200)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, "active")
        self.assertTrue(config.is_active)
        FakeWebhook.event = {"id": "evt_deauth", "type": "account.application.deauthorized", "account": connection.external_account_id, "livemode": False, "data": {"object": {"id": "ca_test_platform"}}}
        self.assertEqual(self.client.post(url, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="valid").status_code, 200)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, "revoked")
        self.assertFalse(config.is_active)

    @patch("apps.payments.stripe_connect_views.disconnect_account")
    def test_disconnect_requires_mfa_and_revokes_connection(self, disconnect):
        connection, config = self.connection()
        url = reverse("seller_stripe_disconnect", args=["seller", connection.uuid])
        self.assertEqual(self.client.post(url).status_code, 403)
        disconnect.assert_not_called()
        self.recent_mfa()
        self.assertEqual(self.client.post(url).status_code, 302)
        disconnect.assert_called_once_with(environment="test", account_id=connection.external_account_id)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, "revoked")
        self.assertFalse(config.is_active)
