import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.accounts.identity import fresh_tg11_mfa

from .merchant_views import _merchant_with_capability, _mfa_step_up, _seller_audit
from .models import MerchantProviderConnection, ProviderConfig
from .nowpayments_connection import (
    NowPaymentsConnectionError,
    check_connection_health,
    configure_nowpayments_connection,
    disconnect_nowpayments_connection,
    self_service_available,
)


class NowPaymentsCredentialsForm(forms.Form):
    display_name = forms.CharField(max_length=120)
    pay_currency = forms.CharField(max_length=20, required=False)
    api_key = forms.CharField(min_length=8, max_length=2048, strip=False, widget=forms.PasswordInput)
    ipn_secret = forms.CharField(min_length=8, max_length=2048, strip=False, widget=forms.PasswordInput)

    def clean_display_name(self):
        value = self.cleaned_data["display_name"].strip()
        if not value:
            raise forms.ValidationError("Enter a name.")
        return value

    def clean_pay_currency(self):
        value = self.cleaned_data["pay_currency"].strip().lower()
        if value and not re.fullmatch(r"[a-z0-9_-]{2,20}", value):
            raise forms.ValidationError("Enter a valid asset code.")
        return value

    def _clean_secret(self, name):
        value = self.cleaned_data[name]
        if value != value.strip() or "\r" in value or "\n" in value:
            raise forms.ValidationError("The credential contains invalid whitespace.")
        return value

    def clean_api_key(self):
        return self._clean_secret("api_key")

    def clean_ipn_secret(self):
        return self._clean_secret("ipn_secret")


class NewNowPaymentsConnectionForm(NowPaymentsCredentialsForm):
    environment = forms.ChoiceField(choices=(("test", "Test"), ("live", "Live")))


def _ready_for_sensitive_action(request, slug):
    if not fresh_tg11_mfa(request):
        return _mfa_step_up(request, slug)
    return None


def _secure_for_environment(request, environment):
    return environment != "live" or request.is_secure()


def _connection_error_message(error, fallback):
    if str(error) == "account_not_configured":
        return (
            "NOWPayments authenticated, but no payment currencies are available. "
            "Add an outcome wallet and enable at least one currency in NOWPayments, then try again."
        )
    return fallback


@login_required
@require_POST
def connect_nowpayments(request, slug):
    merchant = _merchant_with_capability(request, slug, "connections")
    if not self_service_available():
        return HttpResponseBadRequest("NOWPayments self-service is unavailable.")
    step_up = _ready_for_sensitive_action(request, slug)
    if step_up:
        return step_up
    form = NewNowPaymentsConnectionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "NOWPayments credentials were not accepted. Check each field and try again.")
        return redirect("seller_section", slug=slug, section="providers")
    environment = form.cleaned_data["environment"]
    if not _secure_for_environment(request, environment):
        return HttpResponseBadRequest("Live provider credentials require HTTPS.")
    with transaction.atomic():
        connection = MerchantProviderConnection.objects.create(
            merchant=merchant,
            provider="nowpayments",
            environment=environment,
            status=MerchantProviderConnection.STATUS_PENDING,
            authorization_method="nowpayments_credentials",
        )
        config = ProviderConfig.objects.create(
            merchant=merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CRYPTO,
            provider=f"nowpayments-{connection.uuid.hex}",
            adapter="nowpayments",
            display_name=form.cleaned_data["display_name"],
            environment=environment,
            capabilities=["crypto", "stablecoin", "hosted_checkout", "webhooks"],
            is_active=False,
        )
    try:
        configure_nowpayments_connection(
            connection,
            config,
            api_key=form.cleaned_data["api_key"],
            ipn_secret=form.cleaned_data["ipn_secret"],
            display_name=form.cleaned_data["display_name"],
            pay_currency=form.cleaned_data["pay_currency"],
        )
    except NowPaymentsConnectionError as exc:
        _seller_audit(
            request,
            merchant,
            "nowpayments.connect_failed",
            "provider_connection",
            connection.uuid,
            {"environment": environment, "reason": str(exc)},
        )
        messages.error(
            request,
            _connection_error_message(
                exc,
                "NOWPayments could not verify the API key. No payment route was activated.",
            ),
        )
        return redirect("seller_section", slug=slug, section="providers")
    _seller_audit(
        request,
        merchant,
        "nowpayments.connected",
        "provider_connection",
        connection.uuid,
        {"environment": environment},
    )
    messages.success(request, "NOWPayments account connected.")
    return redirect("seller_section", slug=slug, section="providers")


@login_required
@require_POST
def replace_nowpayments_credentials(request, slug, connection_uuid):
    merchant = _merchant_with_capability(request, slug, "connections")
    if not self_service_available():
        return HttpResponseBadRequest("NOWPayments self-service is unavailable.")
    step_up = _ready_for_sensitive_action(request, slug)
    if step_up:
        return step_up
    connection = get_object_or_404(
        MerchantProviderConnection,
        uuid=connection_uuid,
        merchant=merchant,
        provider="nowpayments",
        authorization_method="nowpayments_credentials",
    )
    if not _secure_for_environment(request, connection.environment):
        return HttpResponseBadRequest("Live provider credentials require HTTPS.")
    config = get_object_or_404(ProviderConfig, connection=connection, merchant=merchant, adapter="nowpayments")
    form = NowPaymentsCredentialsForm(request.POST)
    if not form.is_valid():
        messages.error(request, "NOWPayments credentials were not accepted. Check each field and try again.")
        return redirect("seller_section", slug=slug, section="providers")
    try:
        configure_nowpayments_connection(
            connection,
            config,
            api_key=form.cleaned_data["api_key"],
            ipn_secret=form.cleaned_data["ipn_secret"],
            display_name=form.cleaned_data["display_name"],
            pay_currency=form.cleaned_data["pay_currency"],
        )
    except NowPaymentsConnectionError as exc:
        _seller_audit(
            request,
            merchant,
            "nowpayments.replace_failed",
            "provider_connection",
            connection.uuid,
            {"reason": str(exc)},
        )
        messages.error(
            request,
            _connection_error_message(
                exc,
                "The replacement API key was not verified. Existing working credentials were preserved.",
            ),
        )
        return redirect("seller_section", slug=slug, section="providers")
    _seller_audit(request, merchant, "nowpayments.credentials_replaced", "provider_connection", connection.uuid)
    messages.success(request, "NOWPayments credentials replaced.")
    return redirect("seller_section", slug=slug, section="providers")


@login_required
@require_POST
def test_nowpayments_connection(request, slug, connection_uuid):
    merchant = _merchant_with_capability(request, slug, "connections")
    step_up = _ready_for_sensitive_action(request, slug)
    if step_up:
        return step_up
    connection = get_object_or_404(
        MerchantProviderConnection,
        uuid=connection_uuid,
        merchant=merchant,
        provider="nowpayments",
        authorization_method="nowpayments_credentials",
    )
    try:
        check_connection_health(connection)
    except NowPaymentsConnectionError as exc:
        _seller_audit(
            request,
            merchant,
            "nowpayments.health_failed",
            "provider_connection",
            connection.uuid,
            {"reason": str(exc)},
        )
        messages.error(
            request,
            _connection_error_message(
                exc,
                "NOWPayments connection check failed. Routing was disabled if no working credentials remained.",
            ),
        )
        return redirect("seller_section", slug=slug, section="providers")
    _seller_audit(request, merchant, "nowpayments.health_verified", "provider_connection", connection.uuid)
    messages.success(request, "NOWPayments connection verified.")
    return redirect("seller_section", slug=slug, section="providers")


@login_required
@require_POST
def disconnect_nowpayments(request, slug, connection_uuid):
    merchant = _merchant_with_capability(request, slug, "connections")
    step_up = _ready_for_sensitive_action(request, slug)
    if step_up:
        return step_up
    connection = get_object_or_404(
        MerchantProviderConnection,
        uuid=connection_uuid,
        merchant=merchant,
        provider="nowpayments",
        authorization_method="nowpayments_credentials",
    )
    if connection.status != connection.STATUS_REVOKED:
        disconnect_nowpayments_connection(connection)
        _seller_audit(request, merchant, "nowpayments.disconnected", "provider_connection", connection.uuid)
    messages.success(request, "NOWPayments disconnected. Revoke or rotate the API key and IPN secret in NOWPayments as well.")
    return redirect("seller_section", slug=slug, section="providers")
