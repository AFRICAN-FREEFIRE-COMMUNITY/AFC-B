# ---------- TEST ----------

import hmac
import hashlib
import base64
from datetime import datetime, timezone
from urllib.parse import urlencode


def generate_signature(secret_key, method, data):
    # Step 1: URL encode
    encoded = urlencode(data, doseq=True)

    # Step 2: timestamp
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M")

    string_to_sign = f"{method}{encoded}{timestamp}"

    signature = base64.b64encode(
        hmac.new(
            secret_key.encode(),
            string_to_sign.encode(),
            hashlib.sha256
        ).digest()
    ).decode()

    return signature, timestamp


# ------ PURCHASE VOUCHER ----------

import requests

BASE_URL = "https://sandbox.mintroute.com/voucher/v2/api/voucher"


def purchase_voucher(variant, order):
    payload = {
        "username": "YOUR_USERNAME",
        "data": {
            "ean": variant.ean,
            "terminal_id": "WEB001",
            "order_id": f"ORDER_{order.id}",
            "request_type": "purchase",
            "response_type": "short"
        }
    }

    flat_data = {
        "username": payload["username"],
        "data[ean]": variant.ean,
        "data[terminal_id]": "WEB001",
        "data[order_id]": f"ORDER_{order.id}",
        "data[request_type]": "purchase",
        "data[response_type]": "short",
    }

    signature, timestamp = generate_signature(
        "YOUR_SECRET_KEY",
        "POST",
        flat_data
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f'algorithm="hmac-sha256",credential="YOUR_ACCESS_KEY/{timestamp[:8]}",signature="{signature}"',
        "X-Mint-Date": f"{timestamp}00Z"
    }

    response = requests.post(BASE_URL, json=payload, headers=headers, timeout=60)

    return response.json()