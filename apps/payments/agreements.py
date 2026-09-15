import hashlib
from pathlib import Path

from django.conf import settings


AGREEMENT_VERSION = "draft-2026-09-15"
ACCEPTABLE_USE_VERSION = "1.0"


def agreement_snapshot():
    agreement = (Path(settings.BASE_DIR) / "docs" / "MERCHANT_AGREEMENT_DRAFT.md").read_bytes()
    acceptable_use = (Path(settings.BASE_DIR) / "templates" / "pages" / "guidelines.html").read_bytes()
    return {
        "agreement_version": AGREEMENT_VERSION,
        "agreement_hash": hashlib.sha256(agreement).hexdigest(),
        "acceptable_use_version": ACCEPTABLE_USE_VERSION,
        "acceptable_use_hash": hashlib.sha256(acceptable_use).hexdigest(),
        "agreement_text": agreement.decode("utf-8"),
    }
