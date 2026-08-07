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

# ONE message for every "this credential does not authenticate" outcome: an unknown
# prefix, a revoked key, and a known prefix with the wrong secret all say exactly this.
# Why it is a constant rather than three strings (audit finding 2026-08-06): the code
# below USED to answer "Unknown or revoked key." for an unrecognised prefix but
# "Invalid key." for a recognised one, which handed a caller a yes/no oracle on the
# 4-hex-char prefix space - it could sweep 65,536 prefixes and learn which ones AFC has
# issued, purely from the wording. The prefix is not itself a credential (the 48-hex
# secret is), so this was never an authentication bypass, but it leaks the shape of the
# partner estate for free and contradicts what this module already claimed to do. One
# constant makes the three cases genuinely indistinguishable.
UNAUTHENTICATED_MESSAGE = "Unknown or revoked key."

# A syntactically valid sha256 hex digest that no real key can hash to, used ONLY to
# burn the same compare_digest work on the unknown-prefix path that the known-prefix
# path spends. Without it, "no such prefix" returns measurably sooner than "wrong
# secret", rebuilding the same oracle out of response time after the wording was fixed.
_DUMMY_HASH = "0" * 64


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
    """sha256 hex digest of the full key - the only form ever persisted/compared."""
    return hashlib.sha256(full_key.encode()).hexdigest()


def authenticate_partner(request):
    """Resolve X-API-Key -> (Partner, PartnerApiKey). Raise PartnerAuthError on any failure.

    Order matters: shape-check the header first (cheap reject of garbage), look the
    key up by its non-secret prefix, then verify the secret in CONSTANT TIME so a
    timing side-channel can't leak how many leading bytes matched. EVERY remaining
    check - expiry, then the partner's standing - happens only AFTER the secret proves
    out, so their distinct messages ("Key expired." / "Partner suspended.") can only
    ever be read by the holder of the real key. Usage is stamped last.
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
        # Unknown prefix OR revoked key. Spend the same compare_digest the known-prefix
        # branch spends before raising, so the two paths cost the same wall-clock time,
        # then raise the SAME message they do (see UNAUTHENTICATED_MESSAGE above).
        secrets.compare_digest(_DUMMY_HASH, hash_key(provided))
        raise PartnerAuthError(UNAUTHENTICATED_MESSAGE)
    # Constant-time compare of the full-key hash (defeats timing attacks). A mismatch is
    # reported with the SAME wording as an unknown prefix: telling the caller "this
    # prefix exists, the secret is wrong" is the oracle UNAUTHENTICATED_MESSAGE exists
    # to close.
    if not secrets.compare_digest(key.key_hash, hash_key(provided)):
        raise PartnerAuthError(UNAUTHENTICATED_MESSAGE)
    # EXPIRY IS CHECKED HERE, AFTER THE SECRET COMPARE, AND THE ORDER IS THE POINT (audit
    # finding 2026-08-07, backlog item 8). It used to run BEFORE the compare, which left
    # the prefix oracle wide open through a second door: a caller who guessed a prefix and
    # sent a junk secret got "Key expired." for an expired key and UNAUTHENTICATED_MESSAGE
    # for everything else, so 65,536 unauthenticated probes still mapped out part of the
    # partner estate. Verified live before the fix: prefix afcp_97e6 with a
    # forty-eight-zero secret answered "Key expired.". Below the compare, this message is
    # only ever reachable by someone who already holds the real key, which is exactly who
    # needs to be told WHY their integration stopped working rather than a flat "unknown".
    # Same reasoning as the "Partner suspended." branch under it, which was always here.
    if key.expires_at and key.expires_at < timezone.now():
        raise PartnerAuthError("Key expired.")
    if key.partner.status != "active":
        raise PartnerAuthError("Partner suspended.")
    # Stamp last_used_at for auditing/rotation; touch only that column.
    key.last_used_at = timezone.now()
    key.save(update_fields=["last_used_at"])
    return key.partner, key
