"""Stripe adapter — STUBBED. No external HTTP in v1.

Real implementation will:
- Create Checkout Session (USD or NGN) and return session_id
- Verify webhook via Stripe-Signature header parse + HMAC-SHA256
- Idempotency_key = stripe session_id.
"""

import hashlib
import hmac
import time
from typing import Any, Mapping


def verify_signature(
    webhook_secret: str,
    raw_body: bytes,
    signature_header: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify Stripe's `Stripe-Signature` header.

    Header format: `t=<unix_ts>,v1=<hex>` (we parse leniently).
    """
    if not webhook_secret or not raw_body or not signature_header:
        return False
    parts = {}
    for chunk in signature_header.split(","):
        if "=" in chunk:
            k, v = chunk.strip().split("=", 1)
            parts[k] = v
    ts = parts.get("t")
    sig = parts.get("v1")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > tolerance_seconds:
        return False
    payload = f"{ts}.{raw_body.decode('utf-8', 'ignore')}"
    digest = hmac.new(
        webhook_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, sig)


def build_idempotency_key(session_id: str) -> str:
    return f"stripe:{session_id}"


def create_checkout_session(
    *, amount_kobo: int, currency: str = "NGN"
) -> Mapping[str, Any]:
    return {"stubbed": True, "session_id": "cs_stub_v1", "amount_kobo": amount_kobo}
