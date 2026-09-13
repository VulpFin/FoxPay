from .models import MerchantWebhookEvent


def emit_event(merchant, event_type, payload, idempotency_key=""):
    event, _ = MerchantWebhookEvent.objects.get_or_create(
        merchant=merchant,
        idempotency_key=idempotency_key,
        defaults={"event_type": event_type, "payload": payload},
    )
    return event
