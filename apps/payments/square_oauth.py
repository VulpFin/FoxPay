from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .adapters.square_checkout import SQUARE_API_VERSION
from .models import MerchantProviderConnection


SQUARE_SCOPES = ("MERCHANT_PROFILE_READ", "ORDERS_READ", "ORDERS_WRITE", "PAYMENTS_READ", "PAYMENTS_WRITE")


def square_base(environment):
    return "https://connect.squareupsandbox.com" if environment == "test" else "https://connect.squareup.com"


def app_credentials(environment):
    if environment == "test":
        client_id, client_secret = settings.SQUARE_APPLICATION_ID_TEST, settings.SQUARE_APPLICATION_SECRET_TEST
    elif environment == "live":
        client_id, client_secret = settings.SQUARE_APPLICATION_ID_LIVE, settings.SQUARE_APPLICATION_SECRET_LIVE
    else:
        raise ImproperlyConfigured("Invalid Square environment.")
    if not client_id or not client_secret:
        raise ImproperlyConfigured("Square OAuth application credentials are not configured.")
    return client_id, client_secret


def webhook_settings(environment):
    if environment == "test":
        return settings.SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST, settings.SQUARE_OAUTH_WEBHOOK_URL_TEST
    if environment == "live":
        return settings.SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_LIVE, settings.SQUARE_OAUTH_WEBHOOK_URL_LIVE
    return "", ""


def oauth_available(environment):
    try:
        app_credentials(environment)
        return bool(settings.FOXPAY_SQUARE_OAUTH_ENABLED)
    except ImproperlyConfigured:
        return False


def authorize_url(*, environment, state, redirect_uri):
    client_id, _ = app_credentials(environment)
    query = urlencode({"client_id": client_id, "scope": " ".join(SQUARE_SCOPES), "state": state, "redirect_uri": redirect_uri})
    return f"{square_base(environment)}/oauth2/authorize?{query}"


def _json_request(method, url, *, body=None, headers=None):
    response = requests.request(method, url, json=body, headers={"Square-Version": SQUARE_API_VERSION, **(headers or {})}, timeout=(3, 7))
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Square returned an invalid response.")
    return result


def obtain_token(*, environment, grant_type, value, redirect_uri=""):
    client_id, client_secret = app_credentials(environment)
    if grant_type not in {"authorization_code", "refresh_token"}:
        raise ValueError("Invalid Square grant type.")
    body = {"client_id": client_id, "client_secret": client_secret, "grant_type": grant_type}
    body["code" if grant_type == "authorization_code" else "refresh_token"] = value
    if grant_type == "authorization_code":
        if not redirect_uri:
            raise ValueError("Square redirect URI is required for authorization code exchange.")
        body["redirect_uri"] = redirect_uri
    result = _json_request("POST", f"{square_base(environment)}/oauth2/token", body=body)
    expires_value = result.get("expires_at")
    expires_at = parse_datetime(expires_value) if isinstance(expires_value, str) else None
    if (
        not isinstance(result.get("access_token"), str) or not result["access_token"]
        or not isinstance(result.get("refresh_token"), str) or not result["refresh_token"]
        or not isinstance(result.get("merchant_id"), str) or not result["merchant_id"]
        or not expires_at or timezone.is_naive(expires_at) or expires_at <= timezone.now()
    ):
        raise ValueError("Square OAuth token response is incomplete.")
    return result, expires_at


def token_status(*, environment, access_token):
    result = _json_request("POST", f"{square_base(environment)}/oauth2/token/status", headers={"Authorization": f"Bearer {access_token}"})
    client_id, _ = app_credentials(environment)
    if result.get("client_id") != client_id or not isinstance(result.get("scopes"), list):
        raise ValueError("Square token is not associated with this application.")
    return result


def list_usable_locations(*, environment, access_token, merchant_id):
    result = _json_request("GET", f"{square_base(environment)}/v2/locations", headers={"Authorization": f"Bearer {access_token}"})
    locations = result.get("locations")
    if not isinstance(locations, list):
        raise ValueError("Square did not return locations.")
    usable = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        if location.get("merchant_id") not in {None, merchant_id}:
            raise ValueError("Square location does not belong to the seller.")
        if location.get("status") != "ACTIVE" or "CREDIT_CARD_PROCESSING" not in (location.get("capabilities") or []):
            continue
        location_id, currency = location.get("id"), location.get("currency")
        if not isinstance(location_id, str) or not location_id or not isinstance(currency, str) or len(currency) != 3:
            continue
        usable.append({"id": location_id, "name": str(location.get("name") or location_id)[:120], "currency": currency.upper()})
    return usable


def revoke_authorization(*, environment, merchant_id):
    client_id, client_secret = app_credentials(environment)
    result = _json_request(
        "POST", f"{square_base(environment)}/oauth2/revoke",
        body={"client_id": client_id, "merchant_id": merchant_id},
        headers={"Authorization": f"Client {client_secret}"},
    )
    if result.get("success") is not True:
        raise ValueError("Square did not confirm revocation.")


def mark_connection_unhealthy(connection, code):
    safe_codes = {
        "ACCESS_TOKEN_EXPIRED": "square_access_token_expired",
        "ACCESS_TOKEN_REVOKED": "square_access_token_revoked",
        "INSUFFICIENT_SCOPES": "square_insufficient_scopes",
        "FORBIDDEN": "square_insufficient_scopes",
        "UNAUTHORIZED": "square_unauthorized",
    }
    normalized = safe_codes.get(code, "square_authorization_failed")
    MerchantProviderConnection.objects.filter(pk=connection.pk).update(
        status=MerchantProviderConnection.STATUS_ERROR,
        last_error_at=timezone.now(),
        last_error_code=normalized,
    )
    connection.provider_configs.update(is_active=False)


@transaction.atomic
def refresh_connection(connection, *, force=False):
    connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
    if connection.provider != "square" or connection.authorization_method != "square_oauth" or connection.status == connection.STATUS_REVOKED:
        return False
    due = not connection.token_refreshed_at or connection.token_refreshed_at <= timezone.now() - timedelta(days=7)
    expiring = not connection.token_expires_at or connection.token_expires_at <= timezone.now() + timedelta(days=1)
    if not force and not (due or expiring):
        return True
    try:
        result, expires_at = obtain_token(
            environment=connection.environment,
            grant_type="refresh_token",
            value=connection.refresh_token(),
        )
        if result["merchant_id"] != connection.external_account_id:
            raise ValueError("Square refresh returned a different merchant.")
        status = token_status(environment=connection.environment, access_token=result["access_token"])
        if status.get("merchant_id") != connection.external_account_id or not set(SQUARE_SCOPES).issubset(set(status["scopes"])):
            raise ValueError("Square refresh no longer has required permissions.")
    except (requests.RequestException, ValueError, ImproperlyConfigured):
        connection.status = connection.STATUS_ERROR
        connection.last_error_at = timezone.now()
        connection.last_error_code = "square_refresh_failed"
        connection.save(update_fields=["status", "last_error_at", "last_error_code", "updated_at"])
        connection.provider_configs.update(is_active=False)
        return False
    connection.set_access_token(result["access_token"])
    connection.set_refresh_token(result["refresh_token"])
    connection.token_expires_at = expires_at
    connection.token_refreshed_at = timezone.now()
    connection.last_verified_at = timezone.now()
    connection.last_error_at = None
    connection.last_error_code = ""
    connection.save()
    config = connection.provider_configs.filter(kind="card").first()
    if config and config.settings.get("location_id"):
        key, url = webhook_settings(connection.environment)
        config.is_active = bool(key and url.startswith("https://"))
        config.save(update_fields=["is_active", "updated_at"])
        connection.status = connection.STATUS_ACTIVE if config.is_active else connection.STATUS_RESTRICTED
        connection.last_error_code = "" if config.is_active else "webhook_not_configured"
    else:
        connection.status = connection.STATUS_PENDING
    connection.save(update_fields=["status", "last_error_code", "updated_at"])
    return True
