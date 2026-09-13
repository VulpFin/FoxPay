from django.conf import settings

from .models import PaymentAttempt, ProviderConfig


def provider_routes(merchant, method, environment=None):
    env = environment or settings.FOXPAY_ENV
    kind = ProviderConfig.KIND_CARD if method == PaymentAttempt.METHOD_CARD else ProviderConfig.KIND_CRYPTO
    configs = list(
        merchant.provider_configs.filter(kind=kind, environment=env, is_active=True).order_by("priority", "created_at")
    )
    if configs:
        return [(config.adapter_name, config) for config in configs]
    if method == PaymentAttempt.METHOD_CARD:
        return [(merchant.card_provider, None)]
    return [(merchant.crypto_provider, None)]
