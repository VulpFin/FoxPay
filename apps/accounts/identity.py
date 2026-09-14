import uuid
import time

from tg11_auth.models import TG11IdentityLink
from tg11_auth.services import AuthError

from .models import FoxPayProfile


def guard_tg11_login(user, claims):
    if user.is_staff and not claims.used_second_factor:
        raise AuthError("Fox Pay administrators must use TG11 two-factor authentication.")


def on_tg11_login(*, user, claims, created, link, request):
    subject = uuid.UUID(claims.subject)
    profile, _ = FoxPayProfile.objects.get_or_create(user=user)
    profile.tg11_user_uuid = subject
    profile.tg11_subject = claims.subject
    profile.save(update_fields=["tg11_user_uuid", "tg11_subject", "updated_at"])
    request.session["foxpay_tg11_auth_time"] = int(claims.auth_time or 0)
    request.session["foxpay_tg11_mfa"] = bool(claims.used_second_factor)


def linked_subject(user):
    if not user.is_authenticated:
        return None
    link = TG11IdentityLink.objects.filter(user=user, application="foxpay").first()
    if not link or link.migration_status != "linked":
        return None
    try:
        return uuid.UUID(link.subject)
    except (TypeError, ValueError):
        return None


def fresh_tg11_mfa(request, seconds=300):
    auth_time = request.session.get("foxpay_tg11_auth_time", 0)
    return bool(request.session.get("foxpay_tg11_mfa") and auth_time and 0 <= time.time() - auth_time <= seconds)
