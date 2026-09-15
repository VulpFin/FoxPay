import json

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.identity import fresh_tg11_mfa

from .adapters.square_webhooks import (
    record_square_event,
    square_dispute_update,
    square_payment_update,
    square_refund_update,
    verify_square_signature,
)
from .merchant_views import _merchant_with_capability, _mfa_step_up, _seller_audit
from .models import AuditLog, MerchantProviderConnection, ProviderConfig, ProviderEvent
from .onboarding import OnboardingStateError, begin_onboarding, consume_onboarding
from .services import record_webhook
from .square_oauth import SQUARE_SCOPES, authorize_url, list_usable_locations, oauth_available, obtain_token, revoke_authorization, token_status, webhook_settings


def _location_is_ready(connection):
    key, url = webhook_settings(connection.environment)
    return bool(key and url.startswith("https://"))


def _merchant_for_connection(request, slug):
    return _merchant_with_capability(request, slug, "connections")


def _configure_location(connection, location):
    config = ProviderConfig.objects.filter(connection=connection, kind=ProviderConfig.KIND_CARD).first()
    if not config:
        config = ProviderConfig(
            merchant=connection.merchant, connection=connection, kind=ProviderConfig.KIND_CARD,
            provider=f"square-{connection.uuid.hex}", adapter="square", environment=connection.environment,
            display_name="Square",
        )
    config.settings = {
        "location_id": location["id"], "location_currency": location["currency"],
        "square_merchant_id": connection.external_account_id,
    }
    config.is_active = _location_is_ready(connection)
    config.capabilities = ["card", "debit", "hosted_checkout", "refunds", "partial_refunds", "disputes"]
    config.save()
    connection.status = connection.STATUS_ACTIVE if config.is_active else connection.STATUS_RESTRICTED
    connection.last_error_code = "" if config.is_active else "webhook_not_configured"
    connection.save(update_fields=["status", "last_error_code", "updated_at"])
    return config


@login_required
@require_POST
def begin_square_oauth(request, slug, environment):
    merchant = _merchant_for_connection(request, slug)
    if environment not in {"test", "live"} or not oauth_available(environment):
        return HttpResponseBadRequest("Square OAuth is unavailable for this environment.")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    callback = request.build_absolute_uri(reverse("seller_square_callback", args=[environment]))
    session, state = begin_onboarding(
        merchant=merchant, user=request.user, provider="square", environment=environment,
        requested_scopes=list(SQUARE_SCOPES), return_path=reverse("seller_section", args=[slug, "providers"]),
    )
    url = authorize_url(environment=environment, state=state, redirect_uri=callback)
    _seller_audit(request, merchant, "square.connect_started", "onboarding_session", session.pk, {"environment": environment})
    return redirect(url)


@login_required
@require_GET
def square_oauth_callback(request, environment):
    if environment not in {"test", "live"} or not oauth_available(environment):
        return HttpResponseBadRequest("Square OAuth is unavailable for this environment.")
    try:
        session = consume_onboarding(
            raw_state=request.GET.get("state", ""),
            user=request.user,
            provider="square",
            environment=environment,
        )
    except OnboardingStateError:
        return HttpResponseBadRequest("Square connection state is invalid or expired.")
    merchant = _merchant_for_connection(request, session.merchant.slug)
    if request.GET.get("error"):
        _seller_audit(request, merchant, "square.connect_cancelled", "onboarding_session", session.pk)
        messages.error(request, "Square connection was not completed.")
        return redirect(session.return_path)
    code = request.GET.get("code", "")
    if not code or len(code) > 512:
        return HttpResponseBadRequest("Square authorization code is missing.")
    callback = request.build_absolute_uri(reverse("seller_square_callback", args=[environment]))
    try:
        token, expires_at = obtain_token(
            environment=environment,
            grant_type="authorization_code",
            value=code,
            redirect_uri=callback,
        )
    except (requests.RequestException, ValueError):
        _seller_audit(request, merchant, "square.connect_failed", "onboarding_session", session.pk, {"reason": "token_exchange_failed"})
        messages.error(request, "Square connection could not be completed. Please retry.")
        return redirect(session.return_path)
    merchant_id = token["merchant_id"]
    try:
        with transaction.atomic():
            connection = MerchantProviderConnection.objects.select_for_update().filter(provider="square", environment=environment, external_account_id=merchant_id).first()
            if connection and connection.merchant_id != merchant.pk:
                return HttpResponseForbidden("This Square account is already connected to another seller.")
            if not connection:
                connection = MerchantProviderConnection(merchant=merchant, provider="square", environment=environment, authorization_method="square_oauth", external_account_id=merchant_id)
            connection.status = connection.STATUS_PENDING
            connection.authorization_method = "square_oauth"
            connection.revoked_at = None
            connection.connected_at = timezone.now()
            connection.token_refreshed_at = timezone.now()
            connection.token_expires_at = expires_at
            connection.set_access_token(token["access_token"])
            connection.set_refresh_token(token["refresh_token"])
            connection.save()
    except IntegrityError:
        return HttpResponseForbidden("This Square account is already connected elsewhere.")
    try:
        introspection = token_status(environment=environment, access_token=token["access_token"])
        if introspection.get("merchant_id") != merchant_id or not set(SQUARE_SCOPES).issubset(set(introspection["scopes"])):
            raise ValueError("Square scopes or merchant ID did not match.")
        locations = list_usable_locations(environment=environment, access_token=token["access_token"], merchant_id=merchant_id)
    except (requests.RequestException, ValueError):
        connection.status = connection.STATUS_ERROR
        connection.last_error_at = timezone.now()
        connection.last_error_code = "square_verification_failed"
        connection.save(update_fields=["status", "last_error_at", "last_error_code", "updated_at"])
        connection.provider_configs.update(is_active=False)
        messages.error(request, "Square connection needs to be retried.")
        return redirect(session.return_path)
    with transaction.atomic():
        connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
        connection.granted_scopes = introspection["scopes"]
        connection.last_verified_at = timezone.now()
        connection.last_error_at = None
        connection.last_error_code = ""
        connection.metadata = {"locations": locations}
        if len(locations) == 1:
            _configure_location(connection, locations[0])
        elif not locations:
            connection.status = connection.STATUS_RESTRICTED
            connection.last_error_code = "no_usable_location"
            connection.save()
            connection.provider_configs.update(is_active=False)
        else:
            connection.status = connection.STATUS_PENDING
            connection.save()
            connection.provider_configs.update(is_active=False)
        _seller_audit(request, merchant, "square.connected", "provider_connection", connection.uuid, {"environment": environment, "status": connection.status})
    messages.success(request, "Square account connected." if len(locations) == 1 else "Square account connected. Select a location before routing payments.")
    return redirect(session.return_path)


@login_required
@require_POST
def select_square_location(request, slug, connection_uuid):
    merchant = _merchant_for_connection(request, slug)
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    location_id = request.POST.get("location_id", "")
    with transaction.atomic():
        connection = get_object_or_404(MerchantProviderConnection.objects.select_for_update(), uuid=connection_uuid, merchant=merchant, provider="square", authorization_method="square_oauth")
        if connection.status == connection.STATUS_REVOKED:
            return HttpResponseBadRequest("Square connection is revoked.")
        location = next((item for item in connection.metadata.get("locations", []) if item.get("id") == location_id), None)
        if not location:
            return HttpResponseBadRequest("Select a usable Square location.")
        _configure_location(connection, location)
        _seller_audit(request, merchant, "square.location_selected", "provider_connection", connection.uuid, {"location_id": location_id})
    messages.success(request, "Square location selected.")
    return redirect("seller_section", slug=slug, section="providers")


@login_required
@require_POST
def disconnect_square_oauth(request, slug, connection_uuid):
    merchant = _merchant_for_connection(request, slug)
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        connection = get_object_or_404(MerchantProviderConnection.objects.select_for_update(), uuid=connection_uuid, merchant=merchant, provider="square", authorization_method="square_oauth")
        if connection.status == connection.STATUS_REVOKED:
            return redirect("seller_section", slug=slug, section="providers")
        try:
            revoke_authorization(environment=connection.environment, merchant_id=connection.external_account_id)
        except (requests.RequestException, ValueError):
            messages.error(request, "Square could not confirm disconnection. Please retry.")
            return redirect("seller_section", slug=slug, section="providers")
        connection.status = connection.STATUS_REVOKED
        connection.revoked_at = timezone.now()
        connection.clear_tokens()
        connection.save()
        connection.provider_configs.update(is_active=False)
        _seller_audit(request, merchant, "square.disconnected", "provider_connection", connection.uuid)
    messages.success(request, "Square account disconnected.")
    return redirect("seller_section", slug=slug, section="providers")


@csrf_exempt
@require_POST
def square_oauth_webhook(request, environment):
    if environment not in {"test", "live"}:
        return HttpResponseBadRequest("Invalid Square environment.")
    key, url = webhook_settings(environment)
    if not key or not url:
        return JsonResponse({"error": "Square webhook is not configured."}, status=503)
    if not verify_square_signature(request.body, request.headers.get("X-Square-HmacSha256-Signature", ""), key, url):
        return JsonResponse({"error": "Invalid Square signature."}, status=401)
    try:
        event = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid Square event.")
    if not isinstance(event, dict) or not isinstance(event.get("event_id"), str) or not 0 < len(event["event_id"]) <= 160 or not isinstance(event.get("type"), str):
        return HttpResponseBadRequest("Invalid Square event.")
    merchant_id = event.get("merchant_id")
    if not isinstance(merchant_id, str) or not merchant_id:
        return HttpResponseBadRequest("Square merchant ID is missing.")
    connection = MerchantProviderConnection.objects.filter(provider="square", environment=environment, external_account_id=merchant_id, authorization_method="square_oauth").first()
    if not connection:
        return JsonResponse({"received": True, "processed": False})
    if event["type"] == "oauth.authorization.revoked":
        with transaction.atomic():
            provider = f"square-oauth:{connection.pk}"
            _, created = ProviderEvent.objects.get_or_create(
                provider=provider, provider_event_id=event["event_id"],
                defaults={"merchant": connection.merchant, "event_type": event["type"], "normalized_event_type": "provider_connection.revoked", "payload": {"merchant_id": merchant_id}, "processed_at": timezone.now()},
            )
            if created:
                connection.status = connection.STATUS_REVOKED
                connection.revoked_at = timezone.now()
                connection.clear_tokens()
                connection.save()
                connection.provider_configs.update(is_active=False)
                AuditLog.objects.create(merchant=connection.merchant, action="square.authorization_revoked", object_type="provider_connection", object_id=str(connection.uuid))
        return JsonResponse({"received": True, "processed": created})
    config = connection.provider_configs.filter(kind=ProviderConfig.KIND_CARD).first()
    if not config:
        return JsonResponse({"received": True, "processed": False})
    with transaction.atomic():
        recorded = record_square_event(config, event)
        update, error = square_payment_update(config, event)
        if error and recorded:
            AuditLog.objects.create(merchant=connection.merchant, action="square.webhook.reconciliation_skipped", object_type="provider_event", object_id=event["event_id"], metadata={"reason": error})
        if update:
            record_webhook(f"square-payment:{config.pk}", update)
        refund, refund_error = square_refund_update(config, event)
        dispute, dispute_error = square_dispute_update(config, event)
        for action, reason in (
            ("square.refund_reconciliation_skipped", refund_error),
            ("square.dispute_reconciliation_skipped", dispute_error),
        ):
            if reason and recorded:
                AuditLog.objects.create(
                    merchant=connection.merchant,
                    action=action,
                    object_type="provider_event",
                    object_id=event["event_id"][:120],
                    metadata={"reason": reason},
                )
    return JsonResponse({
        "received": True,
        "recorded": recorded,
        "reconciled": bool(update or refund or dispute),
    })
