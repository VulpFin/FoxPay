import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.payments.models import Merchant, ProviderConfig, ProviderCredential


class Command(BaseCommand):
    help = "Configure a NOWPayments hosted crypto provider and encrypted credentials."

    def add_arguments(self, parser):
        parser.add_argument("--merchant", required=True, help="Merchant slug.")
        parser.add_argument("--provider", default="nowpayments-primary")
        parser.add_argument("--display-name", default="NOWPayments")
        parser.add_argument("--environment", default="", help="Defaults to FOXPAY_ENV.")
        parser.add_argument("--priority", type=int, default=20)
        parser.add_argument("--pay-currency", default="", help="Optional NOWPayments asset code, such as btc.")
        parser.add_argument("--api-key-env", default="NOWPAYMENTS_API_KEY")
        parser.add_argument("--ipn-secret-env", default="NOWPAYMENTS_IPN_SECRET")
        parser.add_argument("--activate", action="store_true")
        parser.add_argument("--deactivate", action="store_true")

    def handle(self, *args, **options):
        if options["activate"] and options["deactivate"]:
            raise CommandError("Use either --activate or --deactivate.")
        merchant = Merchant.objects.filter(slug=options["merchant"]).first()
        if not merchant:
            raise CommandError(f"Merchant not found: {options['merchant']}")
        environment = options["environment"] or os.getenv("FOXPAY_ENV", "test")
        if environment not in {"test", "live"}:
            raise CommandError("Environment must be test or live.")
        api_key = os.getenv(options["api_key_env"], "").strip()
        ipn_secret = os.getenv(options["ipn_secret_env"], "").strip()
        existing = ProviderConfig.objects.filter(
            merchant=merchant,
            environment=environment,
            kind=ProviderConfig.KIND_CRYPTO,
            provider=options["provider"],
        ).first()
        active = True if options["activate"] else False if options["deactivate"] else existing.is_active if existing else False
        provider_settings = dict(existing.settings) if existing else {}
        if options["pay_currency"]:
            provider_settings["pay_currency"] = options["pay_currency"].lower()
        with transaction.atomic():
            config, _ = ProviderConfig.objects.update_or_create(
                merchant=merchant,
                environment=environment,
                kind=ProviderConfig.KIND_CRYPTO,
                provider=options["provider"],
                defaults={
                    "adapter": "nowpayments",
                    "display_name": options["display_name"],
                    "priority": options["priority"],
                    "is_active": active,
                    "capabilities": ["crypto", "stablecoin", "hosted_checkout", "webhooks"],
                    "settings": provider_settings,
                },
            )
            for name, value in (("api_key", api_key), ("ipn_secret", ipn_secret)):
                if value:
                    credential, _ = ProviderCredential.objects.get_or_create(provider_config=config, name=name)
                    credential.set_secret(value)
                    credential.revoked_at = None
                    credential.save(update_fields=["encrypted_value", "last_rotated_at", "revoked_at", "updated_at"])
            if config.is_active:
                missing = [name for name in ("api_key", "ipn_secret") if not config.credentials.filter(name=name, revoked_at__isnull=True).exists()]
                if missing:
                    raise CommandError(f"Active NOWPayments providers require: {', '.join(missing)}.")
        self.stdout.write(self.style.SUCCESS("NOWPayments provider configured."))
        self.stdout.write(f"Merchant: {merchant.slug}")
        self.stdout.write(f"Provider: {config.provider}")
        self.stdout.write(f"Environment: {config.environment}")
        self.stdout.write(f"Active: {config.is_active}")
        self.stdout.write(f"IPN URL: https://foxpay.fyi/api/v1/webhooks/nowpayments/{merchant.slug}/{config.provider}/")
