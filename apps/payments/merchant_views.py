from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.identity import linked_subject

from .agreements import agreement_snapshot
from .merchant_applications import MerchantApplicationForm, create_merchant_application
from .models import AuditLog, Merchant
from .permissions import member_merchants, merchant_membership


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


@login_required
@require_GET
def detail(request, slug):
    if not linked_subject(request.user):
        return HttpResponseForbidden("A linked TG11 account is required.")
    merchant = get_object_or_404(member_merchants(request.user), slug=slug)
    membership = merchant_membership(request.user, merchant)
    return render(request, "merchants/detail.html", {"merchant": merchant, "membership": membership})


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
                merchant.risk_level = "standard"
            elif action == "reject":
                if merchant.status != Merchant.STATUS_PENDING:
                    return HttpResponseBadRequest("Only pending applications can be rejected.")
                merchant.status = Merchant.STATUS_DISABLED
                merchant.live_payments_enabled = False
                merchant.suspended_at = now
                merchant.suspension_reason = reason
            elif action in {"restrict", "disable", "kill"}:
                if action == "restrict":
                    merchant.status = Merchant.STATUS_RESTRICTED
                elif action == "disable":
                    merchant.status = Merchant.STATUS_DISABLED
                merchant.live_payments_enabled = False
                merchant.suspended_at = now
                merchant.suspension_reason = reason
            elif action == "reinstate":
                if merchant.status not in {Merchant.STATUS_RESTRICTED, Merchant.STATUS_DISABLED} or not merchant.approved_at:
                    return HttpResponseBadRequest("Merchant is not eligible for reinstatement.")
                merchant.status = Merchant.STATUS_ACTIVE
                merchant.live_payments_enabled = False
                merchant.suspended_at = None
                merchant.suspension_reason = ""
            elif action == "enable_live":
                if merchant.status != Merchant.STATUS_ACTIVE or not merchant.approved_at or not merchant.is_active:
                    return HttpResponseBadRequest("Merchant is not approved for live routing.")
                merchant.live_payments_enabled = True
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
                metadata={"reason": reason, "status": merchant.status, "live_payments_enabled": merchant.live_payments_enabled},
            )
        messages.success(request, "Merchant review updated.")
        return redirect("seller_review")
    merchants = Merchant.objects.select_related("owner", "reviewed_by").order_by("-created_at")[:100]
    return render(request, "merchants/review.html", {"merchants": merchants})
