# FoxPay Architecture

FoxPay is a noncustodial payment orchestration layer. A seller's connected provider account owns each provider-side payment, refund, settlement, negative balance, and payout. FoxPay owns normalized intent state, routing policy, authorization, audit history, and reliable event delivery.

## Trust boundaries

1. TG11 Accounts authenticates people; `MerchantMembership` authorizes each merchant action by role.
2. Merchant API keys are hashed, scoped, environment-bound, and checked against merchant lifecycle and the live-payment kill switch.
3. `MerchantProviderConnection` represents delegated seller authorization. Encrypted tokens are separate from the non-sensitive `ProviderConfig` routing record.
4. Card data is collected only by provider-hosted or provider-owned tokenized interfaces.
5. Provider redirects are user experience only. Signed provider webhooks and reconciliation are authoritative.
6. Outgoing URLs pass allowlist and SSRF protections before use.

Knowing a merchant slug, object UUID, provider reference, or callback URL grants no authority.

## Core state

- `Merchant`, `MerchantMembership`, and `MerchantAgreementAcceptance`: seller lifecycle, immutable acceptance evidence, roles, approval, restriction, and kill switches.
- `MerchantProviderConnection` and `ProviderOnboardingSession`: provider identity, health, encrypted tokens, capabilities, expiring one-time OAuth state, and safe return paths.
- `ProviderConfig`: merchant-visible route name, adapter, environment, priority, and non-sensitive settings. Multiple configurations of the same provider type are allowed.
- `PaymentIntent` and `PaymentAttempt`: provider-neutral requested payment and each concrete provider route.
- `Refund` and `Dispute`: normalized post-payment state.
- `ProviderEvent`: idempotent, minimally retained incoming event records.
- `LedgerTransaction` and `LedgerEntry`: append-only accounting observations. They are not a seller balance or payout system.
- `MerchantWebhookEndpoint`, `MerchantWebhookEvent`, and `MerchantWebhookAttempt`: signed, durable merchant notifications with secret rotation and delivery history.
- `AuditLog` and `AbuseAlert`: security-sensitive action history and bounded operational alerts.

## Payment path

1. The merchant creates an intent using a scoped test or live FoxPay key and idempotency key.
2. FoxPay validates amount, metadata, requested methods, and registered return origins.
3. Routing selects active configurations whose connection, environment, capability, merchant status, and kill-switch state are valid.
4. FoxPay checks merchant/IP velocity and amount limits immediately before each provider call.
5. The adapter creates a direct seller-scoped provider checkout or invoice.
6. The customer completes payment on the provider surface.
7. A verified provider event is durably recorded, resolved to one connection and attempt, and checked for merchant, amount, currency, and external-reference ownership.
8. Async processing normalizes state, records ledger entries, and queues merchant webhook events.
9. Reconciliation revisits old pending payments and refunds when a response or webhook was delayed or ambiguous.

FoxPay does not automatically retry a charge through a different provider; the customer explicitly chooses another route, avoiding hidden double-charge behavior.

## Runtime topology

Production is Apache to loopback Gunicorn, backed by PostgreSQL. A dedicated loopback-only Redis instance provides shared cache, rate-limit counters, and Celery transport. Separate systemd services run Gunicorn, a Celery worker, and a single Celery beat scheduler. Logs go to journald and optionally Sentry with default PII collection disabled.

## Deliberate limits

- PayPal seller onboarding remains feature-flagged until TG11 has required partner approval.
- NOWPayments uses seller-entered account credentials because no approved delegated OAuth flow is assumed.
- NOWPayments crypto refunds remain manual.
- Dispute ingestion and deadlines are normalized; provider evidence submission is not claimed.
- FoxPay provides no custody, acquiring, seller balances, payout execution, KYB/KYC substitute, tax-reporting exemption, or compliance certification.
