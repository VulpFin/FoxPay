from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from apps.payments.models import PaymentAttempt
from .base import Capability, PaymentProviderAdapter
from .stripe_checkout import StripeCheckoutAdapter


class MockCardAdapter(PaymentProviderAdapter):
    provider = "mock"
    capabilities = [Capability.CARD, Capability.DEBIT, Capability.HOSTED_CHECKOUT, Capability.IDEMPOTENCY]

    def create_checkout_session(self, request, intent, payload):
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=self.provider_config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=self.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=intent.amount,
            currency=intent.currency,
            instructions="Test-only hosted card page. Do not use for production card entry.",
        )
        attempt.checkout_url = request.build_absolute_uri(
            reverse("payments:mock_card_checkout_attempt", args=[attempt.id, intent.client_secret])
        )
        attempt.save(update_fields=["checkout_url", "updated_at"])
        return attempt

    create_attempt = create_checkout_session


class HostedCardAdapter(PaymentProviderAdapter):
    provider = "hosted"
    capabilities = [Capability.CARD, Capability.DEBIT, Capability.HOSTED_CHECKOUT, Capability.WEBHOOKS, Capability.MULTICURRENCY]

    def create_checkout_session(self, request, intent, payload):
        config = self.provider_config.settings if self.provider_config else {}
        base_url = config.get("checkout_url") or settings.FOXPAY_CARD_PROVIDER_CHECKOUT_URL
        if not base_url:
            raise ImproperlyConfigured("A checkout_url setting or FOXPAY_CARD_PROVIDER_CHECKOUT_URL is required for hosted card checkout.")

        query = urlencode(
            {
                "payment_intent": intent.public_id,
                "amount": intent.amount,
                "currency": intent.currency,
                "success_url": intent.success_url,
                "cancel_url": intent.cancel_url,
            }
        )
        separator = "&" if "?" in base_url else "?"
        return PaymentAttempt.objects.create(
            intent=intent,
            provider_config=self.provider_config,
            method=PaymentAttempt.METHOD_CARD,
            rail=PaymentAttempt.METHOD_CARD,
            provider=self.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=intent.amount,
            currency=intent.currency,
            checkout_url=f"{base_url}{separator}{query}",
            instructions="Customer completes card payment on the configured hosted provider checkout.",
        )

    create_attempt = create_checkout_session


def get_card_adapter(provider, provider_config=None):
    if provider == "mock":
        return MockCardAdapter(provider_config)
    if provider == "hosted":
        return HostedCardAdapter(provider_config)
    if provider in {"stripe", "stripe_checkout"}:
        return StripeCheckoutAdapter(provider_config)
    raise ImproperlyConfigured(f"Unsupported card provider: {provider}")
