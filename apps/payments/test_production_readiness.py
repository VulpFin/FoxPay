from django.test import SimpleTestCase, override_settings

from .checks import production_payment_dependencies


class ProductionPaymentDependencyCheckTests(SimpleTestCase):
    @override_settings(
        FOXPAY_ENV="staging",
        FOXPAY_TRUSTED_PROXY_IPS=("not-a-network",),
    )
    def test_invalid_environment_and_proxy_are_errors(self):
        identifiers = {
            finding.id for finding in production_payment_dependencies(None)
        }
        self.assertEqual(identifiers, {"payments.E000", "payments.E004"})

    @override_settings(
        FOXPAY_ENV="live",
        FOXPAY_REDIS_URL="redis://127.0.0.1:6379/0",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/1",
        FOXPAY_ASYNC_TASKS_ENABLED=True,
        CELERY_TASK_ALWAYS_EAGER=False,
        ADMINS=(("Operations", "ops@example.com"),),
        FOXPAY_TRUSTED_PROXY_IPS=("127.0.0.1/32",),
    )
    def test_complete_live_dependencies_pass(self):
        self.assertEqual(production_payment_dependencies(None), [])

    @override_settings(
        FOXPAY_ENV="live",
        FOXPAY_REDIS_URL="redis://127.0.0.1:6379/0",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/1",
        FOXPAY_ASYNC_TASKS_ENABLED=True,
        CELERY_TASK_ALWAYS_EAGER=False,
        ADMINS=(("Operations", "ops@example.com"),),
        FOXPAY_TRUSTED_PROXY_IPS=(),
    )
    def test_live_without_trusted_proxy_warns(self):
        findings = production_payment_dependencies(None)
        self.assertEqual([finding.id for finding in findings], ["payments.W002"])
