# ---------- TEST ----------

import hmac
import hashlib
import base64
from datetime import datetime, timezone
from urllib.parse import urlencode

from datetime import datetime

now = datetime.utcnow()

signature_time = now.strftime("%Y%m%dT%H%M")        # for signing
header_time = now.strftime("%Y%m%dT%H%M%SZ")       # for header
date_only = now.strftime("%Y%m%d")   
ACCESS_KEY = "gYShz6WD"              # for credential


# def generate_signature(secret_key, method, data):
#     # Step 1: URL encode
#     encoded = urlencode(data, doseq=True)

#     # Step 2: timestamp
#     timestamp = datetime.now().strftime("%Y%m%dT%H%M")

#     string_to_sign = f"{method}{encoded}{timestamp}"

#     signature = base64.b64encode(
#         hmac.new(
#             secret_key.encode(),
#             string_to_sign.encode(),
#             hashlib.sha256
#         ).digest()
#     ).decode()

#     return signature, timestamp

import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime


def generate_signature(http_method, data_dict, secret_key):
    
    # Step 1: URL encode (RFC1738 style)
    encoded_data = urllib.parse.urlencode(data_dict, doseq=True)

    # Step 2: Timestamp (NO seconds)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M")

    # Step 3: String to sign
    string_to_sign = f"{http_method}{encoded_data}{timestamp}"

    # Step 4: HMAC SHA256
    digest = hmac.new(
        secret_key.encode(),
        string_to_sign.encode(),
        hashlib.sha256
    ).digest()

    signature = base64.b64encode(digest).decode()

    return signature, timestamp

def flatten_data(payload):
    flat = {}

    for key, value in payload.items():
        if isinstance(value, dict):
            for k, v in value.items():
                flat[f"{key}[{k}]"] = v
        else:
            flat[key] = value

    return flat

# ------ PURCHASE VOUCHER ----------

import requests

BASE_URL = "https://sandbox.mintroute.com/voucher/v2/api/voucher"


def purchase_voucher(variant, order):
    payload = {
        "username": "africanff.single",
        "data": {
            "ean": variant.ean,
            "terminal_id": "WEB001",
            "order_id": f"ORDER_{order.id}",
            "request_type": "purchase",
            "response_type": "short"
        }
    }

    # flat_data = {
    #     "username": payload["africanff.single"],
    #     "data[ean]": variant.ean,
    #     "data[terminal_id]": "WEB001",
    #     "data[order_id]": f"ORDER_{order.id}",
    #     "data[request_type]": "purchase",
    #     "data[response_type]": "short",
    # }
    flat_data = flatten_data(payload)

    signature, timestamp = generate_signature(
        secret_key="62aeb8c780f3d3d95c4d3449a6aa4467",
        http_method="POST",
        data_dict=flat_data
    )

    headers = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f'algorithm="hmac-sha256",credential="{ACCESS_KEY}/{date_only}",signature="{signature}"',
    "X-Mint-Date": header_time
}

    response = requests.post(BASE_URL, json=payload, headers=headers, timeout=60)
    # response = requests.post(url, json=payload, headers=headers, timeout=60)

    data = response.json()

    if not data.get("status"):
        return {
            "success": False,
            "error": data.get("error"),
            "code": data.get("error_code")
        }

    return {
        "success": True,
        "voucher": data["data"]["voucher"]
    }

    # return response.json()