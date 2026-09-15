# FoxPay API

Merchant API endpoints are versioned under `/api/v1/`. Send the environment-bound key in `X-FoxPay-Key`. Every response includes `X-Request-ID`; mutation requests should carry a stable `Idempotency-Key`.

Keys are stored as hashes, scoped, shown only once, and rejected when revoked or when the merchant lifecycle, environment, temporary restriction, or live-payment kill switch disallows routing.

Error responses use a safe envelope:

```json
{
  "error": {
    "type": "invalid_request",
    "code": "invalid_request",
    "message": "Human-readable safe message.",
    "request_id": "req_..."
  }
}
```

## Create a payment intent

`POST /api/v1/payment-intents/`

Required scope: `payments:write`

```json
{
  "amount": 2500,
  "currency": "USD",
  "description": "FoxPay test charge",
  "payment_methods": ["card", "crypto"],
  "success_url": "https://shop.example/orders/1001/paid",
  "cancel_url": "https://shop.example/orders/1001",
  "customer": {
    "external_id": "cust_1001",
    "tg11_user_uuid": "00000000-0000-4000-8000-000000000001",
    "email": "customer@example.com",
    "name": "Customer Name"
  },
  "metadata": {
    "order_id": "ORDER-1001"
  }
}
```

Amounts are integer minor units. Currency and provider support are route-dependent. Success and cancel URLs must match a merchant-registered origin; live URLs require HTTPS and pass the outbound URL policy. Arbitrary per-request redirect origins are rejected.

`customer.tg11_user_uuid` must be an identity the merchant authenticated. FoxPay never links a customer's records by email alone. Conflicting merchant customer IDs and TG11 subjects return `409`.

Only automatic capture is supported. `capture_strategy: "manual"` returns `501 manual_capture_unavailable` before a provider checkout is created. Raw card fields such as number, CVV, track, or EMV data return `422 raw_card_data_forbidden`.

The response includes a `foxpay_checkout_url` and eligible options. The merchant must wait for its signed FoxPay webhook or retrieve the intent; a success redirect is not settlement evidence.

## Retrieve a payment intent

`GET /api/v1/payment-intents/{id}/`

Required scope: `payments:read`

Only the API key's merchant can resolve the public intent ID.

## Create a refund

`POST /api/v1/payment-intents/{id}/refunds/`

Required scope: `refunds:write`

```json
{
  "amount": 500,
  "reason": "requested_by_customer",
  "metadata": {
    "support_case": "CASE-42"
  }
}
```

FoxPay locks the payment and existing refunds while reserving the refundable amount, creates a pending refund before the network call, and sends a provider idempotency key. Stripe, Square, PayPal, and mock routes are supported. NOWPayments/manual crypto returns `501 manual_crypto_refund_required`.

A settled provider result returns `201`; an unknown or processing outcome returns `202` with `reconciliation_required: true`. FoxPay never converts a timeout into success. Reusing an idempotency key returns the original merchant refund.

## Sync a subscription reference

`POST /api/v1/subscription-references/`

Required scope: `subscriptions:write`

This upserts a merchant-owned display reference. It does not create, charge, or cancel a provider subscription.

```json
{
  "customer": {
    "external_id": "cust_1001",
    "tg11_user_uuid": "00000000-0000-4000-8000-000000000001"
  },
  "provider": "stripe",
  "provider_reference": "sub_example",
  "plan_name": "Monthly plan",
  "status": "active",
  "amount": 1200,
  "currency": "USD",
  "current_period_end": "2027-01-01T00:00:00Z",
  "cancel_at_period_end": false
}
```

An existing subscription reference cannot be reassigned to another customer or merchant.

## OpenAPI outline

`GET /api/v1/openapi.json`

The generated document is an endpoint outline, not a replacement for provider-specific webhook setup or this behavioral contract.
