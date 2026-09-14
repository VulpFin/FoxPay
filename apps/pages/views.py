"""The pages a payment service owes the people using it.

Static renders, except /status, which asks the database one question. They are
deliberately independent of the payment machinery so they still answer when a
provider or a worker is having a bad day.
"""
from django.db import connection
from django.shortcuts import render
from django.views.decorators.http import require_GET

# One place to bump when the policies change; every policy page shows it.
POLICY = {"effective": "2026-09-14", "updated": "2026-09-14", "version": "1.0"}


def _page(request, name, extra=None):
    return render(request, f"pages/{name}.html", {"policy": POLICY, **(extra or {})})


@require_GET
def about(request):
    return _page(request, "about")


@require_GET
def terms(request):
    return _page(request, "terms")


@require_GET
def privacy(request):
    return _page(request, "privacy")


@require_GET
def guidelines(request):
    return _page(request, "guidelines")


@require_GET
def faq(request):
    return _page(request, "faq")


@require_GET
def support(request):
    return _page(request, "support")


@require_GET
def changelog(request):
    return _page(request, "changelog")


@require_GET
def status(request):
    """A summary, not a diagnostic. Whether Fox Pay is serving and whether its
    database answers; nothing that would help someone map the deployment."""
    database = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database = False
    return _page(request, "status", {"overall": "ok" if database else "degraded", "database": database})
