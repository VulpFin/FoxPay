# Fox Pay API

All current API endpoints are versioned under `/api/v1/`.

Every API response includes an `X-Request-ID` header. Error responses include:

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

## Create Payment Intent

`POST /api/v1/payment-intents/`

Required header:

- `X-FoxPay-Key`

Recommended header:

- `Idempotency-Key`

Example body:

```json
{
  "amount": 2500,
  "currency": "USD",
  "description": "Fox Pay test charge",
  "payment_methods": ["card", "crypto"],
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

`customer.tg11_user_uuid` must come from a TG11 identity the merchant actually
authenticated. FoxPay never links customer data to a dashboard by email alone.
Conflicting merchant customer IDs and TG11 subjects return 409.

Only automatic capture is currently supported. Requests with
`capture_strategy: "manual"` return `501 manual_capture_unavailable` before any
provider session is created.

## Retrieve Payment Intent

`GET /api/v1/payment-intents/{id}/`

Requires `payments:read`.

## Create Refund

`POST /api/v1/payment-intents/{id}/refunds/`

Requires `refunds:write`.

This endpoint currently supports test-mode mock payments only. For Stripe or
other live providers it returns `501 provider_refund_unavailable` and does not
create a refund or ledger entry. Issue live refunds in the provider dashboard
until a provider-backed refund flow is implemented.

## Sync Subscription Reference

`POST /api/v1/subscription-references/`

Requires a merchant API key with `subscriptions:write`. This upserts a
merchant-owned display reference; it does not create a provider subscription,
charge a card, or cancel billing. The authenticated merchant must have verified
the customer's TG11 UUID before sending it.

```json
{
  "customer": {"external_id": "cust_1001", "tg11_user_uuid": "00000000-0000-4000-8000-000000000001"},
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

An existing subscription reference cannot be reassigned to another customer.

## OpenAPI

`GET /api/v1/openapi.json`
