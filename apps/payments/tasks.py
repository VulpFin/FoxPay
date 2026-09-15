import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .abuse import clear_expired_restrictions
from .models import MerchantProviderConnection, PaymentAttempt, ProviderEvent, Refund
from .permissions import routing_block_reason
from .webhook_delivery import deliver_event, deliver_pending_events


logger = logging.getLogger(__name__)


@shared_task(name="apps.payments.tasks.deliver_webhook_event")
def deliver_webhook_event(event_id):
    return deliver_event(event_id)


@shared_task(name="apps.payments.tasks.deliver_pending_webhooks")
def deliver_pending_webhooks(limit=100):
    return deliver_pending_events(limit)


@shared_task(name="apps.payments.tasks.reconcile_pending_refunds")
def reconcile_pending_refunds(limit=100):
    from .services import reconcile_pending_refund

    cutoff = timezone.now() - timedelta(seconds=settings.FOXPAY_RECONCILE_AFTER_SECONDS)
    ids = list(
        Refund.objects.filter(status=Refund.STATUS_PENDING, updated_at__lte=cutoff)
        .order_by("updated_at")
        .values_list("pk", flat=True)[:limit]
    )
    reconciled = 0
    failed = 0
    for refund_id in ids:
        try:
            refund = reconcile_pending_refund(refund_id)
            if refund and refund.status != Refund.STATUS_PENDING:
                reconciled += 1
        except Exception:
            logger.exception("Pending refund reconciliation failed for refund %s", refund_id)
            failed += 1
    return {"checked": len(ids), "reconciled": reconciled, "failed": failed}


@shared_task(name="apps.payments.tasks.reconcile_pending_payments")
def reconcile_pending_payments(limit=100):
    from .payment_reconciliation import reconcile_payment_attempt

    cutoff = timezone.now() - timedelta(seconds=settings.FOXPAY_RECONCILE_AFTER_SECONDS)
    ids = list(
        PaymentAttempt.objects.filter(
            status__in=[PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_ACTION_REQUIRED],
            updated_at__lte=cutoff,
        )
        .exclude(provider_config=None)
        .order_by("updated_at")
        .values_list("pk", flat=True)[:limit]
    )
    reconciled = 0
    failed = 0
    for attempt_id in ids:
        try:
            reconciled += int(bool(reconcile_payment_attempt(attempt_id)))
        except Exception:
            logger.exception("Pending payment reconciliation failed for attempt %s", attempt_id)
            failed += 1
    return {"checked": len(ids), "reconciled": reconciled, "failed": failed}


@shared_task(name="apps.payments.tasks.process_paypal_capture_event")
def process_paypal_capture_event(provider_event_id):
    from .paypal_events import process_paypal_capture_inbox

    return process_paypal_capture_inbox(provider_event_id)


@shared_task(name="apps.payments.tasks.process_pending_provider_events")
def process_pending_provider_events(limit=100):
    from .paypal_events import PAYPAL_CAPTURE_INBOX_PREFIX, process_paypal_capture_inbox

    ids = list(
        ProviderEvent.objects.filter(
            provider__startswith=PAYPAL_CAPTURE_INBOX_PREFIX,
            processed_at__isnull=True,
        )
        .order_by("updated_at", "created_at")
        .values_list("pk", flat=True)[:limit]
    )
    processed = 0
    failed = 0
    for provider_event_id in ids:
        try:
            result = process_paypal_capture_inbox(provider_event_id)
            processed += int(bool(result.get("processed")))
        except Exception:
            logger.exception("PayPal capture processing failed for event %s", provider_event_id)
            failed += 1
            ProviderEvent.objects.filter(pk=provider_event_id).update(updated_at=timezone.now())
    return {"checked": len(ids), "processed": processed, "failed": failed}


@shared_task(name="apps.payments.tasks.refresh_square_tokens")
def refresh_square_tokens(limit=100):
    from .square_oauth import refresh_connection

    connections = MerchantProviderConnection.objects.filter(
        provider="square",
        authorization_method="square_oauth",
        status__in=[
            MerchantProviderConnection.STATUS_ACTIVE,
            MerchantProviderConnection.STATUS_RESTRICTED,
            MerchantProviderConnection.STATUS_ERROR,
        ],
        revoked_at__isnull=True,
    ).select_related("merchant")[:limit]
    refreshed = 0
    skipped = 0
    for connection in connections:
        if routing_block_reason(connection.merchant, connection.environment):
            skipped += 1
            continue
        refreshed += int(bool(refresh_connection(connection)))
    return {"checked": len(connections), "refreshed": refreshed, "skipped": skipped}


def _check_one_connection(connection):
    if routing_block_reason(connection.merchant, connection.environment):
        return False
    if connection.provider == "square" and connection.authorization_method == "square_oauth":
        from .square_oauth import refresh_connection

        return refresh_connection(connection)
    if connection.provider == "nowpayments" and connection.authorization_method == "nowpayments_credentials":
        from .nowpayments_connection import check_connection_health

        return check_connection_health(connection)
    if connection.provider == "paypal" and connection.authorization_method == "paypal_partner":
        from .paypal_partner import refresh_partner_connection

        refresh_partner_connection(connection)
        return True
    if connection.provider == "stripe" and connection.authorization_method == "stripe_connect":
        from .stripe_connect import verify_account

        state = verify_account(environment=connection.environment, account_id=connection.external_account_id)
        connection.status = connection.STATUS_ACTIVE if state["ready"] else connection.STATUS_RESTRICTED
        connection.capabilities = state["capabilities"]
        connection.last_verified_at = timezone.now()
        connection.last_error_at = None
        connection.last_error_code = "" if state["ready"] else "charges_not_enabled"
        connection.metadata = {**connection.metadata, "country": state["country"]}
        connection.save()
        connection.provider_configs.update(is_active=state["ready"])
        return state["ready"]
    return False


@shared_task(name="apps.payments.tasks.check_provider_health")
def check_provider_health(limit=100):
    connections = list(
        MerchantProviderConnection.objects.filter(
            status__in=[
                MerchantProviderConnection.STATUS_ACTIVE,
                MerchantProviderConnection.STATUS_RESTRICTED,
                MerchantProviderConnection.STATUS_ERROR,
            ],
            revoked_at__isnull=True,
        )
        .select_related("merchant")
        .order_by("last_verified_at", "created_at")[:limit]
    )
    healthy = 0
    failed = 0
    for connection in connections:
        try:
            healthy += int(bool(_check_one_connection(connection)))
        except Exception as exc:
            connection.last_error_at = timezone.now()
            connection.last_error_code = f"health_check_{exc.__class__.__name__}"[:80]
            if connection.status != connection.STATUS_REVOKED:
                connection.status = connection.STATUS_ERROR
            connection.save(update_fields=["last_error_at", "last_error_code", "status", "updated_at"])
            connection.provider_configs.update(is_active=False)
            failed += 1
    return {"checked": len(connections), "healthy": healthy, "failed": failed}


@shared_task(name="apps.payments.tasks.clear_temporary_restrictions")
def clear_temporary_restrictions():
    return {"cleared": clear_expired_restrictions()}
