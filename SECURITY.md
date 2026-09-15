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

Every production provider webhook uses its provider-specific signature or
verification protocol before parsing authoritative fields. A signature is not
enough by itself: FoxPay also checks the provider connection, merchant, external
references, amount, and currency. Outgoing merchant webhooks use HMAC-SHA256
with a per-endpoint encrypted secret.

## Monitoring

Sentry is configured with `SENTRY_SEND_DEFAULT_PII=0` by default. Avoid sending cardholder data, raw webhook secrets, API keys, or wallet private keys to logs, exceptions, or monitoring metadata.

## Request IDs

Every response includes `X-Request-ID`. Include that value when investigating API errors, webhook deliveries, ledger entries, or support cases.

## Crypto custody

Merchant-controlled wallets and seller-owned NOWPayments accounts remain outside
FoxPay custody. Do not add wallet private keys, mass payouts, or seller withdrawal
authority to this service. Any future custodial product would require a separate
architecture and legal/compliance program rather than an incremental flag here.
