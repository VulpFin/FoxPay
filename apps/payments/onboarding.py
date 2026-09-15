import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from .models import ProviderOnboardingSession


class OnboardingStateError(ValueError):
    pass


def safe_return_path(path):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(char) < 32 for char in path):
        raise OnboardingStateError("Invalid return path.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or len(path) > 500:
        raise OnboardingStateError("Invalid return path.")
    return path


def begin_onboarding(*, merchant, user, provider, environment, requested_scopes=None, return_path="/seller/", pkce_verifier=""):
    if provider not in {"stripe", "square", "paypal"}:
        raise ValueError("Provider does not support redirect onboarding.")
    if environment not in {"test", "live"}:
        raise ValueError("Invalid provider environment.")
    raw_state = secrets.token_urlsafe(32)
    session = ProviderOnboardingSession(
        merchant=merchant,
        user=user,
        provider=provider,
        environment=environment,
        state_hash=hashlib.sha256(raw_state.encode("ascii")).hexdigest(),
        requested_scopes=requested_scopes or [],
        expires_at=timezone.now() + timedelta(minutes=10),
        return_path=safe_return_path(return_path),
    )
    if pkce_verifier:
        session.set_pkce_verifier(pkce_verifier)
    session.save()
    return session, raw_state


@transaction.atomic
def consume_onboarding(*, raw_state, merchant=None, user, provider, environment):
    if not isinstance(raw_state, str) or not raw_state or len(raw_state) > 256:
        raise OnboardingStateError("Invalid onboarding state.")
    digest = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    session = ProviderOnboardingSession.objects.select_for_update().filter(state_hash=digest).first()
    if (
        not session
        or (merchant is not None and session.merchant_id != merchant.pk)
        or session.user_id != user.pk
        or session.provider != provider
        or session.environment != environment
        or session.consumed_at is not None
        or session.expires_at <= timezone.now()
    ):
        raise OnboardingStateError("Onboarding state is invalid or expired.")
    session.consumed_at = timezone.now()
    session.save(update_fields=["consumed_at"])
    return session
