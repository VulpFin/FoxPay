# Production Deployment

Production target:

`https://foxpay.fyi`

Current server convention:

- Apache terminates HTTPS and reverse-proxies to a local application server.
- Django app source lives under `/var/www/<AppName>/app`.
- Project virtualenv lives outside the app source.
- Static files are collected and served directly by Apache.
- Systemd manages the app process.

Recommended FoxPay layout:

- `/var/www/FoxPay/app`
- `/var/www/FoxPay/.venv`
- `/var/www/FoxPay/staticfiles`
- `/etc/foxpay/foxpay.env`
- `/etc/apache2/sites-available/foxpay.fyi.conf`
- `/etc/systemd/system/foxpay.service`
- `/etc/apache2/ssl/foxpay.fyi.pem`
- `/etc/apache2/ssl/foxpay.fyi.key`

Deployment steps:

1. Back up local source and server state.
2. Sync source to `/var/www/FoxPay/app`.
3. Create or update `/etc/foxpay/foxpay.env` with production secrets.
4. Install dependencies into `/var/www/FoxPay/.venv`.
5. Run `python manage.py check`.
6. Run `python manage.py migrate`.
7. Run `python manage.py collectstatic --noinput`.
8. Start/restart `foxpay.service`.
9. Validate `apachectl configtest`.
10. Reload Apache.
11. Verify `https://foxpay.fyi/healthz/`.

The helper script `scripts/deploy.sh` performs only repeatable app steps. It does not overwrite Apache, systemd, database users, TLS files, or secrets.

