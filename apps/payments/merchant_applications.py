import re
import secrets

from django import forms
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from .agreements import agreement_snapshot
from .models import AuditLog, Merchant, MerchantAgreementAcceptance, MerchantMembership


class MerchantApplicationForm(forms.Form):
    name = forms.CharField(label="Display name", max_length=160)
    legal_name = forms.CharField(label="Legal or business name", max_length=240)
    website_url = forms.URLField(label="Business website", max_length=200)
    support_email = forms.EmailField(max_length=254)
    country = forms.CharField(max_length=2, min_length=2)
    default_currency = forms.CharField(max_length=3, min_length=3, initial="USD")
    business_category = forms.CharField(max_length=120)
    business_description = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}), max_length=2000)
    accept_agreement = forms.BooleanField(label="I accept the FoxPay merchant agreement draft")
    accept_acceptable_use = forms.BooleanField(label="I accept the acceptable-use policy")

    def clean_country(self):
        value = self.cleaned_data["country"].upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise forms.ValidationError("Enter a two-letter country code.")
        return value

    def clean_default_currency(self):
        value = self.cleaned_data["default_currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise forms.ValidationError("Enter a three-letter currency code.")
        return value


@transaction.atomic
def create_merchant_application(request, subject, cleaned_data):
    base = slugify(cleaned_data["name"])[:44].strip("-") or "merchant"
    merchant = None
    for attempt in range(12):
        slug = base if attempt == 0 else f"{base}-{secrets.token_hex(3)}"
        try:
            with transaction.atomic():
                merchant = Merchant.objects.create(
                    owner=request.user,
                    name=cleaned_data["name"],
                    slug=slug,
                    legal_name=cleaned_data["legal_name"],
                    website_url=cleaned_data["website_url"],
                    support_email=cleaned_data["support_email"],
                    country=cleaned_data["country"],
                    default_currency=cleaned_data["default_currency"],
                    business_category=cleaned_data["business_category"],
                    business_description=cleaned_data["business_description"],
                    status=Merchant.STATUS_PENDING,
                    live_payments_enabled=False,
                )
            break
        except IntegrityError:
            if not Merchant.objects.filter(slug=slug).exists():
                raise
    if merchant is None:
        raise IntegrityError("Could not allocate a unique merchant slug.")

    MerchantMembership.objects.create(
        merchant=merchant,
        user=request.user,
        tg11_user_uuid=subject,
        role=MerchantMembership.ROLE_OWNER,
    )
    snapshot = agreement_snapshot()
    MerchantAgreementAcceptance.objects.create(
        merchant=merchant,
        user=request.user,
        agreement_version=snapshot["agreement_version"],
        agreement_hash=snapshot["agreement_hash"],
        acceptable_use_version=snapshot["acceptable_use_version"],
        acceptable_use_hash=snapshot["acceptable_use_hash"],
        request_ip=request.META.get("REMOTE_ADDR") or None,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )
    AuditLog.objects.create(
        merchant=merchant,
        actor=request.user,
        action="merchant.application_submitted",
        object_type="merchant",
        object_id=str(merchant.uuid),
        request_id=getattr(request, "request_id", ""),
        metadata={"agreement_version": snapshot["agreement_version"], "acceptable_use_version": snapshot["acceptable_use_version"]},
    )
    return merchant
