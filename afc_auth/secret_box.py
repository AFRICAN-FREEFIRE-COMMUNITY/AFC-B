"""
afc_auth/secret_box.py - seal a secret an organization gives us (their AI provider key) at rest.

WHY THIS EXISTS (owner 2026-09-12, "treat the key like a password")
    An organization's AI key is their bill. It is stored so OCR can use it on their behalf and for
    nothing else, so it lives sealed: a database dump does not hand every organizer's key to
    whoever holds it. Same construction as the two-factor secrets in afc_auth/two_factor.py
    (Fernet, key derived with HKDF from the deployment's SECRET_KEY), with its OWN `info` string so
    the two ciphers are domain-separated: opening one box never opens the other, and rotating one
    never touches the other.

WHAT IT DOES
    seal(plain) -> str      URL-safe base64, safe in a TextField
    open_sealed(sealed) -> str | ""   "" when the box cannot open it (a SECRET_KEY rotation), so the
                            caller treats the key as gone and asks the organizer to paste it again
                            rather than crash the OCR flow.

HOW IT CONNECTS
    afc_organizers.models.OrganizationAiKey.set_key / get_key are the only callers. The plaintext
    exists in memory for the length of one provider request and is never logged, serialized or
    returned by any endpoint (views_ai_key returns last_four only).
"""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

_BOX = None


def _box() -> Fernet:
    global _BOX
    if _BOX is None:
        base = getattr(settings, "SECRET_BOX_KEY", None) or settings.SECRET_KEY
        if not base:
            raise RuntimeError(
                "Secrets cannot be sealed: neither SECRET_BOX_KEY nor SECRET_KEY is set. "
                "Set DJANGO_SECRET_KEY in the environment.")
        material = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"afc-organization-ai-key-v1",
        ).derive(str(base).encode("utf-8"))
        _BOX = Fernet(base64.urlsafe_b64encode(material))
    return _BOX


def seal(plain: str) -> str:
    return _box().encrypt(plain.encode("utf-8")).decode("ascii")


def open_sealed(sealed: str) -> str:
    if not sealed:
        return ""
    try:
        return _box().decrypt(sealed.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
