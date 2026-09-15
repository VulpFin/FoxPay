import base64
import json
import re
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .models import MerchantProviderConnection, ProviderConfig


PAYPAL_API_BASES = {
    "test": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
PAYPAL_WEB_HOSTS = {
    "test": {"www.sandbox.paypal.com", "sandbox.paypal.com"},
    "live": {"www.paypal.com", "paypal.com"},
}
PAYPAL_FEATURES = ("PAYMENT", "REFUND")
PAYPAL_PRODUCT = "EXPRESS_CHECKOUT"
PAYPAL_MERCHANT_ID_RE = re.compile(r"^[2-9A-HJ-NP-Z]{13}$")


class PayPalPartnerError(Exception):
    pass


def partner_credentials(environment):
    if environment == "test":
        values = (
            settings.PAYPAL_PARTNER_CLIENT_ID_TEST,
            settings.PAYPAL_PARTNER_CLIENT_SECRET_TEST,
            settings.PAYPAL_PARTNER_MERCHANT_ID_TEST,
            settings.PAYPAL_PARTNER_ATTRIBUTION_ID_TEST,
        )
    elif environment == "live":
        values = (
            settings.PAYPAL_PARTNER_CLIENT_ID_LIVE,
            settings.PAYPAL_PARTNER_CLIENT_SECRET_LIVE,
            settings.PAYPAL_PARTNER_MERCHANT_ID_LIVE,
            settings.PAYPAL_PARTNER_ATTRIBUTION_ID_LIVE,
        )
    else:
        raise ImproperlyConfigured("Invalid PayPal partner environment.")
    if not all(values):
        raise ImproperlyConfigured("PayPal partner credentials are not configured.")
    if not PAYPAL_MERCHANT_ID_RE.fullmatch(values[2]):
        raise ImproperlyConfigured("PayPal partner merchant ID is invalid.")
    return values


def partner_webhook_id(environment):
    if environment == "test":
        return settings.PAYPAL_PARTNER_WEBHOOK_ID_TEST
    if environment == "live":
        return settings.PAYPAL_PARTNER_WEBHOOK_ID_LIVE
    return ""


def partner_available(environment):
    if not settings.FOXPAY_PAYPAL_PARTNER_ENABLED:
        return False
    try:
        partner_credentials(environment)
    except ImproperlyConfigured:
        return False
    return True


def auth_assertion(client_id, seller_merchant_id):
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("PayPal client ID is required.")
    if not isinstance(seller_merchant_id, str) or not PAYPAL_MERCHANT_ID_RE.fullmatch(seller_merchant_id):
        raise ValueError("PayPal seller merchant ID is invalid.")

    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'iss': client_id, 'payer_id': seller_merchant_id})}."


def platform_access_token(environment):
    client_id, client_secret, _, _ = partner_credentials(environment)
    try:
        response = requests.post(
            f"{PAYPAL_API_BASES[environment]}/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=(3, 10),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise PayPalPartnerError("partner_token_unavailable") from exc
    if response.status_code != 200:
        raise PayPalPartnerError("partner_token_rejected")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise PayPalPartnerError("invalid_partner_token_response") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise PayPalPartnerError("invalid_partner_token_response")
    return token


def partner_headers(environment, *, seller_merchant_id="", request_id="", token=""):
    client_id, _, partner_merchant_id, attribution_id = partner_credentials(environment)
    seller_id = seller_merchant_id or partner_merchant_id
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token or platform_access_token(environment)}",
        "PayPal-Partner-Attribution-Id": attribution_id,
        "PayPal-Auth-Assertion": auth_assertion(client_id, seller_id),
    }
    if request_id:
        headers["PayPal-Request-Id"] = request_id[:108]
    return headers


def partner_request(
    method,
    path,
    *,
    environment,
    seller_merchant_id="",
    body=None,
    params=None,
    request_id="",
    expected=(200,),
):
    if not isinstance(path, str) or not path.startswith("/") or "//" in path:
        raise PayPalPartnerError("invalid_partner_path")
    try:
        response = requests.request(
            method,
            f"{PAYPAL_API_BASES[environment]}{path}",
            json=body,
            params=params,
            headers=partner_headers(
                environment,
                seller_merchant_id=seller_merchant_id,
                request_id=request_id,
            ),
            timeout=(3, 12),
            allow_redirects=False,
        )
    except (KeyError, requests.RequestException) as exc:
        raise PayPalPartnerError("partner_api_unavailable") from exc
    if response.status_code not in expected:
        raise PayPalPartnerError(f"partner_api_http_{response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise PayPalPartnerError("invalid_partner_response") from exc
    if not isinstance(payload, dict):
        raise PayPalPartnerError("invalid_partner_response")
    return payload


def create_partner_referral(*, merchant, environment, return_url, request_id):
    if len(return_url) > 127:
        raise PayPalPartnerError("return_url_too_long")
    body = {
        "tracking_id": str(merchant.uuid),
        "operations": [
            {
                "operation": "API_INTEGRATION",
                "api_integration_preference": {
                    "rest_api_integration": {
                        "integration_method": "PAYPAL",
                        "integration_type": "THIRD_PARTY",
                        "third_party_details": {"features": list(PAYPAL_FEATURES)},
                    }
                },
            }
        ],
        "products": [PAYPAL_PRODUCT],
        "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
        "partner_config_override": {
            "return_url": return_url,
            "return_url_description": "Return to FoxPay",
        },
    }
    payload = partner_request(
        "POST",
        "/v2/customer/partner-referrals",
        environment=environment,
        body=body,
        request_id=request_id,
        expected=(201,),
    )
    links = payload.get("links") if isinstance(payload.get("links"), list) else []
    action_url = next((item.get("href") for item in links if isinstance(item, dict) and item.get("rel") == "action_url"), "")
    self_url = next((item.get("href") for item in links if isinstance(item, dict) and item.get("rel") == "self"), "")
    action = urlsplit(action_url)
    self_link = urlsplit(self_url)
    if action.scheme != "https" or action.hostname not in PAYPAL_WEB_HOSTS[environment]:
        raise PayPalPartnerError("invalid_referral_action_url")
    if self_link.scheme != "https" or f"{self_link.scheme}://{self_link.netloc}" != PAYPAL_API_BASES[environment]:
        raise PayPalPartnerError("invalid_referral_self_url")
    return action_url


def show_seller_status(*, environment, seller_merchant_id):
    if not PAYPAL_MERCHANT_ID_RE.fullmatch(seller_merchant_id or ""):
        raise PayPalPartnerError("invalid_seller_merchant_id")
    _, _, partner_merchant_id, _ = partner_credentials(environment)
    return partner_request(
        "GET",
        f"/v1/customer/partners/{quote(partner_merchant_id, safe='')}/merchant-integrations/{quote(seller_merchant_id, safe='')}",
        environment=environment,
        seller_merchant_id=seller_merchant_id,
    )


def seller_state(payload, *, environment, expected_tracking_id, expected_merchant_id):
    client_id, _, _, _ = partner_credentials(environment)
    if payload.get("merchant_id") != expected_merchant_id or payload.get("tracking_id") != expected_tracking_id:
        raise PayPalPartnerError("seller_status_identity_mismatch")
    scopes = []
    for integration in payload.get("oauth_integrations") or []:
        if not isinstance(integration, dict) or integration.get("integration_type") != "OAUTH_THIRD_PARTY":
            continue
        for detail in integration.get("oauth_third_party") or []:
            if not isinstance(detail, dict) or detail.get("partner_client_id") != client_id:
                continue
            scopes.extend(scope for scope in detail.get("scopes") or [] if isinstance(scope, str))
    payment_granted = any("/payments/realtimepayment" in scope or "/payments/payment/authcapture" in scope for scope in scopes)
    refund_granted = any("/payments/refund" in scope for scope in scopes)
    product_names = [
        str(product.get("name"))[:80]
        for product in payload.get("products") or []
        if isinstance(product, dict) and product.get("name")
    ][:20]
    risk_states = [
        str(product.get("vetting_status") or product.get("status"))[:80]
        for product in payload.get("products") or []
        if isinstance(product, dict) and (product.get("vetting_status") or product.get("status"))
    ][:20]
    payments_receivable = payload.get("payments_receivable") is True
    email_confirmed = payload.get("primary_email_confirmed") is True
    consent = payment_granted and refund_granted
    ready = payments_receivable and email_confirmed and consent
    capabilities = ["card", "wallet", "hosted_checkout", "webhooks", "multicurrency"]
    if refund_granted:
        capabilities.append("refunds")
    return {
        "ready": ready,
        "capabilities": capabilities,
        "metadata": {
            "display_hint": f"Merchant {expected_merchant_id[-4:]}",
            "tracking_id": expected_tracking_id,
            "account_status": "receivable" if payments_receivable else "limited",
            "consent_status": consent,
            "payments_receivable": payments_receivable,
            "primary_email_confirmed": email_confirmed,
            "products": product_names,
            "risk_status": risk_states,
            "payment_permission": payment_granted,
            "refund_permission": refund_granted,
        },
    }


@transaction.atomic
def apply_seller_status(connection, payload):
    connection = MerchantProviderConnection.objects.select_for_update().get(pk=connection.pk)
    state = seller_state(
        payload,
        environment=connection.environment,
        expected_tracking_id=str(connection.merchant.uuid),
        expected_merchant_id=connection.external_account_id,
    )
    webhook_ready = bool(partner_webhook_id(connection.environment))
    config = connection.provider_configs.filter(kind=ProviderConfig.KIND_CARD, adapter="paypal").first()
    if not config:
        config = ProviderConfig(
            merchant=connection.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider=f"paypal-{connection.uuid.hex}",
            adapter="paypal",
            display_name="PayPal",
            environment=connection.environment,
        )
    config.settings = {
        "paypal_env": "live" if connection.environment == "live" else "sandbox",
        "partner": True,
    }
    config.capabilities = state["capabilities"]
    config.is_active = state["ready"] and webhook_ready
    config.save()
    connection.status = connection.STATUS_ACTIVE if config.is_active else connection.STATUS_RESTRICTED
    connection.granted_scopes = list(PAYPAL_FEATURES) if state["metadata"]["consent_status"] else []
    connection.capabilities = state["capabilities"]
    connection.metadata = state["metadata"]
    connection.last_verified_at = timezone.now()
    connection.last_error_at = None
    if not state["ready"]:
        connection.last_error_code = "paypal_seller_not_ready"
    elif not webhook_ready:
        connection.last_error_code = "webhook_not_configured"
    else:
        connection.last_error_code = ""
    connection.save()
    return connection, config


def refresh_partner_connection(connection):
    payload = show_seller_status(
        environment=connection.environment,
        seller_merchant_id=connection.external_account_id,
    )
    return apply_seller_status(connection, payload)


def verify_partner_webhook(request, *, environment, event):
    webhook_id = partner_webhook_id(environment)
    required = (
        "Paypal-Transmission-Id",
        "Paypal-Transmission-Time",
        "Paypal-Cert-Url",
        "Paypal-Auth-Algo",
        "Paypal-Transmission-Sig",
    )
    if not webhook_id or any(not request.headers.get(header) for header in required):
        return False
    payload = partner_request(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        environment=environment,
        body={
            "transmission_id": request.headers.get("Paypal-Transmission-Id"),
            "transmission_time": request.headers.get("Paypal-Transmission-Time"),
            "cert_url": request.headers.get("Paypal-Cert-Url"),
            "auth_algo": request.headers.get("Paypal-Auth-Algo"),
            "transmission_sig": request.headers.get("Paypal-Transmission-Sig"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        },
    )
    return payload.get("verification_status") == "SUCCESS"


def seller_id_from_event(event):
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    candidates = [
        event.get("merchant_id"),
        resource.get("merchant_id"),
        resource.get("payer_id"),
        resource.get("merchantIdInPayPal"),
    ]
    payee = resource.get("payee") if isinstance(resource.get("payee"), dict) else {}
    candidates.append(payee.get("merchant_id"))
    for unit in resource.get("purchase_units") or []:
        if isinstance(unit, dict) and isinstance(unit.get("payee"), dict):
            candidates.append(unit["payee"].get("merchant_id"))
    for disputed in resource.get("disputed_transactions") or []:
        if not isinstance(disputed, dict):
            continue
        seller = disputed.get("seller") if isinstance(disputed.get("seller"), dict) else {}
        candidates.extend([seller.get("merchant_id"), seller.get("payer_id")])
    return next((value for value in candidates if isinstance(value, str) and PAYPAL_MERCHANT_ID_RE.fullmatch(value)), "")
