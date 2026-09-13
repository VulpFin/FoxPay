# Adding A Provider

Providers should live behind adapters. Do not scatter provider-specific conditionals through views or services.

1. Add an adapter in `apps/payments/adapters/`.
2. Subclass `PaymentProviderAdapter`.
3. Declare capabilities.
4. Implement only supported operations.
5. Store non-sensitive settings in `ProviderConfig.settings`.
6. Store secrets in `ProviderCredential`.
7. Normalize provider webhooks into Fox Pay event names.
8. Add tests for success, decline/failure, webhook idempotency, and sensitive-data masking.

Adapters may support operations such as:

- `create_checkout_session`
- `create_invoice`
- `refund`
- `verify_webhook`
- `normalize_webhook`
- `get_capabilities`

Unsupported operations should remain unimplemented instead of pretending to work.

