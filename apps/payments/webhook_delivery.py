import hashlib
import hmac
import json
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import MerchantWebhookAttempt, MerchantWebhookEndpoint, MerchantWebhookEvent
from .safe_urls import UnsafeURL, safe_webhook_post


def _applicable_endpoints(event):
    endpoints = MerchantWebhookEndpoint.objects.filter(merchant=event.merchant, is_active=True)
    applicable = []
    for endpoint in endpoints:
        if event.event_type == "webhook.test" and event.payload.get("endpoint") != str(endpoint.uuid):
            continue
        if endpoint.enabled_events and "*" not in endpoint.enabled_events and event.event_type not in endpoint.enabled_events:
            continue
        applicable.append(endpoint)
    return applicable


def _body(event):
    return json.dumps(
        {
            "id": str(event.uuid),
            "type": event.event_type,
            "created": event.created_at.isoformat(),
            "data": event.payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _next_retry(attempt_number):
    seconds = min(60 * (2 ** max(attempt_number - 1, 0)), settings.FOXPAY_WEBHOOK_MAX_BACKOFF_SECONDS)
    return timezone.now() + timedelta(seconds=seconds)


def deliver_to_endpoint(event, endpoint):
    body = _body(event)
    secret = endpoint.reveal_secret()
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    attempt_number = event.delivery_attempts.filter(endpoint=endpoint).count() + 1
    try:
        status = safe_webhook_post(
            endpoint.url,
            body,
            {
                "Content-Type": "application/json",
                "FoxPay-Signature": signature,
                "FoxPay-Event-ID": str(event.uuid),
            },
        )
        success = 200 <= status < 300
        MerchantWebhookAttempt.objects.create(
            event=event,
            endpoint=endpoint,
            status_code=status,
            next_retry_at=None if success else _next_retry(attempt_number),
        )
        return success
    except UnsafeURL:
        MerchantWebhookAttempt.objects.create(
            event=event,
            endpoint=endpoint,
            error="Webhook destination is unavailable or unsafe.",
            next_retry_at=_next_retry(attempt_number),
        )
        return False


def deliver_event(event_id):
    lock_key = f"foxpay:webhook-delivery:{event_id}"
    try:
        acquired = cache.add(lock_key, "1", timeout=120)
    except Exception:
        acquired = settings.FOXPAY_ENV != "live"
    if not acquired:
        return {"delivered": 0, "failed": 0, "deferred": 1}
    try:
        event = MerchantWebhookEvent.objects.select_related("merchant").filter(pk=event_id).first()
        if not event or event.status == MerchantWebhookEvent.STATUS_DELIVERED:
            return {"delivered": 0, "failed": 0, "deferred": 0}
        endpoints = _applicable_endpoints(event)
        delivered = 0
        failed = 0
        deferred = 0
        terminal_failure = False
        for endpoint in endpoints:
            attempts = event.delivery_attempts.filter(endpoint=endpoint).order_by("-created_at")
            latest = attempts.first()
            if latest and latest.status_code and 200 <= latest.status_code < 300:
                continue
            attempt_count = attempts.count()
            if attempt_count >= settings.FOXPAY_WEBHOOK_MAX_ATTEMPTS:
                terminal_failure = True
                continue
            if latest and latest.next_retry_at and latest.next_retry_at > timezone.now():
                deferred += 1
                continue
            if deliver_to_endpoint(event, endpoint):
                delivered += 1
            else:
                failed += 1
                if attempt_count + 1 >= settings.FOXPAY_WEBHOOK_MAX_ATTEMPTS:
                    terminal_failure = True
                else:
                    deferred += 1
        if not endpoints or all(
            endpoint.delivery_attempts.filter(
                event=event,
                status_code__gte=200,
                status_code__lt=300,
            ).exists()
            for endpoint in endpoints
        ):
            event.status = MerchantWebhookEvent.STATUS_DELIVERED
        elif terminal_failure and not deferred:
            event.status = MerchantWebhookEvent.STATUS_FAILED
        else:
            event.status = MerchantWebhookEvent.STATUS_PENDING
        event.save(update_fields=["status", "updated_at"])
        return {"delivered": delivered, "failed": failed, "deferred": deferred}
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def deliver_pending_events(limit=50):
    totals = {"delivered": 0, "failed": 0, "deferred": 0}
    event_ids = list(
        MerchantWebhookEvent.objects.filter(status=MerchantWebhookEvent.STATUS_PENDING)
        .order_by("updated_at", "created_at")
        .values_list("pk", flat=True)[:limit]
    )
    for event_id in event_ids:
        result = deliver_event(event_id)
        for key in totals:
            totals[key] += result[key]
    return totals
