import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.payments.models import MerchantWebhookAttempt, MerchantWebhookEndpoint, MerchantWebhookEvent


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
        request = urllib.request.Request(
            endpoint.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "FoxPay-Signature": signature,
                "FoxPay-Event-ID": str(event.uuid),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read(4000).decode("utf-8", errors="replace")
                MerchantWebhookAttempt.objects.create(
                    event=event,
                    endpoint=endpoint,
                    status_code=response.status,
                    response_body=response_body,
                )
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            MerchantWebhookAttempt.objects.create(
                event=event,
                endpoint=endpoint,
                status_code=exc.code,
                response_body=exc.read(4000).decode("utf-8", errors="replace"),
                next_retry_at=timezone.now() + timedelta(minutes=5),
            )
        except Exception as exc:
            MerchantWebhookAttempt.objects.create(
                event=event,
                endpoint=endpoint,
                error=str(exc),
                next_retry_at=timezone.now() + timedelta(minutes=5),
            )
        return False
