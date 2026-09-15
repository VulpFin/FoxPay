import re
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone

from .abuse import AbuseControlError, AbuseLimitExceeded, enforce_api_limits, enforce_intent_creation, enforce_provider_call, record_payment_outcome
from .adapters.base import Capability, ProviderAdapterError, ProviderOperationResult, ProviderRequestError
from .adapters.card import get_card_adapter
from .adapters.crypto import get_crypto_adapter
from .events import emit_event
from .ledger import record_payment_success, record_refund
from .models import APIKey, Customer, IdempotencyRecord, Merchant, PaymentAttempt, PaymentIntent, ProviderConfig, ProviderEvent, Refund, WebhookDelivery
from .permissions import routing_block_reason
from .routing import authorized_config, provider_routes
from .safe_urls import UnsafeURL, return_origin


class APIError(Exception):
    def __init__(self, message, status=400, type="invalid_request", code="invalid_request"):
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        super().__init__(message)


RAW_CARD_FIELD_NAMES = {
    "card",
    "card_data",
    "card_number",
    "cardnumber",
    "primary_account_number",
    "pan",
    "cvv",
    "cvv2",
    "cvc",
    "cvc2",
    "magnetic_stripe",
    "magstripe",
    "track1",
    "track2",
    "emv",
    "cryptogram",
    "payment_card",
}


def _looks_like_card_number(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    digits = re.sub(r"[ -]", "", str(value))
    return digits.isdigit() and 12 <= len(digits) <= 19


def reject_raw_card_fields(value, path=()):
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            card_context = any("card" in item or "payment_method" in item for item in path)
            if normalized in RAW_CARD_FIELD_NAMES or (
                normalized == "number" and (card_context or _looks_like_card_number(nested))
            ):
                raise APIError(
                    "Raw card data is not accepted. Use a provider-hosted checkout.",
                    status=422,
                    type="invalid_request",
                    code="raw_card_data_forbidden",
                )
            reject_raw_card_fields(nested, (*path, normalized))
    elif isinstance(value, list):
        for nested in value:
            reject_raw_card_fields(nested, path)


def error_response(message, status=400, request_id="", type="invalid_request", code="invalid_request"):
    return JsonResponse(
        {"error": {"type": type, "code": code, "message": message, "request_id": request_id}},
        status=status,
    )


def enforce_rate_limit(request, merchant=None, bucket="api"):
    if merchant is None:
        return
    try:
        enforce_api_limits(request, merchant, bucket=bucket)
    except AbuseLimitExceeded as exc:
        raise APIError(str(exc), status=429, type="rate_limit_error", code=exc.code) from exc
    except AbuseControlError as exc:
        raise APIError(str(exc), status=503, type="api_error", code=exc.code) from exc


def authenticate_merchant(raw_key, required_scope=None):
    if not raw_key:
        raise APIError("Missing X-FoxPay-Key header.", status=401, type="authentication_error", code="missing_api_key")

    prefix = raw_key[: APIKey.PREFIX_LENGTH]
    candidates = APIKey.objects.select_related("merchant").filter(prefix=prefix)
    for api_key in candidates:
        if api_key.usable and api_key.environment == settings.FOXPAY_ENV and api_key.matches(raw_key):
            enforce_merchant_routing(api_key.merchant)
            if required_scope and not api_key.has_scope(required_scope):
                raise APIError("API key does not have the required scope.", status=403, type="permission_error", code="missing_scope")
            api_key.mark_used()
            return api_key.merchant
    raise APIError("Invalid Fox Pay API key.", status=401, type="authentication_error", code="invalid_api_key")


def enforce_merchant_routing(merchant, *, fresh=False, environment=None):
    if fresh:
        merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
    reason = routing_block_reason(merchant, environment)
    if reason:
        raise APIError("Merchant payment routing is unavailable.", status=403, type="permission_error", code=reason)
    return merchant


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


def validate_return_urls(merchant, success_url, cancel_url):
    allowed = set(merchant.allowed_return_origins.values_list("origin", flat=True))
    for field, value in (("success_url", success_url), ("cancel_url", cancel_url)):
        if not value:
            continue
        try:
            origin = return_origin(value)
        except UnsafeURL as exc:
            raise APIError(f"{field} is not a valid public URL.") from exc
        if origin not in allowed:
            raise APIError(f"{field} origin is not registered for this merchant.", code="return_origin_not_allowed")


def get_or_create_customer(merchant, payload):
    customer_payload = payload.get("customer")
    if not customer_payload:
        return None
    if not isinstance(customer_payload, dict):
        raise APIError("customer must be an object.")
    external_id = customer_payload.get("external_id", "")
    if not isinstance(external_id, str) or len(external_id) > 160:
        raise APIError("customer.external_id must be a string of at most 160 characters.")
    subject_value = customer_payload.get("tg11_user_uuid")
    if subject_value:
        try:
            subject = uuid.UUID(str(subject_value))
        except (TypeError, ValueError):
            raise APIError("customer.tg11_user_uuid must be a valid UUID.")
        by_external = Customer.objects.filter(merchant=merchant, external_id=external_id).first() if external_id else None
        by_subject = Customer.objects.filter(merchant=merchant, tg11_user_uuid=subject).first()
        if by_external and by_subject and by_external.pk != by_subject.pk:
            raise APIError("Customer identifiers are already linked to different accounts.", status=409)
        customer = by_external or by_subject
        if customer:
            if customer.tg11_user_uuid and customer.tg11_user_uuid != subject:
                raise APIError("Customer is already linked to a different TG11 account.", status=409)
            if external_id and customer.external_id and customer.external_id != external_id:
                raise APIError("TG11 account is already linked to a different customer identifier.", status=409)
            customer.tg11_user_uuid = subject
            if external_id:
                customer.external_id = external_id
            for field in ("email", "name"):
                if field in customer_payload:
                    setattr(customer, field, customer_payload[field])
            if "metadata" in customer_payload:
                customer.metadata = validate_metadata(customer_payload["metadata"] or {})
            customer.save()
            return customer
        return Customer.objects.create(
            merchant=merchant,
            external_id=external_id,
            tg11_user_uuid=subject,
            email=customer_payload.get("email", ""),
            name=customer_payload.get("name", ""),
            metadata=validate_metadata(customer_payload.get("metadata") or {}),
        )
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
        merchant = enforce_merchant_routing(Merchant.objects.get(pk=merchant.pk), environment=intent.environment)
        if provider_config:
            provider_config = type(provider_config).objects.select_related("connection").get(pk=provider_config.pk)
            if not provider_config.is_active or not authorized_config(merchant, provider_config):
                continue
        try:
            enforce_provider_call(merchant, ip_hash=intent.request_ip_hash, environment=intent.environment)
        except AbuseLimitExceeded as exc:
            raise APIError(str(exc), status=429, type="rate_limit_error", code=exc.code) from exc
        except AbuseControlError as exc:
            raise APIError(str(exc), status=503, type="api_error", code=exc.code) from exc
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


def create_payment_intent(request, merchant, payload, idempotency_key=""):
    if not isinstance(payload, dict):
        raise APIError("Request body must be an object.")
    reject_raw_card_fields(payload)
    merchant = enforce_merchant_routing(Merchant.objects.get(pk=merchant.pk))
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
    if not payload.get("payment_methods"):
        payment_methods = [method for method in payment_methods if provider_routes(merchant, method)]
        if not payment_methods:
            raise APIError("No payment providers are available.", code="no_available_providers")
    capture_strategy = payload.get("capture_strategy", PaymentIntent.CAPTURE_AUTOMATIC)
    if capture_strategy != PaymentIntent.CAPTURE_AUTOMATIC:
        raise APIError(
            "Manual capture is not available through Fox Pay yet.",
            status=501,
            type="unsupported_operation",
            code="manual_capture_unavailable",
        )
    metadata = validate_metadata(payload.get("metadata") or {})
    validate_return_urls(merchant, payload.get("success_url", ""), payload.get("cancel_url", ""))
    try:
        ip_hash = enforce_intent_creation(request, merchant, amount)
    except AbuseLimitExceeded as exc:
        raise APIError(str(exc), status=429, type="rate_limit_error", code=exc.code) from exc
    except AbuseControlError as exc:
        raise APIError(str(exc), status=503, type="api_error", code=exc.code) from exc

    with transaction.atomic():
        merchant = enforce_merchant_routing(merchant, fresh=True)
        if idempotency_key:
            existing = (
                IdempotencyRecord.objects.select_related("payment_intent")
                .filter(merchant=merchant, key=idempotency_key)
                .first()
            )
            if existing:
                intent = PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice").get(pk=existing.payment_intent_id)
                intent.foxpay_checkout_url = request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[intent.client_secret]))
                return intent
        customer = get_or_create_customer(merchant, payload)
        intent = PaymentIntent.objects.create(
            merchant=merchant,
            customer=customer,
            amount=amount,
            currency=currency,
            description=payload.get("description", ""),
            requested_payment_methods=payment_methods,
            environment=settings.FOXPAY_ENV,
            capture_strategy=capture_strategy,
            statement_descriptor=payload.get("statement_descriptor", ""),
            reference=payload.get("reference", ""),
            expires_at=payload.get("expires_at") or None,
            success_url=payload.get("success_url", ""),
            cancel_url=payload.get("cancel_url", ""),
            metadata=metadata,
            request_ip_hash=ip_hash,
        )
        if idempotency_key:
            IdempotencyRecord.objects.create(
                merchant=merchant,
                key=idempotency_key,
                payment_intent=intent,
                response_object_type="payment_intent",
                response_object_id=intent.public_id,
            )

    try:
        for method in payment_methods:
            create_attempts_for_method(request, merchant, intent, payload, method)
    except Exception:
        if not intent.attempts.filter(
            status__in=[PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_ACTION_REQUIRED, PaymentAttempt.STATUS_SUCCEEDED]
        ).exists():
            intent.status = PaymentIntent.STATUS_FAILED
            intent.save(update_fields=["status", "updated_at"])
        raise

    hydrated = PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice").get(pk=intent.pk)
    hydrated.foxpay_checkout_url = request.build_absolute_uri(reverse("payments:foxpay_checkout", args=[hydrated.client_secret]))
    emit_event(merchant, "payment_intent.created", serialize_intent(hydrated), idempotency_key=f"payment_intent.created:{hydrated.public_id}")
    return hydrated


def _adapter_for_refund(attempt):
    if attempt.method != PaymentAttempt.METHOD_CARD:
        raise APIError(
            "Crypto refunds require a seller-authorized manual workflow.",
            status=501,
            type="unsupported_operation",
            code="manual_crypto_refund_required",
        )
    adapter_name = attempt.provider_config.adapter_name if attempt.provider_config else attempt.provider
    try:
        adapter = get_card_adapter(adapter_name, attempt.provider_config)
    except ImproperlyConfigured as exc:
        raise APIError(
            "Refunds for this provider are not available through Fox Pay.",
            status=501,
            type="unsupported_operation",
            code="provider_refund_unavailable",
        ) from exc
    if Capability.REFUNDS not in adapter.capabilities:
        raise APIError(
            "Refunds for this provider are not available through Fox Pay.",
            status=501,
            type="unsupported_operation",
            code="provider_refund_unavailable",
        )
    return adapter


def _existing_refund(merchant, idempotency_key):
    if not idempotency_key:
        return None
    refund = Refund.objects.filter(merchant=merchant, idempotency_key=idempotency_key).first()
    if refund:
        return refund
    legacy = IdempotencyRecord.objects.filter(
        merchant=merchant,
        key=f"refund:{idempotency_key}",
        response_object_type="refund",
    ).first()
    if legacy:
        return Refund.objects.filter(merchant=merchant, public_id=legacy.response_object_id).first()
    return None


def _reserve_refund(merchant, intent, payload, idempotency_key):
    if idempotency_key and len(idempotency_key) > 160:
        raise APIError("Idempotency-Key must be 160 characters or fewer.")
    if not isinstance(payload, dict):
        raise APIError("Request body must be an object.")
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 240:
        raise APIError("reason must be a string of at most 240 characters.")
    metadata = validate_metadata(payload.get("metadata") or {})

    with transaction.atomic():
        merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
        enforce_merchant_routing(merchant, environment=intent.environment)
        intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk, merchant=merchant)
        existing = _existing_refund(merchant, idempotency_key)
        if existing:
            return existing, False

        amount = payload.get("amount", intent.amount)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise APIError("amount must be a positive integer.")
        reserved_refunds = list(
            Refund.objects.select_for_update()
            .filter(payment_intent=intent)
            .exclude(status__in=[Refund.STATUS_FAILED, Refund.STATUS_CANCELED])
        )
        reserved_total = sum(item.amount for item in reserved_refunds)
        if reserved_total + amount > intent.amount:
            raise APIError(
                "Refund amount exceeds captured payment amount.",
                type="payment_error",
                code="amount_exceeds_refundable",
            )
        attempt = (
            PaymentAttempt.objects.select_for_update()
            .select_related("provider_config", "provider_config__connection")
            .filter(intent=intent, status=PaymentAttempt.STATUS_SUCCEEDED)
            .order_by("-created_at")
            .first()
        )
        if not attempt:
            raise APIError(
                "Payment has not been captured.",
                status=409,
                type="payment_error",
                code="payment_not_captured",
            )
        if attempt.amount != intent.amount or attempt.currency.upper() != intent.currency.upper():
            raise APIError(
                "Captured payment details do not match the payment intent.",
                status=409,
                type="payment_error",
                code="captured_payment_mismatch",
            )
        if attempt.provider_config and attempt.provider_config.environment != intent.environment:
            raise APIError(
                "Refunds for this provider route are unavailable.",
                status=501,
                type="unsupported_operation",
                code="provider_refund_unavailable",
            )
        _adapter_for_refund(attempt)
        refund = Refund.objects.create(
            merchant=merchant,
            payment_intent=intent,
            payment_attempt=attempt,
            provider_config=attempt.provider_config,
            amount=amount,
            currency=intent.currency,
            status=Refund.STATUS_PENDING,
            provider=attempt.provider,
            idempotency_key=idempotency_key,
            reason=reason,
            metadata=metadata,
        )
        if idempotency_key:
            IdempotencyRecord.objects.get_or_create(
                merchant=merchant,
                key=f"refund:{idempotency_key}",
                defaults={
                    "payment_intent": intent,
                    "response_object_type": "refund",
                    "response_object_id": refund.public_id,
                },
            )
    emit_event(
        merchant,
        "refund.pending",
        serialize_refund(refund),
        idempotency_key=f"refund.pending:{refund.public_id}",
    )
    return refund, True


def _apply_refund_error(refund, exc):
    result = ProviderOperationResult(
        status="pending" if getattr(exc, "ambiguous", False) else "failed",
        provider_status="unknown" if getattr(exc, "ambiguous", False) else "failed",
        failure_code=str(getattr(exc, "code", "provider_error"))[:80],
        failure_category="ambiguous_provider_response" if getattr(exc, "ambiguous", False) else "provider_rejected",
    )
    return reconcile_refund(refund, result)


@transaction.atomic
def reconcile_refund(refund, result, *, request_id=""):
    if not isinstance(result, ProviderOperationResult) or result.status not in {
        Refund.STATUS_PENDING,
        Refund.STATUS_SUCCEEDED,
        Refund.STATUS_FAILED,
        Refund.STATUS_CANCELED,
    }:
        raise ValueError("Invalid normalized refund result.")
    refund_id = refund.pk if isinstance(refund, Refund) else refund
    refund = (
        Refund.objects.select_for_update()
        .select_related("merchant", "payment_intent", "payment_attempt", "provider_config")
        .get(pk=refund_id)
    )
    intent = PaymentIntent.objects.select_for_update().get(pk=refund.payment_intent_id)
    list(Refund.objects.select_for_update().filter(payment_intent=intent).values_list("pk", flat=True))
    if refund.status == Refund.STATUS_SUCCEEDED and result.status != Refund.STATUS_SUCCEEDED:
        return refund
    if (
        result.provider_reference
        and refund.provider_refund_id
        and refund.provider_refund_id != result.provider_reference
    ):
        raise ValueError("Provider refund ID does not match the reserved refund.")

    previous_status = refund.status
    refund.status = result.status
    refund.provider_status = result.provider_status[:80]
    refund.provider_refund_id = refund.provider_refund_id or result.provider_reference[:160]
    refund.failure_code = result.failure_code[:80]
    refund.failure_category = result.failure_category[:80]
    refund.last_reconciled_at = timezone.now()
    refund.last_error_at = timezone.now() if result.failure_code else None
    if result.safe_metadata:
        refund.metadata = {**refund.metadata, "provider": result.safe_metadata}
    refund.save()

    if refund.status == Refund.STATUS_SUCCEEDED:
        record_refund(refund, request_id)
        succeeded_total = (
            Refund.objects.filter(payment_intent=intent, status=Refund.STATUS_SUCCEEDED)
            .aggregate(total=Sum("amount"))["total"]
            or 0
        )
        intent.status = (
            PaymentIntent.STATUS_REFUNDED
            if succeeded_total >= intent.amount
            else PaymentIntent.STATUS_PARTIALLY_REFUNDED
        )
        intent.save(update_fields=["status", "updated_at"])

    if refund.status != previous_status or previous_status == Refund.STATUS_PENDING:
        emit_event(
            refund.merchant,
            f"refund.{refund.status}",
            serialize_refund(refund),
            idempotency_key=f"refund.{refund.status}:{refund.public_id}",
        )
    return refund


def _fresh_refund_adapter(refund):
    merchant = enforce_merchant_routing(
        Merchant.objects.get(pk=refund.merchant_id),
        environment=refund.payment_intent.environment,
    )
    attempt = (
        PaymentAttempt.objects.select_related("provider_config", "provider_config__connection")
        .get(pk=refund.payment_attempt_id, intent__merchant=merchant)
    )
    if attempt.status != PaymentAttempt.STATUS_SUCCEEDED:
        raise APIError(
            "The provider payment is no longer refundable.",
            status=409,
            type="payment_error",
            code="payment_not_captured",
        )
    if attempt.provider_config:
        config = ProviderConfig.objects.select_related("connection").get(
            pk=attempt.provider_config_id,
            merchant=merchant,
            environment=refund.payment_intent.environment,
        )
        if not authorized_config(merchant, config):
            raise APIError(
                "The provider connection is no longer authorized.",
                status=403,
                type="permission_error",
                code="provider_connection_inactive",
            )
        attempt.provider_config = config
    try:
        enforce_provider_call(
            merchant,
            ip_hash=refund.payment_intent.request_ip_hash,
            environment=refund.payment_intent.environment,
        )
    except AbuseLimitExceeded as exc:
        raise APIError(str(exc), status=429, type="rate_limit_error", code=exc.code) from exc
    except AbuseControlError as exc:
        raise APIError(str(exc), status=503, type="api_error", code=exc.code) from exc
    return _adapter_for_refund(attempt)


def create_refund(request, merchant, intent, payload, idempotency_key=""):
    refund, created = _reserve_refund(merchant, intent, payload, idempotency_key)
    if not created:
        return refund
    try:
        adapter = _fresh_refund_adapter(refund)
    except APIError as exc:
        _apply_refund_error(
            refund,
            ProviderRequestError(exc.message, code=exc.code, ambiguous=False),
        )
        raise
    try:
        result = adapter.refund(refund)
    except ProviderRequestError as exc:
        return _apply_refund_error(refund, exc)
    except (ProviderAdapterError, ImproperlyConfigured) as exc:
        return _apply_refund_error(
            refund,
            ProviderRequestError(
                "Provider refund outcome is not yet known.",
                code=exc.__class__.__name__,
                ambiguous=True,
            ),
        )
    return reconcile_refund(refund, result, request_id=getattr(request, "request_id", ""))


def reconcile_pending_refund(refund_id):
    refund = (
        Refund.objects.select_related("merchant", "payment_intent", "payment_attempt", "provider_config")
        .filter(pk=refund_id, status=Refund.STATUS_PENDING)
        .first()
    )
    if not refund:
        return None
    try:
        adapter = _fresh_refund_adapter(refund)
    except APIError:
        return refund
    try:
        result = adapter.retrieve_refund(refund)
    except ProviderRequestError as exc:
        return _apply_refund_error(refund, exc)
    except (ProviderAdapterError, ImproperlyConfigured) as exc:
        return _apply_refund_error(
            refund,
            ProviderRequestError(
                "Provider refund lookup is not yet conclusive.",
                code=exc.__class__.__name__,
                ambiguous=True,
            ),
        )
    return reconcile_refund(refund, result)


def serialize_refund(refund):
    return {
        "id": refund.public_id,
        "object": "refund",
        "payment_intent": refund.payment_intent.public_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "status": refund.status,
        "provider": refund.provider,
        "provider_status": refund.provider_status,
        "failure_code": refund.failure_code,
        "reconciliation_required": refund.status == Refund.STATUS_PENDING,
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
            was_settled = intent.status in {
                PaymentIntent.STATUS_SUCCEEDED,
                PaymentIntent.STATUS_PARTIALLY_REFUNDED,
                PaymentIntent.STATUS_REFUNDED,
            }
            if intent.status not in {PaymentIntent.STATUS_PARTIALLY_REFUNDED, PaymentIntent.STATUS_REFUNDED}:
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
            if not was_settled:
                record_payment_outcome(
                    intent.merchant,
                    succeeded=True,
                    ip_hash=intent.request_ip_hash,
                    amount=intent.amount,
                    provider_risk=payload.get("provider_risk", ""),
                    environment=intent.environment,
                )
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
            settled = intent.status in {
                PaymentIntent.STATUS_SUCCEEDED,
                PaymentIntent.STATUS_PARTIALLY_REFUNDED,
                PaymentIntent.STATUS_REFUNDED,
            }
            method = payload.get("foxpay_method") or PaymentAttempt.METHOD_CRYPTO
            attempts = intent.attempts.filter(method=method)
            attempt_id = payload.get("foxpay_attempt_id")
            if attempt_id:
                attempts = attempts.filter(id=attempt_id)
            for attempt in attempts:
                if attempt.status == PaymentAttempt.STATUS_SUCCEEDED:
                    continue
                attempt.status = PaymentAttempt.STATUS_FAILED
                attempt.provider_status = status
                attempt.failure_code = payload.get("failure_code", "")
                attempt.failure_category = payload.get("failure_category", "")
                attempt.save(update_fields=["status", "provider_status", "failure_code", "failure_category", "updated_at"])
            available = intent.attempts.filter(
                status__in=[PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_ACTION_REQUIRED]
            ).exists()
            if not settled and not available:
                intent.status = PaymentIntent.STATUS_EXPIRED if status == "expired" else PaymentIntent.STATUS_FAILED
                intent.save(update_fields=["status", "updated_at"])
                record_payment_outcome(
                    intent.merchant,
                    succeeded=False,
                    ip_hash=intent.request_ip_hash,
                    amount=intent.amount,
                    provider_risk=payload.get("provider_risk", ""),
                    environment=intent.environment,
                )
            ProviderEvent.objects.create(
                merchant=intent.merchant,
                provider=provider,
                provider_event_id=provider_event_id or f"{provider}:{delivery.id}",
                event_type=payload.get("type", "unknown"),
                normalized_event_type=f"payment_attempt.{status}" if settled or available else f"payment_intent.{intent.status}",
                payment_intent=intent,
                payload=payload,
                processed_at=delivery.updated_at,
            )
            if not settled and not available:
                emit_event(intent.merchant, f"payment_intent.{intent.status}", serialize_intent(intent), idempotency_key=f"payment_intent.{intent.status}:{intent.public_id}")
            delivery.processed = True
            delivery.save(update_fields=["processed", "updated_at"])
            return delivery

    delivery.processed = True
    delivery.save(update_fields=["processed", "updated_at"])
    return delivery
