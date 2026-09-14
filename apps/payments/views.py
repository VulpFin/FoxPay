import hashlib
import hmac
import json

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .adapters.stripe_checkout import construct_stripe_event, normalize_stripe_event
from .adapters.stripe_methods import record_setup_checkout
from .adapters.base import ProviderAdapterError
from .adapters.paypal_checkout import PayPalCheckoutAdapter, capture_amount
from .adapters.nowpayments import ipn_attempt, normalize_ipn, provider_secret, validate_ipn_amount, verify_ipn
from .adapters.square_webhooks import record_square_event, square_payment_update, verify_square_signature
from .events import emit_event
from .ledger import record_payment_success
from .models import AuditLog, PaymentAttempt, PaymentIntent, ProviderConfig, SubscriptionReference
from .services import (
    APIError,
    authenticate_merchant,
    create_payment_intent,
    create_refund,
    enforce_rate_limit,
    error_response,
    get_or_create_customer,
    record_webhook,
    serialize_intent,
    serialize_refund,
)


def parse_json(request):
    try:
        if not request.body:
            return {}
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise APIError(f"Invalid JSON body: {exc}", status=400)


def api_merchant(request, required_scope=None):
    merchant = authenticate_merchant(request.headers.get("X-FoxPay-Key", ""), required_scope=required_scope)
    enforce_rate_limit(request, merchant=merchant)
    return merchant


@csrf_exempt
@require_POST
def payment_intents(request):
    try:
        merchant = api_merchant(request, required_scope="payments:write")
        intent = create_payment_intent(
            request,
            merchant,
            parse_json(request),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
        return JsonResponse(serialize_intent(intent), status=201)
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), type=exc.type, code=exc.code)
    except Exception as exc:
        if settings.DEBUG:
            return error_response(str(exc), 500, request_id=getattr(request, "request_id", ""))
        return error_response("Unable to create payment intent.", 500, request_id=getattr(request, "request_id", ""))


@csrf_exempt
@require_GET
def payment_intent_detail(request, public_id):
    try:
        merchant = api_merchant(request, required_scope="payments:read")
        intent = get_object_or_404(
            PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice"),
            public_id=public_id,
            merchant=merchant,
        )
        return JsonResponse(serialize_intent(intent))
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), type=exc.type, code=exc.code)


@csrf_exempt
@require_POST
def refunds(request, public_id):
    try:
        merchant = api_merchant(request, required_scope="refunds:write")
        intent = get_object_or_404(PaymentIntent.objects.select_for_update(), public_id=public_id, merchant=merchant)
        refund = create_refund(
            request,
            merchant,
            intent,
            parse_json(request),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
        return JsonResponse(serialize_refund(refund), status=201)
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), type=exc.type, code=exc.code)


@csrf_exempt
@require_POST
def subscription_references(request):
    try:
        merchant = api_merchant(request, required_scope="subscriptions:write")
        payload = parse_json(request)
        if not isinstance(payload, dict):
            raise APIError("Request body must be an object.")
        customer_payload = payload.get("customer")
        if not isinstance(customer_payload, dict) or not customer_payload.get("tg11_user_uuid"):
            raise APIError("customer.tg11_user_uuid is required.")
        provider = payload.get("provider")
        reference = payload.get("provider_reference")
        plan_name = payload.get("plan_name")
        status = payload.get("status")
        allowed_statuses = {"active", "trialing", "past_due", "paused", "canceled", "unpaid", "incomplete", "incomplete_expired"}
        if not isinstance(provider, str) or not 0 < len(provider) <= 40:
            raise APIError("provider must be 1 to 40 characters.")
        if not isinstance(reference, str) or not 0 < len(reference) <= 180:
            raise APIError("provider_reference must be 1 to 180 characters.")
        if not isinstance(plan_name, str) or not 0 < len(plan_name) <= 160:
            raise APIError("plan_name must be 1 to 160 characters.")
        if status not in allowed_statuses:
            raise APIError("status is not supported.")
        amount = payload.get("amount")
        if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or amount < 0):
            raise APIError("amount must be a nonnegative integer or null.")
        currency = payload.get("currency") or ""
        if not isinstance(currency, str) or (currency and (len(currency) != 3 or not currency.isalpha())):
            raise APIError("currency must be a three-letter code.")
        if amount is not None and (not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha()):
            raise APIError("currency must be a three-letter code when amount is set.")
        cancel_at_period_end = payload.get("cancel_at_period_end", False)
        if not isinstance(cancel_at_period_end, bool):
            raise APIError("cancel_at_period_end must be a boolean.")
        period_end = payload.get("current_period_end")
        if period_end:
            period_end = parse_datetime(period_end) if isinstance(period_end, str) else None
            if not period_end or timezone.is_naive(period_end):
                raise APIError("current_period_end must be an ISO 8601 timestamp with timezone.")
        with transaction.atomic():
            customer = get_or_create_customer(merchant, payload)
            existing = SubscriptionReference.objects.select_for_update().filter(
                merchant=merchant, provider=provider, provider_reference=reference
            ).first()
            if existing and existing.customer_id != customer.pk:
                raise APIError("Subscription is already linked to another customer.", status=409)
            subscription, created = SubscriptionReference.objects.update_or_create(
                merchant=merchant,
                provider=provider,
                provider_reference=reference,
                defaults={
                    "customer": customer,
                    "plan_name": plan_name,
                    "status": status,
                    "amount": amount,
                    "currency": currency.upper(),
                    "current_period_end": period_end,
                    "cancel_at_period_end": cancel_at_period_end,
                },
            )
        return JsonResponse({"id": subscription.pk, "created": created}, status=201 if created else 200)
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), type=exc.type, code=exc.code)


@require_GET
def foxpay_checkout(request, client_secret):
    intent = get_object_or_404(
        PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice"),
        client_secret=client_secret,
    )
    return render(request, "payments/checkout.html", {"intent": intent, "attempts": intent.attempts.all()})


@require_http_methods(["GET", "POST"])
def mock_card_checkout(request, client_secret, attempt_id=None):
    intent = get_object_or_404(
        PaymentIntent.objects.prefetch_related("attempts"),
        client_secret=client_secret,
    )
    attempts = intent.attempts.filter(method=PaymentAttempt.METHOD_CARD)
    attempt = attempts.filter(id=attempt_id).first() if attempt_id else attempts.first()
    if request.method == "POST":
        action = request.POST.get("action")
        with transaction.atomic():
            if action == "approve":
                was_succeeded = intent.status == PaymentIntent.STATUS_SUCCEEDED
                intent.status = PaymentIntent.STATUS_SUCCEEDED
                intent.save(update_fields=["status", "updated_at"])
                if attempt:
                    attempt.status = PaymentAttempt.STATUS_SUCCEEDED
                    attempt.save(update_fields=["status", "updated_at"])
                if not was_succeeded:
                    record_payment_success(intent, getattr(request, "request_id", ""))
                    emit_event(intent.merchant, "payment_intent.succeeded", serialize_intent(intent), idempotency_key=f"payment_intent.succeeded:{intent.public_id}")
                if intent.success_url:
                    return redirect(intent.success_url)
            elif action == "fail":
                intent.status = PaymentIntent.STATUS_FAILED
                intent.save(update_fields=["status", "updated_at"])
                if attempt:
                    attempt.status = PaymentAttempt.STATUS_FAILED
                    attempt.save(update_fields=["status", "updated_at"])
                if intent.cancel_url:
                    return redirect(intent.cancel_url)
            else:
                return HttpResponseBadRequest("Unknown checkout action.")
    return render(request, "payments/checkout_card.html", {"intent": intent, "attempt": attempt})


@require_GET
def crypto_checkout(request, client_secret, attempt_id=None):
    intent = get_object_or_404(
        PaymentIntent.objects.prefetch_related("attempts", "attempts__crypto_invoice"),
        client_secret=client_secret,
    )
    attempts = intent.attempts.filter(method=PaymentAttempt.METHOD_CRYPTO)
    attempt = attempts.filter(id=attempt_id).first() if attempt_id else attempts.first()
    invoice = getattr(attempt, "crypto_invoice", None) if attempt else None
    return render(request, "payments/checkout_crypto.html", {"intent": intent, "attempt": attempt, "invoice": invoice})


def valid_webhook_signature(request):
    secret = settings.FOXPAY_WEBHOOK_SECRET
    if not secret or secret == "change-me-for-provider-webhooks":
        return settings.DEBUG
    signature = request.headers.get("X-FoxPay-Signature", "")
    digest = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, digest)


@csrf_exempt
@require_POST
def crypto_webhook(request, provider):
    if not valid_webhook_signature(request):
        return error_response("Invalid webhook signature.", 401, request_id=getattr(request, "request_id", ""), type="authentication_error", code="invalid_signature")
    try:
        delivery = record_webhook(provider, parse_json(request))
        return JsonResponse({"received": True, "processed": delivery.processed})
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), type=exc.type, code=exc.code)


@csrf_exempt
@require_POST
def stripe_webhook(request, provider="stripe"):
    try:
        config, event = construct_stripe_event(request, provider=provider)
    except ImproperlyConfigured as exc:
        return error_response(str(exc), 500, request_id=getattr(request, "request_id", ""), code="provider_not_configured")
    except Exception:
        return error_response("Invalid Stripe webhook signature.", 401, request_id=getattr(request, "request_id", ""), type="authentication_error", code="invalid_signature")
    if event.get("account"):
        return error_response("Connect events require the Connect webhook endpoint.", 400, request_id=getattr(request, "request_id", ""), code="wrong_webhook_endpoint")
    try:
        if event.get("type") == "checkout.session.completed" and event.get("data", {}).get("object", {}).get("mode") == "setup":
            if not config:
                return error_response("Stripe setup webhook has no provider configuration.", 503, request_id=getattr(request, "request_id", ""), code="provider_not_configured")
            processed = record_setup_checkout(config, event)
            return JsonResponse({"received": True, "processed": processed})
        delivery = record_webhook(provider, normalize_stripe_event(event))
        return JsonResponse({"received": True, "processed": delivery.processed})
    except Exception:
        return error_response("Stripe webhook could not be processed.", 503, request_id=getattr(request, "request_id", ""), code="webhook_processing_failed")


@csrf_exempt
@require_POST
def nowpayments_ipn(request, merchant_slug, provider):
    config = ProviderConfig.objects.filter(
        merchant__slug=merchant_slug,
        environment=settings.FOXPAY_ENV,
        kind=ProviderConfig.KIND_CRYPTO,
        adapter="nowpayments",
        provider=provider,
    ).first()
    if not config:
        return error_response("NOWPayments provider not found.", 404, request_id=getattr(request, "request_id", ""), code="provider_not_found")
    secret = provider_secret(config, "ipn_secret")
    if not secret:
        return error_response("NOWPayments IPN secret is not configured.", 503, request_id=getattr(request, "request_id", ""), code="provider_not_configured")
    try:
        payload = parse_json(request)
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), code=exc.code)
    if not isinstance(payload, dict) or not verify_ipn(payload, request.headers.get("X-Nowpayments-Sig", ""), secret):
        return error_response("Invalid NOWPayments IPN signature.", 401, request_id=getattr(request, "request_id", ""), type="authentication_error", code="invalid_signature")
    attempt = ipn_attempt(config, payload)
    if not attempt:
        return error_response("NOWPayments invoice not found.", 404, request_id=getattr(request, "request_id", ""), code="invoice_not_found")
    if not payload.get("payment_id") or not validate_ipn_amount(attempt, payload):
        return error_response("NOWPayments payment details do not match the invoice.", 422, request_id=getattr(request, "request_id", ""), code="invoice_mismatch")
    try:
        delivery = record_webhook(provider, normalize_ipn(attempt, payload))
    except APIError as exc:
        return error_response(exc.message, exc.status, request_id=getattr(request, "request_id", ""), code=exc.code)
    attempt.refresh_from_db()
    attempt.provider_status = str(payload.get("payment_status", ""))[:80]
    attempt.provider_response_metadata = {
        **attempt.provider_response_metadata,
        "nowpayments_payment_id": str(payload["payment_id"]),
    }
    attempt.save(update_fields=["provider_status", "provider_response_metadata", "updated_at"])
    return JsonResponse({"received": True, "processed": delivery.processed})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def square_webhook(request, merchant_slug, provider):
    config = ProviderConfig.objects.filter(
        merchant__slug=merchant_slug,
        environment=settings.FOXPAY_ENV,
        kind=ProviderConfig.KIND_CARD,
        adapter="square",
        provider=provider,
    ).first()
    if not config:
        return error_response("Square webhook provider not found.", 404, request_id=getattr(request, "request_id", ""), code="provider_not_found")
    notification_url = config.settings.get("webhook_notification_url", "")
    signature_key = provider_secret(config, "webhook_signature_key")
    if request.method == "GET":
        return JsonResponse({"receiver": "square", "ready": bool(notification_url and signature_key)})
    if not notification_url or not signature_key:
        return error_response("Square webhook signature key is not configured.", 503, request_id=getattr(request, "request_id", ""), code="provider_not_configured")
    if not verify_square_signature(
        request.body,
        request.headers.get("X-Square-HmacSha256-Signature", ""),
        signature_key,
        notification_url,
    ):
        return error_response("Invalid Square webhook signature.", 401, request_id=getattr(request, "request_id", ""), type="authentication_error", code="invalid_signature")
    try:
        event = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response("Invalid Square webhook JSON.", 400, request_id=getattr(request, "request_id", ""), code="invalid_event")
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("event_id"), str)
        or not 0 < len(event["event_id"]) <= 160
        or not isinstance(event.get("type"), str)
        or not 0 < len(event["type"]) <= 120
    ):
        return error_response("Square webhook event_id and type are required.", 400, request_id=getattr(request, "request_id", ""), code="invalid_event")
    with transaction.atomic():
        recorded = record_square_event(config, event)
        update, error = square_payment_update(config, event)
        if error and recorded:
            AuditLog.objects.create(
                merchant=config.merchant,
                action="square.webhook.reconciliation_skipped",
                object_type="provider_event",
                object_id=event["event_id"][:120],
                metadata={"reason": error},
            )
        if update:
            record_webhook(f"square-payment:{config.pk}", update)
    return JsonResponse({"received": True, "recorded": recorded, "reconciled": bool(update)})



@csrf_exempt
@require_http_methods(["GET", "POST"])
def paypal_webhook(request, merchant_slug, provider):
    config = ProviderConfig.objects.filter(
        merchant__slug=merchant_slug,
        environment=settings.FOXPAY_ENV,
        kind=ProviderConfig.KIND_CARD,
        adapter="paypal",
        provider=provider,
    ).first()
    if not config:
        return error_response("PayPal webhook provider not found.", 404, request_id=getattr(request, "request_id", ""), code="provider_not_found")
    adapter = PayPalCheckoutAdapter(config)
    if request.method == "GET":
        return JsonResponse({"receiver": "paypal", "ready": bool(adapter.webhook_id())})
    if not adapter.webhook_id():
        return error_response("PayPal webhook id is not configured.", 503, request_id=getattr(request, "request_id", ""), code="provider_not_configured")
    if not adapter.verify_webhook(request):
        return error_response("Invalid PayPal webhook signature.", 401, request_id=getattr(request, "request_id", ""), type="authentication_error", code="invalid_signature")
    try:
        event = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response("Invalid PayPal webhook JSON.", 400, request_id=getattr(request, "request_id", ""), code="invalid_event")
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("id"), str)
        or not 0 < len(event["id"]) <= 160
        or not isinstance(event.get("event_type"), str)
        or not 0 < len(event["event_type"]) <= 120
    ):
        return error_response("PayPal webhook id and event_type are required.", 400, request_id=getattr(request, "request_id", ""), code="invalid_event")

    normalized = adapter.normalize_webhook(event)
    captured = False
    if normalized.get("status") == "approved" and normalized.get("provider_reference"):
        try:
            order = adapter.capture(normalized["provider_reference"])
        except (ProviderAdapterError, ImproperlyConfigured) as exc:
            AuditLog.objects.create(
                merchant=config.merchant,
                action="paypal.webhook.capture_failed",
                object_type="provider_event",
                object_id=event["id"][:120],
                metadata={"reason": str(exc)},
            )
            return error_response("PayPal capture could not be completed.", 503, request_id=getattr(request, "request_id", ""), code="capture_failed")
        settlement = adapter.settlement_event(order, event_id=f"{event['id']}:capture")
        if settlement:
            captured = True
            event = settlement
            normalized = adapter.normalize_webhook(settlement)

    with transaction.atomic():
        if normalized.get("status") == "succeeded":
            intent = PaymentIntent.objects.filter(public_id=normalized.get("payment_intent", "")).first()
            minor, currency = capture_amount(event.get("resource") or {})
            if intent and minor is not None and (minor != intent.amount or currency != (intent.currency or "").upper()):
                AuditLog.objects.create(
                    merchant=config.merchant,
                    action="paypal.webhook.amount_mismatch",
                    object_type="payment_intent",
                    object_id=intent.public_id,
                    metadata={
                        "expected": intent.amount,
                        "expected_currency": intent.currency,
                        "seen": minor,
                        "seen_currency": currency,
                    },
                )
                normalized["status"] = "amount_mismatch"
        delivery = record_webhook(f"paypal:{config.pk}", normalized)
    return JsonResponse({"received": True, "processed": delivery.processed, "captured": captured})


@staff_member_required
def dashboard(request):
    intents = PaymentIntent.objects.select_related("merchant").prefetch_related("attempts").order_by("-created_at")[:50]
    return render(request, "payments/dashboard.html", {"intents": intents})


@require_GET
def openapi(request):
    base_url = request.build_absolute_uri("/").rstrip("/")
    return JsonResponse(
        {
            "openapi": "3.1.0",
            "info": {"title": "Fox Pay API", "version": "1.0.0"},
            "servers": [{"url": base_url}],
            "paths": {
                "/api/v1/payment-intents/": {
                    "post": {
                        "summary": "Create a payment intent",
                        "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False}],
                    }
                },
                "/api/v1/payment-intents/{id}/": {
                    "get": {"summary": "Retrieve a payment intent"}
                },
                "/api/v1/payment-intents/{id}/refunds/": {
                    "post": {
                        "summary": "Create a refund",
                        "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False}],
                    }
                },
                "/api/v1/subscription-references/": {
                    "post": {"summary": "Upsert a merchant-owned subscription display reference"}
                },
                "/api/v1/webhooks/crypto/{provider}/": {
                    "post": {"summary": "Receive normalized provider crypto webhook"}
                },
                "/api/v1/webhooks/stripe/{provider}/": {
                    "post": {"summary": "Receive Stripe Checkout webhook events"}
                },
                "/api/v1/webhooks/nowpayments/{merchant_slug}/{provider}/": {
                    "post": {"summary": "Receive signed NOWPayments IPN events"}
                },
                "/api/v1/webhooks/square/{merchant_slug}/{provider}/": {
                    "post": {"summary": "Record signed Square webhook events"}
                },
                "/api/v1/webhooks/paypal/{merchant_slug}/{provider}/": {
                    "post": {"summary": "Capture and settle signed PayPal Orders v2 events"}
                },
            },
        }
    )
