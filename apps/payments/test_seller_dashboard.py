import json
import re
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.test import Client
from django.urls import reverse
from tg11_auth.models import TG11IdentityLink

from .models import APIKey, AuditLog, Merchant, MerchantAllowedReturnOrigin, MerchantMembership, MerchantWebhookEndpoint, MerchantWebhookEvent, PaymentIntent
from .safe_urls import UnsafeURL, parsed_public_url, resolve_public_target, return_origin, safe_webhook_post


class SellerDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("seller", email="seller@example.com")
        self.other = User.objects.create_user("other", email="other@example.com")
        TG11IdentityLink.objects.create(user=self.owner, subject=str(uuid.uuid4()), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other, subject=str(uuid.uuid4()), application="foxpay")
        self.merchant = Merchant.objects.create(owner=self.owner, name="Seller", slug="seller", status="active", live_payments_enabled=True)
        self.other_merchant = Merchant.objects.create(owner=self.other, name="Other", slug="other", status="active")
        self.membership = MerchantMembership.objects.create(merchant=self.merchant, user=self.owner, role="owner")
        MerchantMembership.objects.create(merchant=self.other_merchant, user=self.other, role="owner")
        self.client.force_login(self.owner)

    def recent_mfa(self):
        import time
        session = self.client.session
        session["foxpay_tg11_mfa"] = True
        session["foxpay_tg11_auth_time"] = int(time.time())
        session.save()

    def test_seller_pages_are_membership_scoped(self):
        PaymentIntent.objects.create(merchant=self.merchant, amount=1000, currency="USD", description="Mine")
        PaymentIntent.objects.create(merchant=self.other_merchant, amount=2000, currency="USD", description="Theirs")
        response = self.client.get(reverse("seller_section", args=["seller", "payments"]))
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Theirs")
        self.assertEqual(self.client.get(reverse("seller_section", args=["other", "payments"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("seller_section", args=["other", "keys"])).status_code, 404)
        self.assertEqual(self.client.post(reverse("seller_add_return_origin", args=["other"]), {"origin": "https://shop.example.com"}).status_code, 404)

    def test_seller_mutations_require_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(reverse("seller_add_return_origin", args=["seller"]), {"origin": "https://shop.example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MerchantAllowedReturnOrigin.objects.exists())

    def test_roles_restrict_sensitive_sections_and_post_actions(self):
        cases = {
            "administrator": {"webhooks", "members"},
            "finance": set(),
            "developer": {"keys", "webhooks"},
            "support": set(),
            "viewer": set(),
        }
        for role, allowed in cases.items():
            self.membership.role = role
            self.membership.save(update_fields=["role"])
            for section in ("keys", "webhooks", "members", "settings"):
                expected = 200 if section in allowed else 403
                with self.subTest(role=role, section=section):
                    self.assertEqual(self.client.get(reverse("seller_section", args=["seller", section])).status_code, expected)
            self.assertEqual(self.client.post(reverse("seller_add_return_origin", args=["seller"]), {"origin": "https://shop.example.com"}).status_code, 403)

    def test_key_creation_requires_recent_mfa_and_reveals_once(self):
        url = reverse("seller_create_key", args=["seller"])
        self.assertEqual(self.client.post(url, {"name": "Server", "environment": "test"}).status_code, 302)
        self.assertFalse(APIKey.objects.exists())
        self.recent_mfa()
        response = self.client.post(url, {"name": "Server", "environment": "test"})
        self.assertEqual(response.status_code, 200)
        raw = re.search(r"foxpay_test_[A-Za-z0-9_-]+", response.content.decode()).group()
        key = APIKey.objects.get()
        self.assertTrue(key.matches(raw))
        self.assertNotIn(raw, key.key_hash)
        self.assertNotIn(raw, self.client.get(reverse("seller_section", args=["seller", "keys"])).content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        rotate = self.client.post(reverse("seller_rotate_key", args=["seller", key.uuid]))
        self.assertEqual(rotate.status_code, 200)
        self.assertFalse(APIKey.objects.get(pk=key.pk).usable)
        self.assertEqual(APIKey.objects.count(), 2)
        self.assertEqual(AuditLog.objects.filter(action="api_key.rotated").count(), 1)

    def test_developer_cannot_issue_live_key_and_owner_must_be_approved(self):
        self.recent_mfa()
        self.membership.role = "developer"
        self.membership.save(update_fields=["role"])
        self.assertEqual(self.client.post(reverse("seller_create_key", args=["seller"]), {"name": "Live", "environment": "live"}).status_code, 403)
        self.membership.role = "owner"
        self.membership.save(update_fields=["role"])
        self.merchant.status = "pending"
        self.merchant.save(update_fields=["status"])
        self.assertEqual(self.client.post(reverse("seller_create_key", args=["seller"]), {"name": "Live", "environment": "live"}).status_code, 403)
        self.assertFalse(APIKey.objects.exists())

    def test_member_changes_require_mfa_and_preserve_owner_boundary(self):
        subject = uuid.uuid4()
        add = reverse("seller_add_member", args=["seller"])
        self.assertEqual(self.client.post(add, {"tg11_user_uuid": str(subject), "role": "viewer"}).status_code, 302)
        self.recent_mfa()
        self.assertEqual(self.client.post(add, {"tg11_user_uuid": str(subject), "role": "viewer"}).status_code, 302)
        member = MerchantMembership.objects.get(merchant=self.merchant, tg11_user_uuid=subject)
        self.assertEqual(member.role, "viewer")
        self.assertEqual(self.client.post(reverse("seller_change_member_role", args=["seller", member.uuid]), {"role": "administrator"}).status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, "administrator")
        self.assertEqual(self.client.post(reverse("seller_change_member_role", args=["seller", self.membership.uuid]), {"role": "viewer"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("seller_remove_member", args=["seller", self.membership.uuid])).status_code, 403)
        self.assertEqual(self.client.post(reverse("seller_remove_member", args=["other", member.uuid])).status_code, 404)
        self.membership.role = "administrator"
        self.membership.save(update_fields=["role"])
        self.assertEqual(self.client.post(add, {"tg11_user_uuid": str(uuid.uuid4()), "role": "administrator"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("seller_remove_member", args=["seller", member.uuid])).status_code, 403)
        self.assertTrue(AuditLog.objects.filter(action="member.role_changed", merchant=self.merchant).exists())

    def test_return_origin_registration_and_payment_validation(self):
        add = reverse("seller_add_return_origin", args=["seller"])
        response = self.client.post(add, {"origin": "https://shop.example.com/thanks"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MerchantAllowedReturnOrigin.objects.get().origin, "https://shop.example.com")
        self.assertEqual(self.client.post(add, {"origin": "http://127.0.0.1/"}).status_code, 400)
        _, raw = APIKey.issue(self.merchant, environment="test", scopes=["payments:write"])
        self.merchant.allow_legacy_provider_configs = True
        self.merchant.save(update_fields=["allow_legacy_provider_configs"])
        path = reverse("payments:payment_intents")
        payload = {"amount": 1000, "currency": "USD", "payment_methods": ["card"], "success_url": "https://unlisted.example/success"}
        response = self.client.post(path, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=raw)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "return_origin_not_allowed")
        payload["success_url"] = "https://shop.example.com/success"
        response = self.client.post(path, data=json.dumps(payload), content_type="application/json", HTTP_X_FOXPAY_KEY=raw)
        self.assertEqual(response.status_code, 201)

    @patch("apps.payments.merchant_views.resolve_public_target", return_value="93.184.215.14")
    def test_webhook_secret_is_one_time_and_rotation_has_handoff(self, _resolve):
        self.recent_mfa()
        response = self.client.post(reverse("seller_create_webhook", args=["seller"]), {"url": "https://hooks.example.com/foxpay", "description": "Orders"})
        self.assertEqual(response.status_code, 200)
        first = re.search(r"whsec_[A-Za-z0-9_-]+", response.content.decode()).group()
        endpoint = MerchantWebhookEndpoint.objects.get()
        self.assertTrue(endpoint.matches_secret(first))
        self.assertNotIn(first, endpoint.encrypted_secret)
        self.assertNotIn(first, self.client.get(reverse("seller_section", args=["seller", "webhooks"])).content.decode())
        rotation = self.client.post(reverse("seller_rotate_webhook", args=["seller", endpoint.uuid]))
        self.assertEqual(rotation.status_code, 200)
        second = re.search(r"whsec_[A-Za-z0-9_-]+", rotation.content.decode()).group()
        endpoint.refresh_from_db()
        self.assertTrue(endpoint.matches_secret(first))
        self.assertNotIn(second, endpoint.pending_encrypted_secret)
        self.assertFalse(endpoint.matches_secret(second))
        self.assertEqual(self.client.post(reverse("seller_rotate_webhook", args=["seller", endpoint.uuid])).status_code, 400)
        self.assertEqual(self.client.post(reverse("seller_activate_webhook_secret", args=["seller", endpoint.uuid])).status_code, 302)
        endpoint.refresh_from_db()
        self.assertTrue(endpoint.matches_secret(second))
        self.assertFalse(endpoint.matches_secret(first))
        self.assertFalse(endpoint.pending_secret_hash)
        self.client.post(reverse("seller_test_webhook", args=["seller", endpoint.uuid]))
        self.assertEqual(MerchantWebhookEvent.objects.get().payload["endpoint"], str(endpoint.uuid))


class SafeOutboundURLTests(TestCase):
    def test_rejects_private_userinfo_fragments_and_webhook_queries(self):
        bad = [
            "https://127.0.0.1/x", "https://10.0.0.1/x", "https://169.254.169.254/x",
            "https://[::1]/x", "https://localhost/x", "https://metadata.google.internal/x",
            "https://user:secret@example.com/x", "https://@example.com/x", "https://example.com/x#fragment",
            "https://example.com\\@127.0.0.1/x",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                parsed_public_url(url)
        with self.assertRaises(UnsafeURL):
            parsed_public_url("https://hooks.example.com/path?token=secret", allow_query=False)
        self.assertEqual(return_origin("https://shop.example.com:443/success"), "https://shop.example.com")

    @patch("apps.payments.safe_urls.socket.getaddrinfo")
    def test_dns_resolution_rejects_mixed_public_private_answers(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.215.14", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ]
        with self.assertRaises(UnsafeURL):
            resolve_public_target("hooks.example.com", 443)

    @patch("apps.payments.safe_urls.PinnedHTTPSConnection")
    @patch("apps.payments.safe_urls.resolve_public_target", return_value="93.184.215.14")
    def test_request_pins_ip_and_does_not_follow_redirects(self, _resolve, connection_cls):
        connection = connection_cls.return_value
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b"redirect"
        status = safe_webhook_post("https://hooks.example.com/payment", b"{}", {"FoxPay-Signature": "signature"})
        self.assertEqual(status, 302)
        connection_cls.assert_called_once_with("hooks.example.com", 443, "93.184.215.14")
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/payment"))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Host"], "hooks.example.com")
        connection.close.assert_called_once()
