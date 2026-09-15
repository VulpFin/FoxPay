import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from urllib.parse import urlsplit

from .abuse import AbuseControlError, enforce_provider_call
from .adapters.base import ProviderAdapterError
from .adapters.paypal_checkout import PayPalCheckoutAdapter, capture_amount
from .models import AuditLog, Merchant, PaymentAttempt, ProviderConfig, ProviderEvent, Refund
from .paypal_partner import seller_id_from_event
from .permissions import routing_block_reason
from .reconciliation import reconcile_provider_dispute, reconcile_provider_refund
from .routing import authorized_config
from .services import record_webhook


PAYPAL_REFUND_EVENTS = {"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REFUND.FAILED"}
PAYPAL_DISPUTE_EVENTS = {"CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"}
PAYPAL_CAPTURE_INBOX_PREFIX = "paypal-capture:"


logger = logging.getLogger(__name__)


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


def _fresh_capture_config(config, attempt, event):
    merchant = Merchant.objects.get(pk=config.merchant_id)
    reason = routing_block_reason(merchant, config.environment)
    config = ProviderConfig.objects.select_related("connection").get(pk=config.pk, merchant=merchant)
    if reason or not config.is_active or not authorized_config(merchant, config):
        object_id = str(event.get("id", ""))[:120]
        if not AuditLog.objects.filter(
            merchant=merchant,
            action="paypal.capture.routing_blocked",
            object_type="provider_event",
            object_id=object_id,
        ).exists():
            AuditLog.objects.create(
                merchant=merchant,
                action="paypal.capture.routing_blocked",
                object_type="provider_event",
                object_id=object_id,
                metadata={"reason": reason or "provider_connection_inactive"},
            )
        return None
    try:
        enforce_provider_call(
            merchant,
            ip_hash=attempt.intent.request_ip_hash,
            environment=config.environment,
        )
    except AbuseControlError as exc:
        raise ProviderAdapterError("PayPal capture is temporarily unavailable.") from exc
    return config


def _safe_capture_event(event):
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    seller_id = seller_id_from_event(event)
    safe_resource = {
        "id": str(resource.get("id", ""))[:160],
        "custom_id": str(resource.get("custom_id", ""))[:160],
        "invoice_id": str(resource.get("invoice_id", ""))[:240],
    }
    if seller_id:
        safe_resource["payee"] = {"merchant_id": seller_id}
    units = []
    for unit in resource.get("purchase_units") or []:
        if not isinstance(unit, dict):
            continue
        safe_unit = {
            "custom_id": str(unit.get("custom_id", ""))[:160],
            "invoice_id": str(unit.get("invoice_id", ""))[:240],
        }
        unit_payee = unit.get("payee") if isinstance(unit.get("payee"), dict) else {}
        unit_seller = unit_payee.get("merchant_id") if isinstance(unit_payee.get("merchant_id"), str) else seller_id
        if unit_seller:
            safe_unit["payee"] = {"merchant_id": unit_seller}
        units.append(safe_unit)
    if units:
        safe_resource["purchase_units"] = units
    return {
        "id": str(event.get("id", ""))[:160],
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "merchant_id": seller_id,
        "resource": safe_resource,
    }


def _enqueue_capture_event(provider_event_id):
    try:
        from .tasks import process_paypal_capture_event

        process_paypal_capture_event.delay(provider_event_id)
    except Exception:
        logger.exception("Unable to enqueue PayPal capture event %s", provider_event_id)


def record_paypal_capture_event(config, event):
    safe_event = _safe_capture_event(event)
    normalized = PayPalCheckoutAdapter(config).normalize_webhook(safe_event)
    attempt = _matching_attempt(config, normalized)
    namespace = f"{PAYPAL_CAPTURE_INBOX_PREFIX}{config.pk}"
    with transaction.atomic():
        provider_event, _ = ProviderEvent.objects.get_or_create(
            provider=namespace,
            provider_event_id=safe_event["id"],
            defaults={
                "merchant": config.merchant,
                "event_type": safe_event["event_type"],
                "normalized_event_type": "paypal.capture.pending",
                "payment_intent": attempt.intent if attempt else None,
                "payload": {"provider_config_id": config.pk, "event": safe_event},
            },
        )
    if settings.FOXPAY_ASYNC_TASKS_ENABLED:
        if provider_event.processed_at is None:
            transaction.on_commit(lambda: _enqueue_capture_event(provider_event.pk))
        return {"processed": False, "captured": False, "queued": provider_event.processed_at is None}
    return process_paypal_capture_inbox(provider_event.pk)


def process_paypal_capture_inbox(provider_event_id):
    lock_key = f"foxpay:paypal-capture:{provider_event_id}"
    try:
        acquired = cache.add(lock_key, "1", timeout=120)
    except Exception:
        acquired = settings.FOXPAY_ENV != "live"
    if not acquired:
        return {"processed": False, "captured": False, "deferred": True}
    try:
        provider_event = ProviderEvent.objects.filter(
            pk=provider_event_id,
            provider__startswith=PAYPAL_CAPTURE_INBOX_PREFIX,
        ).first()
        if not provider_event:
            return {"processed": False, "captured": False}
        if provider_event.processed_at:
            return {"processed": True, "captured": False, "duplicate": True}
        payload = provider_event.payload if isinstance(provider_event.payload, dict) else {}
        config_id = payload.get("provider_config_id")
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        config = ProviderConfig.objects.select_related("merchant", "connection").filter(
            pk=config_id,
            merchant_id=provider_event.merchant_id,
        ).first()
        if not config or provider_event.provider != f"{PAYPAL_CAPTURE_INBOX_PREFIX}{config_id}":
            provider_event.normalized_event_type = "paypal.capture.rejected"
            provider_event.processed_at = timezone.now()
            provider_event.save(update_fields=["normalized_event_type", "processed_at", "updated_at"])
            return {"processed": False, "captured": False, "mismatch": True}
        result = process_paypal_event(config, event)
        if result.get("processed") or result.get("mismatch"):
            provider_event.normalized_event_type = (
                "paypal.capture.processed" if result.get("processed") else "paypal.capture.rejected"
            )
            provider_event.processed_at = timezone.now()
            provider_event.save(update_fields=["normalized_event_type", "processed_at", "updated_at"])
        else:
            ProviderEvent.objects.filter(pk=provider_event.pk).update(updated_at=timezone.now())
        return result
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def _capture_id_from_refund(resource):
    direct = resource.get("capture_id")
    if isinstance(direct, str) and direct:
        return direct
    for link in resource.get("links") or []:
        if not isinstance(link, dict) or link.get("rel") != "up":
            continue
        path = urlsplit(str(link.get("href", ""))).path.rstrip("/").split("/")
        if len(path) >= 2 and path[-2] == "captures" and path[-1]:
            return path[-1]
    return ""


def _record_special_event(config, event, normalized_type, intent=None, safe_payload=None):
    provider = f"paypal:{config.pk}"
    provider_event, created = ProviderEvent.objects.get_or_create(
        provider=provider,
        provider_event_id=event["id"],
        defaults={
            "merchant": config.merchant,
            "event_type": event["event_type"],
            "normalized_event_type": normalized_type,
            "payment_intent": intent,
            "payload": safe_payload or {},
            "processed_at": timezone.now(),
        },
    )
    return created


def _process_refund_event(config, event):
    if not _seller_matches(config, event):
        _audit_mismatch(config, event, "seller_mismatch")
        return {"processed": False, "captured": False, "mismatch": True}
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    minor, currency = capture_amount(resource)
    provider_status = str(resource.get("status", ""))
    normalized_status = {
        "COMPLETED": Refund.STATUS_SUCCEEDED,
        "PENDING": Refund.STATUS_PENDING,
        "CANCELLED": Refund.STATUS_CANCELED,
        "FAILED": Refund.STATUS_FAILED,
    }.get(provider_status, Refund.STATUS_FAILED if event["event_type"].endswith("FAILED") else Refund.STATUS_PENDING)
    refund, error = reconcile_provider_refund(
        config,
        event_id=event["id"],
        provider_refund_id=str(resource.get("id", "")),
        provider_payment_id=_capture_id_from_refund(resource),
        amount=minor,
        currency=currency,
        provider_status=provider_status,
        normalized_status=normalized_status,
        foxpay_refund_id=str(resource.get("custom_id", "")),
        failure_code="refund_failed" if normalized_status == Refund.STATUS_FAILED else "",
    )
    created = _record_special_event(
        config,
        event,
        f"refund.{refund.status}" if refund else "refund.reconciliation_skipped",
        intent=refund.payment_intent if refund else None,
        safe_payload={
            "refund": refund.public_id if refund else "",
            "provider_refund": str(resource.get("id", ""))[:160],
            "reason": error,
        },
    )
    return {"processed": bool(refund and created), "captured": False, "mismatch": bool(error and error != "refund_not_found")}


def _process_dispute_event(config, event):
    if not _seller_matches(config, event):
        _audit_mismatch(config, event, "seller_mismatch")
        return {"processed": False, "captured": False, "mismatch": True}
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    money = resource.get("dispute_amount") if isinstance(resource.get("dispute_amount"), dict) else {}
    minor, currency = capture_amount({"amount": money})
    transactions = resource.get("disputed_transactions") if isinstance(resource.get("disputed_transactions"), list) else []
    transaction_data = next((item for item in transactions if isinstance(item, dict)), {})
    provider_status = str(resource.get("status", ""))
    outcome = resource.get("dispute_outcome") if isinstance(resource.get("dispute_outcome"), dict) else {}
    outcome_code = str(outcome.get("outcome_code", "")).upper()
    resolved_status = "won" if "SELLER" in outcome_code else "lost" if "BUYER" in outcome_code else "resolved"
    normalized_status = {
        "WAITING_FOR_SELLER_RESPONSE": "needs_response",
        "WAITING_FOR_BUYER_RESPONSE": "waiting_for_buyer",
        "UNDER_REVIEW": "under_review",
        "RESOLVED": resolved_status,
        "OPEN": "open",
    }.get(provider_status, provider_status.lower() or "open")
    due_value = resource.get("seller_response_due_date")
    due_at = parse_datetime(due_value) if isinstance(due_value, str) else None
    dispute, error = reconcile_provider_dispute(
        config,
        event_id=event["id"],
        provider_dispute_id=str(resource.get("dispute_id") or resource.get("id") or ""),
        provider_payment_id=str(transaction_data.get("seller_transaction_id", "")),
        amount=minor,
        currency=currency,
        reason=resource.get("reason", ""),
        status=normalized_status,
        evidence_due_at=due_at,
        safe_metadata={
            "provider_status": provider_status,
            "life_cycle_stage": str(resource.get("dispute_life_cycle_stage", ""))[:40],
            "outcome_code": outcome_code[:80],
        },
    )
    created = _record_special_event(
        config,
        event,
        "dispute.updated" if dispute else "dispute.reconciliation_skipped",
        intent=dispute.payment_intent if dispute else None,
        safe_payload={
            "dispute": str(dispute.uuid) if dispute else "",
            "provider_dispute": str(resource.get("dispute_id") or resource.get("id") or "")[:160],
            "reason": error,
        },
    )
    return {"processed": bool(dispute and created), "captured": False, "mismatch": bool(error)}


def process_paypal_event(config, event):
    if event.get("event_type") in PAYPAL_REFUND_EVENTS:
        return _process_refund_event(config, event)
    if event.get("event_type") in PAYPAL_DISPUTE_EVENTS:
        return _process_dispute_event(config, event)
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
        fresh_config = _fresh_capture_config(config, attempt, event)
        if not fresh_config:
            return {"processed": False, "captured": False, "blocked": True}
        config = fresh_config
        adapter = PayPalCheckoutAdapter(config)
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
