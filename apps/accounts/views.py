import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F, Q
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST

from apps.payments.models import (
    AuditLog,
    Customer,
    MerchantMembership,
    PaymentIntent,
    PaymentMethodReference,
    ProviderConfig,
    SubscriptionReference,
)
from apps.payments.adapters.stripe_methods import create_setup_checkout, detach_saved_method

from .identity import linked_subject


@never_cache
@require_GET
def dashboard(request, section="overview"):
    if not request.user.is_authenticated:
        return render(request, "accounts/welcome.html")

    subject = linked_subject(request.user)
    customers = Customer.objects.none()
    payments = PaymentIntent.objects.none()
    methods = PaymentMethodReference.objects.none()
    subscriptions = SubscriptionReference.objects.none()
    memberships = MerchantMembership.objects.filter(user=request.user)
    if subject:
        customers = Customer.objects.filter(tg11_user_uuid=subject).select_related("merchant")
        payments = PaymentIntent.objects.filter(
            customer__tg11_user_uuid=subject,
            customer__merchant=F("merchant"),
            environment=settings.FOXPAY_ENV,
        ).select_related("merchant").order_by("-created_at")
        methods = PaymentMethodReference.objects.filter(
            customer__tg11_user_uuid=subject,
            customer__merchant=F("merchant"),
        ).select_related("merchant").order_by("-created_at")
        subscriptions = SubscriptionReference.objects.filter(
            customer__tg11_user_uuid=subject,
            customer__merchant=F("merchant"),
        ).select_related("merchant").order_by("-updated_at")
        memberships = MerchantMembership.objects.filter(
            Q(user=request.user) | Q(tg11_user_uuid=subject)
        ).distinct()

    context = {
        "section": section,
        "subject": subject,
        "customers": customers,
        "payment_count": payments.count(),
        "subscription_count": subscriptions.count(),
        "method_count": methods.count(),
        "payments": payments[:100],
        "orders": payments.exclude(reference="")[:100],
        "methods": methods[:100],
        "subscriptions": subscriptions[:100],
        "memberships": memberships.select_related("merchant"),
        "setup_providers": ProviderConfig.objects.filter(
            environment=settings.FOXPAY_ENV,
            kind=ProviderConfig.KIND_CARD,
            adapter__in=["stripe", "stripe_checkout"],
            is_active=True,
            settings__allow_customer_method_setup=True,
            merchant__is_active=True,
        ).select_related("merchant") if subject else ProviderConfig.objects.none(),
    }
    return render(request, "accounts/dashboard.html", context)


@login_required
@require_GET
def payments(request):
    return dashboard(request, "payments")


@login_required
@require_GET
def orders(request):
    return dashboard(request, "orders")


@login_required
@require_GET
def subscriptions(request):
    return dashboard(request, "subscriptions")


@login_required
@require_GET
def payment_methods(request):
    return dashboard(request, "methods")


@login_required
@require_GET
def connections(request):
    return dashboard(request, "connections")


def _fresh_tg11_mfa(request):
    auth_time = request.session.get("foxpay_tg11_auth_time", 0)
    return bool(request.session.get("foxpay_tg11_mfa") and auth_time and 0 <= time.time() - auth_time <= 300)


@login_required
@require_POST
def add_payment_method(request, merchant_slug, provider):
    subject = linked_subject(request.user)
    if not subject:
        return HttpResponseForbidden("Connect a TG11 account first.")
    if not _fresh_tg11_mfa(request):
        return render(request, "accounts/step_up.html", status=403)
    config = get_object_or_404(
        ProviderConfig,
        merchant__slug=merchant_slug,
        merchant__is_active=True,
        environment=settings.FOXPAY_ENV,
        kind=ProviderConfig.KIND_CARD,
        provider=provider,
        adapter__in=["stripe", "stripe_checkout"],
        is_active=True,
        settings__allow_customer_method_setup=True,
    )
    customer, _ = Customer.objects.get_or_create(
        merchant=config.merchant,
        tg11_user_uuid=subject,
        defaults={"email": request.user.email},
    )
    try:
        url = create_setup_checkout(request, config, customer)
    except Exception:
        messages.error(request, "Card setup is temporarily unavailable.")
        return redirect("account_payment_methods")
    AuditLog.objects.create(
        merchant=config.merchant,
        actor=request.user,
        action="payment_method.setup_started",
        object_type="customer",
        object_id=str(customer.uuid),
    )
    return HttpResponseRedirect(url)


@login_required
@require_POST
def remove_payment_method(request, method_uuid):
    subject = linked_subject(request.user)
    if not subject:
        return HttpResponseForbidden("Connect a TG11 account first.")
    if not _fresh_tg11_mfa(request):
        return render(request, "accounts/step_up.html", status=403)
    method = get_object_or_404(
        PaymentMethodReference.objects.select_related("customer", "merchant", "provider_config"),
        uuid=method_uuid,
        customer__tg11_user_uuid=subject,
    )
    if method.customer.merchant_id != method.merchant_id:
        return HttpResponseForbidden("Payment method ownership could not be verified.")
    if SubscriptionReference.objects.filter(
        customer=method.customer,
        status__in=["active", "trialing", "past_due"],
    ).exists():
        messages.error(request, "This account has an active subscription. Update its billing method before removing this card.")
        return redirect("account_payment_methods")
    try:
        detach_saved_method(method)
    except Exception:
        messages.error(request, "The payment method could not be removed right now.")
        return redirect("account_payment_methods")
    AuditLog.objects.create(
        merchant=method.merchant,
        actor=request.user,
        action="payment_method.detached",
        object_type="payment_method_reference",
        object_id=str(method_uuid),
    )
    messages.success(request, "Payment method removed.")
    return redirect("account_payment_methods")
