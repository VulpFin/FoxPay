from django.conf import settings

from .models import PaymentAttempt, ProviderConfig


def authorized_config(merchant, config):
    connection = config.connection
    if connection is None:
        return merchant.allow_legacy_provider_configs or (
            config.environment == "test"
            and config.adapter_name == "mock"
            and getattr(settings, "FOXPAY_PENDING_TEST_PAYMENTS_ENABLED", False)
        )
    if connection.merchant_id != merchant.pk or connection.environment != config.environment:
        return False
    if connection.status != connection.STATUS_ACTIVE or connection.revoked_at:
        return False
    expected_provider = config.adapter_name.removesuffix("_checkout")
    if connection.provider != expected_provider:
        return False
    return connection.authorization_method != "legacy_config" or merchant.allow_legacy_provider_configs


def provider_routes(merchant, method, environment=None):
    env = environment or settings.FOXPAY_ENV
    kind = ProviderConfig.KIND_CARD if method == PaymentAttempt.METHOD_CARD else ProviderConfig.KIND_CRYPTO
    configs = list(
        merchant.provider_configs.filter(kind=kind, environment=env, is_active=True).select_related("connection").order_by("priority", "created_at")
    )
    if configs:
        return [(config.adapter_name, config) for config in configs if authorized_config(merchant, config)]
    if env == "live":
        return []
    if not merchant.allow_legacy_provider_configs:
        return []
    if method == PaymentAttempt.METHOD_CARD:
        return [(merchant.card_provider, None)]
    return [(merchant.crypto_provider, None)]
