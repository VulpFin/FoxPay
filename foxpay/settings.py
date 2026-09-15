from pathlib import Path
import os

import dj_database_url
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.payments",
    "apps.pages",
    "tg11_auth",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "foxpay.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "foxpay.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "tg11_auth.context_processors.tg11",
            ],
        },
    },
]

WSGI_APPLICATION = "foxpay.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'foxpay.sqlite3'}", conn_max_age=600)
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.getenv("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "tg11_auth.backends.TG11Backend",
]
LOGIN_URL = "/auth/tg11/login/"
LOGIN_REDIRECT_URL = "/"

TG11_OIDC_ISSUER = os.getenv("TG11_OIDC_ISSUER", "")
TG11_OIDC_CLIENT_ID = os.getenv("TG11_OIDC_CLIENT_ID", "")
TG11_OIDC_CLIENT_SECRET = os.getenv("TG11_OIDC_CLIENT_SECRET", "")
TG11_OIDC_REDIRECT_URI = os.getenv("TG11_OIDC_REDIRECT_URI", "")
TG11_OIDC_SCOPES = "openid profile email tg11.profile"
TG11_APPLICATION = "foxpay"
TG11_AUTH_AUTOLINK_VERIFIED_EMAIL = False
TG11_AUTH_PROFILE_HOOK = "apps.accounts.identity.on_tg11_login"
TG11_AUTH_LOGIN_GUARD = "apps.accounts.identity.guard_tg11_login"
TG11_AUTH_LOGIN_REDIRECT = "/"
TG11_AUTH_POST_LOGOUT_REDIRECT = "/"
TG11_AUTH_ACCOUNT_URL = "/connections/"
TG11_AUTH_BASE_TEMPLATE = "payments/base.html"

SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = env_bool("DJANGO_CSRF_COOKIE_HTTPONLY", False)
SESSION_COOKIE_SAMESITE = os.getenv("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.getenv("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = os.getenv("DJANGO_X_FRAME_OPTIONS", "DENY")
SECURE_REFERRER_POLICY = os.getenv("DJANGO_SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")

FOXPAY_ENV = os.getenv("FOXPAY_ENV", "test")
FOXPAY_CARD_PROVIDER = os.getenv("FOXPAY_CARD_PROVIDER", "mock")
FOXPAY_CARD_PROVIDER_CHECKOUT_URL = os.getenv("FOXPAY_CARD_PROVIDER_CHECKOUT_URL", "")
FOXPAY_CRYPTO_PROVIDER = os.getenv("FOXPAY_CRYPTO_PROVIDER", "manual")
FOXPAY_WEBHOOK_SECRET = os.getenv("FOXPAY_WEBHOOK_SECRET", "")
FOXPAY_API_RATE_LIMIT_PER_MINUTE = int(os.getenv("FOXPAY_API_RATE_LIMIT_PER_MINUTE", "120"))
FOXPAY_SECRET_ENCRYPTION_KEY = os.getenv("FOXPAY_SECRET_ENCRYPTION_KEY", SECRET_KEY)
if os.getenv("FOXPAY_ENV", "test") == "live" and (not os.getenv("FOXPAY_SECRET_ENCRYPTION_KEY") or FOXPAY_SECRET_ENCRYPTION_KEY == SECRET_KEY):
    raise RuntimeError("Live FoxPay requires an independent FOXPAY_SECRET_ENCRYPTION_KEY.")
FOXPAY_PENDING_TEST_PAYMENTS_ENABLED = env_bool("FOXPAY_PENDING_TEST_PAYMENTS_ENABLED", False)
FOXPAY_SELLER_SIGNUP_ENABLED = env_bool("FOXPAY_SELLER_SIGNUP_ENABLED", False)

PAYPAL_ENV = os.getenv("PAYPAL_ENV", "sandbox")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
FOXPAY_PAYPAL_PARTNER_ENABLED = env_bool("FOXPAY_PAYPAL_PARTNER_ENABLED", False)
PAYPAL_PARTNER_CLIENT_ID_TEST = os.getenv("PAYPAL_PARTNER_CLIENT_ID_TEST", "")
PAYPAL_PARTNER_CLIENT_SECRET_TEST = os.getenv("PAYPAL_PARTNER_CLIENT_SECRET_TEST", "")
PAYPAL_PARTNER_MERCHANT_ID_TEST = os.getenv("PAYPAL_PARTNER_MERCHANT_ID_TEST", "")
PAYPAL_PARTNER_ATTRIBUTION_ID_TEST = os.getenv("PAYPAL_PARTNER_ATTRIBUTION_ID_TEST", "")
PAYPAL_PARTNER_WEBHOOK_ID_TEST = os.getenv("PAYPAL_PARTNER_WEBHOOK_ID_TEST", "")
PAYPAL_PARTNER_CLIENT_ID_LIVE = os.getenv("PAYPAL_PARTNER_CLIENT_ID_LIVE", "")
PAYPAL_PARTNER_CLIENT_SECRET_LIVE = os.getenv("PAYPAL_PARTNER_CLIENT_SECRET_LIVE", "")
PAYPAL_PARTNER_MERCHANT_ID_LIVE = os.getenv("PAYPAL_PARTNER_MERCHANT_ID_LIVE", "")
PAYPAL_PARTNER_ATTRIBUTION_ID_LIVE = os.getenv("PAYPAL_PARTNER_ATTRIBUTION_ID_LIVE", "")
PAYPAL_PARTNER_WEBHOOK_ID_LIVE = os.getenv("PAYPAL_PARTNER_WEBHOOK_ID_LIVE", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_TEST_SECRET_KEY = os.getenv("STRIPE_TEST_SECRET_KEY", "")
STRIPE_CONNECT_CLIENT_ID_LIVE = os.getenv("STRIPE_CONNECT_CLIENT_ID_LIVE", "")
STRIPE_CONNECT_CLIENT_ID_TEST = os.getenv("STRIPE_CONNECT_CLIENT_ID_TEST", "")
STRIPE_CONNECT_WEBHOOK_SECRET_LIVE = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET_LIVE", "")
STRIPE_CONNECT_WEBHOOK_SECRET_TEST = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET_TEST", "")
FOXPAY_STRIPE_CONNECT_ENABLED = env_bool("FOXPAY_STRIPE_CONNECT_ENABLED", False)

FOXPAY_SQUARE_OAUTH_ENABLED = env_bool("FOXPAY_SQUARE_OAUTH_ENABLED", False)
FOXPAY_NOWPAYMENTS_SELF_SERVICE_ENABLED = env_bool("FOXPAY_NOWPAYMENTS_SELF_SERVICE_ENABLED", False)
SQUARE_APPLICATION_ID_TEST = os.getenv("SQUARE_APPLICATION_ID_TEST", "")
SQUARE_APPLICATION_SECRET_TEST = os.getenv("SQUARE_APPLICATION_SECRET_TEST", "")
SQUARE_APPLICATION_ID_LIVE = os.getenv("SQUARE_APPLICATION_ID_LIVE", "")
SQUARE_APPLICATION_SECRET_LIVE = os.getenv("SQUARE_APPLICATION_SECRET_LIVE", "")
SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST = os.getenv("SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST", "")
SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_LIVE = os.getenv("SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_LIVE", "")
SQUARE_OAUTH_WEBHOOK_URL_TEST = os.getenv("SQUARE_OAUTH_WEBHOOK_URL_TEST", "")
SQUARE_OAUTH_WEBHOOK_URL_LIVE = os.getenv("SQUARE_OAUTH_WEBHOOK_URL_LIVE", "")

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", FOXPAY_ENV)
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))
SENTRY_SEND_DEFAULT_PII = env_bool("SENTRY_SEND_DEFAULT_PII", False)

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=SENTRY_ENVIRONMENT,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=SENTRY_SEND_DEFAULT_PII,
    )
