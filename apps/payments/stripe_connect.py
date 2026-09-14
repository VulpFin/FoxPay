import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .adapters.stripe_checkout import object_value, stripe_module


def platform_secret_key(environment):
    value = settings.STRIPE_SECRET_KEY if environment == "live" else settings.STRIPE_TEST_SECRET_KEY
    expected = "sk_live_" if environment == "live" else "sk_test_"
    if not value or not value.startswith(expected):
        raise ImproperlyConfigured("Stripe Connect platform key is not configured for this environment.")
    return value


def connect_client_id(environment):
    value = settings.STRIPE_CONNECT_CLIENT_ID_LIVE if environment == "live" else settings.STRIPE_CONNECT_CLIENT_ID_TEST
    if not value or not value.startswith("ca_"):
        raise ImproperlyConfigured("Stripe Connect client ID is not configured for this environment.")
    return value


def connect_available(environment):
    try:
        return bool(settings.FOXPAY_STRIPE_CONNECT_ENABLED and platform_secret_key(environment) and connect_client_id(environment))
    except ImproperlyConfigured:
        return False


def authorize_url(*, environment, state, redirect_uri):
    stripe = stripe_module()
    return stripe.OAuth.authorize_url(
        client_id=connect_client_id(environment),
        response_type="code",
        scope="read_write",
        state=state,
        redirect_uri=redirect_uri,
    )


def exchange_code(*, environment, code):
    stripe = stripe_module()
    token = stripe.OAuth.token(api_key=platform_secret_key(environment), grant_type="authorization_code", code=code)
    account_id = object_value(token, "stripe_user_id", "")
    livemode = object_value(token, "livemode")
    scope = object_value(token, "scope", "")
    if not isinstance(account_id, str) or not re.fullmatch(r"acct_[A-Za-z0-9]+", account_id):
        raise ValueError("Stripe did not return a valid connected account.")
    if livemode is not (environment == "live") or scope != "read_write":
        raise ValueError("Stripe account mode or scope did not match the request.")
    return account_id


def verify_account(*, environment, account_id):
    stripe = stripe_module()
    account = stripe.Account.retrieve(account_id, api_key=platform_secret_key(environment))
    capabilities = object_value(account, "capabilities", {}) or {}
    card_payments = object_value(capabilities, "card_payments", "")
    ready = bool(object_value(account, "charges_enabled", False) and card_payments == "active")
    return {
        "ready": ready,
        "country": object_value(account, "country", ""),
        "capabilities": ["card_payments"] if card_payments == "active" else [],
    }


def disconnect_account(*, environment, account_id):
    stripe = stripe_module()
    response = stripe.OAuth.deauthorize(
        api_key=platform_secret_key(environment),
        client_id=connect_client_id(environment),
        stripe_user_id=account_id,
    )
    if object_value(response, "stripe_user_id", "") != account_id:
        raise ValueError("Stripe did not confirm disconnection.")
