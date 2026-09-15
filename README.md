# FoxPay

FoxPay is VulpFin's merchant-scoped payment orchestration service. A merchant integrates one API and can offer multiple card, wallet, and crypto routes while each provider remains responsible for card entry, money movement, settlement, and payouts.

FoxPay does not collect raw card data, hold seller funds, maintain payout balances, or pay sellers. Browser redirects are never treated as proof of payment; signed provider events and reconciliation establish normalized payment state.

## What is implemented

- TG11 Accounts sign-in and consumer payment dashboard.
- Seller applications with pending review, immutable agreement acceptance, membership roles, and staff approval.
- Separate seller dashboard for payments, refunds, disputes, API keys, webhooks, return origins, members, audit events, and provider health.
- Multiple prioritized provider routes per merchant, method, and test/live environment.
- Stripe Standard Connect OAuth with connected-account direct charges and Connect webhooks.
- Square OAuth with encrypted access/refresh tokens, location selection, refresh locking, and seller-scoped webhooks.
- PayPal Partner Referrals and seller-scoped API assertions behind a feature flag until partner approval.
- NOWPayments one-time credential entry with immediate encryption, validation, replacement, and removal. This is not OAuth.
- Provider-backed Stripe, Square, and PayPal refunds; NOWPayments refunds remain manual.
- Normalized disputes, reconciliation jobs, signed outgoing webhooks, and Redis-backed abuse controls.
- Merchant status and live-routing kill switches checked at API authentication and immediately before provider calls.

## Payment boundary

Card and debit entry happens only on provider-hosted or provider-owned tokenized interfaces. Never send PAN, CVV, magnetic-stripe, or EMV data to FoxPay. This architecture is intended to reduce exposure; it is not a claim that FoxPay is PCI certified or exempt from legal, tax, sanctions, consumer-protection, or payment-industry obligations.

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

The bootstrap command prints a FoxPay API key once. Treat it as a secret. Local development can use the mock card adapter; live merchant signup and every provider onboarding flow are disabled by default.

Docker development is also available:

```powershell
docker compose up --build -d
docker compose exec web python manage.py bootstrap_merchant --name "VulpFin" --email "admin@example.com" --password "change-me-now"
```

The Compose app containers read uncommitted secrets from `.env`; PostgreSQL and Redis receive only their own configuration.

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
  description = "FoxPay test charge"
  payment_methods = @("card", "crypto")
  success_url = "https://shop.example/success"
  cancel_url = "https://shop.example/cancel"
  metadata = @{ order_id = "ORDER-1001" }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/payment-intents/" -Headers $headers -Body $body
```

Success and cancel origins must first be registered for the merchant. The response contains a FoxPay checkout URL that presents every eligible provider option. Automatic capture is currently required.

## Refund a payment

```powershell
$body = @{ amount = 500; reason = "requested_by_customer" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/payment-intents/fp_pi_example/refunds/" -Headers $headers -Body $body
```

The API creates a pending refund before contacting the provider and sends a stable provider idempotency key. Stripe, Square, PayPal, and mock refunds are supported. A timeout or ambiguous provider response remains pending for reconciliation instead of being reported as success.

## Provider routing

`ProviderConfig` controls presentation and priority; `MerchantProviderConnection` controls seller authorization and health. A route is usable only when both are valid for the same merchant and environment. Lower priority numbers appear first, and a payment intent can expose multiple independent routes without silently retrying a charge.

Seller onboarding lives under `/seller/`. Platform callback and webhook URLs are listed in [Provider setup](docs/PROVIDERS.md). Legacy encrypted `ProviderCredential` rows remain only for migration compatibility and operator-configured bridge routes.

## Runtime

Production uses:

- Apache TLS and reverse proxying.
- Gunicorn for Django.
- PostgreSQL for durable state.
- A dedicated Redis instance for cache, abuse counters, and Celery transport.
- Separate Celery worker and beat services for provider events, retries, reconciliation, token refresh, and health checks.
- Sentry when `SENTRY_DSN` is configured, with default PII collection disabled.

`/healthz/` checks Django and the database. `/readyz/` also verifies a cache round trip. Both return redacted JSON.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Providers](docs/PROVIDERS.md)
- [API](docs/API.md)
- [Webhooks](docs/WEBHOOKS.md)
- [Security](docs/SECURITY.md)
- [Compliance boundaries](docs/COMPLIANCE_BOUNDARIES.md)
- [Production deployment](docs/DEPLOYMENT.md)
- [Production readiness](docs/PRODUCTION_READINESS.md)
- [Rollback](docs/ROLLBACK.md)
- [Merchant agreement draft](docs/MERCHANT_AGREEMENT_DRAFT.md)

Repository: <https://github.com/VulpFin/FoxPay.git>
