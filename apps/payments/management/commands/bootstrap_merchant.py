import json
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from apps.payments.models import APIKey, Merchant, ProviderConfig


class Command(BaseCommand):
    help = "Create an initial Fox Pay admin user, merchant, and API key."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Merchant display name.")
        parser.add_argument("--email", required=True, help="Admin email and username.")
        parser.add_argument("--password", required=True, help="Initial admin password.")
        parser.add_argument("--website-url", default="", help="Merchant website URL.")

    def handle(self, *args, **options):
        User = get_user_model()
        email = options["email"].strip().lower()
        name = options["name"].strip()
        if not email or not name:
            raise CommandError("--name and --email are required.")

        user, created_user = User.objects.get_or_create(
            username=email,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        if created_user:
            user.set_password(options["password"])
            user.save(update_fields=["password"])

        merchant, _ = Merchant.objects.get_or_create(
            slug=slugify(name),
            defaults={
                "name": name,
                "owner": user,
                "website_url": options["website_url"],
                "support_email": email,
            },
        )
        imported_providers = self.import_provider_configs(merchant)
        api_key, raw_key = APIKey.issue(merchant, "Bootstrap API key")

        self.stdout.write(self.style.SUCCESS("Fox Pay bootstrap complete."))
        self.stdout.write(f"Admin user: {email}")
        self.stdout.write(f"Merchant: {merchant.name}")
        self.stdout.write(f"Imported provider configs: {imported_providers}")
        self.stdout.write(f"API key prefix: {api_key.prefix}")
        self.stdout.write("")
        self.stdout.write("One-time API key:")
        self.stdout.write(raw_key)

    def import_provider_configs(self, merchant):
        raw_configs = os.getenv("FOXPAY_PROVIDER_CONFIGS", "").strip()
        if not raw_configs:
            return 0
        try:
            configs = json.loads(raw_configs)
        except json.JSONDecodeError as exc:
            raise CommandError(f"FOXPAY_PROVIDER_CONFIGS is not valid JSON: {exc}") from exc
        if not isinstance(configs, list):
            raise CommandError("FOXPAY_PROVIDER_CONFIGS must be a JSON list.")

        imported = 0
        for config in configs:
            if not isinstance(config, dict):
                raise CommandError("Each FOXPAY_PROVIDER_CONFIGS entry must be a JSON object.")
            kind = config.get("kind")
            provider = config.get("provider")
            if kind not in {ProviderConfig.KIND_CARD, ProviderConfig.KIND_CRYPTO}:
                raise CommandError("Each provider config needs kind=card or kind=crypto.")
            if not provider:
                raise CommandError("Each provider config needs a provider value.")

            ProviderConfig.objects.update_or_create(
                merchant=merchant,
                kind=kind,
                provider=provider,
                defaults={
                    "adapter": config.get("adapter", ""),
                    "display_name": config.get("display_name", ""),
                    "priority": config.get("priority", 100),
                    "is_active": config.get("is_active", True),
                    "settings": config.get("settings", {}),
                },
            )
            imported += 1
        return imported
