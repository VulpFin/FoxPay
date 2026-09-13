import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def encryption_key():
    digest = hashlib.sha256(settings.FOXPAY_SECRET_ENCRYPTION_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(value):
    if value is None:
        value = ""
    return Fernet(encryption_key()).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    if not value:
        return ""
    return Fernet(encryption_key()).decrypt(value.encode("utf-8")).decode("utf-8")
