# Security

Fox Pay is security-critical software.

Current controls:

- Hashed secret API keys.
- Scoped API keys.
- Request IDs on every response.
- Sentry with PII disabled by default.
- Encrypted provider credentials.
- Signed incoming provider webhooks.
- Signed outgoing merchant webhooks.
- Metadata size limits.
- Merchant-scoped data models.
- CSRF protection on browser/admin surfaces.
- No raw card data collection.

Do not log API keys, provider credentials, cardholder data, private keys, raw webhook secrets, or sensitive provider responses.

Sensitive operations that should require step-up authentication once TG11 is integrated:

- creating live API keys
- rotating provider credentials
- changing webhook endpoints
- changing settlement details
- initiating payouts
- changing team administrators

