from django.conf import settings
from django.db.models import Q

from apps.accounts.identity import linked_subject

from .models import Merchant, MerchantMembership


ROLE_CAPABILITIES = {
    MerchantMembership.ROLE_OWNER: frozenset({"view", "settings", "connections", "routing", "members", "webhooks", "keys", "refunds", "closure"}),
    MerchantMembership.ROLE_ADMIN: frozenset({"view", "connections", "routing", "members", "webhooks"}),
    MerchantMembership.ROLE_FINANCE: frozenset({"view", "refunds", "exports"}),
    MerchantMembership.ROLE_DEVELOPER: frozenset({"view", "keys", "webhooks", "test_payments"}),
    MerchantMembership.ROLE_SUPPORT: frozenset({"view"}),
    MerchantMembership.ROLE_VIEWER: frozenset({"view"}),
}


def merchant_membership(user, merchant):
    if not user.is_authenticated:
        return None
    direct = MerchantMembership.objects.filter(merchant=merchant, user=user).first()
    if direct:
        return direct
    subject = linked_subject(user)
    if subject:
        return MerchantMembership.objects.filter(merchant=merchant, tg11_user_uuid=subject).first()
    return None


def member_merchants(user):
    if not user.is_authenticated:
        return Merchant.objects.none()
    identities = Q(memberships__user=user)
    subject = linked_subject(user)
    if subject:
        identities |= Q(memberships__tg11_user_uuid=subject)
    return Merchant.objects.filter(identities).distinct()


def can_manage(membership, capability):
    return bool(membership and capability in ROLE_CAPABILITIES.get(membership.role, frozenset()))


def routing_block_reason(merchant, environment=None):
    environment = environment or settings.FOXPAY_ENV
    if not merchant.is_active:
        return "merchant_disabled"
    if merchant.status == Merchant.STATUS_ACTIVE:
        if environment == "live" and not merchant.live_payments_enabled:
            return "live_payments_disabled"
        return ""
    if merchant.status == Merchant.STATUS_PENDING and environment == "test" and getattr(settings, "FOXPAY_PENDING_TEST_PAYMENTS_ENABLED", False):
        return ""
    return "merchant_not_approved"
