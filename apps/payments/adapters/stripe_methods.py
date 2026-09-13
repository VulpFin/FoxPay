import uuid
from urllib.parse import urlsplit

from django.db import transaction
from django.urls import reverse

from apps.payments.models import (
    PaymentMethodReference,
    ProviderCustomerReference,
    ProviderEvent,
    WebhookDelivery,
)

from .stripe_checkout import StripeCheckoutAdapter, object_value, stripe_module


def _secret(config):
    return StripeCheckoutAdapter(config).secret_key()


def create_setup_checkout(request, config, customer):
    if customer.merchant_id != config.merchant_id:
        raise ValueError("Provider and customer merchants do not match.")
    stripe = stripe_module()
    secret = _secret(config)
    reference = ProviderCustomerReference.objects.filter(provider_config=config, customer=customer).first()
    if not reference:
        customer_params = {
            "metadata": {"foxpay_customer_uuid": str(customer.uuid), "foxpay_merchant_uuid": str(config.merchant.uuid)},
            "api_key": secret,
            "idempotency_key": f"foxpay:customer:{config.pk}:{customer.uuid}",
        }
        if customer.email:
            customer_params["email"] = customer.email
        created = stripe.Customer.create(
            **customer_params,
        )
        customer_id = object_value(created, "id", "")
        if not customer_id or bool(object_value(created, "livemode", False)) != (config.environment == "live"):
            raise ValueError("Stripe customer environment did not match FoxPay.")
        reference, _ = ProviderCustomerReference.objects.get_or_create(
            provider_config=config,
            customer=customer,
            defaults={"provider_reference": customer_id},
        )
    success_url = request.build_absolute_uri(reverse("account_payment_methods")) + "?saved=1"
    cancel_url = request.build_absolute_uri(reverse("account_payment_methods"))
    session = stripe.checkout.Session.create(
        mode="setup",
        customer=reference.provider_reference,
        payment_method_types=["card"],
        client_reference_id=str(customer.uuid),
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"foxpay_customer_uuid": str(customer.uuid), "foxpay_provider_config_id": str(config.pk)},
        setup_intent_data={"metadata": {"foxpay_customer_uuid": str(customer.uuid)}},
        api_key=secret,
        idempotency_key=f"foxpay:setup:{uuid.uuid4()}",
    )
    url = object_value(session, "url", "")
    if (
        not isinstance(url, str)
        or urlsplit(url).scheme != "https"
        or urlsplit(url).hostname != "checkout.stripe.com"
        or bool(object_value(session, "livemode", False)) != (config.environment == "live")
        or object_value(session, "customer") != reference.provider_reference
    ):
        raise ValueError("Stripe did not return a hosted setup URL.")
    return url


@transaction.atomic
def record_setup_checkout(config, event):
    if not config or event.get("type") != "checkout.session.completed":
        return False
    session = event.get("data", {}).get("object", {})
    if not isinstance(session, dict) or session.get("mode") != "setup" or session.get("status") != "complete":
        return False
    metadata = session.get("metadata") or {}
    if not isinstance(metadata, dict) or metadata.get("foxpay_provider_config_id") != str(config.pk):
        return False
    try:
        customer_uuid = uuid.UUID(metadata.get("foxpay_customer_uuid", ""))
    except (TypeError, ValueError):
        return False
    reference = ProviderCustomerReference.objects.select_related("customer").filter(
        provider_config=config,
        customer__uuid=customer_uuid,
        customer__merchant=config.merchant,
        provider_reference=session.get("customer", ""),
    ).first()
    if not reference or session.get("client_reference_id") != str(customer_uuid):
        return False
    if bool(event.get("livemode", False)) != (config.environment == "live"):
        return False
    setup_id = session.get("setup_intent")
    if not isinstance(setup_id, str) or not setup_id:
        return False
    provider = f"stripe-setup:{config.pk}"
    event_id = event.get("id", "")
    if not event_id:
        return False
    if ProviderEvent.objects.filter(provider=provider, provider_event_id=event_id).exists():
        return True
    stripe = stripe_module()
    secret = _secret(config)
    setup = stripe.SetupIntent.retrieve(setup_id, api_key=secret)
    payment_method_id = object_value(setup, "payment_method", "")
    if object_value(setup, "status") != "succeeded" or object_value(setup, "customer") != reference.provider_reference or not payment_method_id:
        raise ValueError("Stripe setup intent is not complete for this customer.")
    method = stripe.PaymentMethod.retrieve(payment_method_id, api_key=secret)
    if object_value(method, "customer") != reference.provider_reference or object_value(method, "type") != "card":
        raise ValueError("Stripe payment method does not belong to this customer.")
    card = object_value(method, "card", {}) or {}
    method_id = object_value(method, "id", "")
    if not method_id:
        raise ValueError("Stripe payment method ID is missing.")
    PaymentMethodReference.objects.update_or_create(
        provider_config=config,
        provider_reference=method_id,
        defaults={
            "merchant": config.merchant,
            "customer": reference.customer,
            "provider": config.provider,
            "type": "card",
            "display_metadata": {
                "brand": object_value(card, "brand", ""),
                "last4": object_value(card, "last4", ""),
                "exp_month": object_value(card, "exp_month", None),
                "exp_year": object_value(card, "exp_year", None),
            },
        },
    )
    summary = {"foxpay_customer_uuid": str(customer_uuid), "provider_config_id": config.pk, "payment_method_id": method_id}
    ProviderEvent.objects.create(
        merchant=config.merchant,
        provider=provider,
        provider_event_id=event_id,
        event_type="checkout.session.completed",
        normalized_event_type="payment_method.saved",
        payload=summary,
    )
    WebhookDelivery.objects.create(
        provider=provider,
        event_type="checkout.session.completed",
        provider_event_id=event_id,
        payload=summary,
        processed=True,
    )
    return True


def detach_saved_method(method):
    config = method.provider_config
    if not config or config.adapter_name not in {"stripe", "stripe_checkout"} or not method.customer:
        raise ValueError("Only Stripe-hosted saved methods can be detached here.")
    reference = ProviderCustomerReference.objects.filter(provider_config=config, customer=method.customer).first()
    if not reference:
        raise ValueError("Provider customer link is missing.")
    stripe = stripe_module()
    secret = _secret(config)
    remote = stripe.PaymentMethod.retrieve(method.provider_reference, api_key=secret)
    if object_value(remote, "customer") != reference.provider_reference:
        raise ValueError("Provider payment method ownership could not be verified.")
    stripe.PaymentMethod.detach(method.provider_reference, api_key=secret)
    method.delete()
