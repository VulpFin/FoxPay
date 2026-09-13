# Security Model

Fox Pay is designed to keep sensitive payment data out of the application wherever possible.

## Card data

Fox Pay must not collect, transmit, log, or store full card numbers, CVV values, magnetic stripe data, or raw payment credentials. Card and debit flows should use hosted fields or hosted checkout pages from a compliant acquirer or processor.

The included mock card adapter is for local development only.

## API keys

Merchant API keys are shown once at creation time and stored as password hashes. Requests authenticate with the `X-FoxPay-Key` header.

API keys support test/live environments, publishable/secret key types, permission scopes, expiration, revocation, and last-used timestamps.

## Provider credentials

Provider credentials are stored separately from non-sensitive provider settings and encrypted at rest. They should never be placed in `ProviderConfig.settings`, logs, Sentry metadata, webhook payloads, or API responses.

## Webhooks

Production webhook endpoints should use provider-specific signature validation. The generic Fox Pay webhook helper supports HMAC-SHA256 through `FOXPAY_WEBHOOK_SECRET`.

## Monitoring

Sentry is configured with `SENTRY_SEND_DEFAULT_PII=0` by default. Avoid sending cardholder data, raw webhook secrets, API keys, or wallet private keys to logs, exceptions, or monitoring metadata.

## Request IDs

Every response includes `X-Request-ID`. Include that value when investigating API errors, webhook deliveries, ledger entries, or support cases.

## Crypto custody

The initial crypto adapter assumes merchants control their own wallets. Fox Pay should not hold private keys unless the product is explicitly upgraded into a custodial service with the required controls, insurance, accounting, and legal review.
