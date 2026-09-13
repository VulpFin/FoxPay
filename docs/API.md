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
    "email": "customer@example.com",
    "name": "Customer Name"
  },
  "metadata": {
    "order_id": "ORDER-1001"
  }
}
```

## Retrieve Payment Intent

`GET /api/v1/payment-intents/{id}/`

Requires `payments:read`.

## Create Refund

`POST /api/v1/payment-intents/{id}/refunds/`

Requires `refunds:write`.

## OpenAPI

`GET /api/v1/openapi.json`

