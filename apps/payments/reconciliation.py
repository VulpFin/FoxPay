from django.db import transaction

from .events import emit_event
from .models import AuditLog, Dispute, PaymentAttempt, Refund
from .services import reconcile_refund, serialize_refund
from .adapters.base import ProviderOperationResult


def attempt_provider_references(attempt):
    metadata = attempt.provider_response_metadata or {}
    return {
        value
        for value in {
            attempt.provider_reference,
            metadata.get("stripe_payment_intent_id", ""),
            metadata.get("stripe_charge_id", ""),
            metadata.get("paypal_order_id", ""),
            metadata.get("paypal_capture_id", ""),
            metadata.get("square_order_id", ""),
        }
        if isinstance(value, str) and value
    }


def _audit_mismatch(config, event_id, action, reason):
    AuditLog.objects.create(
        merchant=config.merchant,
        action=action,
        object_type="provider_event",
        object_id=str(event_id)[:120],
        metadata={"reason": reason},
    )


def reconcile_provider_refund(
    config,
    *,
    event_id,
    provider_refund_id,
    provider_payment_id,
    amount,
    currency,
    provider_status,
    normalized_status,
    foxpay_refund_id="",
    failure_code="",
):
    with transaction.atomic():
        refunds = (
            Refund.objects.select_for_update()
            .select_related("payment_attempt", "payment_intent")
            .filter(merchant=config.merchant, provider_config=config)
        )
        refund = None
        if foxpay_refund_id:
            refund = refunds.filter(public_id=foxpay_refund_id).first()
        if refund is None and provider_refund_id:
            refund = refunds.filter(provider_refund_id=provider_refund_id).first()
        if refund is None:
            return None, "refund_not_found"
        if (
            refund.payment_attempt_id is None
            or refund.payment_intent.merchant_id != config.merchant_id
            or refund.payment_attempt.provider_config_id != config.pk
            or provider_payment_id not in attempt_provider_references(refund.payment_attempt)
            or amount != refund.amount
            or str(currency).upper() != refund.currency.upper()
            or (
                refund.provider_refund_id
                and provider_refund_id
                and refund.provider_refund_id != provider_refund_id
            )
        ):
            _audit_mismatch(config, event_id, "refund.webhook_reconciliation_skipped", "refund_details_mismatch")
            return None, "refund_details_mismatch"
        result = ProviderOperationResult(
            status=normalized_status,
            provider_reference=provider_refund_id,
            provider_status=str(provider_status)[:80],
            failure_code=str(failure_code)[:80],
            failure_category="provider_declined" if normalized_status == Refund.STATUS_FAILED else "",
        )
        return reconcile_refund(refund, result), ""


def serialize_dispute(dispute):
    return {
        "id": str(dispute.uuid),
        "object": "dispute",
        "payment_intent": dispute.payment_intent.public_id,
        "provider": dispute.provider,
        "provider_dispute_id": dispute.provider_dispute_id,
        "reason": dispute.reason,
        "amount": dispute.amount,
        "currency": dispute.currency,
        "status": dispute.status,
        "evidence_due_at": dispute.evidence_due_at.isoformat() if dispute.evidence_due_at else None,
        "created_at": dispute.created_at.isoformat(),
    }


def reconcile_provider_dispute(
    config,
    *,
    event_id,
    provider_dispute_id,
    provider_payment_id,
    amount,
    currency,
    reason,
    status,
    evidence_due_at=None,
    safe_metadata=None,
):
    attempts = (
        PaymentAttempt.objects.select_related("intent")
        .filter(
            provider_config=config,
            intent__merchant=config.merchant,
            status=PaymentAttempt.STATUS_SUCCEEDED,
        )
        .order_by("-created_at")
    )
    attempt = next(
        (candidate for candidate in attempts if provider_payment_id in attempt_provider_references(candidate)),
        None,
    )
    if (
        attempt is None
        or not provider_dispute_id
        or not isinstance(amount, int)
        or amount <= 0
        or amount > attempt.amount
        or str(currency).upper() != attempt.currency.upper()
    ):
        _audit_mismatch(config, event_id, "dispute.webhook_reconciliation_skipped", "dispute_details_mismatch")
        return None, "dispute_details_mismatch"
    with transaction.atomic():
        dispute, created = Dispute.objects.select_for_update().get_or_create(
            merchant=config.merchant,
            provider=config.provider,
            provider_dispute_id=provider_dispute_id,
            defaults={
                "payment_intent": attempt.intent,
                "payment_attempt": attempt,
                "provider_config": config,
                "reason": str(reason)[:120],
                "amount": amount,
                "currency": str(currency).upper(),
                "status": str(status)[:40],
                "evidence_due_at": evidence_due_at,
                "metadata": safe_metadata or {},
            },
        )
        if not created and (
            dispute.payment_intent_id != attempt.intent_id
            or dispute.payment_attempt_id != attempt.pk
            or dispute.provider_config_id != config.pk
            or dispute.amount != amount
            or dispute.currency != str(currency).upper()
        ):
            _audit_mismatch(config, event_id, "dispute.webhook_reconciliation_skipped", "stored_dispute_mismatch")
            return None, "stored_dispute_mismatch"
        if not created:
            dispute.reason = str(reason)[:120]
            dispute.status = str(status)[:40]
            dispute.evidence_due_at = evidence_due_at or dispute.evidence_due_at
            dispute.metadata = safe_metadata or dispute.metadata
            dispute.save(update_fields=["reason", "status", "evidence_due_at", "metadata", "updated_at"])
        emit_event(
            config.merchant,
            "dispute.created" if created else "dispute.updated",
            serialize_dispute(dispute),
            idempotency_key=f"dispute:{provider_dispute_id}:{status}",
        )
    return dispute, ""
