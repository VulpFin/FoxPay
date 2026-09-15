#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

APP_DIR="${APP_DIR:-/var/www/FoxPay/app}"
VENV_DIR="${VENV_DIR:-/var/www/FoxPay/.venv}"
ENV_FILE="${ENV_FILE:-/etc/foxpay/foxpay.env}"
WEB_SERVICE="${WEB_SERVICE:-foxpay.service}"
WORKER_SERVICE="${WORKER_SERVICE:-foxpay-worker.service}"
BEAT_SERVICE="${BEAT_SERVICE:-foxpay-beat.service}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOCK_FILE="${LOCK_FILE:-/run/lock/foxpay-deploy.lock}"
HEALTH_BASE_URL="${HEALTH_BASE_URL:-http://127.10.0.11:8000}"
HEALTH_HOST="${HEALTH_HOST:-foxpay.fyi}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another FoxPay deployment is already running." >&2
  exit 1
fi

if [ ! -r "$ENV_FILE" ]; then
  echo "Required FoxPay environment file is not readable: $ENV_FILE" >&2
  exit 1
fi

cd "$APP_DIR"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --disable-pip-version-check -r requirements.txt
"$VENV_DIR/bin/python" manage.py check --deploy
"$VENV_DIR/bin/python" manage.py migrate --noinput
"$VENV_DIR/bin/python" manage.py collectstatic --noinput

systemctl daemon-reload
systemctl restart "$WORKER_SERVICE" "$BEAT_SERVICE" "$WEB_SERVICE"
systemctl is-active --quiet "$WORKER_SERVICE"
systemctl is-active --quiet "$BEAT_SERVICE"

ready=0
for attempt in $(seq 1 20); do
  if curl --fail --silent --show-error --max-time 10 \
    -H "Host: $HEALTH_HOST" \
    -H "X-Forwarded-Proto: https" \
    "$HEALTH_BASE_URL/healthz/" >/dev/null && \
    curl --fail --silent --show-error --max-time 10 \
      -H "Host: $HEALTH_HOST" \
      -H "X-Forwarded-Proto: https" \
      "$HEALTH_BASE_URL/readyz/" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  systemctl --no-pager --full status "$WEB_SERVICE" "$WORKER_SERVICE" "$BEAT_SERVICE" || true
  echo "FoxPay health or readiness check did not pass after restart." >&2
  exit 1
fi

if systemctl list-unit-files foxpay-webhooks.timer --no-legend 2>/dev/null | grep -q foxpay-webhooks.timer; then
  systemctl disable --now foxpay-webhooks.timer
fi

systemctl --no-pager --full status "$WEB_SERVICE" "$WORKER_SERVICE" "$BEAT_SERVICE"
echo "FoxPay deployment is healthy and ready."
