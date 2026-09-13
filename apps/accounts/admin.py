from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import FoxPayProfile, User


@admin.register(User)
class FoxPayUserAdmin(UserAdmin):
    pass


@admin.register(FoxPayProfile)
class FoxPayProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "tg11_user_uuid", "tg11_subject", "created_at")
    search_fields = ("user__username", "tg11_subject", "tg11_user_uuid")
