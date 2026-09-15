import secrets
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.identity import fresh_tg11_mfa, linked_subject, tg11_mfa_step_up
from tg11_auth.models import TG11IdentityLink

from .agreements import agreement_snapshot
from .merchant_applications import MerchantApplicationForm, create_merchant_application
from .events import emit_event
from .models import APIKey, AuditLog, Dispute, Merchant, MerchantAllowedReturnOrigin, MerchantWebhookEndpoint, MerchantWebhookAttempt, MerchantMembership, PaymentIntent, Refund
from .permissions import can_manage, member_merchants, merchant_membership, routing_block_reason
from .safe_urls import UnsafeURL, parsed_public_url, resolve_public_target, return_origin


@require_GET
def agreement(request):
    return render(request, "merchants/agreement.html", agreement_snapshot())


@login_required
@require_http_methods(["GET", "POST"])
def apply(request):
    subject = linked_subject(request.user)
    if not subject:
        return HttpResponseForbidden("A linked TG11 account is required.")
    if not settings.FOXPAY_SELLER_SIGNUP_ENABLED:
        return render(request, "merchants/signup_paused.html", status=503)
    form = MerchantApplicationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        merchant = create_merchant_application(request, subject, form.cleaned_data)
        messages.success(request, "Application submitted for review.")
        return redirect("seller_detail", slug=merchant.slug)
    return render(request, "merchants/apply.html", {"form": form, **agreement_snapshot()})


@login_required
@require_GET
def index(request):
    if not linked_subject(request.user):
        return HttpResponseForbidden("A linked TG11 account is required.")
    merchants = member_merchants(request.user).order_by("name")
    return render(request, "merchants/index.html", {"merchants": merchants, "signup_enabled": settings.FOXPAY_SELLER_SIGNUP_ENABLED})


def _render_detail(request, slug, section="overview", one_time_secret=""):
    if not linked_subject(request.user):
        return HttpResponseForbidden("A linked TG11 account is required.")
    merchant = get_object_or_404(member_merchants(request.user), slug=slug)
    membership = merchant_membership(request.user, merchant)
    sections = {"overview", "providers", "payments", "disputes", "keys", "webhooks", "members", "settings", "audit"}
    if section not in sections:
        return HttpResponseBadRequest("Unknown seller section.")
    if section == "keys" and not can_manage(membership, "keys"):
        raise PermissionDenied
    if section == "webhooks" and not can_manage(membership, "webhooks"):
        raise PermissionDenied
    if section == "members" and not can_manage(membership, "members"):
        raise PermissionDenied
    if section == "settings" and not can_manage(membership, "settings"):
        raise PermissionDenied
    context = {
        "merchant": merchant,
        "membership": membership,
        "section": section,
        "section_template": f"merchants/sections/{section}.html",
        "one_time_secret": one_time_secret,
        "can_keys": can_manage(membership, "keys"),
        "can_webhooks": can_manage(membership, "webhooks"),
        "can_members": can_manage(membership, "members"),
        "can_settings": can_manage(membership, "settings"),
        "can_refunds": can_manage(membership, "refunds"),
        "can_connections": can_manage(membership, "connections"),
    }
    if section == "overview":
        context["payment_count"] = merchant.payment_intents.count()
        context["dispute_count"] = merchant.disputes.count()
        context["connection_count"] = merchant.provider_connections.filter(status="active").count()
    elif section == "providers":
        from .nowpayments_connection import self_service_available
        from .paypal_partner import partner_available
        from .stripe_connect import connect_available
        from .square_oauth import oauth_available

        connections = list(
            merchant.provider_connections.prefetch_related("provider_configs")
            .order_by("provider", "environment", "created_at")
        )
        for connection in connections:
            if connection.authorization_method != "nowpayments_credentials":
                continue
            config = next((item for item in connection.provider_configs.all() if item.adapter_name == "nowpayments"), None)
            if config:
                connection.dashboard_config = config
                connection.dashboard_ipn_url = request.build_absolute_uri(
                    reverse("payments:nowpayments_ipn", args=[merchant.slug, config.provider])
                )
        context["connections"] = connections
        context["stripe_connect_test_available"] = connect_available("test")
        context["stripe_connect_live_available"] = connect_available("live")
        context["square_oauth_test_available"] = oauth_available("test")
        context["square_oauth_live_available"] = oauth_available("live")
        context["nowpayments_self_service_available"] = self_service_available()
        context["paypal_partner_test_available"] = partner_available("test")
        context["paypal_partner_live_available"] = partner_available("live")
    elif section == "payments":
        payments = list(
            merchant.payment_intents.prefetch_related("attempts", "refunds").order_by("-created_at")[:100]
        )
        for payment in payments:
            refunded = sum(item.amount for item in payment.refunds.all() if item.status == Refund.STATUS_SUCCEEDED)
            reserved = sum(
                item.amount
                for item in payment.refunds.all()
                if item.status not in {Refund.STATUS_FAILED, Refund.STATUS_CANCELED}
            )
            payment.dashboard_refunded_amount = refunded
            payment.dashboard_refundable_amount = max(payment.amount - reserved, 0)
            payment.dashboard_refund_key = secrets.token_urlsafe(24)
        context["payments"] = payments
    elif section == "disputes":
        context["disputes"] = merchant.disputes.select_related("payment_intent").order_by("-created_at")[:100]
    elif section == "keys":
        context["keys"] = merchant.api_keys.order_by("-created_at")[:100]
    elif section == "webhooks":
        context["endpoints"] = merchant.webhook_endpoints.order_by("-created_at")[:100]
        context["deliveries"] = MerchantWebhookAttempt.objects.filter(endpoint__merchant=merchant).select_related("endpoint", "event").order_by("-created_at")[:100]
    elif section == "members":
        context["members"] = merchant.memberships.select_related("user").order_by("role", "created_at")
    elif section == "settings":
        context["origins"] = merchant.allowed_return_origins.order_by("origin")
    elif section == "audit":
        context["audit_events"] = merchant.audit_logs.select_related("actor").order_by("-created_at")[:100]
    response = render(request, "merchants/detail.html", context)
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_GET
@never_cache
def detail(request, slug, section="overview"):
    return _render_detail(request, slug, section)


def _merchant_with_capability(request, slug, capability):
    if not linked_subject(request.user):
        raise PermissionDenied
    merchant = get_object_or_404(member_merchants(request.user), slug=slug)
    if not can_manage(merchant_membership(request.user, merchant), capability):
        raise PermissionDenied
    return merchant


def _mfa_step_up(request, slug):
    return tg11_mfa_step_up(f"/seller/{slug}/")


def _seller_audit(request, merchant, action, object_type, object_id, metadata=None):
    AuditLog.objects.create(
        merchant=merchant, actor=request.user, action=action, object_type=object_type,
        object_id=str(object_id), request_id=getattr(request, "request_id", ""), metadata=metadata or {},
    )


@login_required
@require_http_methods(["POST"])
def create_seller_refund(request, slug, public_id):
    merchant = _merchant_with_capability(request, slug, "refunds")
    intent = get_object_or_404(PaymentIntent, merchant=merchant, public_id=public_id)
    try:
        amount = int(request.POST.get("amount", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Enter a valid refund amount.")
    from .services import APIError, create_refund

    try:
        refund = create_refund(
            request,
            merchant,
            intent,
            {"amount": amount, "reason": request.POST.get("reason", "")},
            idempotency_key=request.POST.get("idempotency_key", ""),
        )
    except APIError as exc:
        messages.error(request, exc.message)
        return redirect("seller_section", slug=slug, section="payments")
    _seller_audit(
        request,
        merchant,
        "refund.created",
        "refund",
        refund.public_id,
        {"status": refund.status, "amount": refund.amount, "currency": refund.currency},
    )
    messages.success(request, f"Refund {refund.public_id} is {refund.status}.")
    return redirect("seller_section", slug=slug, section="payments")


@login_required
@require_http_methods(["POST"])
@never_cache
def create_key(request, slug):
    merchant = _merchant_with_capability(request, slug, "keys")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    name = request.POST.get("name", "").strip()[:120]
    environment = request.POST.get("environment", "test")
    if not name or environment not in {"test", "live"}:
        return HttpResponseBadRequest("A key name and valid environment are required.")
    membership = merchant_membership(request.user, merchant)
    if environment == "live":
        if membership.role != MerchantMembership.ROLE_OWNER or routing_block_reason(merchant, "live"):
            raise PermissionDenied
        scopes = ["payments:read", "payments:write", "refunds:write", "subscriptions:write"]
    else:
        scopes = ["payments:read", "payments:write", "subscriptions:write"]
    with transaction.atomic():
        key, raw = APIKey.issue(merchant, name=name, environment=environment, scopes=scopes)
        _seller_audit(request, merchant, "api_key.created", "api_key", key.uuid, {"environment": environment, "prefix": key.prefix})
    return _render_detail(request, slug, "keys", one_time_secret=raw)


@login_required
@require_http_methods(["POST"])
def revoke_key(request, slug, key_uuid):
    merchant = _merchant_with_capability(request, slug, "keys")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        key = get_object_or_404(APIKey.objects.select_for_update(), uuid=key_uuid, merchant=merchant)
        key.is_active = False
        key.revoked_at = timezone.now()
        key.save(update_fields=["is_active", "revoked_at", "updated_at"])
        _seller_audit(request, merchant, "api_key.revoked", "api_key", key.uuid)
    messages.success(request, "API key revoked.")
    return redirect("seller_section", slug=slug, section="keys")


@login_required
@require_http_methods(["POST"])
@never_cache
def rotate_key(request, slug, key_uuid):
    merchant = _merchant_with_capability(request, slug, "keys")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        key = get_object_or_404(APIKey.objects.select_for_update(), uuid=key_uuid, merchant=merchant, is_active=True, revoked_at__isnull=True)
        if key.environment == "live" and (merchant_membership(request.user, merchant).role != MerchantMembership.ROLE_OWNER or routing_block_reason(merchant, "live")):
            raise PermissionDenied
        replacement, raw = APIKey.issue(merchant, name=key.name, environment=key.environment, scopes=key.scopes)
        key.is_active = False
        key.revoked_at = timezone.now()
        key.save(update_fields=["is_active", "revoked_at", "updated_at"])
        _seller_audit(request, merchant, "api_key.rotated", "api_key", replacement.uuid, {"replaces": str(key.uuid), "environment": key.environment})
    return _render_detail(request, slug, "keys", one_time_secret=raw)


@login_required
@require_http_methods(["POST"])
def add_return_origin(request, slug):
    merchant = _merchant_with_capability(request, slug, "settings")
    try:
        origin = return_origin(request.POST.get("origin", ""), live=settings.FOXPAY_ENV == "live")
    except UnsafeURL:
        return HttpResponseBadRequest("Enter a valid public return URL.")
    with transaction.atomic():
        item, created = MerchantAllowedReturnOrigin.objects.get_or_create(merchant=merchant, origin=origin)
        if created:
            _seller_audit(request, merchant, "return_origin.added", "return_origin", item.pk, {"origin": origin})
    messages.success(request, "Return origin registered.")
    return redirect("seller_section", slug=slug, section="settings")


@login_required
@require_http_methods(["POST"])
def remove_return_origin(request, slug, origin_id):
    merchant = _merchant_with_capability(request, slug, "settings")
    with transaction.atomic():
        item = get_object_or_404(MerchantAllowedReturnOrigin.objects.select_for_update(), pk=origin_id, merchant=merchant)
        _seller_audit(request, merchant, "return_origin.removed", "return_origin", item.pk, {"origin": item.origin})
        item.delete()
    messages.success(request, "Return origin removed.")
    return redirect("seller_section", slug=slug, section="settings")


@login_required
@require_http_methods(["POST"])
@never_cache
def create_webhook(request, slug):
    merchant = _merchant_with_capability(request, slug, "webhooks")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    url = request.POST.get("url", "").strip()
    description = request.POST.get("description", "").strip()[:160]
    try:
        parsed = parsed_public_url(url, live=True, allow_query=False)
        resolve_public_target(parsed.hostname, parsed.port or 443)
    except UnsafeURL:
        return HttpResponseBadRequest("Webhook URL must be a resolvable public HTTPS destination without a query string.")
    with transaction.atomic():
        endpoint, secret = MerchantWebhookEndpoint.create_with_secret(merchant, url, description=description)
        _seller_audit(request, merchant, "webhook.created", "webhook_endpoint", endpoint.uuid)
    return _render_detail(request, slug, "webhooks", one_time_secret=secret)


@login_required
@require_http_methods(["POST"])
@never_cache
def rotate_webhook(request, slug, endpoint_uuid):
    merchant = _merchant_with_capability(request, slug, "webhooks")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        endpoint = get_object_or_404(MerchantWebhookEndpoint.objects.select_for_update(), uuid=endpoint_uuid, merchant=merchant, is_active=True)
        if endpoint.pending_secret_hash:
            return HttpResponseBadRequest("A secret rotation is already pending.")
        secret = endpoint.begin_secret_rotation()
        _seller_audit(request, merchant, "webhook.rotation_started", "webhook_endpoint", endpoint.uuid)
    return _render_detail(request, slug, "webhooks", one_time_secret=secret)


@login_required
@require_http_methods(["POST"])
def activate_webhook_secret(request, slug, endpoint_uuid):
    merchant = _merchant_with_capability(request, slug, "webhooks")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        endpoint = get_object_or_404(MerchantWebhookEndpoint.objects.select_for_update(), uuid=endpoint_uuid, merchant=merchant, is_active=True)
        if not endpoint.pending_secret_hash:
            return HttpResponseBadRequest("No pending rotation.")
        endpoint.activate_secret_rotation()
        _seller_audit(request, merchant, "webhook.rotation_activated", "webhook_endpoint", endpoint.uuid)
    messages.success(request, "New webhook signing secret is active.")
    return redirect("seller_section", slug=slug, section="webhooks")


@login_required
@require_http_methods(["POST"])
def disable_webhook(request, slug, endpoint_uuid):
    merchant = _merchant_with_capability(request, slug, "webhooks")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    with transaction.atomic():
        endpoint = get_object_or_404(MerchantWebhookEndpoint.objects.select_for_update(), uuid=endpoint_uuid, merchant=merchant)
        endpoint.is_active = False
        endpoint.save(update_fields=["is_active", "updated_at"])
        _seller_audit(request, merchant, "webhook.disabled", "webhook_endpoint", endpoint.uuid)
    messages.success(request, "Webhook endpoint disabled.")
    return redirect("seller_section", slug=slug, section="webhooks")


@login_required
@require_http_methods(["POST"])
def test_webhook(request, slug, endpoint_uuid):
    merchant = _merchant_with_capability(request, slug, "webhooks")
    endpoint = get_object_or_404(MerchantWebhookEndpoint, uuid=endpoint_uuid, merchant=merchant, is_active=True)
    event = emit_event(merchant, "webhook.test", {"merchant": str(merchant.uuid), "endpoint": str(endpoint.uuid)}, idempotency_key=f"webhook.test:{secrets.token_hex(12)}")
    _seller_audit(request, merchant, "webhook.test_queued", "webhook_endpoint", endpoint_uuid, {"event_id": str(event.uuid)})
    messages.success(request, "Test event queued.")
    return redirect("seller_section", slug=slug, section="webhooks")


def _assignable_role(membership, role):
    if role not in {MerchantMembership.ROLE_ADMIN, MerchantMembership.ROLE_FINANCE, MerchantMembership.ROLE_DEVELOPER, MerchantMembership.ROLE_SUPPORT, MerchantMembership.ROLE_VIEWER}:
        return False
    return role != MerchantMembership.ROLE_ADMIN or membership.role == MerchantMembership.ROLE_OWNER


@login_required
@require_http_methods(["POST"])
def add_member(request, slug):
    merchant = _merchant_with_capability(request, slug, "members")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    role = request.POST.get("role", "")
    if not _assignable_role(merchant_membership(request.user, merchant), role):
        raise PermissionDenied
    try:
        subject = uuid.UUID(request.POST.get("tg11_user_uuid", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Enter a TG11 account UUID.")
    with transaction.atomic():
        if MerchantMembership.objects.filter(merchant=merchant, tg11_user_uuid=subject).exists():
            return HttpResponseBadRequest("This TG11 account is already a member.")
        link = TG11IdentityLink.objects.select_related("user").filter(application="foxpay", subject=str(subject), migration_status="linked").first()
        if link and MerchantMembership.objects.filter(merchant=merchant, user=link.user).exists():
            return HttpResponseBadRequest("This FoxPay user is already a member.")
        member = MerchantMembership.objects.create(merchant=merchant, user=link.user if link else None, tg11_user_uuid=subject, role=role)
        _seller_audit(request, merchant, "member.added", "merchant_membership", member.uuid, {"role": role})
    messages.success(request, "Member added.")
    return redirect("seller_section", slug=slug, section="members")


@login_required
@require_http_methods(["POST"])
def change_member_role(request, slug, member_uuid):
    merchant = _merchant_with_capability(request, slug, "members")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    actor_membership = merchant_membership(request.user, merchant)
    role = request.POST.get("role", "")
    if not _assignable_role(actor_membership, role):
        raise PermissionDenied
    with transaction.atomic():
        member = get_object_or_404(MerchantMembership.objects.select_for_update(), merchant=merchant, uuid=member_uuid)
        if member.role == MerchantMembership.ROLE_OWNER or (member.role == MerchantMembership.ROLE_ADMIN and actor_membership.role != MerchantMembership.ROLE_OWNER):
            raise PermissionDenied
        before = member.role
        member.role = role
        member.save(update_fields=["role", "updated_at"])
        _seller_audit(request, merchant, "member.role_changed", "merchant_membership", member.uuid, {"previous_role": before, "role": role})
    messages.success(request, "Member role updated.")
    return redirect("seller_section", slug=slug, section="members")


@login_required
@require_http_methods(["POST"])
def remove_member(request, slug, member_uuid):
    merchant = _merchant_with_capability(request, slug, "members")
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    actor_membership = merchant_membership(request.user, merchant)
    with transaction.atomic():
        member = get_object_or_404(MerchantMembership.objects.select_for_update(), merchant=merchant, uuid=member_uuid)
        if member.role == MerchantMembership.ROLE_OWNER or (member.role == MerchantMembership.ROLE_ADMIN and actor_membership.role != MerchantMembership.ROLE_OWNER):
            raise PermissionDenied
        _seller_audit(request, merchant, "member.removed", "merchant_membership", member.uuid, {"role": member.role})
        member.delete()
    messages.success(request, "Member removed.")
    return redirect("seller_section", slug=slug, section="members")


@staff_member_required
@require_http_methods(["GET", "POST"])
def review(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "").strip()[:500]
        if action not in {"approve", "reject", "restrict", "disable", "kill", "reinstate", "enable_live"}:
            return HttpResponseBadRequest("Unknown review action.")
        if action in {"reject", "restrict", "disable", "kill", "reinstate"} and not reason:
            return HttpResponseBadRequest("A review reason is required.")
        with transaction.atomic():
            merchant = get_object_or_404(Merchant.objects.select_for_update(), pk=request.POST.get("merchant_id"))
            now = timezone.now()
            if action == "approve":
                if merchant.status != Merchant.STATUS_PENDING:
                    return HttpResponseBadRequest("Only pending applications can be approved.")
                merchant.status = Merchant.STATUS_ACTIVE
                merchant.approved_at = now
                merchant.approved_by = request.user
                merchant.live_payments_enabled = True
                merchant.routing_enabled = True
                merchant.risk_level = "standard"
            elif action == "reject":
                if merchant.status != Merchant.STATUS_PENDING:
                    return HttpResponseBadRequest("Only pending applications can be rejected.")
                merchant.status = Merchant.STATUS_DISABLED
                merchant.live_payments_enabled = False
                merchant.routing_enabled = False
                merchant.suspended_at = now
                merchant.suspension_reason = reason
            elif action in {"restrict", "disable", "kill"}:
                if action == "restrict":
                    merchant.status = Merchant.STATUS_RESTRICTED
                elif action == "disable":
                    merchant.status = Merchant.STATUS_DISABLED
                merchant.live_payments_enabled = False
                merchant.routing_enabled = False
                merchant.suspended_at = now
                merchant.suspension_reason = reason
            elif action == "reinstate":
                if (
                    merchant.status not in {Merchant.STATUS_ACTIVE, Merchant.STATUS_RESTRICTED, Merchant.STATUS_DISABLED}
                    or not merchant.approved_at
                ):
                    return HttpResponseBadRequest("Merchant is not eligible for reinstatement.")
                merchant.status = Merchant.STATUS_ACTIVE
                merchant.live_payments_enabled = False
                merchant.routing_enabled = True
                merchant.temporarily_restricted_until = None
                merchant.temporary_restriction_reason = ""
                merchant.suspended_at = None
                merchant.suspension_reason = ""
            elif action == "enable_live":
                if merchant.status != Merchant.STATUS_ACTIVE or not merchant.approved_at or not merchant.is_active:
                    return HttpResponseBadRequest("Merchant is not approved for live routing.")
                merchant.routing_enabled = True
                merchant.live_payments_enabled = True
                merchant.temporarily_restricted_until = None
                merchant.temporary_restriction_reason = ""
                merchant.suspended_at = None
                merchant.suspension_reason = ""
            merchant.reviewed_at = now
            merchant.reviewed_by = request.user
            merchant.save()
            AuditLog.objects.create(
                merchant=merchant,
                actor=request.user,
                action=f"merchant.review.{action}",
                object_type="merchant",
                object_id=str(merchant.uuid),
                request_id=getattr(request, "request_id", ""),
                metadata={
                    "reason": reason,
                    "status": merchant.status,
                    "routing_enabled": merchant.routing_enabled,
                    "live_payments_enabled": merchant.live_payments_enabled,
                },
            )
        messages.success(request, "Merchant review updated.")
        return redirect("seller_review")
    merchants = Merchant.objects.select_related("owner", "reviewed_by").order_by("-created_at")[:100]
    return render(request, "merchants/review.html", {"merchants": merchants})
