# Rollback

Rollback depends on the failure mode. Do not reset the database unless a verified backup is being restored intentionally.

## Source rollback

1. Stop FoxPay only:

   `systemctl stop foxpay`

2. Restore the previous source backup into `/var/www/FoxPay/app`.
3. Reinstall dependencies if the previous revision needs different packages.
4. Run `python manage.py check`.
5. Restart FoxPay:

   `systemctl start foxpay`

## Database rollback

For PostgreSQL:

1. Stop `foxpay`.
2. Create an additional emergency dump before changing anything.
3. Restore the verified pre-deploy dump with `psql` or `pg_restore`, depending on dump format.
4. Run `python manage.py showmigrations`.
5. Start `foxpay`.

## Apache rollback

1. Restore the backed-up vhost file from `/root/backups/foxpay/<timestamp>/`.
2. Run `apachectl configtest`.
3. If syntax is OK, run `systemctl reload apache2`.

## systemd rollback

1. Restore `/etc/systemd/system/foxpay.service` from backup.
2. Run `systemctl daemon-reload`.
3. Restart `foxpay`.

## Environment rollback

1. Restore `/etc/foxpay/foxpay.env` from the secure backup.
2. Ensure permissions are still `600`.
3. Restart `foxpay`.

Always verify:

- `systemctl status foxpay`
- `journalctl -u foxpay -n 100`
- `https://foxpay.fyi/healthz/`
