import os
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse

from apps.payments.models import Merchant, ProviderConfig, ProviderCredential


class Command(BaseCommand):
    help = "Register a Square webhook-only provider and optionally store its signing key."

    def add_arguments(self, parser):
        parser.add_argument("--merchant", required=True, help="Merchant slug.")
        parser.add_argument("--provider", default="square-primary")
        parser.add_argument("--environment", default="", help="Defaults to FOXPAY_ENV.")
        parser.add_argument("--notification-url", default="", help="Exact HTTPS URL configured in Square.")
        parser.add_argument("--signature-key-env", default="SQUARE_WEBHOOK_SIGNATURE_KEY")

    def handle(self, *args, **options):
        merchant = Merchant.objects.filter(slug=options["merchant"]).first()
        if not merchant:
            raise CommandError(f"Merchant not found: {options['merchant']}")
        provider = options["provider"]
        if not provider or len(provider) > 40 or "/" in provider:
            raise CommandError("Provider must be a nonempty path segment of at most 40 characters.")
        environment = options["environment"] or os.getenv("FOXPAY_ENV", "test")
        if environment not in {"test", "live"}:
            raise CommandError("Environment must be test or live.")
        path = reverse("payments:square_webhook", args=[merchant.slug, provider])
        existing = ProviderConfig.objects.filter(
            merchant=merchant,
            environment=environment,
            kind=ProviderConfig.KIND_CARD,
            provider=provider,
        ).first()
        if existing and existing.adapter_name != "square":
            raise CommandError("A non-Square provider already uses this code.")
        notification_url = (
            options["notification_url"]
            or (existing.settings.get("webhook_notification_url") if existing else "")
            or f"https://foxpay.fyi{path}"
        )
        parsed = urlsplit(notification_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != path
        ):
            raise CommandError("Notification URL must be HTTPS and end at the exact Square webhook path without a query or fragment.")
        signature_key = os.getenv(options["signature_key_env"], "").strip()
        with transaction.atomic():
            if existing:
                config = existing
                config.settings = {**config.settings, "webhook_notification_url": notification_url}
                config.save(update_fields=["settings", "updated_at"])
            else:
                config = ProviderConfig.objects.create(
                    merchant=merchant,
                    environment=environment,
                    kind=ProviderConfig.KIND_CARD,
                    provider=provider,
                    adapter="square",
                    display_name="Square (webhook only)",
                    capabilities=["webhooks"],
                    is_active=False,
                    settings={"webhook_notification_url": notification_url},
                )
            if signature_key:
                credential, _ = ProviderCredential.objects.get_or_create(
                    provider_config=config,
                    name="webhook_signature_key",
                )
                credential.set_secret(signature_key)
                credential.revoked_at = None
                credential.save(update_fields=["encrypted_value", "last_rotated_at", "revoked_at", "updated_at"])
        self.stdout.write(self.style.SUCCESS("Square webhook provider configured."))
        self.stdout.write(f"Notification URL: {notification_url}")
        self.stdout.write(f"Signature key configured: {config.credentials.filter(name='webhook_signature_key', revoked_at__isnull=True).exists()}")
        self.stdout.write("Configure location and access token with configure_square_checkout_provider before checkout activation.")
