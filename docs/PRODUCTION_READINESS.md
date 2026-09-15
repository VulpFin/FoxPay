# Production Readiness Checklist

This checklist separates technical deployment from permission to onboard outside sellers. A green server does not complete legal, provider, PCI, sanctions, tax, or accounting review.

## Release and recovery

- [ ] Feature branch is reviewed; full tests and clean/upgrade migrations pass.
- [ ] `manage.py check --deploy` passes with production-like settings.
- [ ] Deployed commit is recorded and contains no secrets or real provider identifiers.
- [ ] Local pre-deploy source archive exists outside the repository.
- [ ] Verified server pre-deploy PostgreSQL/config/source backup exists and has a local SHA-256-matched copy.
- [ ] [Rollback procedure](ROLLBACK.md) has been reviewed for this migration set.

## Merchant launch policy

- [ ] `FOXPAY_SELLER_SIGNUP_ENABLED=0` until counsel approves the agreement, acceptable-use policy, privacy terms, and seller review process.
- [ ] New sellers default to `pending` with live routing off.
- [ ] Staff approval, rejection, restriction, disable, and immediate kill-switch actions have named operators.
- [ ] `gage@tg11.org` has the intended staff/superuser access through the approved TG11/local-admin model.
- [ ] Application fees remain disabled/zero unless a reviewed fee and refund policy is configured centrally.
- [ ] Merchant agreement placeholders for identity/address, notices, governing law, indemnity, and liability are completed by counsel; a new version is issued and reaccepted.

## Secrets and identity

- [ ] `/etc/foxpay/foxpay.env` is `root:root` mode `0600` and included only in protected backups.
- [ ] Django signing and `FOXPAY_SECRET_ENCRYPTION_KEY` values are independent, random, backed up, and not printed in reports.
- [ ] `DJANGO_DEBUG=0`; allowed hosts and CSRF origins list only deployed domains.
- [ ] TG11 OIDC client uses exactly `https://foxpay.fyi/auth/tg11/callback/`.
- [ ] Fresh-MFA claims from TG11 are confirmed for sensitive seller actions.
- [ ] Sentry PII collection stays off unless a reviewed privacy decision changes it.
- [ ] SMTP and `DJANGO_ADMINS` deliver a real test alert to the operations mailbox.

## Provider consoles

### Stripe

- [ ] Platform test/live keys and Connect client IDs are stored only in the protected environment.
- [ ] Redirect URIs are exactly `/seller/stripe/test/callback/` and `/seller/stripe/live/callback/` on `https://foxpay.fyi`.
- [ ] Connect webhook endpoints are exactly `/api/v1/webhooks/stripe/connect/test/` and `/live/` with matching signing secrets.
- [ ] Events for connected accounts include Checkout, refund, dispute, account update, and deauthorization events FoxPay handles.
- [ ] A sandbox Standard account proves direct-charge and refund scoping; platform application fee is absent by default.

### Square

- [ ] FoxPay application has test/live OAuth credentials and only the documented scopes.
- [ ] Redirect URIs are exactly `/seller/square/test/callback/` and `/seller/square/live/callback/` on `https://foxpay.fyi`.
- [ ] Webhook URLs exactly match `SQUARE_OAUTH_WEBHOOK_URL_TEST/LIVE`, including trailing slash, and signature keys are stored.
- [ ] Sandbox onboarding, multi-location selection, checkout, signed payment/refund/dispute events, token refresh, and revocation are exercised.
- [ ] Apple Pay domain association remains reachable and has been registered where Square requires it.

### PayPal

- [ ] TG11 has explicit Partner Referrals approval for the intended markets/features before enabling `FOXPAY_PAYPAL_PARTNER_ENABLED`.
- [ ] Partner test/live credentials, merchant ID, attribution ID, and webhook IDs are stored only in the protected environment.
- [ ] Callback and partner webhook URLs from [Provider connections](PROVIDERS.md) are registered.
- [ ] Sandbox referral status, auth assertion payer ID, approved-order async capture, refunds, disputes, and consent revocation pass.
- [ ] Seller client secrets are never requested.

### NOWPayments

- [ ] Self-service remains disabled until credential handling and contractual use are approved.
- [ ] Seller enters API key and IPN secret once over HTTPS with fresh MFA; values never reappear in HTML/logs.
- [ ] Each route's scoped IPN URL is registered and a signed sandbox notification passes amount/currency/ownership checks.
- [ ] Removal guidance tells the seller to rotate/revoke at NOWPayments too.
- [ ] Product copy says this is credential-based, not OAuth, and crypto refunds remain manual.

## Infrastructure

- [ ] PostgreSQL uses the dedicated `foxpay` role/database and has a tested backup process.
- [ ] Dedicated `foxpay-infra` Redis is healthy, persistent, `noeviction`, and bound only to `127.10.0.11:6379`.
- [ ] Cache/broker/result databases and `foxpay` key prefixes are configured.
- [ ] Gunicorn, Celery worker, and one Celery beat scheduler are active under the non-root `foxpay` user.
- [ ] Legacy `foxpay-webhooks.timer` is disabled only after Celery health is proven.
- [ ] Application source is not group/world writable; secrets are not readable by the runtime user except through systemd loading.
- [ ] UFW exposes no FoxPay Gunicorn, Redis, or PostgreSQL port publicly.

## Proxy and TLS

- [ ] Apache config test succeeds before reload.
- [ ] Current Cloudflare proxy CIDRs match the vhost allowlist; `mod_remoteip` is enabled.
- [ ] Apache strips inbound forwarding headers and passes one canonical address; Django trusts only `127.0.0.1/32`.
- [ ] HTTP redirects to HTTPS with path/query preserved and no Cloudflare loop.
- [ ] Cloudflare SSL/TLS mode is **Full (strict)**.
- [ ] Origin certificate covers `foxpay.fyi`, is unexpired, and certificate ownership/mode are restricted.
- [ ] HSTS, CSP, frame, content-type, referrer, and permissions headers appear once and do not break hosted provider redirects.
- [ ] `/.well-known/apple-developer-merchantid-domain-association` returns the intended file unchanged.

## Behavioral verification

- [ ] Pending/restricted/disabled/killed sellers cannot create live provider calls.
- [ ] Test keys cannot select live routes; live keys cannot select test routes.
- [ ] Cross-merchant slugs, UUIDs, callbacks, provider references, refunds, members, keys, and webhooks are denied.
- [ ] Missing/expired/replayed/mismatched OAuth state is denied.
- [ ] Return URL allowlist and webhook SSRF tests include local/private/reserved addresses and DNS rebinding.
- [ ] Raw card-shaped input is rejected.
- [ ] Refund idempotency, concurrent refundable amount, timeout ambiguity, and later reconciliation pass.
- [ ] Abuse counters are shared through Redis and live behavior fails closed when Redis is unavailable.
- [ ] Valid duplicate provider events return safely without duplicate ledger/webhook effects.
- [ ] Outgoing webhook signature, rotation, retry/backoff, delivery history, and terminal failure behavior pass.

## Monitoring and final evidence

- [ ] `/`, `/healthz/`, and `/readyz/` return expected public results without sensitive diagnostics.
- [ ] Journald shows no restart loop, repeated exception, token, authorization header, or credential value.
- [ ] Sentry receives a scrubbed test event in the `live` environment.
- [ ] Provider outage/revocation and old pending payment/refund reconciliation are observable.
- [ ] Server post-deploy backup and local SHA-256-matched copy exist.
- [ ] Completion report records URL, branch/commit, services, database/migrations, Apache/TLS/Cloudflare state, all verification boundaries, backup paths, and remaining provider/legal inputs.
