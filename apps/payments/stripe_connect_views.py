from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.identity import fresh_tg11_mfa

from .adapters.stripe_checkout import object_to_dict, stripe_module
from .merchant_views import _merchant_with_capability, _seller_audit
from .models import MerchantProviderConnection, ProviderConfig
from .onboarding import OnboardingStateError, begin_onboarding, consume_onboarding
from .stripe_connect import authorize_url, connect_available, disconnect_account, exchange_code, verify_account
from .stripe_connect_events import handle_connect_event


def _merchant_for_connection(request, slug):
    return _merchant_with_capability(request, slug, "connections")


@login_required
@require_POST
def begin_stripe_connect(request, slug, environment):
    merchant = _merchant_for_connection(request, slug)
    if environment not in {"test", "live"} or not connect_available(environment):
        return HttpResponseBadRequest("Stripe Connect is unavailable for this environment.")
    if not fresh_tg11_mfa(request):
        return render(request, "accounts/step_up.html", {"action_name": "Connecting a payment provider", "next_path": f"/seller/{slug}/providers/"}, status=403)
    callback = request.build_absolute_uri(reverse("seller_stripe_callback", args=[slug, environment]))
    session, state = begin_onboarding(
        merchant=merchant, user=request.user, provider="stripe", environment=environment,
        requested_scopes=["read_write"], return_path=reverse("seller_section", args=[slug, "providers"]),
    )
    url = authorize_url(environment=environment, state=state, redirect_uri=callback)
    _seller_audit(request, merchant, "stripe.connect_started", "onboarding_session", session.pk, {"environment": environment})
    return redirect(url)


@login_required
@require_GET
def stripe_connect_callback(request, slug, environment):
    merchant = _merchant_for_connection(request, slug)
    if environment not in {"test", "live"} or not connect_available(environment):
        return HttpResponseBadRequest("Stripe Connect is unavailable for this environment.")
    try:
        session = consume_onboarding(
            raw_state=request.GET.get("state", ""), merchant=merchant, user=request.user,
            provider="stripe", environment=environment,
        )
    except OnboardingStateError:
        return HttpResponseBadRequest("Stripe connection state is invalid or expired.")
    if request.GET.get("error"):
        _seller_audit(request, merchant, "stripe.connect_cancelled", "onboarding_session", session.pk)
        messages.error(request, "Stripe connection was not completed.")
        return redirect(session.return_path)
    code = request.GET.get("code", "")
    if not code or len(code) > 512:
        return HttpResponseBadRequest("Stripe authorization code is missing.")
    try:
        account_id = exchange_code(environment=environment, code=code)
        state = verify_account(environment=environment, account_id=account_id)
    except Exception:
        _seller_audit(request, merchant, "stripe.connect_failed", "onboarding_session", session.pk, {"reason": "provider_verification_failed"})
        messages.error(request, "Stripe connection could not be verified. Please retry.")
        return redirect(session.return_path)
    try:
        with transaction.atomic():
            connection = MerchantProviderConnection.objects.select_for_update().filter(
                provider="stripe", environment=environment, external_account_id=account_id,
            ).first()
            if connection and connection.merchant_id != merchant.pk:
                return HttpResponseForbidden("This Stripe account is already connected to another seller.")
            if not connection:
                connection = MerchantProviderConnection(
                    merchant=merchant, provider="stripe", environment=environment,
                    authorization_method="stripe_connect", external_account_id=account_id,
                )
            connection.status = connection.STATUS_ACTIVE if state["ready"] else connection.STATUS_RESTRICTED
            connection.authorization_method = "stripe_connect"
            connection.revoked_at = None
            connection.connected_at = timezone.now()
            connection.last_verified_at = timezone.now()
            connection.last_error_at = None
            connection.last_error_code = ""
            connection.granted_scopes = ["read_write"]
            connection.capabilities = state["capabilities"]
            connection.metadata = {"country": state["country"], "charges_enabled": state["ready"]}
            connection.clear_tokens()
            connection.save()
            config = ProviderConfig.objects.filter(connection=connection, kind=ProviderConfig.KIND_CARD).first()
            if not config:
                config = ProviderConfig(
                    merchant=merchant, connection=connection, kind=ProviderConfig.KIND_CARD,
                    provider=f"stripe-{connection.uuid.hex}", adapter="stripe", environment=environment,
                    display_name="Stripe", settings={"payment_method_types": ["card"]},
                )
            config.is_active = state["ready"]
            config.capabilities = [
                "card",
                "debit",
                "wallet",
                "hosted_checkout",
                "refunds",
                "partial_refunds",
                "disputes",
            ] if state["ready"] else []
            config.save()
            _seller_audit(request, merchant, "stripe.connected", "provider_connection", connection.uuid, {"environment": environment, "status": connection.status})
    except IntegrityError:
        return HttpResponseForbidden("This Stripe account is already connected elsewhere.")
    messages.success(request, "Stripe account connected." if state["ready"] else "Stripe account connected; payments will be available when Stripe activates card payments.")
    return redirect(session.return_path)


@login_required
@require_POST
def disconnect_stripe_connect(request, slug, connection_uuid):
    merchant = _merchant_for_connection(request, slug)
    if not fresh_tg11_mfa(request):
        return render(request, "accounts/step_up.html", {"action_name": "Disconnecting a payment provider", "next_path": f"/seller/{slug}/providers/"}, status=403)
    with transaction.atomic():
        connection = get_object_or_404(
            MerchantProviderConnection.objects.select_for_update(), uuid=connection_uuid, merchant=merchant,
            provider="stripe", authorization_method="stripe_connect",
        )
        if connection.status == connection.STATUS_REVOKED:
            return redirect("seller_section", slug=slug, section="providers")
        try:
            disconnect_account(environment=connection.environment, account_id=connection.external_account_id)
        except Exception:
            messages.error(request, "Stripe could not confirm disconnection. Please retry.")
            return redirect("seller_section", slug=slug, section="providers")
        connection.status = connection.STATUS_REVOKED
        connection.revoked_at = timezone.now()
        connection.clear_tokens()
        connection.save()
        connection.provider_configs.update(is_active=False)
        _seller_audit(request, merchant, "stripe.disconnected", "provider_connection", connection.uuid)
    messages.success(request, "Stripe account disconnected.")
    return redirect("seller_section", slug=slug, section="providers")


@csrf_exempt
@require_POST
def stripe_connect_webhook(request, endpoint_environment):
    if endpoint_environment not in {"test", "live"}:
        return HttpResponseBadRequest("Invalid Stripe endpoint environment.")
    secret = settings.STRIPE_CONNECT_WEBHOOK_SECRET_LIVE if endpoint_environment == "live" else settings.STRIPE_CONNECT_WEBHOOK_SECRET_TEST
    if not secret:
        return JsonResponse({"error": "Connect webhook is not configured."}, status=503)
    try:
        event = stripe_module().Webhook.construct_event(request.body, request.headers.get("Stripe-Signature", ""), secret)
        event = object_to_dict(event) or event
    except Exception:
        return JsonResponse({"error": "Invalid Stripe Connect signature."}, status=401)
    if not isinstance(event, dict) or not isinstance(event.get("livemode"), bool):
        return HttpResponseBadRequest("Invalid Stripe Connect event.")
    if endpoint_environment == "test" and event["livemode"]:
        return HttpResponseBadRequest("Live event sent to test endpoint.")
    environment = "live" if event["livemode"] else "test"
    account_id = event.get("account", "")
    if not isinstance(account_id, str) or not account_id.startswith("acct_"):
        return HttpResponseBadRequest("Connected account is missing.")
    connection = MerchantProviderConnection.objects.filter(
        provider="stripe", environment=environment, external_account_id=account_id,
        authorization_method="stripe_connect",
    ).first()
    if not connection:
        return JsonResponse({"received": True, "processed": False})
    try:
        processed = handle_connect_event(connection, event)
    except ValueError:
        return JsonResponse({"error": "Stripe event does not match this connection."}, status=422)
    except Exception:
        return JsonResponse({"error": "Stripe event could not be processed."}, status=503)
    return JsonResponse({"received": True, "processed": processed})
