from django.contrib import admin
from django.urls import include, path

from .views import healthz
from apps.accounts import views as account_views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("auth/tg11/", include("tg11_auth.urls")),
    path("", account_views.dashboard, name="account_dashboard"),
    path("payments/", account_views.payments, name="account_payments"),
    path("orders/", account_views.orders, name="account_orders"),
    path("subscriptions/", account_views.subscriptions, name="account_subscriptions"),
    path("payment-methods/", account_views.payment_methods, name="account_payment_methods"),
    path("payment-methods/add/<slug:merchant_slug>/<str:provider>/", account_views.add_payment_method, name="account_add_payment_method"),
    path("payment-methods/remove/<uuid:method_uuid>/", account_views.remove_payment_method, name="account_remove_payment_method"),
    path("connections/", account_views.connections, name="account_connections"),
    path("", include("apps.payments.urls")),
]
