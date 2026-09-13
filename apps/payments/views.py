import hashlib
import hmac
import json

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .events import emit_event
from .ledger import record_payment_success
from .models import PaymentAttempt, PaymentIntent
from .services import (
    APIError,
    authenticate_merchant,
    create_payment_intent,
    create_refund,
    enforce_rate_limit,
    error_response,
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
                "/api/v1/webhooks/crypto/{provider}/": {
                    "post": {"summary": "Receive normalized provider crypto webhook"}
                },
            },
        }
    )
