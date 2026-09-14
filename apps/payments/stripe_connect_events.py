from django.db import transaction
from django.utils import timezone

from .events import emit_event
from .ledger import record_payment_success
from .models import AuditLog, MerchantProviderConnection, PaymentAttempt, PaymentIntent, ProviderEvent
from .services import serialize_intent


CONNECT_CHECKOUT_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}


@transaction.atomic
def handle_connect_event(connection, event):
    connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
    event_id = event.get("id", "")
    event_type = event.get("type", "")
    if (
        not isinstance(event_id, str) or not 0 < len(event_id) <= 160
        or not isinstance(event_type, str) or not 0 < len(event_type) <= 120
        or event.get("account") != connection.external_account_id
        or event.get("livemode") is not (connection.environment == "live")
    ):
        raise ValueError("Invalid Connect event identity.")
    namespace = f"stripe-connect:{connection.pk}"
    if ProviderEvent.objects.filter(provider=namespace, provider_event_id=event_id).exists():
        return False
    data = event.get("data", {}).get("object", {})
    if not isinstance(data, dict):
        raise ValueError("Invalid Connect event object.")
    now = timezone.now()
    intent = None
    normalized = "ignored"
    safe_payload = {"account": connection.external_account_id, "livemode": event["livemode"]}

    if event_type == "account.application.deauthorized":
        connection.status = connection.STATUS_REVOKED
        connection.revoked_at = now
        connection.clear_tokens()
        connection.save()
        connection.provider_configs.update(is_active=False)
        AuditLog.objects.create(
            merchant=connection.merchant, action="stripe.deauthorized", object_type="provider_connection",
            object_id=str(connection.uuid), metadata={"event_id": event_id},
        )
        normalized = "provider_connection.revoked"
    elif event_type == "account.updated":
        if data.get("id") != connection.external_account_id:
            raise ValueError("Account update did not match connection.")
        if connection.status != connection.STATUS_REVOKED:
            capabilities = data.get("capabilities") or {}
            ready = bool(data.get("charges_enabled") and capabilities.get("card_payments") == "active")
            connection.status = connection.STATUS_ACTIVE if ready else connection.STATUS_RESTRICTED
            connection.capabilities = ["card_payments"] if ready else []
            connection.last_verified_at = now
            connection.metadata = {"country": data.get("country", ""), "charges_enabled": ready}
            connection.save()
            connection.provider_configs.update(is_active=ready)
            normalized = "provider_connection.active" if ready else "provider_connection.restricted"
    elif event_type in CONNECT_CHECKOUT_EVENTS:
        if data.get("mode") != "payment" or data.get("livemode") is not event["livemode"]:
            raise ValueError("Checkout mode or environment mismatch.")
        metadata = data.get("metadata") or {}
        attempt_id = metadata.get("foxpay_attempt_id", "")
        if not str(attempt_id).isdigit():
            raise ValueError("Checkout attempt is missing.")
        attempt = PaymentAttempt.objects.select_for_update().select_related("provider_config", "intent").filter(pk=int(attempt_id)).first()
        if not attempt or not attempt.provider_config:
            raise ValueError("Checkout attempt is missing.")
        intent = PaymentIntent.objects.select_for_update().get(pk=attempt.intent_id)
        if (
            attempt.provider_config.connection_id != connection.pk
            or attempt.provider_config.merchant_id != connection.merchant_id
            or intent.merchant_id != connection.merchant_id
            or intent.environment != connection.environment
            or attempt.method != PaymentAttempt.METHOD_CARD
            or attempt.amount != intent.amount
            or attempt.currency.upper() != intent.currency.upper()
            or metadata.get("foxpay_payment_intent") != intent.public_id
            or metadata.get("foxpay_merchant_id") != str(intent.merchant.uuid)
            or data.get("client_reference_id") != intent.public_id
            or data.get("id") != attempt.provider_reference
            or data.get("amount_total") != intent.amount
            or str(data.get("currency", "")).upper() != intent.currency.upper()
        ):
            raise ValueError("Checkout details do not match intent and connection.")
        safe_payload.update({"payment_intent": intent.public_id, "attempt_id": attempt.pk, "checkout_session": attempt.provider_reference})
        if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"} and data.get("payment_status") == "paid":
            attempt.status = PaymentAttempt.STATUS_SUCCEEDED
            attempt.provider_status = "paid"
            attempt.save(update_fields=["status", "provider_status", "updated_at"])
            if intent.status not in {PaymentIntent.STATUS_REFUNDED, PaymentIntent.STATUS_PARTIALLY_REFUNDED}:
                intent.status = PaymentIntent.STATUS_SUCCEEDED
                intent.save(update_fields=["status", "updated_at"])
            record_payment_success(intent)
            emit_event(connection.merchant, "payment_intent.succeeded", serialize_intent(intent), idempotency_key=f"payment_intent.succeeded:{intent.public_id}")
            normalized = "payment_intent.succeeded"
        elif event_type in {"checkout.session.async_payment_failed", "checkout.session.expired"}:
            if attempt.status != PaymentAttempt.STATUS_SUCCEEDED:
                attempt.status = PaymentAttempt.STATUS_FAILED
                attempt.provider_status = "expired" if event_type.endswith("expired") else "failed"
                attempt.save(update_fields=["status", "provider_status", "updated_at"])
            remaining = intent.attempts.filter(status__in=[PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_ACTION_REQUIRED]).exists()
            if not remaining and intent.status not in {PaymentIntent.STATUS_SUCCEEDED, PaymentIntent.STATUS_PARTIALLY_REFUNDED, PaymentIntent.STATUS_REFUNDED}:
                intent.status = PaymentIntent.STATUS_EXPIRED if event_type.endswith("expired") else PaymentIntent.STATUS_FAILED
                intent.save(update_fields=["status", "updated_at"])
            normalized = "payment_attempt.failed"
        else:
            if intent.status == PaymentIntent.STATUS_REQUIRES_PAYMENT:
                intent.status = PaymentIntent.STATUS_PROCESSING
                intent.save(update_fields=["status", "updated_at"])
            normalized = "payment_intent.processing"

    ProviderEvent.objects.create(
        merchant=connection.merchant, provider=namespace, provider_event_id=event_id,
        event_type=event_type, normalized_event_type=normalized, payment_intent=intent,
        payload=safe_payload, processed_at=now,
    )
    return True
