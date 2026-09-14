import hashlib
import hmac
import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.payments.models import MerchantWebhookAttempt, MerchantWebhookEndpoint, MerchantWebhookEvent
from apps.payments.safe_urls import UnsafeURL, safe_webhook_post


class Command(BaseCommand):
    help = "Deliver pending Fox Pay merchant webhook events."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        delivered = 0
        failed = 0
        events = MerchantWebhookEvent.objects.filter(status=MerchantWebhookEvent.STATUS_PENDING).order_by("created_at")[: options["limit"]]
        for event in events:
            endpoints = MerchantWebhookEndpoint.objects.filter(merchant=event.merchant, is_active=True)
            event_failed = False
            for endpoint in endpoints:
                if event.event_type == "webhook.test" and event.payload.get("endpoint") != str(endpoint.uuid):
                    continue
                if endpoint.enabled_events and "*" not in endpoint.enabled_events and event.event_type not in endpoint.enabled_events:
                    continue
                ok = self.deliver(event, endpoint)
                if ok:
                    delivered += 1
                else:
                    event_failed = True
                    failed += 1
            event.status = MerchantWebhookEvent.STATUS_FAILED if event_failed else MerchantWebhookEvent.STATUS_DELIVERED
            event.save(update_fields=["status", "updated_at"])

        self.stdout.write(f"Delivered attempts: {delivered}")
        self.stdout.write(f"Failed attempts: {failed}")

    def deliver(self, event, endpoint):
        body = json.dumps(
            {
                "id": str(event.uuid),
                "type": event.event_type,
                "created": event.created_at.isoformat(),
                "data": event.payload,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        secret = endpoint.reveal_secret()
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        try:
            status = safe_webhook_post(
                endpoint.url,
                body,
                {"Content-Type": "application/json", "FoxPay-Signature": signature, "FoxPay-Event-ID": str(event.uuid)},
            )
            MerchantWebhookAttempt.objects.create(
                event=event,
                endpoint=endpoint,
                status_code=status,
                next_retry_at=timezone.now() + timedelta(minutes=5) if not 200 <= status < 300 else None,
            )
            return 200 <= status < 300
        except UnsafeURL:
            MerchantWebhookAttempt.objects.create(
                event=event,
                endpoint=endpoint,
                error="Webhook destination is unavailable or unsafe.",
                next_retry_at=timezone.now() + timedelta(minutes=5),
            )
        return False
