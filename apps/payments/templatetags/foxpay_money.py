from decimal import Decimal

from django import template


register = template.Library()
ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
THREE_DECIMAL = {"BHD", "JOD", "KWD", "OMR", "TND"}


@register.filter
def money(amount, currency="USD"):
    if amount is None:
        return "-"
    currency = str(currency).upper()
    places = 0 if currency in ZERO_DECIMAL else 3 if currency in THREE_DECIMAL else 2
    value = Decimal(amount) / (10 ** places)
    return f"{currency} {value:,.{places}f}"
