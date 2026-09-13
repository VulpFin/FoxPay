from django.urls import path

from . import views


app_name = "payments"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/v1/openapi.json", views.openapi, name="openapi"),
    path("api/v1/payment-intents/", views.payment_intents, name="payment_intents"),
    path("api/v1/payment-intents/<str:public_id>/", views.payment_intent_detail, name="payment_intent_detail"),
    path("api/v1/payment-intents/<str:public_id>/refunds/", views.refunds, name="refunds"),
    path("api/v1/webhooks/crypto/<str:provider>/", views.crypto_webhook, name="crypto_webhook"),
    path("pay/<str:client_secret>/", views.foxpay_checkout, name="foxpay_checkout"),
    path("checkout/card/<int:attempt_id>/<str:client_secret>/", views.mock_card_checkout, name="mock_card_checkout_attempt"),
    path("checkout/card/<str:client_secret>/", views.mock_card_checkout, name="mock_card_checkout"),
    path("checkout/crypto/<int:attempt_id>/<str:client_secret>/", views.crypto_checkout, name="crypto_checkout_attempt"),
    path("checkout/crypto/<str:client_secret>/", views.crypto_checkout, name="crypto_checkout"),
]
