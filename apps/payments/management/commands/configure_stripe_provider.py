import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.payments.models import Merchant, ProviderConfig, ProviderCredential


class Command(BaseCommand):
    help = "Create or update a Stripe Checkout provider config and encrypted credentials."

    def add_arguments(self, parser):
        parser.add_argument("--merchant", required=True, help="Merchant slug.")
        parser.add_argument("--provider", default="stripe-primary", help="Merchant-facing provider code.")
        parser.add_argument("--display-name", default="Stripe Checkout", help="Provider display name.")
        parser.add_argument("--environment", default="", help="FoxPay environment. Defaults to FOXPAY_ENV.")
        parser.add_argument("--priority", type=int, default=10)
        parser.add_argument("--activate", action="store_true", help="Mark this provider active.")
        parser.add_argument("--deactivate", action="store_true", help="Mark this provider inactive.")
        parser.add_argument("--secret-key", default="", help="Stripe secret key. Prefer --secret-key-env for shell history safety.")
        parser.add_argument("--secret-key-env", default="STRIPE_SECRET_KEY", help="Environment variable containing the Stripe secret key.")
        parser.add_argument("--webhook-secret", default="", help="Stripe webhook endpoint secret. Prefer --webhook-secret-env.")
        parser.add_argument("--webhook-secret-env", default="STRIPE_WEBHOOK_SECRET", help="Environment variable containing the Stripe webhook secret.")
        parser.add_argument("--automatic-tax", action="store_true", help="Enable Stripe automatic_tax for Checkout Sessions.")

    def handle(self, *args, **options):
        if options["activate"] and options["deactivate"]:
            raise CommandError("Use either --activate or --deactivate, not both.")

        merchant = Merchant.objects.filter(slug=options["merchant"]).first()
        if not merchant:
            raise CommandError(f"Merchant not found: {options['merchant']}")

        environment = options["environment"] or os.getenv("FOXPAY_ENV", "test")
        secret_key = options["secret_key"] or os.getenv(options["secret_key_env"], "")
        webhook_secret = options["webhook_secret"] or os.getenv(options["webhook_secret_env"], "")
        if secret_key and not secret_key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
            raise CommandError("Stripe secret key must start with sk_test_, sk_live_, rk_test_, or rk_live_.")
        if webhook_secret and not webhook_secret.startswith("whsec_"):
            raise CommandError("Stripe webhook secret must start with whsec_.")
        if environment == "test" and secret_key.startswith(("sk_live_", "rk_live_")):
            raise CommandError("Refusing to activate a live Stripe key in the test FoxPay environment.")
        if environment == "live" and secret_key.startswith(("sk_test_", "rk_test_")):
            raise CommandError("Refusing to activate a test Stripe key in the live FoxPay environment.")

        settings_json = {"payment_method_types": ["card"]}
        if options["automatic_tax"]:
            settings_json["automatic_tax"] = True

        is_active = True if options["activate"] else False if options["deactivate"] else False
        stored = []
        with transaction.atomic():
            config, _ = ProviderConfig.objects.update_or_create(
                merchant=merchant,
                environment=environment,
                kind=ProviderConfig.KIND_CARD,
                provider=options["provider"],
                defaults={
                    "adapter": "stripe",
                    "display_name": options["display_name"],
                    "priority": options["priority"],
                    "is_active": is_active,
                    "capabilities": [
                        "card",
                        "debit",
                        "wallet",
                        "hosted_checkout",
                        "webhooks",
                        "idempotency",
                        "multicurrency",
                    ],
                    "settings": settings_json,
                },
            )
            if secret_key:
                self.store_secret(config, "secret_key", secret_key)
                stored.append("secret_key")
            if webhook_secret:
                self.store_secret(config, "webhook_secret", webhook_secret)
                stored.append("webhook_secret")
            if config.is_active and not config.credentials.filter(name="secret_key", revoked_at__isnull=True).exists():
                raise CommandError("Active Stripe providers require a secret_key credential.")

        self.stdout.write(self.style.SUCCESS("Stripe provider configured."))
        self.stdout.write(f"Merchant: {merchant.slug}")
        self.stdout.write(f"Provider: {config.provider}")
        self.stdout.write(f"Environment: {config.environment}")
        self.stdout.write(f"Active: {config.is_active}")
        self.stdout.write(f"Stored encrypted credentials: {', '.join(stored) if stored else 'none'}")

    def store_secret(self, config, name, value):
        credential, _ = ProviderCredential.objects.get_or_create(provider_config=config, name=name)
        credential.set_secret(value)
        credential.revoked_at = None
        credential.save(update_fields=["encrypted_value", "last_rotated_at", "revoked_at", "updated_at"])
