# afc_partner_api/auth.py
# ──────────────────────────────────────────────────────────────────────────────
# Partner API-key auth. Mirrors validate_token's role (afc_auth.views): a single
# helper the read endpoints call to turn a request into an authenticated principal.
# Keys are random secrets; only their sha256 hash is stored, and the plaintext is
# shown to the AFC admin exactly once at issue time (so a DB leak never exposes a
# usable credential). Full spec: WEBSITE/tasks/partner-api-design.md (§6 auth).
# ──────────────────────────────────────────────────────────────────────────────
import hashlib
import secrets

from django.utils import timezone

from .models import Partner, PartnerApiKey

KEY_NAMESPACE = "afcp"   # all partner keys look like  afcp_<prefix>_<secret>


class PartnerAuthError(Exception):
    """Raised when a request cannot be authenticated as a partner (-> 401)."""


def generate_key():
    """Return (full_key, key_prefix, key_hash). full_key is shown once; never stored.

    The prefix is the stable lookup handle persisted alongside the hash; the secret
    tail is the part that is hashed-then-discarded. Splitting it this way lets us
    index the row by prefix without ever storing anything that can authenticate.
    """
    prefix = f"{KEY_NAMESPACE}_{secrets.token_hex(2)}"   # e.g. afcp_3f9a (2 hex bytes -> 4 chars)
    secret = secrets.token_hex(24)                       # 24 bytes -> 48 hex chars of entropy
    full = f"{prefix}_{secret}"
    return full, prefix, hash_key(full)


def hash_key(full_key: str) -> str:
    """sha256 hex digest of the full key — the only form ever persisted/compared."""
    return hashlib.sha256(full_key.encode()).hexdigest()


def authenticate_partner(request):
    """Resolve X-API-Key -> (Partner, PartnerApiKey). Raise PartnerAuthError on any failure.

    Order matters: shape-check the header first (cheap reject of garbage), look the
    key up by its non-secret prefix, then verify the secret in CONSTANT TIME so a
    timing side-channel can't leak how many leading bytes matched. Only after the
    credential proves valid do we check the partner's standing and stamp usage.
    """
    provided = request.headers.get("X-API-Key", "")
    parts = provided.split("_")
    # Expect exactly  afcp_<prefix>_<secret>  (three underscore-separated parts).
    if len(parts) != 3 or parts[0] != KEY_NAMESPACE:
        raise PartnerAuthError("Missing or malformed X-API-Key.")
    prefix = f"{parts[0]}_{parts[1]}"
    key = (PartnerApiKey.objects
           .select_related("partner")
           .filter(key_prefix=prefix, status="active").first())
    if not key:
        # Same generic error whether the prefix is unknown or the key was revoked —
        # don't leak which one to a caller probing for valid prefixes.
        raise PartnerAuthError("Unknown or revoked key.")
    if key.expires_at and key.expires_at < timezone.now():
        raise PartnerAuthError("Key expired.")
    # Constant-time compare of the full-key hash (defeats timing attacks).
    if not secrets.compare_digest(key.key_hash, hash_key(provided)):
        raise PartnerAuthError("Invalid key.")
    if key.partner.status != "active":
        raise PartnerAuthError("Partner suspended.")
    # Stamp last_used_at for auditing/rotation; touch only that column.
    key.last_used_at = timezone.now()
    key.save(update_fields=["last_used_at"])
    return key.partner, key
