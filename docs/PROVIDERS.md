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
    "payment_method_types": ["card"]
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

## Current Adapters

- `mock`: local card/debit sandbox checkout.
- `stripe`: creates Stripe-hosted Checkout Sessions for card and debit card payment.
- `hosted`: redirects to a configured hosted card checkout URL.
- `manual`: non-custodial crypto invoice to merchant wallet addresses.

## Current Routing

Routing is deterministic:

1. Active configs matching merchant, environment, and method.
2. Sort by priority.
3. Create payment options for every matching provider.
4. Fall back to merchant-level legacy fields if no configs exist.
