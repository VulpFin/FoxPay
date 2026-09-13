from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models


class User(AbstractUser):
    """Project user model kept swappable from day one."""


class FoxPayProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="foxpay_profile")
    tg11_user_uuid = models.UUIDField(unique=True, blank=True, null=True)
    tg11_subject = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.tg11_subject or self.user.get_username()
