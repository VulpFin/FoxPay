from django.db.models.signals import post_delete
from django.dispatch import receiver
from tg11_auth.models import TG11IdentityLink

from .models import FoxPayProfile


@receiver(post_delete, sender=TG11IdentityLink)
def clear_profile_link(sender, instance, **kwargs):
    FoxPayProfile.objects.filter(user_id=instance.user_id).update(tg11_user_uuid=None, tg11_subject="")
