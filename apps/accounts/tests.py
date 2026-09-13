import uuid
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from tg11_auth.client import Claims
from tg11_auth.models import TG11IdentityLink
from tg11_auth.services import AuthError

from apps.payments.models import (
    Customer,
    Merchant,
    PaymentIntent,
    PaymentMethodReference,
    ProviderConfig,
    SubscriptionReference,
)

from .identity import guard_tg11_login, linked_subject, on_tg11_login
from .models import FoxPayProfile


class AccountDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("alice", email="alice@example.com", password="test-pass")
        self.other = User.objects.create_user("bob", email="bob@example.com", password="test-pass")
        self.owner = User.objects.create_user("owner", password="test-pass")
        self.subject = uuid.uuid4()
        self.other_subject = uuid.uuid4()
        TG11IdentityLink.objects.create(user=self.user, subject=str(self.subject), application="foxpay")
        TG11IdentityLink.objects.create(user=self.other, subject=str(self.other_subject), application="foxpay")
        merchant = Merchant.objects.create(owner=self.owner, name="VulpFin", slug="vulpfin")
        mine = Customer.objects.create(merchant=merchant, email="different@example.com", tg11_user_uuid=self.subject)
        theirs = Customer.objects.create(merchant=merchant, email=self.user.email, tg11_user_uuid=self.other_subject)
        PaymentIntent.objects.create(merchant=merchant, customer=mine, amount=2400, currency="USD", description="My purchase", reference="ORDER-1")
        PaymentIntent.objects.create(merchant=merchant, customer=theirs, amount=9900, currency="USD", description="Someone else's purchase", reference="ORDER-2")
        SubscriptionReference.objects.create(merchant=merchant, customer=mine, provider="stripe", provider_reference="sub_mine", plan_name="My plan", status="active", amount=1200, currency="USD")
        SubscriptionReference.objects.create(merchant=merchant, customer=theirs, provider="stripe", provider_reference="sub_theirs", plan_name="Other plan", status="active")
        PaymentMethodReference.objects.create(merchant=merchant, customer=mine, provider="stripe", provider_reference="pm_mine", type="card", display_metadata={"brand": "Visa", "last4": "4242"})
        PaymentMethodReference.objects.create(merchant=merchant, customer=theirs, provider="stripe", provider_reference="pm_theirs", type="card", display_metadata={"brand": "Mastercard", "last4": "9999"})

    def test_anonymous_sees_sign_in_not_payment_data(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")
        self.assertContains(response, 'id="theme-toggle"')
        self.assertContains(response, "foxpay/theme.js")
        self.assertNotContains(response, "My purchase")

    def test_scoped_payments_orders_subscriptions_and_methods(self):
        self.client.force_login(self.user)
        cases = [
            ("/payments/", "My purchase", "Someone else&#x27;s purchase"),
            ("/orders/", "ORDER-1", "ORDER-2"),
            ("/subscriptions/", "My plan", "Other plan"),
            ("/payment-methods/", "4242", "9999"),
        ]
        for path, visible, hidden in cases:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, visible)
                self.assertNotContains(response, hidden)

    def test_email_alone_never_links_customer_data(self):
        TG11IdentityLink.objects.filter(user=self.user).delete()
        self.client.force_login(self.user)
        response = self.client.get("/payments/")
        self.assertContains(response, "Connect your TG11 account")
        self.assertNotContains(response, "My purchase")


class IdentityTests(TestCase):
    def test_staff_requires_second_factor(self):
        user = get_user_model()(username="admin", is_staff=True)
        with self.assertRaises(AuthError):
            guard_tg11_login(user, Claims(subject=str(uuid.uuid4()), amr=["pwd"]))
        guard_tg11_login(user, Claims(subject=str(uuid.uuid4()), amr=["pwd", "otp"]))

    def test_profile_hook_and_unlink(self):
        user = get_user_model().objects.create_user("alice")
        subject = uuid.uuid4()
        link = TG11IdentityLink.objects.create(user=user, subject=str(subject), application="foxpay")
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.session = {}
        on_tg11_login(user=user, claims=Claims(subject=str(subject), auth_time=100, amr=["pwd", "otp"]), created=False, link=link, request=request)
        self.assertEqual(linked_subject(user), subject)
        self.assertEqual(FoxPayProfile.objects.get(user=user).tg11_user_uuid, subject)
        self.assertTrue(request.session["foxpay_tg11_mfa"])
        link.delete()
        self.assertIsNone(linked_subject(user))
        self.assertIsNone(FoxPayProfile.objects.get(user=user).tg11_user_uuid)


class SavedMethodViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("alice", email="alice@example.com")
        owner = User.objects.create_user("owner")
        self.subject = uuid.uuid4()
        TG11IdentityLink.objects.create(user=self.user, subject=str(self.subject), application="foxpay")
        self.merchant = Merchant.objects.create(owner=owner, name="VulpFin", slug="vulpfin")
        self.config = ProviderConfig.objects.create(
            merchant=self.merchant, kind="card", provider="stripe-primary", adapter="stripe",
            environment="test", is_active=True, settings={"allow_customer_method_setup": True},
        )
        self.client.force_login(self.user)

    def authenticate_recently(self):
        session = self.client.session
        session["foxpay_tg11_auth_time"] = int(time.time())
        session["foxpay_tg11_mfa"] = True
        session.save()

    def test_add_card_requires_recent_mfa(self):
        path = "/payment-methods/add/vulpfin/stripe-primary/"
        response = self.client.post(path)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "https://accounts.tg11.org/account/security", status_code=403)
        self.authenticate_recently()
        with patch("apps.accounts.views.create_setup_checkout", return_value="https://checkout.stripe.com/c/pay/test"):
            response = self.client.post(path)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://checkout.stripe.com/c/pay/test")
        self.assertEqual(Customer.objects.get().tg11_user_uuid, self.subject)

    def test_cannot_remove_another_users_method(self):
        self.authenticate_recently()
        other_customer = Customer.objects.create(merchant=self.merchant, tg11_user_uuid=uuid.uuid4())
        method = PaymentMethodReference.objects.create(
            merchant=self.merchant, customer=other_customer, provider_config=self.config,
            provider="stripe-primary", provider_reference="pm_other", type="card",
        )
        with patch("apps.accounts.views.detach_saved_method") as detach:
            response = self.client.post(f"/payment-methods/remove/{method.uuid}/")
        self.assertEqual(response.status_code, 404)
        detach.assert_not_called()
