# Security

FoxPay is security-critical orchestration software. The core rule is that every action is authorized twice where timing matters: once at the request boundary and again immediately before a provider call.

## Controls

- Merchant API keys are random, hashed, scoped, environment-bound, shown once, revocable, and rotatable.
- Browser actions use Django authentication, merchant-role checks, CSRF protection, and POST for mutations.
- Fresh TG11 MFA is required for live key creation, provider disconnection or credential replacement, webhook secret rotation, and member privilege changes.
- Pending, restricted, disabled, temporarily restricted, or killed merchants cannot route live payments.
- Provider authorization is connection-scoped; encrypted Square/NOWPayments secrets use a key separate from the database and Django signing secret.
- Seller Stripe secrets and seller PayPal client secrets are never accepted.
- OAuth state is random, stored as a hash, expires, is one-use, and is tied to user, merchant, provider, and environment.
- Provider signatures are necessary but every event also passes account, merchant, reference, amount, and currency checks.
- Redis-backed merchant/IP velocity, attempted-amount, provider-call, and suspicious-failure controls fail closed for live routing when shared enforcement is unavailable.
- A clear card-testing pattern creates an alert and temporary merchant restriction; provider fraud systems remain responsible for instrument-level risk.
- Return URLs must match registered merchant origins. Merchant webhooks use a pinned-IP HTTPS client that blocks local, private, link-local, reserved, multicast, and metadata destinations and follows no redirects.
- Raw card-like fields are rejected at the API boundary.
- Audit and provider payloads retain safe summaries, not authorization headers or full secret-bearing responses.

## Secret handling

Never place API keys, OAuth tokens, provider credentials, cardholder data, private keys, raw webhook secrets, Sentry auth data, TG11 tokens, or database URLs in Git, URLs, logs, audit metadata, browser HTML, JavaScript, screenshots, or support tickets.

Production secrets live in `/etc/foxpay/foxpay.env` with restricted ownership and mode `0600`. Provider ciphertext is useless without `FOXPAY_SECRET_ENCRYPTION_KEY`, which must be independently generated and backed up. Rotating that encryption key requires an explicit credential re-encryption procedure; replacing it blindly makes existing connections unreadable.

## Reverse proxy trust

Django trusts forwarded client IP data only from CIDRs in `FOXPAY_TRUSTED_PROXY_IPS`. In production, Cloudflare addresses are trusted by Apache `mod_remoteip`; Apache removes inbound forwarding headers and sets a single canonical client address before proxying. Django trusts only Apache's loopback address. Update Cloudflare ranges from its authoritative list as an operational maintenance task.

## Operational response

Compromised FoxPay keys should be revoked immediately. A compromised provider connection should be disabled in FoxPay and revoked at the provider. Operators can disable live routing independently of provider payout state. FoxPay has no seller payout control and must never represent a routing restriction as a frozen or paused payout.

Security architecture reduces scope; it does not establish PCI certification, legal classification, or compliance exemption. Those require current professional assessment and evidence from the deployed system.
