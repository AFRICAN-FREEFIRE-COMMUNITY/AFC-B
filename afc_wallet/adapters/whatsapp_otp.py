"""WhatsApp OTP adapter — MOCK only in v1.

Real implementation will use WhatsApp Cloud API + a Termii fallback for
Nigeria. The mock always accepts "000000" so QA flows can be deterministic
without a Twilio sandbox.
"""

import secrets
import string
from typing import Tuple


MOCK_ACCEPTED_OTP = "000000"


def send_otp(*, e164_number: str) -> Tuple[bool, str]:
    """STUB — pretend we sent an OTP. Returns (sent_ok, otp_id)."""
    if not e164_number or not e164_number.startswith("+"):
        return (False, "")
    otp_id = "".join(
        secrets.choice(string.ascii_lowercase + string.digits)
        for _ in range(12)
    )
    return (True, otp_id)


def verify_otp(*, otp_id: str, code: str) -> bool:
    """In mock mode, "000000" always succeeds; everything else fails."""
    if not code:
        return False
    return code.strip() == MOCK_ACCEPTED_OTP
