import base64
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tg11_auth.models import TG11IdentityLink

from .adapters.base import ProviderAdapterError
from .adapters.paypal_checkout import PayPalCheckoutAdapter
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
from .paypal_partner import (
    PAYPAL_FEATURES,
    auth_assertion,
    create_partner_referral,
    seller_state,
)


PARTNER_SETTINGS = {
    "FOXPAY_PAYPAL_PARTNER_ENABLED": True,
    "PAYPAL_PARTNER_CLIENT_ID_TEST": "foxpay-partner-client-id",
    "PAYPAL_PARTNER_CLIENT_SECRET_TEST": "foxpay-partner-client-secret",
    "PAYPAL_PARTNER_MERCHANT_ID_TEST": "PQRSTUVWXYZAB",
    "PAYPAL_PARTNER_ATTRIBUTION_ID_TEST": "FOX_PAY_PARTNER",
    "PAYPAL_PARTNER_WEBHOOK_ID_TEST": "WH-PARTNER-TEST",
}
SELLER_ID = "CDEFGHJKLMNPQ"
OTHER_SELLER_ID = "DEFGHJKLMNPQR"


def decode_assertion_part(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


@override_settings(**PARTNER_SETTINGS)
class PayPalPartnerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("paypal-seller", email="paypal-seller@example.com")
        self.other_user = User.objects.create_user("paypal-other", email="paypal-other@example.com")
        TG11IdentityLink.objects.create(user=self.user, subject=str(uuid.uuid4()), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other_user, subject=str(uuid.uuid4()), application="foxpay")
        self.merchant = Merchant.objects.create(
            owner=self.user,
            name="PayPal Seller",
            slug="paypal-seller",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
        )
        self.other_merchant = Merchant.objects.create(
            owner=self.other_user,
            name="PayPal Other",
            slug="paypal-other",
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

    def status_payload(self, seller_id=SELLER_ID, tracking_id=None, *, ready=True):
        return {
            "merchant_id": seller_id,
            "tracking_id": tracking_id or str(self.merchant.uuid),
            "payments_receivable": ready,
            "primary_email_confirmed": ready,
            "products": [{"name": "EXPRESS_CHECKOUT", "vetting_status": "SUBSCRIBED"}],
            "oauth_integrations": [
                {
                    "integration_type": "OAUTH_THIRD_PARTY",
                    "integration_method": "PAYPAL",
                    "oauth_third_party": [
                        {
                            "partner_client_id": PARTNER_SETTINGS["PAYPAL_PARTNER_CLIENT_ID_TEST"],
                            "merchant_client_id": "seller-client-id-is-not-a-secret",
                            "scopes": [
                                "https://uri.paypal.com/services/payments/realtimepayment",
                                "https://uri.paypal.com/services/payments/refund",
                                "https://uri.paypal.com/services/payments/payment/authcapture",
                            ],
                        }
                    ],
                }
            ],
        }

    def connection(self, *, seller_id=SELLER_ID, status=MerchantProviderConnection.STATUS_ACTIVE):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="paypal",
            environment="test",
            authorization_method="paypal_partner",
            external_account_id=seller_id,
            status=status,
            connected_at=timezone.now(),
            granted_scopes=list(PAYPAL_FEATURES),
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider=f"paypal-{connection.uuid.hex}",
            adapter="paypal",
            display_name="PayPal",
            environment="test",
            is_active=status == MerchantProviderConnection.STATUS_ACTIVE,
            settings={"paypal_env": "sandbox", "partner": True},
        )
        return connection, config

    def partner_event(self, event_type, seller_id=SELLER_ID, event_id="WH-PARTNER-1", resource=None):
        return {
            "id": event_id,
            "event_type": event_type,
            "resource": resource or {"merchant_id": seller_id},
        }

    def post_partner_event(self, event):
        with patch("apps.payments.paypal_partner_views.verify_partner_webhook", return_value=True):
            return self.client.post(
                reverse("paypal_partner_webhook", args=["test"]),
                data=json.dumps(event),
                content_type="application/json",
            )

    def test_referral_payload_requests_only_payment_and_refund(self):
        links = {
            "links": [
                {
                    "rel": "self",
                    "href": "https://api-m.sandbox.paypal.com/v2/customer/partner-referrals/referral-token",
                },
                {
                    "rel": "action_url",
                    "href": "https://www.sandbox.paypal.com/us/merchantsignup/partner/onboardingentry?token=referral-token",
                },
            ]
        }
        with patch("apps.payments.paypal_partner.partner_request", return_value=links) as request:
            action = create_partner_referral(
                merchant=self.merchant,
                environment="test",
                return_url="https://foxpay.example/seller/paypal/test/callback/?state=state",
                request_id="referral-request",
            )
        self.assertIn("sandbox.paypal.com", action)
        body = request.call_args.kwargs["body"]
        details = body["operations"][0]["api_integration_preference"]["rest_api_integration"]
        self.assertEqual(details["integration_type"], "THIRD_PARTY")
        self.assertEqual(details["third_party_details"]["features"], ["PAYMENT", "REFUND"])
        self.assertEqual(body["products"], ["EXPRESS_CHECKOUT"])
        self.assertEqual(body["tracking_id"], str(self.merchant.uuid))
        serialized = json.dumps(body)
        self.assertNotIn("PARTNER_FEE", serialized)
        self.assertNotIn("DELAY_FUNDS_DISBURSEMENT", serialized)

    def test_begin_and_callback_verify_status_and_reject_replay(self):
        start = reverse("seller_paypal_connect", args=[self.merchant.slug, "test"])
        self.assertEqual(self.client.post(start).status_code, 403)
        self.recent_mfa()
        with patch(
            "apps.payments.paypal_partner_views.create_partner_referral",
            return_value="https://www.sandbox.paypal.com/onboarding?token=one-time",
        ) as referral:
            response = self.client.post(start)
        self.assertEqual(response.status_code, 302)
        return_url = referral.call_args.kwargs["return_url"]
        state = parse_qs(urlsplit(return_url).query)["state"][0]
        onboarding = ProviderOnboardingSession.objects.get()
        self.assertNotIn(state, onboarding.state_hash)
        callback = reverse("seller_paypal_callback", args=["test"])
        query = {
            "state": state,
            "merchantId": str(self.merchant.uuid),
            "merchantIdInPayPal": SELLER_ID,
            "permissionsGranted": "false",
            "consentStatus": "false",
            "isEmailConfirmed": "false",
        }
        with patch("apps.payments.paypal_partner_views.show_seller_status", return_value=self.status_payload()) as status:
            response = self.client.get(callback, query)
        self.assertEqual(response.status_code, 302)
        status.assert_called_once_with(environment="test", seller_merchant_id=SELLER_ID)
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)
        self.assertTrue(connection.metadata["payments_receivable"])
        self.assertTrue(connection.metadata["consent_status"])
        self.assertEqual(connection.external_account_id, SELLER_ID)
        self.assertFalse(connection.encrypted_access_token)
        self.assertFalse(connection.encrypted_refresh_token)
        self.assertTrue(config.is_active)
        self.assertFalse(config.credentials.exists())
        self.assertEqual(self.client.get(callback, query).status_code, 400)

    def test_callback_query_is_not_authoritative(self):
        _, state = begin_onboarding(
            merchant=self.merchant,
            user=self.user,
            provider="paypal",
            environment="test",
            requested_scopes=list(PAYPAL_FEATURES),
        )
        callback = reverse("seller_paypal_callback", args=["test"])
        query = {"state": state, "merchantId": str(self.merchant.uuid), "merchantIdInPayPal": SELLER_ID}
        mismatched = self.status_payload(tracking_id=str(self.other_merchant.uuid))
        with patch("apps.payments.paypal_partner_views.show_seller_status", return_value=mismatched):
            self.assertEqual(self.client.get(callback, query).status_code, 302)
        self.assertFalse(MerchantProviderConnection.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="paypal.connect_failed").exists())

    def test_auth_assertion_uses_payer_id_and_never_email(self):
        assertion = auth_assertion("platform-client", SELLER_ID)
        header, payload, signature = assertion.split(".")
        self.assertEqual(decode_assertion_part(header), {"alg": "none"})
        self.assertEqual(decode_assertion_part(payload), {"iss": "platform-client", "payer_id": SELLER_ID})
        self.assertEqual(signature, "")
        self.assertNotIn("email", assertion.lower())

    def test_adapter_uses_platform_assertion_and_explicit_seller_payee(self):
        connection, config = self.connection()
        adapter = PayPalCheckoutAdapter(config)
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="test")
        provider_order = {
            "id": "PAYPAL-ORDER-1",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-ORDER-1"}],
        }
        with patch.object(adapter, "call", return_value=(201, provider_order)) as call:
            attempt = adapter.create_checkout_session(RequestFactory().post("/checkout/"), intent, {})
        body = call.call_args.args[2]
        self.assertEqual(body["purchase_units"][0]["payee"], {"merchant_id": SELLER_ID})
        self.assertTrue(call.call_args.kwargs["request_id"].startswith("foxpay-order-"))
        self.assertEqual(attempt.provider_config, config)
        self.assertFalse(config.credentials.exists())

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @staticmethod
            def read():
                return b"{}"

        with patch("apps.payments.adapters.paypal_checkout.urllib.request.urlopen", return_value=Response()) as open_url:
            adapter.call("GET", "/v2/checkout/orders/PAYPAL-ORDER-1", token="platform-token", request_id="request-1")
        provider_request = open_url.call_args.args[0]
        assertion = provider_request.get_header("Paypal-auth-assertion")
        self.assertTrue(assertion)
        self.assertEqual(decode_assertion_part(assertion.split(".")[1])["payer_id"], SELLER_ID)
        self.assertEqual(provider_request.get_header("Paypal-partner-attribution-id"), "FOX_PAY_PARTNER")
        self.assertEqual(provider_request.get_header("Paypal-request-id"), "request-1")
        self.assertNotIn(PARTNER_SETTINGS["PAYPAL_PARTNER_CLIENT_SECRET_TEST"], str(provider_request.headers))

        connection.status = MerchantProviderConnection.STATUS_REVOKED
        connection.revoked_at = timezone.now()
        connection.save(update_fields=["status", "revoked_at", "updated_at"])
        with self.assertRaises(ProviderAdapterError):
            adapter.call("GET", "/v2/checkout/orders/PAYPAL-ORDER-1", token="platform-token")

    def test_partner_payment_webhook_routes_by_seller_and_attempt(self):
        connection, config = self.connection()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="test")
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=2500,
            currency="USD",
        )
        event = self.partner_event(
            "PAYMENT.CAPTURE.COMPLETED",
            resource={
                "id": "CAPTURE-1",
                "payee": {"merchant_id": connection.external_account_id},
                "custom_id": intent.public_id,
                "invoice_id": f"{intent.public_id}:{attempt.id}",
                "amount": {"currency_code": "USD", "value": "25.00"},
            },
        )
        response = self.post_partner_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["processed"])
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCEEDED)

        other_intent = PaymentIntent.objects.create(merchant=self.other_merchant, amount=2500, currency="USD", environment="test")
        event["id"] = "WH-PARTNER-CROSS"
        event["resource"]["custom_id"] = other_intent.public_id
        event["resource"]["invoice_id"] = f"{other_intent.public_id}:{attempt.id}"
        response = self.post_partner_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["mismatch"])
        other_intent.refresh_from_db()
        self.assertNotEqual(other_intent.status, PaymentIntent.STATUS_SUCCEEDED)

    def test_consent_revocation_is_verified_idempotent_and_disables_route(self):
        connection, config = self.connection()
        event = self.partner_event("MERCHANT.PARTNER-CONSENT.REVOKED")
        self.assertEqual(self.post_partner_event(event).json(), {"received": True, "processed": True})
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_REVOKED)
        self.assertFalse(config.is_active)
        recorded = ProviderEvent.objects.get()
        self.assertEqual(recorded.merchant, self.merchant)
        self.assertNotIn("summary", recorded.payload)
        self.assertEqual(self.post_partner_event(event).json(), {"received": True, "processed": False})

    def test_onboarding_webhook_refreshes_status_and_disconnect_requires_mfa(self):
        connection, config = self.connection(status=MerchantProviderConnection.STATUS_RESTRICTED)
        event = self.partner_event("MERCHANT.ONBOARDING.COMPLETED")
        with patch("apps.payments.paypal_partner_views.refresh_partner_connection", return_value=(connection, config)) as refresh:
            self.assertEqual(self.post_partner_event(event).json(), {"received": True, "processed": True})
        refresh.assert_called_once()
        disconnect = reverse("seller_paypal_disconnect", args=[self.merchant.slug, connection.uuid])
        self.assertEqual(self.client.post(disconnect).status_code, 403)
        self.recent_mfa()
        self.assertEqual(self.client.post(disconnect).status_code, 302)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_REVOKED)
        self.assertFalse(config.is_active)

    @override_settings(FOXPAY_PAYPAL_PARTNER_ENABLED=False)
    def test_feature_flag_hides_and_blocks_partner_onboarding(self):
        self.recent_mfa()
        self.assertEqual(
            self.client.post(reverse("seller_paypal_connect", args=[self.merchant.slug, "test"])).status_code,
            400,
        )
        response = self.client.get(reverse("seller_section", args=[self.merchant.slug, "providers"]))
        self.assertNotContains(response, "Connect PayPal test")

    def test_incomplete_provider_status_is_restricted(self):
        state = seller_state(
            self.status_payload(ready=False),
            environment="test",
            expected_tracking_id=str(self.merchant.uuid),
            expected_merchant_id=SELLER_ID,
        )
        self.assertFalse(state["ready"])
        self.assertFalse(state["metadata"]["payments_receivable"])
