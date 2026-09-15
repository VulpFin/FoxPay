from django.db import transaction

from .adapters.paypal_checkout import PayPalCheckoutAdapter, capture_amount
from .models import AuditLog, PaymentAttempt
from .paypal_partner import seller_id_from_event
from .services import record_webhook


def _matching_attempt(config, normalized):
    public_id = normalized.get("payment_intent", "")
    if not public_id:
        return None
    attempts = PaymentAttempt.objects.select_related("intent").filter(
        provider_config=config,
        intent__merchant=config.merchant,
        intent__public_id=public_id,
        method=PaymentAttempt.METHOD_CARD,
    )
    attempt_id = normalized.get("foxpay_attempt_id")
    if attempt_id:
        attempts = attempts.filter(pk=attempt_id)
    return attempts.first()


def _seller_matches(config, event):
    connection = config.connection
    if not connection or connection.authorization_method != "paypal_partner":
        return True
    return seller_id_from_event(event) == connection.external_account_id


def _audit_mismatch(config, event, reason):
    AuditLog.objects.create(
        merchant=config.merchant,
        action="paypal.webhook.reconciliation_skipped",
        object_type="provider_event",
        object_id=str(event.get("id", ""))[:120],
        metadata={"reason": reason},
    )


def process_paypal_event(config, event):
    adapter = PayPalCheckoutAdapter(config)
    normalized = adapter.normalize_webhook(event)
    status = normalized.get("status")
    attempt = _matching_attempt(config, normalized) if status else None
    if status and (not attempt or not _seller_matches(config, event)):
        _audit_mismatch(config, event, "payment_details_mismatch")
        return {"processed": False, "captured": False, "mismatch": True}
    if status == "approved" and normalized.get("provider_reference"):
        if attempt.provider_reference and attempt.provider_reference != normalized["provider_reference"]:
            _audit_mismatch(config, event, "order_id_mismatch")
            return {"processed": False, "captured": False, "mismatch": True}
        order = adapter.capture(normalized["provider_reference"])
        settlement = adapter.settlement_event(order, event_id=f"{event['id']}:capture")
        if not settlement:
            return {"processed": False, "captured": False, "mismatch": False}
        event = settlement
        normalized = adapter.normalize_webhook(settlement)
        attempt = _matching_attempt(config, normalized)
        if not attempt:
            _audit_mismatch(config, event, "capture_details_mismatch")
            return {"processed": False, "captured": False, "mismatch": True}
        captured = True
    else:
        captured = False

    with transaction.atomic():
        if normalized.get("status") == "succeeded":
            minor, currency = capture_amount(event.get("resource") or {})
            if minor is None or minor != attempt.amount or currency != (attempt.currency or "").upper():
                AuditLog.objects.create(
                    merchant=config.merchant,
                    action="paypal.webhook.amount_mismatch",
                    object_type="payment_intent",
                    object_id=attempt.intent.public_id,
                    metadata={
                        "expected": attempt.amount,
                        "expected_currency": attempt.currency,
                        "seen": minor,
                        "seen_currency": currency,
                    },
                )
                normalized["status"] = "amount_mismatch"
        delivery = record_webhook(f"paypal:{config.pk}", normalized)
    return {"processed": delivery.processed, "captured": captured, "mismatch": False}
