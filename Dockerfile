FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system foxpay \
    && adduser --system --ingroup foxpay --home /app foxpay \
    && chown foxpay:foxpay /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=foxpay:foxpay . .

USER foxpay
RUN python manage.py collectstatic --noinput

CMD ["sh", "-c", "python manage.py migrate && gunicorn foxpay.wsgi:application --bind 0.0.0.0:8000"]
