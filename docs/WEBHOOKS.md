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

Square webhook subscriptions use this production notification URL:

```text
https://foxpay.fyi/api/v1/webhooks/square/vulpfin/square-primary/
```

Run `configure_square_webhook_provider --merchant vulpfin --environment live`
to create the inactive receiver before saving the subscription.
Select `payment.created` and `payment.updated`; add `refund.created` and
`refund.updated` when tracking Square refunds. Square displays the subscription
Signature Key after it is saved. Put it in `SQUARE_WEBHOOK_SIGNATURE_KEY` in the
server environment and rerun the command. The key is then stored as an encrypted
`webhook_signature_key` provider credential. Never put it in Git or command-line
arguments. The URL must match the saved subscription URL byte-for-byte, including
its trailing slash, because Square signs the URL together with the raw body.

The Square receiver returns 503 for POST until the signature key is configured.
Afterward it verifies `x-square-hmacsha256-signature`, deduplicates `event_id`,
and records a minimal event summary without card details. When Square hosted
checkout is active, it also reconciles `payment.created` and `payment.updated`
for the exact order, location, amount, currency, and merchant. Unmatched or
mismatched events never settle an intent. Other subscribed event types are only
recorded; they do not mutate payment state.

## Outgoing Merchant Webhooks

Merchant webhook events are stored in `MerchantWebhookEvent`.

Endpoints are stored in `MerchantWebhookEndpoint`. Endpoint secrets are encrypted at rest and used to sign outgoing requests with `FoxPay-Signature`.

Deliver pending events:

```powershell
python manage.py deliver_webhooks
```

The current implementation is a management-command outbox. Production deployments should run delivery through a durable queue with retries, backoff, dead-letter handling, and monitoring.
