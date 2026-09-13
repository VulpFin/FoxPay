# Providers

Provider configuration is merchant-scoped and environment-specific.

Important fields:

- `kind`: `card` or `crypto`
- `provider`: merchant-facing provider code
- `adapter`: Fox Pay adapter implementation
- `environment`: `test` or `live`
- `priority`: lower values are offered first
- `capabilities`: adapter capability metadata
- `settings`: non-sensitive provider settings
- `credentials`: encrypted provider secrets

Sensitive credentials must go through `ProviderCredential`, not arbitrary JSON settings.

## Stripe Checkout

The Stripe adapter uses hosted Checkout. Fox Pay creates a provider attempt and
Stripe Checkout Session, then redirects the customer to Stripe for card entry.
Credit and debit cards both use Stripe's `card` payment method type.

Recommended provider config:

```json
{
  "kind": "card",
  "provider": "stripe-primary",
  "adapter": "stripe",
  "display_name": "Stripe Checkout",
  "priority": 10,
  "is_active": true,
  "settings": {
    "payment_method_types": ["card"],
    "automatic_tax": true,
    "tax_behavior": "exclusive"
  }
}
```

Store secrets as encrypted credentials:

```powershell
python manage.py configure_stripe_provider `
  --merchant vulpfin `
  --provider stripe-primary `
  --environment test `
  --activate `
  --secret-key-env STRIPE_SECRET_KEY `
  --webhook-secret-env STRIPE_WEBHOOK_SECRET
```

Stripe webhooks should point to:

```text
https://foxpay.fyi/api/v1/webhooks/stripe/stripe-primary/
```

Supported incoming Stripe events:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`

Fox Pay verifies the `Stripe-Signature` header against the encrypted
`webhook_secret` credential before normalizing the event.

Set `allow_customer_method_setup: true` in a merchant's Stripe provider
settings to allow signed-in TG11 customers to save cards through Stripe-hosted
setup Checkout. The same signed webhook records masked card references. Card
removal requires a recent TG11 MFA sign-in and is blocked while that customer
has an active subscription reference.

## Square Hosted Checkout

Square uses a hosted payment link created by the Checkout API, so FoxPay does
not handle card entry. First run `configure_square_webhook_provider` to store
the subscription signing key. Then set `SQUARE_ACCESS_TOKEN` and run:

```text
python manage.py configure_square_checkout_provider --merchant vulpfin --environment live --location-id LOCATION_ID --square-merchant-id MERCHANT_ID
```

The access token is encrypted into `ProviderCredential`. Add `--activate` only
after confirming the token, location, and signed webhook all match. FoxPay
offers Square alongside other active card providers. Signed `payment.created`
and `payment.updated` events can settle only attempts matching the Square order
ID, location, amount, currency, and merchant ID. Refunds remain unavailable
through the FoxPay API. See [WEBHOOKS.md](WEBHOOKS.md).

## NOWPayments Hosted Crypto

Fox Pay creates a NOWPayments hosted invoice with a unique `order_id` for each
crypto attempt. Its IPN callback is signed with the account's IPN secret. Only
the `finished` status can settle a Fox Pay payment, after the callback matches
the attempt and USD invoice amount. `partially_paid` is not settled.

Configure the provider after creating a NOWPayments API key and IPN secret:

```powershell
python manage.py configure_nowpayments_provider `
  --merchant vulpfin `
  --provider nowpayments-primary `
  --environment live `
  --activate
```

The command reads `NOWPAYMENTS_API_KEY` and `NOWPAYMENTS_IPN_SECRET` from the
environment and stores them encrypted. Omit `--activate` to register the IPN
URL before credentials are available. Only USD-priced invoices are currently
supported by this adapter; NOWPayments can present a selected crypto asset via
the optional `--pay-currency` setting.

## Current Adapters

- `mock`: local card/debit sandbox checkout.
- `stripe`: creates Stripe-hosted Checkout Sessions for card and debit card payment.
- `square`: creates Square-hosted payment links and reconciles signed payment events.
- `hosted`: redirects to a configured hosted card checkout URL.
- `manual`: non-custodial crypto invoice to merchant wallet addresses.
- `nowpayments`: creates hosted crypto invoices and verifies signed IPNs.

## Current Routing

Routing is deterministic:

1. Active configs matching merchant, environment, and method.
2. Sort by priority.
3. Create payment options for every matching provider.
4. In test mode only, fall back to merchant-level legacy fields if no configs exist.
