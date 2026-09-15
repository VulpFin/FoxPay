import hashlib
import hmac
import ipaddress
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.mail import mail_admins
from django.db import transaction
from django.utils import timezone

from .models import AbuseAlert, AuditLog, Merchant


class AbuseControlError(Exception):
    code = "abuse_control_unavailable"


class AbuseLimitExceeded(AbuseControlError):
    code = "velocity_limit_exceeded"


def request_ip(request):
    meta = getattr(request, "META", {})
    remote_value = str(meta.get("REMOTE_ADDR", "")).strip()
    try:
        remote = ipaddress.ip_address(remote_value)
    except ValueError:
        return "unknown"
    trusted_proxy = False
    for value in getattr(settings, "FOXPAY_TRUSTED_PROXY_IPS", ()):
        try:
            if remote in ipaddress.ip_network(value, strict=False):
                trusted_proxy = True
                break
        except ValueError:
            continue
    if trusted_proxy:
        forwarded = str(meta.get("HTTP_X_FORWARDED_FOR", "")).split(",", 1)[0].strip()
        real_ip = str(meta.get("HTTP_X_REAL_IP", "")).strip()
        for candidate in (forwarded, real_ip):
            if not candidate:
                continue
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return str(remote)


def hash_ip(value):
    key = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def request_ip_hash(request):
    return hash_ip(request_ip(request))


def _cache_key(environment, metric, identity):
    return f"foxpay:risk:v1:{environment}:{metric}:{identity}"


def _fail_closed(environment):
    return environment == "live" and getattr(settings, "FOXPAY_ABUSE_FAIL_CLOSED", True)


def increment(environment, metric, identity, *, amount=1, timeout=60, strict=True):
    key = _cache_key(environment, metric, identity)
    try:
        if cache.add(key, amount, timeout=timeout):
            return amount
        return cache.incr(key, amount)
    except Exception as exc:
        if strict and _fail_closed(environment):
            raise AbuseControlError("Production abuse controls are unavailable.") from exc
        return 0


def metric_value(environment, metric, identity):
    try:
        return int(cache.get(_cache_key(environment, metric, identity), 0) or 0)
    except Exception:
        return 0


def _notify_staff(alert_id):
    try:
        alert = AbuseAlert.objects.select_related("merchant").get(pk=alert_id)
        mail_admins(
            subject=f"FoxPay routing restriction: {alert.merchant.slug}",
            message=(
                f"FoxPay temporarily restricted {alert.merchant.name} "
                f"({alert.environment}) after a payment-abuse signal. "
                f"Rule: {alert.rule_code}. Alert: {alert.uuid}."
            ),
            fail_silently=True,
        )
        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                "FoxPay temporarily restricted merchant routing",
                level="warning",
                scope={
                    "tags": {
                        "merchant": alert.merchant.slug,
                        "environment": alert.environment,
                        "risk_rule": alert.rule_code,
                    }
                },
            )
        except Exception:
            pass
    except AbuseAlert.DoesNotExist:
        pass


def create_alert(merchant, environment, rule_code, summary, *, ip_hash="", counters=None, restrict=False):
    now = timezone.now()
    restricted_until = None
    with transaction.atomic():
        merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
        alert = (
            AbuseAlert.objects.select_for_update()
            .filter(
                merchant=merchant,
                environment=environment,
                rule_code=rule_code,
                status=AbuseAlert.STATUS_OPEN,
                created_at__gte=now - timedelta(hours=1),
            )
            .order_by("-created_at")
            .first()
        )
        created = alert is None
        if alert is None:
            alert = AbuseAlert.objects.create(
                merchant=merchant,
                environment=environment,
                rule_code=rule_code,
                request_ip_hash=ip_hash,
                summary=summary[:240],
                counters=counters or {},
            )
        else:
            alert.counters = counters or alert.counters
            alert.save(update_fields=["counters", "updated_at"])
        if restrict:
            restricted_until = now + timedelta(seconds=settings.FOXPAY_TEMP_RESTRICTION_SECONDS)
            if not merchant.temporarily_restricted_until or merchant.temporarily_restricted_until < restricted_until:
                merchant.temporarily_restricted_until = restricted_until
                merchant.temporary_restriction_reason = rule_code
                merchant.save(
                    update_fields=[
                        "temporarily_restricted_until",
                        "temporary_restriction_reason",
                        "updated_at",
                    ]
                )
            alert.restricted_until = restricted_until
            alert.save(update_fields=["restricted_until", "updated_at"])
        if created:
            AuditLog.objects.create(
                merchant=merchant,
                action="routing.automatic_restriction" if restrict else "risk.alert_created",
                object_type="abuse_alert",
                object_id=str(alert.uuid),
                metadata={
                    "environment": environment,
                    "rule_code": rule_code,
                    "restricted_until": restricted_until.isoformat() if restricted_until else None,
                },
            )
            transaction.on_commit(lambda: _notify_staff(alert.pk))
    return alert


def enforce_api_limits(request, merchant, bucket="api"):
    environment = getattr(settings, "FOXPAY_ENV", "test")
    ip_hash = request_ip_hash(request)
    merchant_count = increment(environment, f"api:{bucket}:merchant", merchant.pk)
    ip_count = increment(environment, f"api:{bucket}:ip", ip_hash)
    if ip_count > settings.FOXPAY_API_IP_RATE_LIMIT_PER_MINUTE:
        create_alert(
            merchant,
            environment,
            "api_ip_velocity",
            "An origin exceeded the API request velocity policy.",
            ip_hash=ip_hash,
            counters={"request_count": ip_count},
        )
        raise AbuseLimitExceeded("Rate limit exceeded.")
    if merchant_count > settings.FOXPAY_API_RATE_LIMIT_PER_MINUTE:
        create_alert(
            merchant,
            environment,
            "api_merchant_velocity",
            "The merchant exceeded the API request velocity policy.",
            counters={"request_count": merchant_count},
            restrict=True,
        )
        raise AbuseLimitExceeded("Rate limit exceeded.")


def enforce_intent_creation(request, merchant, amount):
    environment = getattr(settings, "FOXPAY_ENV", "test")
    ip_hash = request_ip_hash(request)
    merchant_count = increment(environment, "intent:min:merchant", merchant.pk)
    ip_count = increment(environment, "intent:min:ip", ip_hash)
    hourly_amount = increment(environment, "amount:hour:merchant", merchant.pk, amount=amount, timeout=3600)
    daily_amount = increment(environment, "amount:day:merchant", merchant.pk, amount=amount, timeout=86400)
    starts = increment(environment, "checkout:hour:merchant", merchant.pk, timeout=3600)
    increment(environment, "checkout:hour:ip", ip_hash, timeout=3600)
    if amount <= settings.FOXPAY_SMALL_PAYMENT_AMOUNT:
        increment(environment, "small:hour:merchant", merchant.pk, timeout=3600)
        increment(environment, "small:hour:ip", ip_hash, timeout=3600)

    if ip_count > settings.FOXPAY_INTENT_RATE_LIMIT_PER_IP:
        create_alert(
            merchant,
            environment,
            "intent_ip_velocity",
            "An origin exceeded the checkout creation policy.",
            ip_hash=ip_hash,
            counters={"intent_count": ip_count},
        )
        raise AbuseLimitExceeded("Checkout creation limit exceeded.")
    rule = ""
    if merchant_count > settings.FOXPAY_INTENT_RATE_LIMIT_PER_MERCHANT:
        rule = "intent_merchant_velocity"
    elif hourly_amount > settings.FOXPAY_ATTEMPT_AMOUNT_LIMIT_HOURLY:
        rule = "attempted_amount_hourly"
    elif daily_amount > settings.FOXPAY_ATTEMPT_AMOUNT_LIMIT_DAILY:
        rule = "attempted_amount_daily"
    if rule:
        create_alert(
            merchant,
            environment,
            rule,
            "The merchant exceeded a checkout abuse-control policy.",
            counters={
                "intent_count": merchant_count,
                "checkout_count": starts,
                "hourly_amount": hourly_amount,
                "daily_amount": daily_amount,
            },
            restrict=True,
        )
        raise AbuseLimitExceeded("Checkout creation limit exceeded.")
    return ip_hash


def enforce_provider_call(merchant, *, ip_hash="", environment=None):
    environment = environment or getattr(settings, "FOXPAY_ENV", "test")
    merchant_count = increment(environment, "provider:min:merchant", merchant.pk)
    if ip_hash:
        ip_count = increment(environment, "provider:min:ip", ip_hash)
        if ip_count > settings.FOXPAY_PROVIDER_CALL_RATE_LIMIT_PER_IP:
            create_alert(
                merchant,
                environment,
                "provider_ip_velocity",
                "An origin exceeded the provider-call policy.",
                ip_hash=ip_hash,
                counters={"provider_call_count": ip_count},
            )
            raise AbuseLimitExceeded("Provider call limit exceeded.")
    if merchant_count > settings.FOXPAY_PROVIDER_CALL_RATE_LIMIT_PER_MERCHANT:
        create_alert(
            merchant,
            environment,
            "provider_merchant_velocity",
            "The merchant exceeded the provider-call policy.",
            counters={"provider_call_count": merchant_count},
            restrict=True,
        )
        raise AbuseLimitExceeded("Provider call limit exceeded.")


def record_payment_outcome(merchant, *, succeeded, ip_hash="", amount=0, provider_risk="", environment=None):
    environment = environment or getattr(settings, "FOXPAY_ENV", "test")
    outcome = "success" if succeeded else "failure"
    increment(environment, f"outcome:hour:{outcome}:merchant", merchant.pk, timeout=3600, strict=False)
    if ip_hash:
        increment(environment, f"outcome:hour:{outcome}:ip", ip_hash, timeout=3600, strict=False)

    failures = metric_value(environment, "outcome:hour:failure:merchant", merchant.pk)
    successes = metric_value(environment, "outcome:hour:success:merchant", merchant.pk)
    starts = metric_value(environment, "checkout:hour:merchant", merchant.pk)
    small = metric_value(environment, "small:hour:merchant", merchant.pk)
    outcomes = failures + successes
    failure_ratio = failures / outcomes if outcomes else 0
    small_ratio = small / starts if starts else 0
    provider_block = str(provider_risk).lower() in {"blocked", "highest", "fraudulent"}
    suspicious_pattern = (
        outcomes >= settings.FOXPAY_CARD_TEST_MIN_OUTCOMES
        and failure_ratio >= settings.FOXPAY_CARD_TEST_FAILURE_RATIO
        and small_ratio >= settings.FOXPAY_CARD_TEST_SMALL_RATIO
    )
    if provider_block or suspicious_pattern:
        create_alert(
            merchant,
            environment,
            "provider_fraud_signal" if provider_block else "card_testing_pattern",
            "Provider risk data or checkout outcomes indicate possible payment abuse.",
            ip_hash=ip_hash,
            counters={
                "outcomes": outcomes,
                "failures": failures,
                "failure_ratio_bps": int(failure_ratio * 10000),
                "small_ratio_bps": int(small_ratio * 10000),
            },
            restrict=True,
        )


def clear_expired_restrictions():
    now = timezone.now()
    merchants = Merchant.objects.filter(temporarily_restricted_until__lte=now)
    count = merchants.update(temporarily_restricted_until=None, temporary_restriction_reason="")
    AbuseAlert.objects.filter(
        status=AbuseAlert.STATUS_OPEN,
        restricted_until__lte=now,
    ).update(status=AbuseAlert.STATUS_RESOLVED, resolved_at=now)
    return count
