# Webhooks

FoxPay receives signed provider events and sends independently signed normalized events to merchants. Browser callbacks and redirects are never authoritative payment notifications.

## Incoming provider events

For every supported receiver FoxPay:

1. reads the untouched request body required by that provider's signature scheme;
2. verifies the environment-specific signature or verification API result;
3. resolves the provider account to one `MerchantProviderConnection` or legacy route;
4. records an idempotent event with only the minimum safe fields needed for reconciliation;
5. validates merchant, intent, attempt, external references, amount, and currency before mutation;
6. acknowledges valid duplicates without applying state twice;
7. queues work that should not happen in the request, including PayPal approved-order capture;
8. emits a normalized merchant event after state changes.

A valid signature alone is insufficient. Unknown accounts and mismatched objects cannot settle a payment.

### Stripe Connect

```text
https://foxpay.fyi/api/v1/webhooks/stripe/connect/test/
https://foxpay.fyi/api/v1/webhooks/stripe/connect/live/
```

Configure these as platform Connect destinations for events on connected accounts. FoxPay verifies `Stripe-Signature` with the matching platform Connect secret and routes using the event's top-level `account`. Production Stripe destinations may deliver test events; FoxPay still derives the effective environment from signed `livemode` and refuses live events on the test endpoint.

Handled state includes Checkout completion/failure/expiry, refunds, disputes, account capability updates, and deauthorization.

### Square OAuth

```text
https://foxpay.fyi/api/v1/webhooks/square/oauth/test/
https://foxpay.fyi/api/v1/webhooks/square/oauth/live/
```

The configured URL must match Square's saved notification URL byte-for-byte, including the trailing slash, because Square signs the URL and raw body together. FoxPay routes by Square merchant/location identifiers and validates payment, refund, and dispute ownership before reconciliation. OAuth revocation disables the connection.

### PayPal Partner

```text
https://foxpay.fyi/api/v1/webhooks/paypal/partner/test/
https://foxpay.fyi/api/v1/webhooks/paypal/partner/live/
```

FoxPay asks PayPal's verification endpoint to validate the transmission headers and body against the configured partner webhook ID. `CHECKOUT.ORDER.APPROVED` is durably recorded but is not marked paid; the worker rechecks authorization and captures the order using seller-scoped partner credentials. Capture, refund, dispute, onboarding, and consent-revocation events are normalized only after merchant ownership checks.

### NOWPayments

Each seller route has its own URL:

```text
https://foxpay.fyi/api/v1/webhooks/nowpayments/{merchant-slug}/{provider-code}/
```

NOWPayments signs canonicalized JSON with HMAC-SHA512 in `x-nowpayments-sig`. FoxPay verifies the seller connection's encrypted IPN secret before parsing settlement fields. The invoice/order reference, merchant, amount, and currency must match. Missing configuration returns `503`; invalid signatures return `401`; partial/intermediate payments do not settle an intent.

Legacy operator-configured Stripe, Square, and PayPal route endpoints remain available only for migration compatibility. New sellers use the connection-scoped endpoints above.

## Outgoing merchant events

Each enabled `MerchantWebhookEndpoint` receives a compact JSON body:

```json
{
  "id": "event-uuid",
  "type": "payment.succeeded",
  "created": "2026-09-15T00:00:00+00:00",
  "data": {}
}
```

Headers include:

- `FoxPay-Event-ID`: stable event UUID for merchant idempotency;
- `FoxPay-Signature`: lowercase hex HMAC-SHA256 of the exact request body using the endpoint secret;
- `Content-Type: application/json`.

The secret is shown once and encrypted at rest. Rotation creates a staged secret; activation retires the previous secret without exposing it again.

Celery beat schedules pending events, and workers deliver them with bounded exponential backoff. Events are processed oldest-first in bounded batches; endpoint delivery has a Redis lock and a maximum attempt count. Delivery history records safe status/error information, not request credentials.

Merchant webhook URLs use the shared safe HTTP client: HTTPS is required, unsafe address classes are rejected after DNS resolution, the validated connection target is pinned, redirects are not followed, and response size/time are bounded.

The `deliver_webhooks` management command is retained for controlled recovery; it is not the normal production scheduler.
