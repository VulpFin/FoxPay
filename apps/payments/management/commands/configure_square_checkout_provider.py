import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.payments.models import Merchant, ProviderConfig, ProviderCredential


class Command(BaseCommand):
    help = "Configure Square-hosted checkout for an existing signed webhook provider."

    def add_arguments(self, parser):
        parser.add_argument("--merchant", required=True)
        parser.add_argument("--provider", default="square-primary")
        parser.add_argument("--environment", default="")
        parser.add_argument("--location-id", required=True)
        parser.add_argument("--square-merchant-id", default="")
        parser.add_argument("--access-token-env", default="SQUARE_ACCESS_TOKEN")
        parser.add_argument("--activate", action="store_true")

    def handle(self, *args, **options):
        merchant = Merchant.objects.filter(slug=options["merchant"]).first()
        if not merchant:
            raise CommandError("Merchant not found.")
        environment = options["environment"] or os.getenv("FOXPAY_ENV", "test")
        if environment not in {"test", "live"}:
            raise CommandError("Environment must be test or live.")
        config = ProviderConfig.objects.filter(
            merchant=merchant,
            environment=environment,
            kind=ProviderConfig.KIND_CARD,
            provider=options["provider"],
        ).first()
        if not config or config.adapter_name != "square":
            raise CommandError("Configure the Square webhook provider first.")
        location_id = options["location_id"].strip()
        square_merchant_id = options["square_merchant_id"].strip()
        if not location_id or len(location_id) > 100 or len(square_merchant_id) > 100:
            raise CommandError("Square location or merchant ID is invalid.")
        token = os.getenv(options["access_token_env"], "").strip()
        existing_token = config.credentials.filter(name="access_token", revoked_at__isnull=True).exists()
        has_signature_key = config.credentials.filter(name="webhook_signature_key", revoked_at__isnull=True).exists()
        if not token and not existing_token:
            raise CommandError("Square access token is required.")
        if options["activate"] and not has_signature_key:
            raise CommandError("Cannot activate Square checkout without a webhook signature key.")
        with transaction.atomic():
            config.settings = {
                **config.settings,
                "location_id": location_id,
                **({"square_merchant_id": square_merchant_id} if square_merchant_id else {}),
            }
            config.display_name = "Square"
            config.capabilities = ["card", "debit", "wallet", "hosted_checkout", "webhooks", "idempotency"]
            if options["activate"]:
                config.is_active = True
            config.save(update_fields=["settings", "display_name", "capabilities", "is_active", "updated_at"])
            if token:
                credential, _ = ProviderCredential.objects.get_or_create(provider_config=config, name="access_token")
                credential.set_secret(token)
                credential.revoked_at = None
                credential.save(update_fields=["encrypted_value", "last_rotated_at", "revoked_at", "updated_at"])
        self.stdout.write(self.style.SUCCESS("Square checkout configured."))
        self.stdout.write(f"Provider active: {config.is_active}")
