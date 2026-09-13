# Fox Pay

Fox Pay is a VulpFin payment platform scaffold for accepting card and crypto payment options without coupling the product to Stripe, Shopify, or a single processor.

The first version is intentionally provider-adapter based:

- Card and debit payments are handled through hosted processor/acquirer pages. Fox Pay does not collect or store raw card numbers.
- Stripe Checkout can be enabled as the first production card/debit route while additional providers are added behind the same adapter boundary.
- Crypto payments are represented as invoices and webhook-confirmed settlement events.
- Merchants receive API keys and create payment intents through a JSON API.
- Fox Pay records customers, refunds, provider events, merchant webhook events, audit logs, and double-entry ledger transactions.
- Django Admin and the first Fox Pay dashboard provide operational visibility while the merchant dashboard matures.

## Why hosted card checkout

Building a raw card processor requires PCI DSS scope, acquiring relationships, card network certification, fraud controls, dispute workflows, and regional money-transmission compliance. Fox Pay keeps the core product independent while staying on the right side of that boundary: it orchestrates payment intents and adapters, then delegates sensitive card entry to a compliant card provider.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py bootstrap_merchant --name "VulpFin" --email "admin@example.com" --password "change-me-now"
python manage.py runserver
```

The bootstrap command prints a one-time Fox Pay API key. Keep it secret.

## Docker setup

```powershell
docker compose up --build -d
docker compose exec web python manage.py bootstrap_merchant --name "VulpFin" --email "admin@example.com" --password "change-me-now"
```

## Create a payment intent

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "X-FoxPay-Key" = "foxpay_test_your_key_here"
  "Idempotency-Key" = "ORDER-1001-create"
}

$body = @{
  amount = 2500
  currency = "USD"
  description = "Fox Pay test charge"
  payment_methods = @("card", "crypto")
  success_url = "https://example.com/success"
  cancel_url = "https://example.com/cancel"
  metadata = @{ order_id = "ORDER-1001" }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/payment-intents/" -Headers $headers -Body $body
```

The response includes `foxpay_checkout_url`. Redirect the customer there to show all available Fox Pay payment options.

## Refund a payment intent

```powershell
$body = @{ amount = 500; reason = "requested_by_customer" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/payment-intents/fp_pi_example/refunds/" -Headers $headers -Body $body
```

## Provider configuration

Merchants can use multiple active provider configs per payment method. In Django Admin, open a merchant and add provider configs with:

- `kind`: `card` or `crypto`
- `provider`: merchant-facing provider code, such as `card-primary`, `card-eu`, or `btc-wallet-backup`
- `adapter`: Fox Pay adapter implementation, such as `mock`, `stripe`, `hosted`, or `manual`
- `priority`: lower numbers are shown first
- `settings`: provider-specific JSON

When a payment intent is created, Fox Pay creates payment options for every active provider config of the requested method. If no provider configs exist yet, it falls back to the legacy merchant fields below.

Card fallback settings:

- `FOXPAY_CARD_PROVIDER=mock` creates a test-only hosted checkout page.
- `FOXPAY_CARD_PROVIDER=stripe` creates Stripe-hosted Checkout Sessions.
- `FOXPAY_CARD_PROVIDER=hosted` points card users to `FOXPAY_CARD_PROVIDER_CHECKOUT_URL`.

Hosted card provider config example:

```json
{
  "checkout_url": "https://backup-card.example/checkout"
}
```

Crypto fallback settings:

- `FOXPAY_CRYPTO_PROVIDER=manual` creates a crypto invoice using merchant wallet addresses configured in Admin.
- Webhooks can update payment intents after settlement confirmation.

Manual crypto provider config example:

```json
{
  "addresses": {
    "BTC": "bc1qprimary...",
    "ETH": "0xprimary..."
  }
}
```

You can also seed provider configs during `bootstrap_merchant` by setting
`FOXPAY_PROVIDER_CONFIGS` in `.env`. See `.env.example` for primary/backup
card and crypto examples.

Stripe provider setup:

```powershell
$env:STRIPE_SECRET_KEY = "sk_test_or_live..."
$env:STRIPE_WEBHOOK_SECRET = "whsec_..."
python manage.py configure_stripe_provider --merchant vulpfin --provider stripe-primary --activate
```

Use the matching Fox Pay environment for the key type: test keys with
`FOXPAY_ENV=test`, live keys with `FOXPAY_ENV=live`.

## Monitoring

Sentry is configured through environment variables:

- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_TRACES_SAMPLE_RATE`
- `SENTRY_SEND_DEFAULT_PII`

Health checks are available at `/healthz/`.

## Documentation

- `docs/ARCHITECTURE.md`
- `docs/PAYMENT_LIFECYCLE.md`
- `docs/PROVIDERS.md`
- `docs/ADDING_A_PROVIDER.md`
- `docs/WEBHOOKS.md`
- `docs/CRYPTO.md`
- `docs/SECURITY.md`
- `docs/COMPLIANCE_BOUNDARIES.md`
- `docs/TG11_AUTH.md`
- `docs/API.md`
- `docs/INTEGRATION.md`

## GitHub

This checkout is configured for:

```text
https://github.com/VulpFin/FoxPay.git
```
