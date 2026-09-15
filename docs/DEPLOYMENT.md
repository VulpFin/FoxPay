# Production Deployment

Production target: `https://foxpay.fyi`

The inspected host convention is Apache on Ubuntu, loopback Gunicorn, project-specific virtualenvs, systemd, and PostgreSQL. FoxPay uses:

- source: `/var/www/FoxPay/app`
- virtualenv: `/var/www/FoxPay/.venv`
- collected static: `/var/www/FoxPay/staticfiles`
- protected environment: `/etc/foxpay/foxpay.env`
- Apache vhost: `/etc/apache2/sites-available/foxpay.fyi.conf`
- web/worker/beat units: `/etc/systemd/system/foxpay*.service`
- dedicated Redis Compose project: `foxpay-infra`, bound only to `127.10.0.11:6379`
- PostgreSQL database/role: `foxpay`
- Gunicorn: `127.10.0.11:8000`

Do not run the application as root or alter another site's vhost, service, database, Redis container, firewall rule, or certificate.

## 1. Back up before change

Record local branch, commit, status, and remotes. Create a timestamped local source archive outside the repository that includes uncommitted source but excludes `.git`, `.venv`, `.env`, caches, collected static, and build output.

On the server create `/root/backups/foxpay/<timestamp>/MANIFEST.txt`, then back up every file that will change, the current source revision/archive, `/etc/foxpay/foxpay.env`, Apache vhost, FoxPay systemd units, Redis deployment configuration, Apple merchant-domain file, and TLS configuration. Create a custom-format PostgreSQL dump:

```bash
sudo -u postgres pg_dump --format=custom --file=/root/backups/foxpay/<timestamp>/foxpay.pgdump foxpay
pg_restore --list /root/backups/foxpay/<timestamp>/foxpay.pgdump >/dev/null
```

Keep backup directories mode `0700`. Download the completed backup outside the active checkout and compare SHA-256 manifests before modifying production.

## 2. Update reviewed source

The server checkout must be clean or separately preserved before update. Fetch the intended production branch, verify its commit, and fast-forward only. Do not deploy an unreviewed working tree or use a destructive reset.

Install/update only the project virtualenv:

```bash
/var/www/FoxPay/.venv/bin/pip install --disable-pip-version-check -r /var/www/FoxPay/app/requirements.txt
```

## 3. Configure Redis

FoxPay must not reuse another application's Redis namespace. Start the dedicated loopback-only instance:

```bash
docker compose -p foxpay-infra -f /var/www/FoxPay/app/deploy/redis/docker-compose.yml up -d
docker compose -p foxpay-infra -f /var/www/FoxPay/app/deploy/redis/docker-compose.yml ps
```

The persistent volume uses AOF and `noeviction`. Never publish port 6379 on a public interface.

## 4. Configure the environment

Create `/etc/foxpay/foxpay.env` as `root:root` mode `0600`. Start from `.env.example`, preserve existing secrets, and use independently generated values for Django signing and provider-credential encryption.

Required production shape includes:

```text
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=foxpay.fyi
DJANGO_CSRF_TRUSTED_ORIGINS=https://foxpay.fyi
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_SECURE_HSTS_SECONDS=31536000
FOXPAY_ENV=live
FOXPAY_TRUSTED_PROXY_IPS=127.0.0.1/32
REDIS_URL=redis://127.10.0.11:6379/0
CELERY_BROKER_URL=redis://127.10.0.11:6379/1
CELERY_RESULT_BACKEND=redis://127.10.0.11:6379/2
CELERY_TASK_ALWAYS_EAGER=0
FOXPAY_ASYNC_TASKS_ENABLED=1
FOXPAY_SELLER_SIGNUP_ENABLED=0
FOXPAY_PAYPAL_PARTNER_ENABLED=0
SENTRY_SEND_DEFAULT_PII=0
```

Keep public seller signup off until the merchant agreement and legal/compliance review are complete. Keep PayPal Partner onboarding off until approval exists. Provider values remain blank unless the matching account, environment, callback, and webhook have been configured.

Set `DJANGO_ADMINS` and SMTP variables so abuse alerts reach an operator. A console email backend writes mail content to journald and is not acceptable for dependable production alerts.

## 5. Install services and Apache

Install the three reviewed files from `deploy/systemd/`, then:

```bash
systemctl daemon-reload
systemctl enable foxpay.service foxpay-worker.service foxpay-beat.service
```

Install `deploy/apache/foxpay.fyi.conf`. It preserves the Cloudflare Origin CA certificate and Apple merchant association alias, canonicalizes the Cloudflare client IP through `mod_remoteip`, removes untrusted forwarding headers, and proxies only to loopback.

Before enabling/reloading:

```bash
a2enmod remoteip
apachectl configtest
systemctl reload apache2
```

The Cloudflare CIDRs in the vhost must be compared with Cloudflare's authoritative current IPv4/IPv6 lists during deployment and periodically afterward.

## 6. Permissions

Application source and templates must not be world- or service-writable. A suitable baseline is root-owned source, group-readable by `www-data`, files mode `0640`, directories mode `0750`, and a `foxpay:www-data` virtualenv. Keep `/etc/foxpay/foxpay.env` at `0600`. Change only FoxPay paths and preserve executable bits on scripts.

## 7. Release

From the reviewed server checkout, run as root:

```bash
/var/www/FoxPay/app/scripts/deploy.sh
```

The helper takes a deployment lock, requires the environment file, installs dependencies, runs `manage.py check --deploy`, applies migrations, collects static assets, restarts web/worker/beat, checks `/healthz/` and `/readyz/`, and retires the legacy webhook timer only after the new runtime is ready. It never pulls Git, overwrites server configuration, creates database users, or changes secrets.

## 8. Verify

Verify service health and recent logs:

```bash
systemctl is-active foxpay foxpay-worker foxpay-beat
journalctl -u foxpay -u foxpay-worker -u foxpay-beat --since "10 minutes ago" --no-pager
docker compose -p foxpay-infra -f /var/www/FoxPay/app/deploy/redis/docker-compose.yml ps
sudo -u postgres psql -d foxpay -c "SELECT 1"
/var/www/FoxPay/.venv/bin/python /var/www/FoxPay/app/manage.py showmigrations --plan
```

Externally verify `/`, `/healthz/`, `/readyz/`, static assets, TG11 login/callback, invalid API-key rejection, environment isolation, hosted mock/test checkout, idempotency, signed provider sandbox events, outgoing webhook retry/history, and invalid/expired checkout tokens. Do not send a real financial transaction for deployment testing.

Confirm HTTP redirects to HTTPS without a loop, security headers appear once, Gunicorn/Redis/PostgreSQL are not publicly reachable, and the Apple association file remains byte-identical and reachable at `/.well-known/apple-developer-merchantid-domain-association`.

Cloudflare SSL/TLS mode should be **Full (strict)**. The origin certificate must cover `foxpay.fyi` and be unexpired; a Cloudflare Origin CA certificate is intentionally validated by Cloudflare rather than ordinary direct clients.

## 9. Back up the known-good release

Create a second timestamped server backup containing the deployed commit, dependency freeze, database dump, Apache vhost, all FoxPay units, Redis config, protected environment, certificate metadata, and a manifest. Keep it mode `0700`, download it outside the checkout, and compare SHA-256 manifests. Do not delete either server backup.

Use [ROLLBACK.md](ROLLBACK.md) if any production verification fails.
