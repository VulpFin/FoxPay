from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def production_payment_dependencies(app_configs, **kwargs):
    if settings.FOXPAY_ENV != "live":
        return []
    findings = []
    if not settings.FOXPAY_REDIS_URL:
        findings.append(
            Error(
                "Live FoxPay requires REDIS_URL for shared abuse counters.",
                id="payments.E001",
            )
        )
    if not settings.CELERY_BROKER_URL or not settings.FOXPAY_ASYNC_TASKS_ENABLED:
        findings.append(
            Error(
                "Live FoxPay requires an enabled durable Celery broker.",
                id="payments.E002",
            )
        )
    if settings.CELERY_TASK_ALWAYS_EAGER:
        findings.append(
            Error(
                "Celery eager mode must be disabled for live FoxPay.",
                id="payments.E003",
            )
        )
    if not getattr(settings, "ADMINS", ()):
        findings.append(
            Warning(
                "No Django ADMINS are configured for payment-abuse alerts.",
                id="payments.W001",
            )
        )
    return findings
