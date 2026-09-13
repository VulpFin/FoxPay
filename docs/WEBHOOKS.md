# Webhooks

Fox Pay has two webhook directions.

## Incoming Provider Webhooks

Provider webhook flow:

1. Provider calls Fox Pay.
2. Fox Pay verifies the signature.
3. Fox Pay records the delivery.
4. Fox Pay normalizes provider state.
5. Fox Pay updates payment records.
6. Fox Pay emits merchant-facing webhook events.

Incoming webhook processing is idempotent through provider event IDs.

Stripe Checkout webhooks use Stripe's native signature verification and raw
request body. Configure the endpoint in Stripe as:

```text
https://foxpay.fyi/api/v1/webhooks/stripe/stripe-primary/
```

Store the endpoint signing secret as the provider credential named
`webhook_secret`. The `configure_stripe_provider` command can do this from
`STRIPE_WEBHOOK_SECRET`.

NOWPayments IPNs use `x-nowpayments-sig`, an HMAC-SHA512 signature of the JSON
body with recursively sorted keys. Enter this callback URL in the NOWPayments
dashboard and use the same URL when creating invoices:

```text
https://foxpay.fyi/api/v1/webhooks/nowpayments/vulpfin/nowpayments-primary/
```

The account-generated IPN secret must be stored as the encrypted provider
credential `ipn_secret`. Until it is configured, this endpoint returns 503 and
does not accept notifications. The `finished` status settles a matching Fox Pay
invoice; `partially_paid` and intermediate states do not.

## Outgoing Merchant Webhooks

Merchant webhook events are stored in `MerchantWebhookEvent`.

Endpoints are stored in `MerchantWebhookEndpoint`. Endpoint secrets are encrypted at rest and used to sign outgoing requests with `FoxPay-Signature`.

Deliver pending events:

```powershell
python manage.py deliver_webhooks
```

The current implementation is a management-command outbox. Production deployments should run delivery through a durable queue with retries, backoff, dead-letter handling, and monitoring.
