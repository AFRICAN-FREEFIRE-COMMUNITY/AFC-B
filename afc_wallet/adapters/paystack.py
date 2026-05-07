"""Paystack adapter — STUBBED. No external HTTP in v1.

Real implementation will:
- POST /transaction/initialize -> get authorization_url + access_code + reference
- Verify webhook via HMAC-SHA512 of the raw body using PAYSTACK_SECRET_KEY
- Use the `reference` as the WalletTxn.idempotency_key so retries no-op.
"""

import hashlib
import hmac
from typing import Any, Mapping


def verify_signature(
    secret_key: str, raw_body: bytes, signature_header: str
) -> bool:
    """Verify Paystack's `x-paystack-signature` header (HMAC-SHA512)."""
    if not secret_key or not raw_body or not signature_header:
        return False
    digest = hmac.new(
        secret_key.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(digest, signature_header)


def build_idempotency_key(reference: str) -> str:
    """Return the idempotency_key used on the resulting WalletTxn."""
    return f"paystack:{reference}"


def initialize_payment(
    *, email: str, amount_kobo: int, reference: str, callback_url: str
) -> Mapping[str, Any]:
    """STUBBED — would POST to paystack /transaction/initialize."""
    return {
        "stubbed": True,
        "authorization_url": f"https://stub.paystack/{reference}",
        "access_code": f"stub_{reference}",
        "reference": reference,
    }
