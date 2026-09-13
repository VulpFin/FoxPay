#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/FoxPay/app}"
VENV_DIR="${VENV_DIR:-/var/www/FoxPay/.venv}"
ENV_FILE="${ENV_FILE:-/etc/foxpay/foxpay.env}"
SERVICE_NAME="${SERVICE_NAME:-foxpay}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r requirements.txt
"$VENV_DIR/bin/python" manage.py check
"$VENV_DIR/bin/python" manage.py migrate
"$VENV_DIR/bin/python" manage.py collectstatic --noinput

systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME"
curl --fail --silent --show-error --max-time 10 http://127.10.0.11:8000/healthz/
