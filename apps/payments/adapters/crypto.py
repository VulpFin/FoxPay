from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.payments.models import CryptoInvoice, PaymentAttempt
from .base import Capability, PaymentProviderAdapter


class ManualCryptoAdapter(PaymentProviderAdapter):
    provider = "manual"
    capabilities = [Capability.CRYPTO, Capability.STABLECOIN, Capability.ASYNC_SETTLEMENT, Capability.WEBHOOKS]

    def create_invoice(self, request, intent, payload):
        config = self.provider_config.settings if self.provider_config else {}
        asset = str(payload.get("crypto_asset", "BTC")).upper()
        network = payload.get("crypto_network") or ("bitcoin" if asset == "BTC" else asset.lower())
        addresses = config.get("addresses") or intent.merchant.crypto_addresses
        address = addresses.get(asset, "")
        amount_decimal = payload.get("crypto_asset_amount")

        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=self.provider_config,
            method=PaymentAttempt.METHOD_CRYPTO,
            rail=PaymentAttempt.METHOD_CRYPTO,
            provider=self.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Customer pays the displayed wallet invoice. Settlement should be confirmed by webhook or admin review.",
        )
        attempt.checkout_url = request.build_absolute_uri(
            reverse("payments:crypto_checkout_attempt", args=[attempt.id, intent.client_secret])
        )
        attempt.save(update_fields=["checkout_url", "updated_at"])
        CryptoInvoice.objects.create(
            payment_intent=intent,
            attempt=attempt,
            asset=asset,
            network=network,
            address=address,
            expected_amount_decimal=amount_decimal or None,
            amount_decimal=amount_decimal or None,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        return attempt

    create_attempt = create_invoice


def get_crypto_adapter(provider, provider_config=None):
    if provider == "manual":
        return ManualCryptoAdapter(provider_config)
    raise ValueError(f"Unsupported crypto provider: {provider}")
