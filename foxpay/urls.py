from django.contrib import admin
from django.urls import include, path

from .views import healthz


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("", include("apps.payments.urls")),
]
