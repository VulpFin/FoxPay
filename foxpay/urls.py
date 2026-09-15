from django.contrib import admin
from django.urls import include, path

from .views import healthz
from apps.accounts import views as account_views
from apps.payments import merchant_views
from apps.payments import stripe_connect_views
from apps.payments import square_oauth_views
from apps.payments import nowpayments_views
from apps.payments import paypal_partner_views


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
    path("seller/", merchant_views.index, name="seller_index"),
    path("seller/apply/", merchant_views.apply, name="seller_apply"),
    path("seller/agreement/", merchant_views.agreement, name="seller_agreement"),
    path("seller/review/", merchant_views.review, name="seller_review"),
    path("seller/<slug:slug>/stripe/<str:environment>/connect/", stripe_connect_views.begin_stripe_connect, name="seller_stripe_connect"),
    path("seller/<slug:slug>/stripe/<str:environment>/callback/", stripe_connect_views.stripe_connect_callback, name="seller_stripe_callback"),
    path("seller/<slug:slug>/stripe/<uuid:connection_uuid>/disconnect/", stripe_connect_views.disconnect_stripe_connect, name="seller_stripe_disconnect"),
    path("api/v1/webhooks/stripe/connect/<str:endpoint_environment>/", stripe_connect_views.stripe_connect_webhook, name="stripe_connect_webhook"),
    path("seller/<slug:slug>/square/<str:environment>/connect/", square_oauth_views.begin_square_oauth, name="seller_square_connect"),
    path("seller/<slug:slug>/square/<str:environment>/callback/", square_oauth_views.square_oauth_callback, name="seller_square_callback"),
    path("seller/<slug:slug>/square/<uuid:connection_uuid>/location/", square_oauth_views.select_square_location, name="seller_square_location"),
    path("seller/<slug:slug>/square/<uuid:connection_uuid>/disconnect/", square_oauth_views.disconnect_square_oauth, name="seller_square_disconnect"),
    path("api/v1/webhooks/square/oauth/<str:environment>/", square_oauth_views.square_oauth_webhook, name="square_oauth_webhook"),
    path("seller/<slug:slug>/nowpayments/connect/", nowpayments_views.connect_nowpayments, name="seller_nowpayments_connect"),
    path("seller/<slug:slug>/nowpayments/<uuid:connection_uuid>/replace/", nowpayments_views.replace_nowpayments_credentials, name="seller_nowpayments_replace"),
    path("seller/<slug:slug>/nowpayments/<uuid:connection_uuid>/test/", nowpayments_views.test_nowpayments_connection, name="seller_nowpayments_test"),
    path("seller/<slug:slug>/nowpayments/<uuid:connection_uuid>/disconnect/", nowpayments_views.disconnect_nowpayments, name="seller_nowpayments_disconnect"),
    path("seller/<slug:slug>/paypal/<str:environment>/connect/", paypal_partner_views.begin_paypal_partner, name="seller_paypal_connect"),
    path("seller/paypal/<str:environment>/callback/", paypal_partner_views.paypal_partner_callback, name="seller_paypal_callback"),
    path("seller/<slug:slug>/paypal/<uuid:connection_uuid>/refresh/", paypal_partner_views.refresh_paypal_partner, name="seller_paypal_refresh"),
    path("seller/<slug:slug>/paypal/<uuid:connection_uuid>/disconnect/", paypal_partner_views.disconnect_paypal_partner, name="seller_paypal_disconnect"),
    path("api/v1/webhooks/paypal/partner/<str:environment>/", paypal_partner_views.paypal_partner_webhook, name="paypal_partner_webhook"),
    path("seller/<slug:slug>/keys/create/", merchant_views.create_key, name="seller_create_key"),
    path("seller/<slug:slug>/keys/<uuid:key_uuid>/revoke/", merchant_views.revoke_key, name="seller_revoke_key"),
    path("seller/<slug:slug>/keys/<uuid:key_uuid>/rotate/", merchant_views.rotate_key, name="seller_rotate_key"),
    path("seller/<slug:slug>/returns/add/", merchant_views.add_return_origin, name="seller_add_return_origin"),
    path("seller/<slug:slug>/returns/<int:origin_id>/remove/", merchant_views.remove_return_origin, name="seller_remove_return_origin"),
    path("seller/<slug:slug>/webhooks/create/", merchant_views.create_webhook, name="seller_create_webhook"),
    path("seller/<slug:slug>/webhooks/<uuid:endpoint_uuid>/rotate/", merchant_views.rotate_webhook, name="seller_rotate_webhook"),
    path("seller/<slug:slug>/webhooks/<uuid:endpoint_uuid>/activate/", merchant_views.activate_webhook_secret, name="seller_activate_webhook_secret"),
    path("seller/<slug:slug>/webhooks/<uuid:endpoint_uuid>/disable/", merchant_views.disable_webhook, name="seller_disable_webhook"),
    path("seller/<slug:slug>/webhooks/<uuid:endpoint_uuid>/test/", merchant_views.test_webhook, name="seller_test_webhook"),
    path("seller/<slug:slug>/members/add/", merchant_views.add_member, name="seller_add_member"),
    path("seller/<slug:slug>/members/<uuid:member_uuid>/role/", merchant_views.change_member_role, name="seller_change_member_role"),
    path("seller/<slug:slug>/members/<uuid:member_uuid>/remove/", merchant_views.remove_member, name="seller_remove_member"),
    path("seller/<slug:slug>/", merchant_views.detail, name="seller_detail"),
    path("seller/<slug:slug>/<slug:section>/", merchant_views.detail, name="seller_section"),
    path("", include("apps.pages.urls")),
    path("", include("apps.payments.urls")),
]
