import json
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.identity import fresh_tg11_mfa

from .adapters.base import ProviderAdapterError
from .merchant_views import _merchant_with_capability, _mfa_step_up, _seller_audit
from .models import AuditLog, MerchantProviderConnection, ProviderConfig, ProviderEvent
from .onboarding import OnboardingStateError, begin_onboarding, consume_onboarding
from .paypal_partner import (
    PAYPAL_FEATURES,
    PAYPAL_MERCHANT_ID_RE,
    PayPalPartnerError,
    apply_seller_status,
    create_partner_referral,
    partner_available,
    partner_credentials,
    partner_webhook_id,
    refresh_partner_connection,
    seller_id_from_event,
    seller_state,
    show_seller_status,
    verify_partner_webhook,
)
from .paypal_events import process_paypal_event


def _safe_event_payload(event, seller_id):
    return {
        "event_type": event["event_type"][:120],
        "seller_merchant_id": seller_id,
    }


@login_required
@require_POST
def begin_paypal_partner(request, slug, environment):
    merchant = _merchant_with_capability(request, slug, "connections")
    if environment not in {"test", "live"} or not partner_available(environment):
        return HttpResponseBadRequest("PayPal partner onboarding is unavailable for this environment.")
    if environment == "live" and not request.is_secure():
        return HttpResponseBadRequest("Live provider onboarding requires HTTPS.")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    session, state = begin_onboarding(
        merchant=merchant,
        user=request.user,
        provider="paypal",
        environment=environment,
        requested_scopes=list(PAYPAL_FEATURES),
        return_path=reverse("seller_section", args=[slug, "providers"]),
    )
    callback = request.build_absolute_uri(reverse("seller_paypal_callback", args=[environment]))
    return_url = f"{callback}?{urlencode({'state': state})}"
    try:
        action_url = create_partner_referral(
            merchant=merchant,
            environment=environment,
            return_url=return_url,
            request_id=f"foxpay-referral-{merchant.uuid}-{session.pk}",
        )
    except PayPalPartnerError as exc:
        session.consumed_at = timezone.now()
        session.metadata = {"tracking_id": str(merchant.uuid), "error_code": str(exc)}
        session.save(update_fields=["consumed_at", "metadata"])
        _seller_audit(
            request,
            merchant,
            "paypal.connect_failed",
            "onboarding_session",
            session.pk,
            {"environment": environment, "reason": str(exc)},
        )
        messages.error(request, "PayPal onboarding could not be started.")
        return redirect(session.return_path)
    session.metadata = {"tracking_id": str(merchant.uuid), "referral_created": True}
    session.save(update_fields=["metadata"])
    _seller_audit(request, merchant, "paypal.connect_started", "onboarding_session", session.pk, {"environment": environment})
    return redirect(action_url)


@login_required
@require_GET
def paypal_partner_callback(request, environment):
    if environment not in {"test", "live"} or not partner_available(environment):
        return HttpResponseBadRequest("PayPal partner onboarding is unavailable for this environment.")
    if environment == "live" and not request.is_secure():
        return HttpResponseBadRequest("Live provider onboarding requires HTTPS.")
    try:
        session = consume_onboarding(
            raw_state=request.GET.get("state", ""),
            user=request.user,
            provider="paypal",
            environment=environment,
        )
    except OnboardingStateError:
        return HttpResponseBadRequest("PayPal connection state is invalid or expired.")
    merchant = _merchant_with_capability(request, session.merchant.slug, "connections")
    tracking_id = request.GET.get("merchantId", "")
    seller_id = request.GET.get("merchantIdInPayPal", "")
    if tracking_id != str(merchant.uuid) or not PAYPAL_MERCHANT_ID_RE.fullmatch(seller_id):
        return HttpResponseBadRequest("PayPal callback identity is invalid.")
    try:
        status_payload = show_seller_status(environment=environment, seller_merchant_id=seller_id)
        seller_state(
            status_payload,
            environment=environment,
            expected_tracking_id=str(merchant.uuid),
            expected_merchant_id=seller_id,
        )
    except PayPalPartnerError as exc:
        _seller_audit(
            request,
            merchant,
            "paypal.connect_failed",
            "onboarding_session",
            session.pk,
            {"environment": environment, "reason": str(exc)},
        )
        messages.error(request, "PayPal did not confirm this seller connection.")
        return redirect(session.return_path)
    try:
        with transaction.atomic():
            connection = MerchantProviderConnection.objects.select_for_update().filter(
                provider="paypal",
                environment=environment,
                external_account_id=seller_id,
            ).first()
            if connection and connection.merchant_id != merchant.pk:
                return HttpResponseForbidden("This PayPal account is already connected to another seller.")
            if not connection:
                connection = MerchantProviderConnection(
                    merchant=merchant,
                    provider="paypal",
                    environment=environment,
                    authorization_method="paypal_partner",
                    external_account_id=seller_id,
                )
            connection.authorization_method = "paypal_partner"
            connection.status = connection.STATUS_PENDING
            connection.connected_at = connection.connected_at or timezone.now()
            connection.revoked_at = None
            connection.clear_tokens()
            connection.save()
        connection, _ = apply_seller_status(connection, status_payload)
    except (IntegrityError, PayPalPartnerError):
        return HttpResponseForbidden("This PayPal account could not be connected.")
    _seller_audit(
        request,
        merchant,
        "paypal.connected",
        "provider_connection",
        connection.uuid,
        {"environment": environment, "status": connection.status},
    )
    if connection.status == connection.STATUS_ACTIVE:
        messages.success(request, "PayPal account connected.")
    else:
        messages.warning(request, "PayPal is connected but has not confirmed every required capability.")
    return redirect(session.return_path)


@login_required
@require_POST
def refresh_paypal_partner(request, slug, connection_uuid):
    merchant = _merchant_with_capability(request, slug, "connections")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    connection = get_object_or_404(
        MerchantProviderConnection,
        uuid=connection_uuid,
        merchant=merchant,
        provider="paypal",
        authorization_method="paypal_partner",
    )
    if connection.status == connection.STATUS_REVOKED:
        return HttpResponseBadRequest("PayPal connection is revoked.")
    try:
        connection, _ = refresh_partner_connection(connection)
    except PayPalPartnerError as exc:
        connection.status = connection.STATUS_ERROR
        connection.last_error_at = timezone.now()
        connection.last_error_code = "paypal_status_failed"
        connection.save(update_fields=["status", "last_error_at", "last_error_code", "updated_at"])
        connection.provider_configs.update(is_active=False)
        _seller_audit(request, merchant, "paypal.health_failed", "provider_connection", connection.uuid, {"reason": str(exc)})
        messages.error(request, "PayPal connection status could not be verified.")
        return redirect("seller_section", slug=slug, section="providers")
    _seller_audit(request, merchant, "paypal.health_verified", "provider_connection", connection.uuid, {"status": connection.status})
    messages.success(request, "PayPal connection status refreshed.")
    return redirect("seller_section", slug=slug, section="providers")


@login_required
@require_POST
def disconnect_paypal_partner(request, slug, connection_uuid):
    merchant = _merchant_with_capability(request, slug, "connections")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        connection = get_object_or_404(
            MerchantProviderConnection.objects.select_for_update(),
            uuid=connection_uuid,
            merchant=merchant,
            provider="paypal",
            authorization_method="paypal_partner",
        )
        connection.status = connection.STATUS_REVOKED
        connection.revoked_at = timezone.now()
        connection.clear_tokens()
        connection.save()
        connection.provider_configs.update(is_active=False)
        _seller_audit(request, merchant, "paypal.disconnected", "provider_connection", connection.uuid)
    messages.success(request, "PayPal routing disconnected. Remove FoxPay consent in PayPal to revoke it provider-side.")
    return redirect("seller_section", slug=slug, section="providers")


@csrf_exempt
@require_POST
def paypal_partner_webhook(request, environment):
    if environment not in {"test", "live"}:
        return HttpResponseBadRequest("Invalid PayPal environment.")
    try:
        partner_credentials(environment)
    except ImproperlyConfigured:
        return JsonResponse({"error": "PayPal partner credentials are not configured."}, status=503)
    if not partner_webhook_id(environment):
        return JsonResponse({"error": "PayPal partner webhook is not configured."}, status=503)
    try:
        event = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid PayPal event.")
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("id"), str)
        or not 0 < len(event["id"]) <= 160
        or not isinstance(event.get("event_type"), str)
        or not 0 < len(event["event_type"]) <= 120
    ):
        return HttpResponseBadRequest("Invalid PayPal event.")
    try:
        verified = verify_partner_webhook(request, environment=environment, event=event)
    except PayPalPartnerError:
        return JsonResponse({"error": "PayPal webhook verification is unavailable."}, status=503)
    if not verified:
        return JsonResponse({"error": "Invalid PayPal signature."}, status=401)
    seller_id = seller_id_from_event(event)
    if not seller_id:
        return HttpResponseBadRequest("PayPal seller merchant ID is missing.")
    connection = MerchantProviderConnection.objects.filter(
        provider="paypal",
        environment=environment,
        external_account_id=seller_id,
        authorization_method="paypal_partner",
    ).first()
    if not connection:
        return JsonResponse({"received": True, "processed": False})
    event_type = event["event_type"]
    payment_event_types = {
        "CHECKOUT.ORDER.APPROVED",
        "CHECKOUT.ORDER.VOIDED",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.DENIED",
        "PAYMENT.CAPTURE.REVERSED",
    }
    if event_type in payment_event_types:
        config = connection.provider_configs.filter(
            kind=ProviderConfig.KIND_CARD,
            adapter="paypal",
            is_active=True,
        ).first()
        if not config or connection.status != connection.STATUS_ACTIVE or connection.revoked_at:
            return JsonResponse({"received": True, "processed": False})
        try:
            result = process_paypal_event(config, event)
        except (ProviderAdapterError, ImproperlyConfigured):
            return JsonResponse({"error": "PayPal payment event could not be processed."}, status=503)
        return JsonResponse({"received": True, **result})
    if event_type not in {"MERCHANT.ONBOARDING.COMPLETED", "MERCHANT.PARTNER-CONSENT.REVOKED"}:
        return JsonResponse({"received": True, "processed": False})
    provider = f"paypal-partner:{connection.pk}"
    with transaction.atomic():
        provider_event, created = ProviderEvent.objects.get_or_create(
            provider=provider,
            provider_event_id=event["id"],
            defaults={
                "merchant": connection.merchant,
                "event_type": event_type,
                "normalized_event_type": "provider_connection.updated",
                "payload": _safe_event_payload(event, seller_id),
            },
        )
        if not created and provider_event.processed_at:
            return JsonResponse({"received": True, "processed": False})
    if event_type == "MERCHANT.PARTNER-CONSENT.REVOKED":
        with transaction.atomic():
            connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
            connection.status = connection.STATUS_REVOKED
            connection.revoked_at = timezone.now()
            connection.save(update_fields=["status", "revoked_at", "updated_at"])
            connection.provider_configs.update(is_active=False)
            provider_event.processed_at = timezone.now()
            provider_event.normalized_event_type = "provider_connection.revoked"
            provider_event.save(update_fields=["processed_at", "normalized_event_type", "updated_at"])
            AuditLog.objects.create(
                merchant=connection.merchant,
                action="paypal.partner_consent_revoked",
                object_type="provider_connection",
                object_id=str(connection.uuid),
            )
        return JsonResponse({"received": True, "processed": True})
    try:
        refresh_partner_connection(connection)
    except PayPalPartnerError:
        return JsonResponse({"error": "PayPal seller status could not be refreshed."}, status=503)
    provider_event.processed_at = timezone.now()
    provider_event.save(update_fields=["processed_at", "updated_at"])
    return JsonResponse({"received": True, "processed": True})
