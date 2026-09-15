import hashlib

from django.core.exceptions import ImproperlyConfigured

from .abuse import AbuseControlError, enforce_provider_call
from .adapters.base import ProviderAdapterError
from .adapters.card import get_card_adapter
from .adapters.paypal_checkout import PayPalCheckoutAdapter
from .adapters.square_webhooks import record_square_event, square_payment_update
from .adapters.stripe_checkout import object_to_dict, stripe_module
from .models import Merchant, PaymentAttempt, ProviderConfig
from .paypal_events import process_paypal_event
from .permissions import routing_block_reason
from .routing import authorized_config
from .services import record_webhook


def _ready_attempt(attempt_id):
    attempt = (
        PaymentAttempt.objects.select_related("intent", "intent__merchant", "provider_config", "provider_config__connection")
        .filter(
            pk=attempt_id,
            status__in=[PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_ACTION_REQUIRED],
        )
        .first()
    )
    if not attempt or not attempt.provider_config_id:
        return None
    merchant = Merchant.objects.get(pk=attempt.intent.merchant_id)
    if routing_block_reason(merchant, attempt.intent.environment):
        return None
    config = ProviderConfig.objects.select_related("connection").get(pk=attempt.provider_config_id)
    if config.merchant_id != merchant.pk or config.environment != attempt.intent.environment or not authorized_config(merchant, config):
        return None
    try:
        enforce_provider_call(
            merchant,
            ip_hash=attempt.intent.request_ip_hash,
            environment=attempt.intent.environment,
        )
    except AbuseControlError:
        return None
    attempt.provider_config = config
    return attempt


def _stripe(attempt):
    adapter = get_card_adapter(attempt.provider_config.adapter_name, attempt.provider_config)
    stripe_account = None
    connection = attempt.provider_config.connection
    if connection and connection.authorization_method == "stripe_connect":
        stripe_account = connection.external_account_id
    session = stripe_module().checkout.Session.retrieve(
        attempt.provider_reference,
        api_key=adapter.secret_key(),
        **({"stripe_account": stripe_account} if stripe_account else {}),
    )
    data = object_to_dict(session) or session
    if not isinstance(data, dict):
        return False
    if data.get("payment_status") == "paid":
        event_type = "checkout.session.completed"
    elif data.get("status") == "expired":
        event_type = "checkout.session.expired"
    else:
        return False
    digest = hashlib.sha256(f"{data.get('id')}:{event_type}:{data.get('payment_status')}".encode()).hexdigest()[:32]
    if connection and connection.authorization_method == "stripe_connect":
        from .stripe_connect_events import handle_connect_event

        return handle_connect_event(
            connection,
            {
                "id": f"foxpay-reconcile-{digest}",
                "type": event_type,
                "account": connection.external_account_id,
                "livemode": attempt.intent.environment == "live",
                "data": {"object": data},
            },
        )
    status = "paid" if data.get("payment_status") == "paid" else "expired"
    delivery = record_webhook(
        f"stripe:{attempt.provider_config_id}",
        {
            "id": f"foxpay-reconcile-{digest}",
            "type": event_type,
            "payment_intent": attempt.intent.public_id,
            "foxpay_attempt_id": str(attempt.pk),
            "foxpay_method": PaymentAttempt.METHOD_CARD,
            "status": status,
            "provider_reference": str(data.get("id", "")),
        },
    )
    return delivery.processed


def _paypal(attempt):
    adapter = PayPalCheckoutAdapter(attempt.provider_config)
    order = adapter.retrieve(attempt.provider_reference)
    settlement = adapter.settlement_event(order)
    if settlement:
        connection = attempt.provider_config.connection
        if connection and connection.authorization_method == "paypal_partner":
            settlement["resource"]["payee"] = {"merchant_id": connection.external_account_id}
        result = process_paypal_event(attempt.provider_config, settlement)
        return result["processed"]
    if order.get("status") == "VOIDED":
        normalized = adapter.normalize_webhook({
            "id": f"reconcile:{order.get('id')}:{order.get('status')}",
            "event_type": "CHECKOUT.ORDER.VOIDED",
            "resource": order,
        })
        return record_webhook(f"paypal:{attempt.provider_config_id}", normalized).processed
    return False


def _square(attempt):
    adapter = get_card_adapter("square", attempt.provider_config)
    connection = attempt.provider_config.connection
    merchant_id = (
        connection.external_account_id
        if connection and connection.authorization_method == "square_oauth"
        else attempt.provider_config.settings.get("square_merchant_id", "")
    )
    if not merchant_id:
        return False
    order_id = attempt.provider_response_metadata.get("square_order_id", "")
    if not order_id:
        return False
    order_data = adapter._refund_request("GET", f"/v2/orders/{order_id}")
    order = order_data.get("order") if isinstance(order_data, dict) else None
    if not isinstance(order, dict):
        return False
    payment_ids = [
        item.get("payment_id") or item.get("id")
        for item in (order.get("tenders") or [])
        if isinstance(item, dict) and (item.get("payment_id") or item.get("id"))
    ]
    for payment_id in payment_ids:
        payment_data = adapter._refund_request("GET", f"/v2/payments/{payment_id}")
        payment = payment_data.get("payment") if isinstance(payment_data, dict) else None
        if not isinstance(payment, dict):
            continue
        digest = hashlib.sha256(f"{payment_id}:{payment.get('status')}".encode()).hexdigest()[:32]
        event = {
            "event_id": f"foxpay-reconcile-{digest}",
            "type": "payment.updated",
            "merchant_id": merchant_id,
            "location_id": attempt.provider_response_metadata.get("square_location_id", ""),
            "data": {"type": "payment", "id": payment_id, "object": {"payment": payment}},
        }
        recorded = record_square_event(attempt.provider_config, event)
        update, _ = square_payment_update(attempt.provider_config, event)
        if update:
            record_webhook(f"square-payment:{attempt.provider_config_id}", update)
            return True
        if recorded:
            return False
    return False


def reconcile_payment_attempt(attempt_id):
    attempt = _ready_attempt(attempt_id)
    if not attempt:
        return False
    adapter = attempt.provider_config.adapter_name
    try:
        if adapter in {"stripe", "stripe_checkout"}:
            return _stripe(attempt)
        if adapter in {"paypal", "paypal_checkout"}:
            return _paypal(attempt)
        if adapter == "square":
            return _square(attempt)
    except (ProviderAdapterError, ImproperlyConfigured, ValueError):
        return False
    return False
