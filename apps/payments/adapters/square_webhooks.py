import base64
import hashlib
import hmac

from django.db import transaction
from django.utils import timezone

from apps.payments.models import PaymentAttempt, ProviderEvent, WebhookDelivery


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
            "merchant": config.merchant,
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


def square_payment_update(config, event):
    if event.get("type") not in {"payment.created", "payment.updated"}:
        return None, ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    objects = data.get("object") if isinstance(data.get("object"), dict) else {}
    payment = objects.get("payment") if isinstance(objects.get("payment"), dict) else {}
    order_id = payment.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        return None, ""
    attempt = PaymentAttempt.objects.select_related("intent").filter(
        provider_config=config,
        provider_response_metadata__square_order_id=order_id,
        intent__environment=config.environment,
    ).first()
    if not attempt:
        return None, ""
    if attempt.intent.merchant_id != config.merchant_id:
        return None, "payment_details_mismatch"
    amount = payment.get("total_money") if isinstance(payment.get("total_money"), dict) else {}
    if (
        payment.get("location_id") != attempt.provider_response_metadata.get("square_location_id")
        or amount.get("amount") != attempt.amount
        or amount.get("currency") != attempt.currency
        or (config.settings.get("square_merchant_id") and event.get("merchant_id") != config.settings["square_merchant_id"])
    ):
        return None, "payment_details_mismatch"
    status = {"COMPLETED": "succeeded", "FAILED": "failed", "CANCELED": "canceled"}.get(payment.get("status"))
    if not status:
        return None, ""
    payment_id = payment.get("id")
    if not isinstance(payment_id, str) or not payment_id:
        return None, "payment_id_missing"
    return {
        "id": event["event_id"],
        "type": event["type"],
        "payment_intent": attempt.intent.public_id,
        "foxpay_attempt_id": str(attempt.id),
        "foxpay_method": PaymentAttempt.METHOD_CARD,
        "status": status,
        "provider_reference": payment_id,
    }, ""
