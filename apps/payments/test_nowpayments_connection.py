import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tg11_auth.models import TG11IdentityLink

from .adapters.base import ProviderAdapterError
from .adapters.nowpayments import NowPaymentsAdapter, canonical_ipn
from .models import (
    AuditLog,
    Merchant,
    MerchantMembership,
    MerchantProviderConnection,
    PaymentAttempt,
    PaymentIntent,
    ProviderConfig,
    ProviderCredential,
    ProviderEvent,
)
from .nowpayments_connection import NowPaymentsConnectionError, check_nowpayments_api_key


@override_settings(FOXPAY_NOWPAYMENTS_SELF_SERVICE_ENABLED=True, FOXPAY_ENV="test")
class NowPaymentsConnectionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("crypto-seller", email="crypto-seller@example.com")
        self.other_user = User.objects.create_user("crypto-other", email="crypto-other@example.com")
        TG11IdentityLink.objects.create(user=self.user, subject=str(uuid.uuid4()), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other_user, subject=str(uuid.uuid4()), application="foxpay")
        self.merchant = Merchant.objects.create(
            owner=self.user,
            name="Crypto Seller",
            slug="crypto-seller",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
        )
        self.other_merchant = Merchant.objects.create(
            owner=self.other_user,
            name="Crypto Other",
            slug="crypto-other",
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

    def credentials(self, suffix="1234", **changes):
        data = {
            "display_name": "NOWPayments primary",
            "environment": "test",
            "pay_currency": "btc",
            "api_key": f"np-api-key-secret-{suffix}",
            "ipn_secret": f"np-ipn-secret-{suffix}",
        }
        data.update(changes)
        return data

    def connect(self, suffix="1234", *, follow=False, health=None):
        health = health or {"currency_count": 42}
        with patch("apps.payments.nowpayments_connection.check_nowpayments_api_key", return_value=health):
            return self.client.post(
                reverse("seller_nowpayments_connect", args=[self.merchant.slug]),
                self.credentials(suffix),
                follow=follow,
            )

    def test_connect_requires_mfa_encrypts_once_and_never_renders_credentials(self):
        url = reverse("seller_nowpayments_connect", args=[self.merchant.slug])
        self.assertEqual(self.client.post(url, self.credentials()).status_code, 403)
        self.assertFalse(MerchantProviderConnection.objects.exists())
        self.recent_mfa()
        response = self.connect(follow=True)
        self.assertEqual(response.status_code, 200)
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)
        self.assertEqual(connection.authorization_method, "nowpayments_credentials")
        self.assertTrue(config.is_active)
        self.assertEqual(config.connection, connection)
        self.assertEqual(config.settings["pay_currency"], "btc")
        self.assertEqual(connection.metadata["display_hint"], "Key ending in 1234")
        active = config.credentials.filter(revoked_at__isnull=True).order_by("name")
        self.assertEqual(list(active.values_list("name", flat=True)), ["api_key", "ipn_secret"])
        self.assertEqual(active.get(name="api_key").reveal_secret(), "np-api-key-secret-1234")
        self.assertEqual(active.get(name="ipn_secret").reveal_secret(), "np-ipn-secret-1234")
        self.assertNotIn("np-api-key-secret-1234", active.get(name="api_key").encrypted_value)
        self.assertNotIn("np-ipn-secret-1234", active.get(name="ipn_secret").encrypted_value)
        rendered = response.content.decode("utf-8")
        audit = json.dumps(list(AuditLog.objects.values("action", "metadata")))
        for secret in ("np-api-key-secret-1234", "np-ipn-secret-1234"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, audit)
        self.assertContains(
            response,
            "http://testserver/api/v1/webhooks/nowpayments/crypto-seller/" + config.provider + "/",
        )

    def test_failed_validation_keeps_route_inactive_and_erases_staged_ciphertext(self):
        self.recent_mfa()
        with patch(
            "apps.payments.nowpayments_connection.check_nowpayments_api_key",
            side_effect=NowPaymentsConnectionError("api_key_rejected"),
        ):
            response = self.client.post(
                reverse("seller_nowpayments_connect", args=[self.merchant.slug]),
                self.credentials("failed-secret"),
                follow=True,
            )
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ERROR)
        self.assertFalse(config.is_active)
        self.assertFalse(ProviderCredential.objects.exists())
        self.assertNotContains(response, "np-api-key-secret-failed-secret")
        self.assertNotContains(response, "np-ipn-secret-failed-secret")

    def test_multiple_accounts_and_cross_merchant_isolation(self):
        self.recent_mfa()
        self.assertEqual(self.connect("1111").status_code, 302)
        self.assertEqual(self.connect("2222").status_code, 302)
        self.assertEqual(MerchantProviderConnection.objects.filter(merchant=self.merchant).count(), 2)
        self.assertEqual(ProviderConfig.objects.filter(merchant=self.merchant, is_active=True).count(), 2)
        connection = MerchantProviderConnection.objects.filter(merchant=self.merchant).first()
        self.client.force_login(self.other_user)
        self.recent_mfa()
        self.assertEqual(
            self.client.post(reverse("seller_nowpayments_test", args=[self.merchant.slug, connection.uuid])).status_code,
            404,
        )

    def test_one_api_key_cannot_be_owned_by_two_merchants(self):
        self.recent_mfa()
        self.connect("shared-key")
        self.client.force_login(self.other_user)
        self.recent_mfa()
        with patch("apps.payments.nowpayments_connection.check_nowpayments_api_key", return_value={"currency_count": 42}):
            response = self.client.post(
                reverse("seller_nowpayments_connect", args=[self.other_merchant.slug]),
                self.credentials("shared-key"),
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            MerchantProviderConnection.objects.filter(
                external_account_id__startswith="key_",
                status=MerchantProviderConnection.STATUS_ACTIVE,
            ).count(),
            1,
        )
        rejected = MerchantProviderConnection.objects.get(merchant=self.other_merchant)
        self.assertEqual(rejected.status, MerchantProviderConnection.STATUS_ERROR)
        self.assertFalse(rejected.provider_configs.get().is_active)

    def test_replacement_is_atomic_and_failed_replacement_preserves_working_credentials(self):
        self.recent_mfa()
        self.connect()
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        replace = reverse("seller_nowpayments_replace", args=[self.merchant.slug, connection.uuid])
        replacement = self.credentials(
            "5678",
            display_name="NOWPayments backup",
            pay_currency="usdttrc20",
        )
        replacement.pop("environment")
        with patch("apps.payments.nowpayments_connection.check_nowpayments_api_key", return_value={"currency_count": 50}):
            self.assertEqual(self.client.post(replace, replacement).status_code, 302)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(config.credentials.get(name="api_key", revoked_at__isnull=True).reveal_secret(), "np-api-key-secret-5678")
        self.assertEqual(config.credentials.get(name="ipn_secret", revoked_at__isnull=True).reveal_secret(), "np-ipn-secret-5678")
        self.assertEqual(config.display_name, "NOWPayments backup")
        self.assertEqual(config.settings["pay_currency"], "usdttrc20")
        self.assertEqual(connection.metadata["display_hint"], "Key ending in 5678")

        failed = self.credentials("bad-replacement")
        failed.pop("environment")
        with patch(
            "apps.payments.nowpayments_connection.check_nowpayments_api_key",
            side_effect=NowPaymentsConnectionError("api_key_rejected"),
        ):
            self.assertEqual(self.client.post(replace, failed).status_code, 302)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_ACTIVE)
        self.assertTrue(config.is_active)
        self.assertEqual(config.credentials.get(name="api_key", revoked_at__isnull=True).reveal_secret(), "np-api-key-secret-5678")
        self.assertFalse(config.credentials.filter(name__startswith="pending:").exists())

    def test_health_check_and_disconnect_erase_credentials_and_disable_ipn(self):
        self.recent_mfa()
        self.connect()
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        health_url = reverse("seller_nowpayments_test", args=[self.merchant.slug, connection.uuid])
        with patch("apps.payments.nowpayments_connection.check_nowpayments_api_key", return_value={"currency_count": 51}) as check:
            self.assertEqual(self.client.post(health_url).status_code, 302)
        check.assert_called_once_with(api_key="np-api-key-secret-1234", environment="test")
        connection.refresh_from_db()
        self.assertEqual(connection.metadata["currency_count"], 51)

        disconnect = reverse("seller_nowpayments_disconnect", args=[self.merchant.slug, connection.uuid])
        self.assertEqual(self.client.post(disconnect).status_code, 302)
        connection.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(connection.status, MerchantProviderConnection.STATUS_REVOKED)
        self.assertFalse(config.is_active)
        self.assertFalse(config.credentials.exclude(encrypted_value="").exists())
        ipn = reverse("payments:nowpayments_ipn", args=[self.merchant.slug, config.provider])
        self.assertEqual(self.client.post(ipn, data="{}", content_type="application/json").status_code, 503)

    @patch("apps.payments.nowpayments_connection.requests.get")
    def test_api_key_check_uses_fixed_endpoint_header_and_refuses_redirects(self, get):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"currencies": ["btc", "eth"]}

        get.return_value = Response()
        self.assertEqual(
            check_nowpayments_api_key(api_key="private-api-key", environment="live"),
            {"currency_count": 2},
        )
        self.assertEqual(get.call_args.args[0], "https://api.nowpayments.io/v1/currencies")
        self.assertEqual(get.call_args.kwargs["headers"], {"x-api-key": "private-api-key"})
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("private-api-key", get.call_args.args[0])

    def test_linked_adapter_uses_seller_key_and_rejects_revoked_connection(self):
        self.recent_mfa()
        self.connect()
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="test")
        provider_response = {"id": "np-invoice-1", "invoice_url": "https://nowpayments.io/payment/?iid=np-invoice-1"}
        with patch("apps.payments.adapters.nowpayments.create_invoice_request", return_value=provider_response) as create:
            attempt = NowPaymentsAdapter(config).create_invoice(RequestFactory().post("/checkout/"), intent, {})
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_ACTION_REQUIRED)
        self.assertEqual(create.call_args.args[0], "np-api-key-secret-1234")
        self.assertEqual(create.call_args.args[1], "test")

        connection.status = MerchantProviderConnection.STATUS_REVOKED
        connection.revoked_at = timezone.now()
        connection.save(update_fields=["status", "revoked_at", "updated_at"])
        second = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="test")
        with patch("apps.payments.adapters.nowpayments.create_invoice_request") as create, self.assertRaises(ProviderAdapterError):
            NowPaymentsAdapter(config).create_invoice(RequestFactory().post("/checkout/"), second, {})
        create.assert_not_called()

    def test_ipn_records_only_allowlisted_fields(self):
        self.recent_mfa()
        self.connect()
        connection = MerchantProviderConnection.objects.get()
        config = ProviderConfig.objects.get()
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=2500, currency="USD", environment="test")
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CRYPTO,
            provider=config.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=2500,
            currency="USD",
        )
        order_id = f"foxpay:{intent.public_id}:{attempt.pk}"
        attempt.provider_response_metadata = {"nowpayments_order_id": order_id}
        attempt.provider_reference = "np-invoice-1"
        attempt.save(update_fields=["provider_response_metadata", "provider_reference", "updated_at"])
        payload = {
            "payment_id": 998877,
            "payment_status": "finished",
            "order_id": order_id,
            "price_amount": 25,
            "price_currency": "usd",
            "pay_amount": 0.001,
            "actually_paid": 0.001,
            "pay_currency": "btc",
            "pay_address": "sensitive-wallet-address",
            "purchase_id": "sensitive-purchase-id",
        }
        signature = hmac.new(
            b"np-ipn-secret-1234",
            canonical_ipn(payload),
            hashlib.sha512,
        ).hexdigest()
        response = self.client.post(
            reverse("payments:nowpayments_ipn", args=[self.merchant.slug, config.provider]),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_NOWPAYMENTS_SIG=signature,
        )
        self.assertEqual(response.status_code, 200)
        recorded = ProviderEvent.objects.get(provider=f"nowpayments:{config.pk}")
        serialized = json.dumps(recorded.payload)
        self.assertEqual(recorded.merchant, self.merchant)
        self.assertNotIn("sensitive-wallet-address", serialized)
        self.assertNotIn("sensitive-purchase-id", serialized)
        self.assertNotIn("raw", recorded.payload)

    def test_live_credentials_are_rejected_over_plain_http(self):
        self.recent_mfa()
        response = self.client.post(
            reverse("seller_nowpayments_connect", args=[self.merchant.slug]),
            self.credentials(environment="live"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MerchantProviderConnection.objects.exists())
