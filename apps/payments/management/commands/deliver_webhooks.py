from django.core.management.base import BaseCommand

from apps.payments.webhook_delivery import deliver_pending_events


class Command(BaseCommand):
    help = "Deliver pending Fox Pay merchant webhook events."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        result = deliver_pending_events(options["limit"])
        self.stdout.write(f"Delivered attempts: {result['delivered']}")
        self.stdout.write(f"Failed attempts: {result['failed']}")
        self.stdout.write(f"Deferred attempts: {result['deferred']}")
