import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from tg11_auth.models import TG11IdentityLink

from .agreements import agreement_snapshot
from .models import APIKey, AuditLog, Merchant, MerchantAgreementAcceptance, MerchantMembership, PaymentIntent
from .permissions import ROLE_CAPABILITIES, can_manage, member_merchants, merchant_membership, routing_block_reason
from .services import APIError, authenticate_merchant, create_attempts_for_method


class MerchantLifecycleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("seller", email="seller@example.com")
        self.other = User.objects.create_user("other", email="other@example.com")
        self.staff = User.objects.create_user("reviewer", is_staff=True)
        self.subject = uuid.uuid4()
        TG11IdentityLink.objects.create(user=self.owner, subject=str(self.subject), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other, subject=str(uuid.uuid4()), application="foxpay")
        self.application = {
            "name": "Example Shop",
            "legal_name": "Example Shop LLC",
            "website_url": "https://shop.example.com",
            "support_email": "help@example.com",
            "country": "us",
            "default_currency": "usd",
            "business_category": "Clothing",
            "business_description": "We sell our own printed shirts.",
            "accept_agreement": "on",
            "accept_acceptable_use": "on",
        }

    @override_settings(FOXPAY_SELLER_SIGNUP_ENABLED=True)
    def test_application_is_pending_atomic_and_records_agreement(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("seller_apply"), self.application, REMOTE_ADDR="192.0.2.10", HTTP_USER_AGENT="FoxPay test")
        self.assertEqual(response.status_code, 302)
        merchant = Merchant.objects.get()
        self.assertEqual(merchant.slug, "example-shop")
        self.assertEqual(merchant.status, Merchant.STATUS_PENDING)
        self.assertFalse(merchant.live_payments_enabled)
        self.assertEqual(merchant.country, "US")
        self.assertEqual(merchant.default_currency, "USD")
        membership = MerchantMembership.objects.get(merchant=merchant)
        self.assertEqual(membership.role, MerchantMembership.ROLE_OWNER)
        self.assertEqual(membership.tg11_user_uuid, self.subject)
        acceptance = MerchantAgreementAcceptance.objects.get(merchant=merchant)
        snapshot = agreement_snapshot()
        self.assertEqual(acceptance.agreement_hash, snapshot["agreement_hash"])
        self.assertEqual(acceptance.acceptable_use_hash, snapshot["acceptable_use_hash"])
        self.assertEqual(acceptance.request_ip, "192.0.2.10")
        self.assertEqual(acceptance.user_agent, "FoxPay test")
        self.assertEqual(AuditLog.objects.get(merchant=merchant).action, "merchant.application_submitted")
        with self.assertRaises(ValueError):
            acceptance.save()
        with self.assertRaises(ValueError):
            acceptance.delete()

    @override_settings(FOXPAY_SELLER_SIGNUP_ENABLED=True)
    def test_slug_collision_uses_unique_suffix(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("seller_apply"), self.application)
        self.client.post(reverse("seller_apply"), self.application)
        slugs = list(Merchant.objects.order_by("slug").values_list("slug", flat=True))
        self.assertEqual(len(slugs), 2)
        self.assertIn("example-shop", slugs)
        self.assertTrue(any(slug.startswith("example-shop-") for slug in slugs))

    @override_settings(FOXPAY_SELLER_SIGNUP_ENABLED=True)
    def test_application_rolls_back_on_acceptance_failure(self):
        self.client.force_login(self.owner)
        with patch("apps.payments.merchant_applications.MerchantAgreementAcceptance.objects.create", side_effect=RuntimeError("acceptance failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("seller_apply"), self.application)
        self.assertFalse(Merchant.objects.exists())
        self.assertFalse(MerchantMembership.objects.exists())

    def test_signup_closed_and_tg11_link_required(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("seller_apply")).status_code, 503)
        TG11IdentityLink.objects.filter(user=self.owner).delete()
        with override_settings(FOXPAY_SELLER_SIGNUP_ENABLED=True):
            self.assertEqual(self.client.get(reverse("seller_apply")).status_code, 403)

    def test_membership_isolation_and_role_boundaries(self):
        merchant = Merchant.objects.create(owner=self.owner, name="First", slug="first")
        another = Merchant.objects.create(owner=self.other, name="Second", slug="second")
        membership = MerchantMembership.objects.create(merchant=merchant, user=self.owner, tg11_user_uuid=self.subject, role="owner")
        MerchantMembership.objects.create(merchant=another, user=self.other, role="owner")
        self.assertEqual(list(member_merchants(self.owner)), [merchant])
        self.assertEqual(merchant_membership(self.owner, merchant), membership)
        self.assertIsNone(merchant_membership(self.owner, another))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("seller_detail", args=["first"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("seller_detail", args=["second"])).status_code, 404)
        expected = {
            "owner": {"view", "settings", "connections", "routing", "members", "webhooks", "keys", "refunds", "closure"},
            "administrator": {"view", "connections", "routing", "members", "webhooks"},
            "finance": {"view", "refunds", "exports"},
            "developer": {"view", "keys", "webhooks", "test_payments"},
            "support": {"view"},
            "viewer": {"view"},
        }
        self.assertEqual({role: set(caps) for role, caps in ROLE_CAPABILITIES.items()}, expected)
        for role, caps in expected.items():
            membership.role = role
            for capability in {cap for values in expected.values() for cap in values}:
                self.assertEqual(can_manage(membership, capability), capability in caps)

    def test_staff_review_approves_then_kills_live_routing(self):
        merchant = Merchant.objects.create(owner=self.owner, name="First", slug="first")
        self.client.force_login(self.staff)
        review_url = reverse("seller_review")
        self.assertEqual(self.client.get(review_url).status_code, 200)
        self.assertEqual(self.client.post(review_url, {"merchant_id": merchant.pk, "action": "approve"}).status_code, 302)
        merchant.refresh_from_db()
        self.assertEqual(merchant.status, Merchant.STATUS_ACTIVE)
        self.assertTrue(merchant.live_payments_enabled)
        self.assertEqual(merchant.approved_by, self.staff)
        self.assertEqual(self.client.post(review_url, {"merchant_id": merchant.pk, "action": "kill", "reason": "Risk review"}).status_code, 302)
        merchant.refresh_from_db()
        self.assertFalse(merchant.live_payments_enabled)
        self.assertEqual(merchant.suspension_reason, "Risk review")
        self.assertEqual(list(AuditLog.objects.filter(merchant=merchant).values_list("action", flat=True)), ["merchant.review.approve", "merchant.review.kill"])


@override_settings(FOXPAY_ENV="live")
class LiveRoutingTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.merchant = Merchant.objects.create(owner=self.owner, name="First", slug="first", status=Merchant.STATUS_ACTIVE, live_payments_enabled=True)
        _, self.key = APIKey.issue(self.merchant, environment="live", scopes=["payments:write"])

    def test_pending_restricted_disabled_and_killed_merchants_rejected(self):
        for status, live in [("pending", True), ("restricted", True), ("disabled", True), ("active", False)]:
            with self.subTest(status=status, live=live):
                self.merchant.status = status
                self.merchant.live_payments_enabled = live
                self.merchant.save(update_fields=["status", "live_payments_enabled"])
                with self.assertRaises(APIError) as caught:
                    authenticate_merchant(self.key, required_scope="payments:write")
                self.assertEqual(caught.exception.status, 403)

    def test_stale_merchant_is_rechecked_before_provider_adapter(self):
        intent = PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD", environment="live")
        stale = Merchant.objects.get(pk=self.merchant.pk)
        self.merchant.live_payments_enabled = False
        self.merchant.save(update_fields=["live_payments_enabled"])
        request = RequestFactory().post("/api/v1/payment-intents/")
        with patch("apps.payments.services.get_card_adapter") as adapter:
            with self.assertRaises(APIError):
                create_attempts_for_method(request, stale, intent, {}, "card")
        adapter.assert_not_called()

    def test_test_key_cannot_authorize_live_request(self):
        _, test_key = APIKey.issue(self.merchant, environment="test")
        with self.assertRaises(APIError) as caught:
            authenticate_merchant(test_key)
        self.assertEqual(caught.exception.status, 401)


class PendingTestRoutingTests(TestCase):
    def test_pending_test_merchant_requires_explicit_policy(self):
        owner = get_user_model().objects.create_user("owner")
        merchant = Merchant.objects.create(owner=owner, name="First", slug="first")
        self.assertEqual(routing_block_reason(merchant, "test"), "merchant_not_approved")
        with override_settings(FOXPAY_PENDING_TEST_PAYMENTS_ENABLED=True):
            self.assertEqual(routing_block_reason(merchant, "test"), "")
            self.assertEqual(routing_block_reason(merchant, "live"), "merchant_not_approved")
