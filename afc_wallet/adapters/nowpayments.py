"""NowPayments (crypto) adapter — STUBBED. No external HTTP in v1."""

import hashlib
import hmac
import json


def verify_signature(
    ipn_secret: str, raw_body: bytes, signature_header: str
) -> bool:
    """NowPayments signs the JSON-sorted body with HMAC-SHA512."""
    if not ipn_secret or not raw_body or not signature_header:
        return False
    try:
        payload = json.loads(raw_body)
    except Exception:
        return False
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hmac.new(
        ipn_secret.encode("utf-8"), canonical, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(digest, signature_header)


def build_idempotency_key(payment_id: str) -> str:
    return f"nowpayments:{payment_id}"
