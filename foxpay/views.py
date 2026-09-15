import secrets

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


def _database_ready():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


@require_GET
def healthz(request):
    try:
        _database_ready()
    except Exception:
        return JsonResponse({"status": "unavailable", "service": "foxpay"}, status=503)
    return JsonResponse({"status": "ok", "service": "foxpay"})


@require_GET
def readyz(request):
    key = f"readiness:{secrets.token_hex(12)}"
    marker = secrets.token_hex(16)
    try:
        _database_ready()
        cache.set(key, marker, timeout=15)
        if cache.get(key) != marker:
            raise RuntimeError("Cache readiness marker did not round-trip.")
        cache.delete(key)
    except Exception:
        return JsonResponse({"status": "unavailable", "service": "foxpay"}, status=503)
    return JsonResponse({"status": "ready", "service": "foxpay"})
