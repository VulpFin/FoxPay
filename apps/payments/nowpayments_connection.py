import hashlib
import secrets

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .adapters.nowpayments import API_BASE_URLS, provider_secret
from .models import MerchantProviderConnection, ProviderConfig, ProviderCredential


CANONICAL_CREDENTIAL_NAMES = ("api_key", "ipn_secret")


class NowPaymentsConnectionError(Exception):
    pass


def self_service_available():
    return bool(settings.FOXPAY_NOWPAYMENTS_SELF_SERVICE_ENABLED)


def api_key_identity(api_key):
    return f"key_{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def check_nowpayments_api_key(*, api_key, environment):
    base_url = API_BASE_URLS.get(environment)
    if not base_url:
        raise NowPaymentsConnectionError("invalid_environment")
    try:
        response = requests.get(
            f"{base_url}/currencies",
            headers={"x-api-key": api_key},
            timeout=(3, 7),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise NowPaymentsConnectionError("provider_unavailable") from exc
    if response.status_code != 200:
        raise NowPaymentsConnectionError("api_key_rejected")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise NowPaymentsConnectionError("invalid_provider_response") from exc
    currencies = payload.get("currencies") if isinstance(payload, dict) else None
    if not isinstance(currencies, list):
        raise NowPaymentsConnectionError("invalid_provider_response")
    if not currencies:
        raise NowPaymentsConnectionError("account_not_configured")
    return {"currency_count": min(len(currencies), 10000)}


def _stage_credentials(config, api_key, ipn_secret):
    nonce = secrets.token_hex(12)
    staged_names = []
    with transaction.atomic():
        for name, value in (("api_key", api_key), ("ipn_secret", ipn_secret)):
            staged_name = f"pending:{nonce}:{name}"
            credential = ProviderCredential(provider_config=config, name=staged_name)
            credential.set_secret(value)
            credential.save()
            staged_names.append(staged_name)
    return staged_names


def _erase_credentials(queryset):
    queryset.update(encrypted_value="", revoked_at=timezone.now())
    queryset.delete()


def _has_active_credentials(config):
    active_names = set(
        config.credentials.filter(name__in=CANONICAL_CREDENTIAL_NAMES, revoked_at__isnull=True)
        .values_list("name", flat=True)
    )
    return active_names == set(CANONICAL_CREDENTIAL_NAMES)


def mark_connection_unhealthy(connection, config, code):
    connection.refresh_from_db()
    config.refresh_from_db()
    connection.last_error_at = timezone.now()
    connection.last_error_code = code
    if connection.status != connection.STATUS_ACTIVE or not config.is_active or not _has_active_credentials(config):
        connection.status = connection.STATUS_ERROR
        config.is_active = False
        config.save(update_fields=["is_active", "updated_at"])
    connection.save(update_fields=["status", "last_error_at", "last_error_code", "updated_at"])


@transaction.atomic
def _activate_staged_credentials(
    connection,
    config,
    staged_names,
    *,
    api_key,
    display_name,
    pay_currency,
    health,
):
    connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
    config = ProviderConfig.objects.select_for_update().get(pk=config.pk, connection=connection)
    identity = api_key_identity(api_key)
    duplicate = MerchantProviderConnection.objects.filter(
        provider="nowpayments",
        environment=connection.environment,
        external_account_id=identity,
    ).exclude(pk=connection.pk)
    if duplicate.exists():
        raise NowPaymentsConnectionError("account_already_connected")

    now = timezone.now()
    config.credentials.filter(
        name__in=CANONICAL_CREDENTIAL_NAMES,
        revoked_at__isnull=True,
    ).update(encrypted_value="", revoked_at=now)
    staged = {
        credential.name.rsplit(":", 1)[-1]: credential
        for credential in config.credentials.select_for_update().filter(name__in=staged_names)
    }
    if set(staged) != set(CANONICAL_CREDENTIAL_NAMES):
        raise NowPaymentsConnectionError("staged_credentials_missing")
    for name in CANONICAL_CREDENTIAL_NAMES:
        credential = staged[name]
        credential.name = name
        credential.revoked_at = None
        credential.last_rotated_at = now
        credential.save(update_fields=["name", "revoked_at", "last_rotated_at", "updated_at"])

    provider_settings = dict(config.settings)
    if pay_currency:
        provider_settings["pay_currency"] = pay_currency.lower()
    else:
        provider_settings.pop("pay_currency", None)
    config.settings = provider_settings
    config.display_name = display_name
    config.capabilities = ["crypto", "stablecoin", "hosted_checkout", "webhooks"]
    config.is_active = True
    config.save(update_fields=["settings", "display_name", "capabilities", "is_active", "updated_at"])

    connection.authorization_method = "nowpayments_credentials"
    connection.external_account_id = identity
    connection.status = connection.STATUS_ACTIVE
    connection.capabilities = ["crypto", "stablecoin", "hosted_checkout", "webhooks"]
    connection.granted_scopes = ["currencies:read", "payments:create", "payments:read", "ipn:verify"]
    connection.connected_at = connection.connected_at or now
    connection.revoked_at = None
    connection.last_verified_at = now
    connection.last_error_at = None
    connection.last_error_code = ""
    connection.metadata = {
        "display_hint": f"Key ending in {api_key[-4:]}",
        "api_key_fingerprint": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
        "currency_count": health["currency_count"],
        "ipn_secret_status": "stored",
        "provider_code": config.provider,
    }
    try:
        connection.save()
    except IntegrityError as exc:
        raise NowPaymentsConnectionError("account_already_connected") from exc
    return connection, config


def configure_nowpayments_connection(
    connection,
    config,
    *,
    api_key,
    ipn_secret,
    display_name,
    pay_currency="",
):
    staged_names = _stage_credentials(config, api_key, ipn_secret)
    try:
        health = check_nowpayments_api_key(api_key=api_key, environment=connection.environment)
        return _activate_staged_credentials(
            connection,
            config,
            staged_names,
            api_key=api_key,
            display_name=display_name,
            pay_currency=pay_currency,
            health=health,
        )
    except NowPaymentsConnectionError as exc:
        _erase_credentials(config.credentials.filter(name__in=staged_names))
        mark_connection_unhealthy(connection, config, str(exc))
        raise


def check_connection_health(connection):
    config = connection.provider_configs.filter(kind=ProviderConfig.KIND_CRYPTO, adapter="nowpayments").first()
    if not config or connection.status == connection.STATUS_REVOKED:
        raise NowPaymentsConnectionError("connection_unavailable")
    api_key = provider_secret(config, "api_key")
    ipn_secret = provider_secret(config, "ipn_secret")
    if not api_key or not ipn_secret:
        mark_connection_unhealthy(connection, config, "credentials_missing")
        raise NowPaymentsConnectionError("credentials_missing")
    try:
        health = check_nowpayments_api_key(api_key=api_key, environment=connection.environment)
    except NowPaymentsConnectionError as exc:
        mark_connection_unhealthy(connection, config, str(exc))
        raise
    now = timezone.now()
    connection.last_verified_at = now
    connection.last_error_at = None
    connection.last_error_code = ""
    connection.status = connection.STATUS_ACTIVE
    connection.metadata = {**connection.metadata, **health, "ipn_secret_status": "stored"}
    connection.save(update_fields=["last_verified_at", "last_error_at", "last_error_code", "status", "metadata", "updated_at"])
    config.is_active = True
    config.save(update_fields=["is_active", "updated_at"])
    return True


@transaction.atomic
def disconnect_nowpayments_connection(connection):
    connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
    now = timezone.now()
    configs = connection.provider_configs.select_for_update()
    for config in configs:
        config.credentials.update(encrypted_value="", revoked_at=now)
        config.is_active = False
        config.save(update_fields=["is_active", "updated_at"])
    connection.status = connection.STATUS_REVOKED
    connection.revoked_at = now
    connection.last_error_at = None
    connection.last_error_code = ""
    connection.clear_tokens()
    connection.save()
    return connection
