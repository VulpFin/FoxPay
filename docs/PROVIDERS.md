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

## Current Adapters

- `mock`: local card/debit sandbox checkout.
- `hosted`: redirects to a configured hosted card checkout URL.
- `manual`: non-custodial crypto invoice to merchant wallet addresses.

## Current Routing

Routing is deterministic:

1. Active configs matching merchant, environment, and method.
2. Sort by priority.
3. Create payment options for every matching provider.
4. Fall back to merchant-level legacy fields if no configs exist.

