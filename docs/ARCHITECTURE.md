# Fox Pay Architecture

Fox Pay is a payment orchestration layer. Merchant applications integrate with the Fox Pay API once, and Fox Pay routes payments through merchant-configured providers and rails.

Current flow:

1. Merchant authenticates with a scoped Fox Pay API key.
2. Merchant creates a payment intent.
3. Fox Pay creates payment attempts through active provider configurations.
4. Customer uses Fox Pay hosted checkout or a provider-hosted checkout URL.
5. Provider webhooks update normalized Fox Pay state.
6. Fox Pay records ledger transactions and emits merchant webhook events.

Fox Pay deliberately does not collect raw card numbers by default. Card entry belongs in compliant hosted or tokenized provider surfaces.

## Implemented Core Modules

- `apps.payments.models`: merchants, provider configs, customers, attempts, refunds, ledger, webhooks, audit logs.
- `apps.payments.adapters`: adapter base interface plus mock/hosted/manual implementations.
- `apps.payments.routing`: deterministic provider routing.
- `apps.payments.services`: API orchestration, idempotency, refunds, webhook normalization.
- `apps.payments.ledger`: append-only double-entry ledger helpers.
- `foxpay.middleware`: request IDs.

## Prepared But Not Production-Enabled

- TG11 OIDC sign-in.
- Real acquirer, ACH, bank, wallet, and stablecoin integrations.
- Durable async queue workers.
- KYB/KYC, sanctions, payouts, custody, and marketplace money movement.

