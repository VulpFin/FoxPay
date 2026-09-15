import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tg11_auth.models import TG11IdentityLink

from .adapters.base import ProviderAdapterError
from .adapters.square_checkout import SquareCheckoutAdapter
from .models import (
    AuditLog,
    Merchant,
    MerchantMembership,
    MerchantProviderConnection,
    PaymentAttempt,
    PaymentIntent,
    ProviderConfig,
    ProviderEvent,
    ProviderOnboardingSession,
)
from .onboarding import begin_onboarding
from .square_oauth import SQUARE_SCOPES, obtain_token, refresh_connection


SQUARE_SETTINGS = {
    "FOXPAY_SQUARE_OAUTH_ENABLED": True,
    "SQUARE_APPLICATION_ID_TEST": "sq0idp-platform-test",
    "SQUARE_APPLICATION_SECRET_TEST": "sq0csp-platform-test",
    "SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST": "square-webhook-test",
    "SQUARE_OAUTH_WEBHOOK_URL_TEST": "https://testserver/api/v1/webhooks/square/oauth/test/",
}


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "payment_link": {
                "id": "square-link-1",
                "order_id": "square-order-1",
                "url": "https://sandbox.square.link/u/square-link-1",
            }
        }


class FakeAuthErrorResponse:
    status_code = 401

    def raise_for_status(self):
        raise requests.HTTPError("Square rejected the token", response=self)

    def json(self):
        return {"errors": [{"code": "ACCESS_TOKEN_REVOKED", "detail": "sensitive provider detail"}]}


@override_settings(**SQUARE_SETTINGS)
class SquareOAuthTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("square-seller", email="square-seller@example.com")
        self.other_user = User.objects.create_user("square-other", email="square-other@example.com")
        TG11IdentityLink.objects.create(user=self.user, subject=str(uuid.uuid4()), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other_user, subject=str(uuid.uuid4()), application="foxpay")
        self.merchant = Merchant.objects.create(
            owner=self.user,
            name="Square Seller",
            slug="square-seller",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
        )
        self.other_merchant = Merchant.objects.create(
            owner=self.other_user,
            name="Square Other",
            slug="square-other",
            status=Merchant.STATUS_ACTIVE,
        )
        MerchantMembership.objects.create(merchant=self.merchant, user=self.user, role=MerchantMembership.ROLE_OWNER)
        MerchantMembership.objects.create(merchant=self.other_merchant, user=self.other_user, role=MerchantMembership.ROLE_OWNER)
        self.client.force_login(self.user)

    def recent_mfa(self):
        session = self.client.session
        session["foxpay_tg11_mfa"] = True
        session["foxpay_tg11_auth_time"] = int(time.time())
        session.save()

    def connection(self, *, status=MerchantProviderConnection.STATUS_ACTIVE, refreshed_at=None, merchant_id="SQ_MERCHANT_1"):
        connection = MerchantProviderConnection(
            merchant=self.merchant,
            provider="square",
            environment="test",
            authorization_method="square_oauth",
            external_account_id=merchant_id,
            status=status,
            granted_scopes=list(SQUARE_SCOPES),
            connected_at=timezone.now(),
            token_refreshed_at=refreshed_at or timezone.now(),
            token_expires_at=timezone.now() + timedelta(days=25),
        )
        connection.set_access_token("square-access-token")
        connection.set_refresh_token("square-refresh-token")
        connection.save()
        return connection

    def config(self, connection, *, active=True):
        return ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider=f"square-{connection.uuid.hex}",
            adapter="square",
            environment="test",
            is_active=active,
            settings={
                "location_id": "SQ_LOCATION_1",
                "location_currency": "USD",
                "square_merchant_id": connection.external_account_id,
            },
        )

    def callback(self, state, locations):
        token = {
            "access_token": "square-access-secret",
            "refresh_token": "square-refresh-secret",
            "merchant_id": "SQ_MERCHANT_1",
        }
        status = {"client_id": SQUARE_SETTINGS["SQUARE_APPLICATION_ID_TEST"], "merchant_id": "SQ_MERCHANT_1", "scopes": list(SQUARE_SCOPES)}
        with (
            patch("apps.payments.square_oauth_views.obtain_token", return_value=(token, timezone.now() + timedelta(days=30))) as obtain,
            patch("apps.payments.square_oauth_views.token_status", return_value=status),
            patch("apps.payments.square_oauth_views.list_usable_locations", return_value=locations),
        ):
            response = self.client.get(
                reverse("seller_square_callback", args=[self.merchant.slug, "test"]),
                {"state": state, "code": "square-auth-code"},
            )
        if response.status_code != 400:
            self.assertEqual(obtain.call_args.kwargs["redirect_uri"], "http://testserver/seller/square-seller/square/test/callback/")
        return response

    def test_begin_and_callback_encrypt_tokens_activate_one_location_and_reject_replay(self):
        start = reverse("seller_square_connect", args=[self.merchant.slug, "test"])
        self.assertEqual(self.client.post(start).status_code, 403)
        self.recent_mfa()
        with patch("apps.payments.square_oauth_views.authorize_url", return_value="https://connect.squareupsandbox.com/oauth2/authorize") as authorize:
            response = self.client.post(start)
        self.assertEqual(response.status_code, 302)
        state = authorize.call_args.kwargs["state"]
        onboarding = ProviderOnboardingSession.objects.get()
        self.assertNotIn(state, onboarding.state_hash)

        locations = [{"id": "SQ_LOCATION_1", "name": "Main", "currency": "USD"}]
        self.assertEqual(self.callback(state, locations).status_code, 302)
        connection = MerchantProviderConnection.objects.get()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)
        self.assertEqual(connection.access_token(), "square-access-secret")
        self.assertEqual(connection.refresh_token(), "square-refresh-secret")
        self.assertNotIn("square-access-secret", connection.encrypted_access_token)
        self.assertNotIn("square-refresh-secret", connection.encrypted_refresh_token)
        config = ProviderConfig.objects.get()
        self.assertEqual(config.connection, connection)
        self.assertEqual(config.settings["location_id"], "SQ_LOCATION_1")
        self.assertTrue(config.is_active)
        self.assertNotIn("square-access-secret", json.dumps(list(AuditLog.objects.values("metadata"))))
        self.assertEqual(self.callback(state, locations).status_code, 400)

    def test_callback_state_cannot_cross_merchants(self):
        _, state = begin_onboarding(
            merchant=self.merchant,
            user=self.user,
            provider="square",
            environment="test",
            requested_scopes=list(SQUARE_SCOPES),
        )
        self.client.force_login(self.other_user)
        response = self.client.get(
            reverse("seller_square_callback", args=[self.other_merchant.slug, "test"]),
            {"state": state, "code": "square-auth-code"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(ProviderOnboardingSession.objects.get().consumed_at)

    def test_multiple_locations_require_an_owned_selection(self):
        self.recent_mfa()
        _, state = begin_onboarding(
            merchant=self.merchant,
            user=self.user,
            provider="square",
            environment="test",
            requested_scopes=list(SQUARE_SCOPES),
        )
        locations = [
            {"id": "SQ_LOCATION_1", "name": "Main", "currency": "USD"},
            {"id": "SQ_LOCATION_2", "name": "Annex", "currency": "USD"},
        ]
        self.assertEqual(self.callback(state, locations).status_code, 302)
        connection = MerchantProviderConnection.objects.get()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_PENDING)
        self.assertFalse(ProviderConfig.objects.exists())
        select = reverse("seller_square_location", args=[self.merchant.slug, connection.uuid])
        self.assertEqual(self.client.post(select, {"location_id": "SQ_LOCATION_FAKE"}).status_code, 400)
        self.assertEqual(self.client.post(select, {"location_id": "SQ_LOCATION_2"}).status_code, 302)
        config = ProviderConfig.objects.get()
        self.assertEqual(config.settings["location_id"], "SQ_LOCATION_2")
        connection.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)

    def test_refresh_uses_row_lock_rotates_encrypted_tokens_and_disables_on_failure(self):
        connection = self.connection(refreshed_at=timezone.now() - timedelta(days=8))
        config = self.config(connection, active=False)
        refreshed = {
            "access_token": "square-access-rotated",
            "refresh_token": "square-refresh-rotated",
            "merchant_id": connection.external_account_id,
        }
        status = {"merchant_id": connection.external_account_id, "scopes": list(SQUARE_SCOPES)}
        manager = MerchantProviderConnection.objects
        with (
            patch.object(manager, "select_for_update", wraps=manager.select_for_update) as select_for_update,
            patch("apps.payments.square_oauth.obtain_token", return_value=(refreshed, timezone.now() + timedelta(days=30))),
            patch("apps.payments.square_oauth.token_status", return_value=status),
        ):
            self.assertTrue(refresh_connection(connection))
        select_for_update.assert_called_once_with()
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.access_token(), "square-access-rotated")
        self.assertEqual(connection.refresh_token(), "square-refresh-rotated")
        self.assertNotIn("square-access-rotated", connection.encrypted_access_token)
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)
        self.assertTrue(config.is_active)

        with patch("apps.payments.square_oauth.obtain_token", side_effect=requests.Timeout("provider timeout")):
            self.assertFalse(refresh_connection(connection, force=True))
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ERROR)
        self.assertEqual(connection.last_error_code, "square_refresh_failed")
        self.assertFalse(config.is_active)

    @patch("apps.payments.square_oauth._json_request")
    def test_authorization_code_exchange_repeats_redirect_uri(self, request_json):
        request_json.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "merchant_id": "SQ_MERCHANT_1",
            "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
        }
        obtain_token(
            environment="test",
            grant_type="authorization_code",
            value="authorization-code",
            redirect_uri="https://foxpay.example/square/callback/",
        )
        body = request_json.call_args.kwargs["body"]
        self.assertEqual(body["redirect_uri"], "https://foxpay.example/square/callback/")
        self.assertEqual(body["code"], "authorization-code")
        with self.assertRaises(ValueError):
            obtain_token(environment="test", grant_type="authorization_code", value="authorization-code")

    def test_checkout_uses_the_connections_token_and_location(self):
        connection = self.connection()
        config = self.config(connection)
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1750,
            currency="USD",
            environment="test",
            description="Square OAuth checkout",
        )
        request = RequestFactory().post("/checkout/")
        with patch("apps.payments.adapters.square_checkout.requests.post", return_value=FakeResponse()) as post:
            attempt = SquareCheckoutAdapter(config).create_checkout_session(request, intent, {})
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_ACTION_REQUIRED)
        self.assertEqual(attempt.provider_config, config)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer square-access-token")
        self.assertEqual(post.call_args.kwargs["json"]["quick_pay"]["location_id"], "SQ_LOCATION_1")
        self.assertNotIn("square-access-token", json.dumps(post.call_args.kwargs["json"]))

    def test_checkout_revoked_token_quarantines_connection_without_storing_provider_detail(self):
        connection = self.connection()
        config = self.config(connection)
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1750, currency="USD", environment="test")
        request = RequestFactory().post("/checkout/")
        with (
            patch("apps.payments.adapters.square_checkout.requests.post", return_value=FakeAuthErrorResponse()),
            self.assertRaises(ProviderAdapterError),
        ):
            SquareCheckoutAdapter(config).create_checkout_session(request, intent, {})
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ERROR)
        self.assertEqual(connection.last_error_code, "square_access_token_revoked")
        self.assertNotIn("sensitive provider detail", connection.last_error_code)
        self.assertFalse(config.is_active)

    def signed_webhook(self, event):
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(
            SQUARE_SETTINGS["SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST"].encode("utf-8"),
            SQUARE_SETTINGS["SQUARE_OAUTH_WEBHOOK_URL_TEST"].encode("utf-8") + body,
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        return self.client.post(
            reverse("square_oauth_webhook", args=["test"]),
            data=body,
            content_type="application/json",
            HTTP_X_SQUARE_HMACSHA256_SIGNATURE=signature,
        )

    def test_signed_webhook_routes_payment_by_merchant_and_location(self):
        connection = self.connection()
        config = self.config(connection)
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1750, currency="USD", environment="test")
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=1750,
            currency="USD",
            provider_response_metadata={"square_order_id": "square-order-1", "square_location_id": "SQ_LOCATION_1"},
        )
        event = {
            "event_id": "square-event-1",
            "type": "payment.updated",
            "merchant_id": connection.external_account_id,
            "location_id": "SQ_LOCATION_1",
            "data": {
                "type": "payment",
                "id": "square-payment-1",
                "object": {
                    "payment": {
                        "id": "square-payment-1",
                        "order_id": "square-order-1",
                        "location_id": "SQ_LOCATION_1",
                        "status": "COMPLETED",
                        "total_money": {"amount": 1750, "currency": "USD"},
                    }
                },
            },
        }
        response = self.signed_webhook(event)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True, "recorded": True, "reconciled": True})
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)
        recorded = ProviderEvent.objects.get(provider=f"square:{config.pk}")
        self.assertEqual(recorded.merchant, self.merchant)
        self.assertEqual(self.signed_webhook(event).json(), {"received": True, "recorded": False, "reconciled": True})

    def test_revocation_webhook_and_disconnect_clear_tokens(self):
        connection = self.connection()
        config = self.config(connection)
        event = {
            "event_id": "square-revoked-1",
            "type": "oauth.authorization.revoked",
            "merchant_id": connection.external_account_id,
            "data": {"type": "oauth_authorization", "id": connection.external_account_id},
        }
        self.assertEqual(self.signed_webhook(event).json(), {"received": True, "processed": True})
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_REVOKED)
        self.assertFalse(connection.encrypted_access_token)
        self.assertFalse(connection.encrypted_refresh_token)
        self.assertFalse(config.is_active)

        connection = self.connection(status=MerchantProviderConnection.STATUS_ACTIVE, merchant_id="SQ_MERCHANT_2")
        config = self.config(connection)
        disconnect = reverse("seller_square_disconnect", args=[self.merchant.slug, connection.uuid])
        self.assertEqual(self.client.post(disconnect).status_code, 403)
        self.recent_mfa()
        with patch("apps.payments.square_oauth_views.revoke_authorization") as revoke:
            self.assertEqual(self.client.post(disconnect).status_code, 302)
        revoke.assert_called_once_with(environment="test", merchant_id=connection.external_account_id)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_REVOKED)
        self.assertFalse(connection.encrypted_access_token)
        self.assertFalse(config.is_active)
