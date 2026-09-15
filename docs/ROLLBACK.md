# Production Rollback

Rollback must use the matching pre-deploy manifest and backups. Do not use `git reset --hard`, delete tables, fake migrations, or restore a database casually. Preserve the failed state first so it can be diagnosed.

## 1. Stabilize and preserve

1. Record the failing commit, service states, migration list, and recent FoxPay logs.
2. Create an emergency PostgreSQL dump and copies of the current environment, vhost, units, Redis config, and source state.
3. Stop only FoxPay application services:

```bash
systemctl stop foxpay.service foxpay-worker.service foxpay-beat.service
```

Apache, PostgreSQL, Docker, and unrelated applications stay running unless their own verified FoxPay-specific failure requires action.

## 2. Choose the smallest rollback

### Application restart only

For a transient dependency failure, restore Redis/database availability and restart all FoxPay services. Do not change source or data.

### Source revision

If migrations are backward-compatible, restore the prior reviewed source archive or check out the exact commit named in the pre-deploy manifest. Reinstall that revision's requirements and collect static files. Run its `manage.py check --deploy` using the protected environment, then start web/worker/beat and verify both health endpoints.

Do not point old code at a schema it cannot understand. Review the migration files between revisions first.

### Database

Use this only when the release changed data/schema incompatibly and forward repair is not safer.

1. Keep FoxPay web, worker, and beat stopped.
2. Verify the pre-deploy dump with `pg_restore --list` and confirm it belongs to the expected timestamp/database.
3. Preserve the current failed database with another dump.
4. Restore into the dedicated `foxpay` database using the server's approved PostgreSQL procedure. Terminate only FoxPay database sessions if necessary; do not affect other databases.
5. Verify ownership, `SELECT 1`, and `showmigrations` against the restored source.

The exact `pg_restore` flags depend on whether the target database is recreated or cleaned. Have an operator review them before execution; an incorrect `--clean` target can destroy unrelated data.

### Environment

Restore `/etc/foxpay/foxpay.env` from the secure backup, preserving `root:root` and mode `0600`. Restart all FoxPay services. Never print or diff secret values into a shared terminal/log.

### systemd

Restore every changed FoxPay unit, not only the web unit:

```bash
systemctl daemon-reload
systemctl enable foxpay.service foxpay-worker.service foxpay-beat.service
systemctl restart foxpay-worker.service foxpay-beat.service foxpay.service
```

If rolling back to the legacy webhook timer, enable it only after stopping the Celery scheduler path that would duplicate deliveries.

### Apache

Restore only `/etc/apache2/sites-available/foxpay.fyi.conf` and any FoxPay-specific module/config change listed in the manifest. Always validate before a graceful reload:

```bash
apachectl configtest
systemctl reload apache2
```

If validation fails, put the last known-good vhost back and do not reload.

### Redis

Application rollback normally keeps the dedicated `foxpay-infra` Redis volume. Pending Celery messages and abuse counters are operational state, so do not delete the volume. Restore Redis configuration only when it caused the incident. Purging queues requires a separate, reviewed decision because it can discard captures, reconciliation, token refresh, or webhook work.

## 3. Validate the rollback

Confirm:

- `foxpay`, `foxpay-worker`, and `foxpay-beat` are active and not restart-looping;
- dedicated Redis is healthy and PostgreSQL answers;
- `/healthz/` returns `200` and `/readyz/` returns `200`;
- `/` loads through Cloudflare HTTPS without a redirect loop;
- TG11 authentication and a harmless test API request work;
- no real provider call is made from a test key;
- recent logs contain no repeating traceback or secret value;
- the Apple merchant association file is still reachable.

Record the restored source commit, database backup timestamp, configuration files restored, verification results, reason for rollback, and any queued payment/refund events requiring reconciliation.
