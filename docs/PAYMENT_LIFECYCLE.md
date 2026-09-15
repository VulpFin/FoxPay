# Payment Lifecycle

A `PaymentIntent` is a merchant's request to collect one amount and currency. It is provider-neutral. A `PaymentAttempt` is one concrete checkout or invoice created through one provider configuration.

Canonical intent states include `created`, `awaiting_payment_method`, `pending`, `processing`, `requires_action`, `authorized`, `captured`, `succeeded`, `partially_refunded`, `refunded`, `failed`, `canceled`, and `expired`. Provider-native states remain on attempts and refund records as bounded status/metadata fields.

## Attempts and failover

One intent can present several eligible attempts, such as Stripe and Square card checkout plus a NOWPayments crypto invoice. Priorities order the options. FoxPay does not automatically resubmit a payment to a backup provider because an automatic retry could double-charge; the customer must explicitly choose and complete another route.

The provider-hosted page collects any card or wallet details. A success browser return means only that the customer returned. FoxPay settles the intent only after a verified provider event or an authenticated reconciliation result matches the merchant, attempt, external references, amount, and currency.

## Refunds

Refunds can be partial. FoxPay locks the intent and its refunds while calculating the remaining refundable amount, reserves a `pending` refund before making a network request, and sends a durable provider idempotency key.

Stripe direct-charge, Square, PayPal capture, and mock refunds are implemented through the adapter interface. A definitive response or verified event moves the refund to `succeeded`, `failed`, or `canceled`; a timeout or unknown result remains `pending` and is revisited by reconciliation. Successful refund ledger entries are idempotent and move the intent to `partially_refunded` or `refunded` based on the total confirmed amount.

NOWPayments and manual crypto refunds remain seller-controlled manual workflows. FoxPay does not request a destination address or initiate a crypto withdrawal.

## Disputes

Supported Stripe, Square, and PayPal chargeback/dispute events are normalized into merchant-scoped `Dispute` rows with provider status, amount, currency, reason, and evidence deadline where supplied. The seller dashboard provides visibility. Provider evidence submission is not claimed by this release.

## Recovery

Celery beat schedules old pending payment and refund reconciliation. Worker operations recheck merchant status, environment permission, provider connection health, kill switches, and abuse controls immediately before any provider call. Reconciliation never changes another merchant's object and never guesses success from a browser redirect.
