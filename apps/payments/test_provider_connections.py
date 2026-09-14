from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import Merchant, MerchantProviderConnection, ProviderConfig, ProviderOnboardingSession
from .onboarding import OnboardingStateError, begin_onboarding, consume_onboarding, safe_return_path
from .routing import provider_routes


class ProviderConnectionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("seller")
        self.other = User.objects.create_user("other")
        self.merchant = Merchant.objects.create(owner=self.user, name="Seller", slug="seller", status="active")
        self.other_merchant = Merchant.objects.create(owner=self.other, name="Other", slug="other", status="active")

    def test_tokens_are_encrypted_and_clearable(self):
        connection = MerchantProviderConnection(merchant=self.merchant, provider="square", environment="test", authorization_method="square_oauth")
        connection.set_access_token("square-access-token")
        connection.set_refresh_token("square-refresh-token")
        connection.save()
        connection.refresh_from_db()
        self.assertNotIn("square-access-token", connection.encrypted_access_token)
        self.assertNotIn("square-refresh-token", connection.encrypted_refresh_token)
        self.assertEqual(connection.access_token(), "square-access-token")
        self.assertEqual(connection.refresh_token(), "square-refresh-token")
        connection.clear_tokens()
        connection.save()
        self.assertEqual(connection.access_token(), "")
        self.assertEqual(connection.refresh_token(), "")

    def test_new_seller_route_requires_active_matching_connection(self):
        config = ProviderConfig.objects.create(merchant=self.merchant, kind="card", environment="test", provider="stripe-primary", adapter="stripe")
        self.assertEqual(provider_routes(self.merchant, "card"), [])
        connection = MerchantProviderConnection.objects.create(merchant=self.merchant, provider="stripe", environment="test", authorization_method="stripe_connect", external_account_id="acct_test", status="pending")
        config.connection = connection
        config.save(update_fields=["connection"])
        self.assertEqual(provider_routes(self.merchant, "card"), [])
        connection.status = "active"
        connection.save(update_fields=["status"])
        self.assertEqual(len(provider_routes(self.merchant, "card")), 1)
        config.connection = MerchantProviderConnection.objects.create(merchant=self.other_merchant, provider="stripe", environment="test", authorization_method="stripe_connect", external_account_id="acct_other", status="active")
        config.save(update_fields=["connection"])
        self.assertEqual(provider_routes(self.merchant, "card"), [])
        config.connection = connection
        config.save(update_fields=["connection"])
        connection.status = "revoked"
        connection.revoked_at = timezone.now()
        connection.save(update_fields=["status", "revoked_at"])
        self.assertEqual(provider_routes(self.merchant, "card"), [])

    def test_legacy_config_requires_explicit_migration_flag(self):
        ProviderConfig.objects.create(merchant=self.merchant, kind="card", environment="test", provider="stripe-primary", adapter="stripe")
        self.assertEqual(provider_routes(self.merchant, "card"), [])
        self.merchant.allow_legacy_provider_configs = True
        self.merchant.save(update_fields=["allow_legacy_provider_configs"])
        self.assertEqual(len(provider_routes(self.merchant, "card")), 1)

    @override_settings(FOXPAY_PENDING_TEST_PAYMENTS_ENABLED=True)
    def test_explicit_pending_test_policy_allows_only_mock_route(self):
        self.merchant.status = "pending"
        self.merchant.save(update_fields=["status"])
        ProviderConfig.objects.create(merchant=self.merchant, kind="card", environment="test", provider="mock-test", adapter="mock")
        ProviderConfig.objects.create(merchant=self.merchant, kind="crypto", environment="test", provider="wallet-test", adapter="manual")
        self.assertEqual(len(provider_routes(self.merchant, "card")), 1)
        self.assertEqual(provider_routes(self.merchant, "crypto"), [])


class ProviderOnboardingStateTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("seller")
        self.other = User.objects.create_user("other")
        self.merchant = Merchant.objects.create(owner=self.user, name="Seller", slug="seller")
        self.other_merchant = Merchant.objects.create(owner=self.other, name="Other", slug="other")

    def issue(self, **kwargs):
        return begin_onboarding(merchant=self.merchant, user=self.user, provider="stripe", environment="test", **kwargs)

    def consume(self, state, **kwargs):
        return consume_onboarding(raw_state=state, merchant=kwargs.get("merchant", self.merchant), user=kwargs.get("user", self.user), provider=kwargs.get("provider", "stripe"), environment=kwargs.get("environment", "test"))

    def test_state_is_hashed_one_time_and_bound_to_identity_provider_environment(self):
        session, state = self.issue(requested_scopes=["read_write"], return_path="/seller/seller/")
        self.assertNotIn(state, session.state_hash)
        for mismatch in ({"merchant": self.other_merchant}, {"user": self.other}, {"provider": "square"}, {"environment": "live"}):
            with self.subTest(mismatch=mismatch), self.assertRaises(OnboardingStateError):
                self.consume(state, **mismatch)
        self.assertIsNone(ProviderOnboardingSession.objects.get(pk=session.pk).consumed_at)
        consumed = self.consume(state)
        self.assertIsNotNone(consumed.consumed_at)
        with self.assertRaises(OnboardingStateError):
            self.consume(state)
        with self.assertRaises(OnboardingStateError):
            self.consume("wrong-state")

    def test_expired_state_rejected_and_pkce_verifier_encrypted(self):
        session, state = self.issue(pkce_verifier="super-secret-verifier")
        self.assertNotIn("super-secret-verifier", session.encrypted_pkce_verifier)
        self.assertEqual(session.pkce_verifier(), "super-secret-verifier")
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        with self.assertRaises(OnboardingStateError):
            self.consume(state)
        self.assertIsNone(ProviderOnboardingSession.objects.get(pk=session.pk).consumed_at)

    def test_return_path_must_be_local(self):
        for value in ("https://attacker.example/", "//attacker.example/", "/\\attacker.example/", "/seller/#fragment", "/seller/\r\nHeader:bad"):
            with self.subTest(value=value), self.assertRaises(OnboardingStateError):
                safe_return_path(value)
        self.assertEqual(safe_return_path("/seller/seller/?tab=providers"), "/seller/seller/?tab=providers")


class LegacyProviderUpgradeTests(TransactionTestCase):
    def test_active_merchant_and_config_survive_schema_upgrade(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("payments", "0009_providercustomerreference_and_more")])
        old_apps = executor.loader.project_state([("payments", "0009_providercustomerreference_and_more")]).apps
        User = old_apps.get_model("accounts", "User")
        Merchant = old_apps.get_model("payments", "Merchant")
        ProviderConfig = old_apps.get_model("payments", "ProviderConfig")
        user = User.objects.create(username="legacy")
        merchant = Merchant.objects.create(owner_id=user.pk, name="Legacy", slug="legacy", status="active", is_active=True)
        config = ProviderConfig.objects.create(merchant_id=merchant.pk, kind="card", provider="stripe-primary", adapter="stripe", environment="live", is_active=True)

        executor = MigrationExecutor(connection)
        executor.migrate([("payments", "0011_provider_connections")])
        new_apps = executor.loader.project_state([("payments", "0011_provider_connections")]).apps
        NewMerchant = new_apps.get_model("payments", "Merchant")
        NewConfig = new_apps.get_model("payments", "ProviderConfig")
        upgraded = NewMerchant.objects.get(pk=merchant.pk)
        linked = NewConfig.objects.select_related("connection").get(pk=config.pk)
        self.assertTrue(upgraded.live_payments_enabled)
        self.assertTrue(upgraded.allow_legacy_provider_configs)
        self.assertIsNotNone(upgraded.approved_at)
        self.assertEqual(linked.connection.authorization_method, "legacy_config")
        self.assertEqual(linked.connection.provider, "stripe")
