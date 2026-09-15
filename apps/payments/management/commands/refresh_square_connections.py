from django.core.management.base import BaseCommand

from apps.payments.models import MerchantProviderConnection
from apps.payments.square_oauth import refresh_connection


class Command(BaseCommand):
    help = "Refresh seller Square OAuth tokens that are due or nearing expiration. Schedule at least daily."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        refreshed = 0
        failed = 0
        connections = MerchantProviderConnection.objects.filter(
            provider="square", authorization_method="square_oauth", revoked_at__isnull=True,
        ).exclude(status=MerchantProviderConnection.STATUS_REVOKED).order_by("token_refreshed_at", "pk")[: options["limit"]]
        for connection in connections:
            if refresh_connection(connection):
                refreshed += 1
            else:
                failed += 1
        self.stdout.write(f"Square connections checked: {refreshed + failed}; failed: {failed}")
