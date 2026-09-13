import base64
import hashlib
import hmac

from django.db import transaction
from django.utils import timezone

from apps.payments.models import ProviderEvent, WebhookDelivery


def verify_square_signature(body, signature, signature_key, notification_url):
    if not body or not isinstance(signature, str) or not signature or not signature_key or not notification_url:
        return False
    digest = hmac.new(
        signature_key.encode("utf-8"),
        notification_url.encode("utf-8") + body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest)
    try:
        supplied = signature.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected, supplied)


def _text(value, limit=160):
    return value[:limit] if isinstance(value, str) else ""


def square_event_summary(config, event):
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    resource_type = _text(data.get("type"), 40)
    objects = data.get("object") if isinstance(data.get("object"), dict) else {}
    resource = objects.get(resource_type) if isinstance(objects.get(resource_type), dict) else {}
    return {
        "foxpay_provider": config.provider,
        "webhook_route_merchant": config.merchant.slug,
        "square_merchant_id": _text(event.get("merchant_id")),
        "square_location_id": _text(event.get("location_id")),
        "resource_type": resource_type,
        "resource_id": _text(data.get("id")),
        "status": _text(resource.get("status") or resource.get("state"), 80),
        "order_id": _text(resource.get("order_id")),
    }


@transaction.atomic
def record_square_event(config, event):
    event_id = event["event_id"]
    event_type = event["type"]
    provider = f"square:{config.pk}"
    summary = square_event_summary(config, event)
    _, created = ProviderEvent.objects.get_or_create(
        provider=provider,
        provider_event_id=event_id,
        defaults={
            "event_type": event_type,
            "normalized_event_type": "square.webhook.received",
            "payload": summary,
            "processed_at": timezone.now(),
        },
    )
    if created:
        WebhookDelivery.objects.create(
            provider=provider,
            event_type=event_type,
            provider_event_id=event_id,
            payload=summary,
            processed=True,
        )
    return created
