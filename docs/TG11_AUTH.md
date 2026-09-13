# TG11 Authentication

Target model:

1. Users authenticate with Sign in with TG11 through OIDC/OAuth.
2. Fox Pay stores the TG11 UUID on `FoxPayProfile`.
3. Merchants are separate organizations.
4. Users access merchants through `MerchantMembership`.

Implemented now:

- `FoxPayProfile` model for TG11 identity references.
- `MerchantMembership` model with roles.

Not implemented yet:

- TG11 OIDC client settings.
- Login/callback views.
- Token validation.
- MFA/passkey step-up checks.
- Object-level dashboard permission enforcement beyond model structure.

