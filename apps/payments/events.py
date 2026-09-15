import logging

from django.conf import settings
from django.db import transaction

from .models import MerchantWebhookEvent


logger = logging.getLogger(__name__)


def _enqueue(event_id):
    try:
        from .tasks import deliver_webhook_event

        deliver_webhook_event.delay(event_id)
    except Exception:
        logger.exception("Unable to enqueue FoxPay merchant webhook event %s", event_id)


def emit_event(merchant, event_type, payload, idempotency_key=""):
    if idempotency_key:
        event, created = MerchantWebhookEvent.objects.get_or_create(
            merchant=merchant,
            idempotency_key=idempotency_key,
            defaults={"event_type": event_type, "payload": payload},
        )
    else:
        event = MerchantWebhookEvent.objects.create(
            merchant=merchant,
            event_type=event_type,
            payload=payload,
        )
        created = True
    if created and settings.FOXPAY_ASYNC_TASKS_ENABLED:
        transaction.on_commit(lambda: _enqueue(event.pk))
    return event
