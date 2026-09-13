# Payment Lifecycle

A payment intent represents a merchant's intent to collect a specific amount in a specific currency. It is not tied to one provider.

Canonical statuses include:

- `created`
- `awaiting_payment_method`
- `pending`
- `processing`
- `requires_action`
- `authorized`
- `captured`
- `partially_refunded`
- `refunded`
- `succeeded`
- `failed`
- `canceled`
- `expired`

Provider-specific statuses are stored separately on payment attempts as `provider_status` and `provider_response_metadata`.

## Attempts

A payment intent can have multiple attempts. For example, one card route can fail and another can succeed, or a BTC invoice can expire and a USDC route can later be selected.

Fox Pay does not automatically retry money movement in a way that could double-charge. The routing layer exposes options; explicit customer or merchant action should drive failover.

## Refunds

Refunds are modeled independently and can be partial. Refund ledger entries reverse the payment clearing flow. Crypto refunds are not treated as card refunds; they need a separate destination-address workflow before production use.

