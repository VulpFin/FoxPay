from django.urls import path

from . import views


urlpatterns = [
    path("about/", views.about, name="page_about"),
    path("terms/", views.terms, name="page_terms"),
    path("privacy/", views.privacy, name="page_privacy"),
    path("guidelines/", views.guidelines, name="page_guidelines"),
    path("faq/", views.faq, name="page_faq"),
    path("support/", views.support, name="page_support"),
    path("status/", views.status, name="page_status"),
    path("changelog/", views.changelog, name="page_changelog"),
]
