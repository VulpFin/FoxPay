from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse

from .adapters.base import ProviderAdapterError
from .adapters.card import get_card_adapter
from .adapters.crypto import get_crypto_adapter
from .events import emit_event
from .ledger import record_payment_success, record_refund
from .models import APIKey, Customer, IdempotencyRecord, PaymentAttempt, PaymentIntent, ProviderEvent, Refund, WebhookDelivery
from .routing import provider_routes


class APIError(Exception):
    def __init__(self, message, status=400, type="invalid_request", code="invalid_request"):
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        super().__init__(message)


def error_response(message, status=400, request_id="", type="invalid_request", code="invalid_request"):
    return JsonResponse(
        {"error": {"type": type, "code": code, "message": message, "request_id": request_id}},
        status=status,
    )


def enforce_rate_limit(request, merchant=None, bucket="api"):
    identifier = merchant.id if merchant else request.META.get("REMOTE_ADDR", "unknown")
    key = f"foxpay:rate:{bucket}:{identifier}"
    current = cache.get(key, 0) + 1
    cache.set(key, current, 60)
    if current > settings.FOXPAY_API_RATE_LIMIT_PER_MINUTE:
        raise APIError("Rate limit exceeded.", status=429, type="rate_limit_error", code="rate_limit_exceeded")


def authenticate_merchant(raw_key, required_scope=None):
    if not raw_key:
        raise APIError("Missing X-FoxPay-Key header.", status=401, type="authentication_error", code="missing_api_key")

    prefix = raw_key[: APIKey.PREFIX_LENGTH]
    candidates = APIKey.objects.select_related("merchant").filter(prefix=prefix, merchant__is_active=True)
    for api_key in candidates:
        if api_key.usable and api_key.matches(raw_key):
            if required_scope and not api_key.has_scope(required_scope):
                raise APIError("API key does not have the required scope.", status=403, type="permission_error", code="missing_scope")
            api_key.mark_used()
            return api_key.merchant
    raise APIError("Invalid Fox Pay API key.", status=401, type="authentication_error", code="invalid_api_key")


def serialize_attempt(attempt):
    data = {
        "id": attempt.id,
        "method": attempt.method,
        "provider": attempt.provider,
        "status": attempt.status,
        "checkout_url": attempt.checkout_url,
        "instructions": attempt.instructions,
    }
    if attempt.method == PaymentAttempt.METHOD_CRYPTO and hasattr(attempt, "crypto_invoice"):
        invoice = attempt.crypto_invoice
        data["crypto_invoice"] = {
            "asset": invoice.asset,
            "network": invoice.network,
            "address": invoice.address,
            "amount_decimal": str(invoice.amount_decimal) if invoice.amount_decimal is not None else None,
            "expires_at": invoice.expires_at.isoformat() if invoice.expires_at else None,
            "confirmations_required": invoice.confirmations_required,
            "confirmations_seen": invoice.confirmations_seen,
        }
    return data


def serialize_intent(intent):
    return {
        "id": intent.public_id,
        "object": "payment_intent",
        "amount": intent.amount,
        "currency": intent.currency,
        "description": intent.description,
        "status": intent.status,
        "client_secret": intent.client_secret,
        "success_url": intent.success_url,
        "cancel_url": intent.cancel_url,
        "metadata": intent.metadata,
        "customer": str(intent.customer.uuid) if intent.customer else None,
        "environment": intent.environment,
        "capture_strategy": intent.capture_strategy,
        "statement_descriptor": intent.statement_descriptor,
        "reference": intent.reference,
        "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
        "foxpay_checkout_url": getattr(intent, "foxpay_checkout_url", ""),
        "payment_options": [serialize_attempt(attempt) for attempt in intent.attempts.all().order_by("created_at")],
        "created_at": intent.created_at.isoformat(),
    }


def clean_payment_methods(methods):
    if not methods:
        return ["card", "crypto"]
    if not isinstance(methods, list):
        raise APIError("payment_methods must be a list.")
    allowed = {"card", "crypto"}
    cleaned = []
    for method in methods:
        if method not in allowed:
            raise APIError(f"Unsupported payment method: {method}")
        if method not in cleaned:
            cleaned.append(method)
    return cleaned


def validate_metadata(metadata):
    if not isinstance(metadata, dict):
        raise APIError("metadata must be an object.")
    encoded = str(metadata)
    if len(encoded) > 8000:
        raise APIError("metadata is too large.")
    for key, value in metadata.items():
        if len(str(key)) > 80 or len(str(value)) > 500:
            raise APIError("metadata keys and values must be reasonably sized.")
    return metadata


def get_or_create_customer(merchant, payload):
    customer_payload = payload.get("customer")
    if not customer_payload:
        return None
    if not isinstance(customer_payload, dict):
        raise APIError("customer must be an object.")
    external_id = customer_payload.get("external_id", "")
    if external_id:
        customer, _ = Customer.objects.update_or_create(
            merchant=merchant,
            external_id=external_id,
            defaults={
                "email": customer_payload.get("email", ""),
                "name": customer_payload.get("name", ""),
                "metadata": validate_metadata(customer_payload.get("metadata") or {}),
            },
        )
        return customer
    return Customer.objects.create(
        merchant=merchant,
        email=customer_payload.get("email", ""),
        name=customer_payload.get("name", ""),
        metadata=validate_metadata(customer_payload.get("metadata") or {}),
    )


def create_attempts_for_method(request, merchant, intent, payload, method):
    created = []
    failures = []
    for provider, provider_config in provider_routes(merchant, method):
        try:
            if method == PaymentAttempt.METHOD_CARD:
                created.append(get_card_adapter(provider, provider_config).create_attempt(request, intent, payload))
            if method == PaymentAttempt.METHOD_CRYPTO:
                created.append(get_crypto_adapter(provider, provider_config).create_attempt(request, intent, payload))
        except (ImproperlyConfigured, ProviderAdapterError, ValueError) as exc:
            failures.append(f"{provider}: {exc}")
    if not created:
        raise APIError(f"No available {method} providers. " + " ".join(failures))
    return created


@transaction.atomic
def create_payment_intent(request, merchant, payload, idempotency_key=""):
    if idempotency_key:
        if len(idempotency_key) > 160:
            raise APIError("Idempotency-Key must be 160 characters or fewer.")
        existing = (
            IdempotencyRecord.objects.select_related("payment_intent")
            .filter(merchant=merchant, key=idempotency_key)
            .first()
        )
        if existing:
            intent = PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice").get(pk=existing.payment_intent_id)
            intent.foxpay_checkout_url = request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[intent.client_secret]))
            return intent

    amount = payload.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        raise APIError("amount must be a positive integer in the smallest currency unit.")

    currency = str(payload.get("currency") or merchant.default_currency).upper()
    if len(currency) != 3:
        raise APIError("currency must be a three-letter ISO currency code.")

    payment_methods = clean_payment_methods(payload.get("payment_methods"))
    metadata = validate_metadata(payload.get("metadata") or {})
    customer = get_or_create_customer(merchant, payload)
    intent = PaymentIntent.objects.create(
        merchant=merchant,
        customer=customer,
        amount=amount,
        currency=currency,
        description=payload.get("description", ""),
        requested_payment_methods=payment_methods,
        environment=settings.FOXPAY_ENV,
        capture_strategy=payload.get("capture_strategy", PaymentIntent.CAPTURE_AUTOMATIC),
        statement_descriptor=payload.get("statement_descriptor", ""),
        reference=payload.get("reference", ""),
        expires_at=payload.get("expires_at") or None,
        success_url=payload.get("success_url", ""),
        cancel_url=payload.get("cancel_url", ""),
        metadata=metadata,
    )

    for method in payment_methods:
        create_attempts_for_method(request, merchant, intent, payload, method)

    if idempotency_key:
        IdempotencyRecord.objects.create(
            merchant=merchant,
            key=idempotency_key,
            payment_intent=intent,
            response_object_type="payment_intent",
            response_object_id=intent.public_id,
        )

    hydrated = PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice").get(pk=intent.pk)
    hydrated.foxpay_checkout_url = request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[hydrated.client_secret]))
    emit_event(merchant, "payment_intent.created", serialize_intent(hydrated), idempotency_key=f"payment_intent.created:{hydrated.public_id}")
    return hydrated


@transaction.atomic
def create_refund(request, merchant, intent, payload, idempotency_key=""):
    if idempotency_key:
        existing = (
            IdempotencyRecord.objects.select_related("payment_intent")
            .filter(merchant=merchant, key=f"refund:{idempotency_key}")
            .first()
        )
        if existing:
            refund = Refund.objects.filter(public_id=existing.response_object_id, merchant=merchant).first()
            if refund:
                return refund

    amount = payload.get("amount") or intent.amount
    if not isinstance(amount, int) or amount <= 0:
        raise APIError("amount must be a positive integer.")
    refunded_total = sum(refund.amount for refund in intent.refunds.exclude(status=Refund.STATUS_FAILED))
    if refunded_total + amount > intent.amount:
        raise APIError("Refund amount exceeds captured payment amount.", type="payment_error", code="amount_exceeds_refundable")

    attempt = intent.attempts.filter(status=PaymentAttempt.STATUS_SUCCEEDED).order_by("-created_at").first()
    refund = Refund.objects.create(
        merchant=merchant,
        payment_intent=intent,
        payment_attempt=attempt,
        provider_config=attempt.provider_config if attempt else None,
        amount=amount,
        currency=intent.currency,
        status=Refund.STATUS_SUCCEEDED,
        provider=attempt.provider if attempt else "",
        reason=payload.get("reason", ""),
        metadata=validate_metadata(payload.get("metadata") or {}),
    )
    record_refund(refund, getattr(request, "request_id", ""))
    if idempotency_key:
        IdempotencyRecord.objects.create(
            merchant=merchant,
            key=f"refund:{idempotency_key}",
            payment_intent=intent,
            response_object_type="refund",
            response_object_id=refund.public_id,
        )

    refunded_total += amount
    intent.status = PaymentIntent.STATUS_REFUNDED if refunded_total == intent.amount else PaymentIntent.STATUS_PARTIALLY_REFUNDED
    intent.save(update_fields=["status", "updated_at"])
    emit_event(merchant, "refund.succeeded", serialize_refund(refund), idempotency_key=f"refund.succeeded:{refund.public_id}")
    return refund


def serialize_refund(refund):
    return {
        "id": refund.public_id,
        "object": "refund",
        "payment_intent": refund.payment_intent.public_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "status": refund.status,
        "provider": refund.provider,
        "reason": refund.reason,
        "metadata": refund.metadata,
        "created_at": refund.created_at.isoformat(),
    }


@transaction.atomic
def record_webhook(provider, payload):
    provider_event_id = payload.get("id", "")
    if provider_event_id and ProviderEvent.objects.filter(provider=provider, provider_event_id=provider_event_id).exists():
        delivery = WebhookDelivery.objects.create(
            provider=provider,
            event_type=payload.get("type", "unknown"),
            provider_event_id=provider_event_id,
            payload=payload,
            processed=True,
        )
        return delivery

    delivery = WebhookDelivery.objects.create(
        provider=provider,
        event_type=payload.get("type", "unknown"),
        provider_event_id=provider_event_id,
        payload=payload,
    )

    public_id = payload.get("payment_intent") or payload.get("payment_intent_id")
    status = payload.get("status")
    transaction_id = payload.get("transaction_id", "")
    confirmations = payload.get("confirmations")

    if public_id and status in {"paid", "confirmed", "succeeded"}:
        intent = PaymentIntent.objects.filter(public_id=public_id).first()
        if intent:
            intent.mark_succeeded()
            record_payment_success(intent)
            method = payload.get("foxpay_method") or PaymentAttempt.METHOD_CRYPTO
            attempts = intent.attempts.filter(method=method)
            attempt_id = payload.get("foxpay_attempt_id")
            if attempt_id:
                attempts = attempts.filter(id=attempt_id)
            for attempt in attempts:
                attempt.status = PaymentAttempt.STATUS_SUCCEEDED
                if payload.get("provider_reference"):
                    attempt.provider_reference = payload["provider_reference"]
                attempt.provider_status = status
                attempt.provider_response_metadata = {
                    **attempt.provider_response_metadata,
                    "webhook_event_type": payload.get("type", ""),
                    "webhook_provider_event_id": provider_event_id,
                }
                attempt.save(update_fields=["status", "provider_reference", "provider_status", "provider_response_metadata", "updated_at"])
                if hasattr(attempt, "crypto_invoice"):
                    invoice = attempt.crypto_invoice
                    invoice.transaction_id = transaction_id or invoice.transaction_id
                    if confirmations is not None:
                        invoice.confirmations_seen = int(confirmations)
                    invoice.settlement_state = invoice.SETTLEMENT_CONFIRMED
                    invoice.save(update_fields=["transaction_id", "confirmations_seen", "settlement_state", "updated_at"])
            ProviderEvent.objects.create(
                merchant=intent.merchant,
                provider=provider,
                provider_event_id=provider_event_id or f"{provider}:{delivery.id}",
                event_type=payload.get("type", "unknown"),
                normalized_event_type="payment_intent.succeeded",
                payment_intent=intent,
                payload=payload,
                processed_at=delivery.updated_at,
            )
            emit_event(intent.merchant, "payment_intent.succeeded", serialize_intent(intent), idempotency_key=f"payment_intent.succeeded:{intent.public_id}")
            delivery.processed = True
            delivery.save(update_fields=["processed", "updated_at"])
            return delivery

    if public_id and status in {"failed", "expired", "canceled", "cancelled"}:
        intent = PaymentIntent.objects.filter(public_id=public_id).first()
        if intent:
            intent.status = PaymentIntent.STATUS_EXPIRED if status == "expired" else PaymentIntent.STATUS_FAILED
            intent.save(update_fields=["status", "updated_at"])
            method = payload.get("foxpay_method") or PaymentAttempt.METHOD_CRYPTO
            attempts = intent.attempts.filter(method=method)
            attempt_id = payload.get("foxpay_attempt_id")
            if attempt_id:
                attempts = attempts.filter(id=attempt_id)
            for attempt in attempts:
                attempt.status = PaymentAttempt.STATUS_FAILED
                attempt.provider_status = status
                attempt.failure_code = payload.get("failure_code", "")
                attempt.failure_category = payload.get("failure_category", "")
                attempt.save(update_fields=["status", "provider_status", "failure_code", "failure_category", "updated_at"])
            ProviderEvent.objects.create(
                merchant=intent.merchant,
                provider=provider,
                provider_event_id=provider_event_id or f"{provider}:{delivery.id}",
                event_type=payload.get("type", "unknown"),
                normalized_event_type=f"payment_intent.{intent.status}",
                payment_intent=intent,
                payload=payload,
                processed_at=delivery.updated_at,
            )
            emit_event(intent.merchant, f"payment_intent.{intent.status}", serialize_intent(intent), idempotency_key=f"payment_intent.{intent.status}:{intent.public_id}")
            delivery.processed = True
            delivery.save(update_fields=["processed", "updated_at"])
            return delivery

    delivery.processed = True
    delivery.save(update_fields=["processed", "updated_at"])
    return delivery
