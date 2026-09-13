import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse

from apps.payments.models import Merchant, ProviderConfig, ProviderCredential


class Command(BaseCommand):
    help = "Create or update a PayPal Checkout provider config and its encrypted credentials."

    def add_arguments(self, parser):
        parser.add_argument("--merchant", required=True, help="Merchant slug.")
        parser.add_argument("--provider", default="paypal-primary", help="Merchant-facing provider code.")
        parser.add_argument("--display-name", default="PayPal", help="Provider display name.")
        parser.add_argument("--environment", default="", help="Fox Pay environment. Defaults to FOXPAY_ENV.")
        parser.add_argument("--paypal-env", choices=["sandbox", "live"], default="", help="PayPal API environment. Defaults to match the Fox Pay environment.")
        parser.add_argument("--priority", type=int, default=20)
        parser.add_argument("--activate", action="store_true", help="Mark this provider active.")
        parser.add_argument("--deactivate", action="store_true", help="Mark this provider inactive.")
        parser.add_argument("--client-id", default="", help="PayPal REST client id. Prefer --client-id-env for shell history safety.")
        parser.add_argument("--client-id-env", default="PAYPAL_CLIENT_ID")
        parser.add_argument("--client-secret", default="", help="PayPal REST secret. Prefer --client-secret-env.")
        parser.add_argument("--client-secret-env", default="PAYPAL_CLIENT_SECRET")
        parser.add_argument("--webhook-id", default="", help="Webhook id from the PayPal dashboard. Prefer --webhook-id-env.")
        parser.add_argument("--webhook-id-env", default="PAYPAL_WEBHOOK_ID")

    def handle(self, *args, **options):
        if options["activate"] and options["deactivate"]:
            raise CommandError("Use either --activate or --deactivate, not both.")

        merchant = Merchant.objects.filter(slug=options["merchant"]).first()
        if not merchant:
            raise CommandError(f"Merchant not found: {options['merchant']}")

        provider = options["provider"]
        if not provider or len(provider) > 40 or "/" in provider:
            raise CommandError("Provider must be a nonempty path segment of at most 40 characters.")

        environment = options["environment"] or os.getenv("FOXPAY_ENV", "test")
        if environment not in {"test", "live"}:
            raise CommandError("Environment must be test or live.")
        paypal_env = options["paypal_env"] or ("live" if environment == "live" else "sandbox")
        if environment == "test" and paypal_env == "live":
            raise CommandError("Refusing to point the test Fox Pay environment at live PayPal.")

        client_id = options["client_id"] or os.getenv(options["client_id_env"], "")
        client_secret = options["client_secret"] or os.getenv(options["client_secret_env"], "")
        webhook_id = options["webhook_id"] or os.getenv(options["webhook_id_env"], "")

        existing = ProviderConfig.objects.filter(
            merchant=merchant,
            environment=environment,
            kind=ProviderConfig.KIND_CARD,
            provider=provider,
        ).first()
        if existing and existing.adapter_name != "paypal":
            raise CommandError("A non-PayPal provider already uses this code.")

        if options["activate"]:
            is_active = True
        elif options["deactivate"]:
            is_active = False
        else:
            is_active = bool(existing and existing.is_active)

        stored = []
        with transaction.atomic():
            config, _ = ProviderConfig.objects.update_or_create(
                merchant=merchant,
                environment=environment,
                kind=ProviderConfig.KIND_CARD,
                provider=provider,
                defaults={
                    "adapter": "paypal",
                    "display_name": options["display_name"],
                    "priority": options["priority"],
                    "is_active": is_active,
                    "capabilities": ["card", "wallet", "hosted_checkout", "webhooks", "multicurrency"],
                    "settings": {**(existing.settings if existing else {}), "paypal_env": paypal_env},
                },
            )
            for name, value in (("client_id", client_id), ("client_secret", client_secret), ("webhook_id", webhook_id)):
                if value:
                    self.store_secret(config, name, value)
                    stored.append(name)
            if config.is_active and not all(
                config.credentials.filter(name=name, revoked_at__isnull=True).exists()
                for name in ("client_id", "client_secret")
            ):
                raise CommandError("Active PayPal providers need both client_id and client_secret credentials.")

        self.stdout.write(self.style.SUCCESS("PayPal provider configured."))
        self.stdout.write(f"Merchant: {merchant.slug}")
        self.stdout.write(f"Provider: {config.provider}")
        self.stdout.write(f"Fox Pay environment: {config.environment}")
        self.stdout.write(f"PayPal environment: {paypal_env}")
        self.stdout.write(f"Active: {config.is_active}")
        self.stdout.write(f"Stored encrypted credentials: {', '.join(stored) if stored else 'none'}")
        self.stdout.write(f"Webhook path to register in PayPal: {reverse('payments:paypal_webhook', args=[merchant.slug, config.provider])}")
        if not config.credentials.filter(name="webhook_id", revoked_at__isnull=True).exists():
            self.stdout.write(self.style.WARNING("No webhook_id yet: webhooks will be refused until one is stored."))

    def store_secret(self, config, name, value):
        credential, _ = ProviderCredential.objects.get_or_create(provider_config=config, name=name)
        credential.set_secret(value)
        credential.revoked_at = None
        credential.save(update_fields=["encrypted_value", "last_rotated_at", "revoked_at", "updated_at"])
