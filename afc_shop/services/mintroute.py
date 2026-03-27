import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime


def generate_signature(http_method, data_dict, secret_key, timestamp):
    encoded_data = urllib.parse.urlencode(data_dict, doseq=True)
    string_to_sign = f"{http_method}{encoded_data}{timestamp}"

    digest = hmac.new(
        secret_key.encode(),
        string_to_sign.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(digest).decode()


def flatten_data(payload):
    flat = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            for k, v in value.items():
                flat[f"{key}[{k}]"] = v
        else:
            flat[key] = value
    return flat


import requests
import uuid
from django.conf import settings


BASE_URL = "https://sandbox.mintroute.com/voucher/v2/api/voucher"


def purchase_voucher(variant, order):

    now = datetime.utcnow()

    signature_time = now.strftime("%Y%m%dT%H%M")
    header_time = now.strftime("%Y%m%dT%H%M%SZ")
    date_only = now.strftime("%Y%m%d")

    payload = {
        "username": settings.MINTROUTE_USERNAME,
        "data": {
            "ean": variant.ean,
            "location": "UK",
            "terminal_id": "WEB001",
            "order_id": f"ORD-{order.id}-{uuid.uuid4().hex[:6]}",
            "request_type": "purchase",
            "response_type": "short"
        }
    }

    flat_data = flatten_data(payload)

    signature = generate_signature(
        "POST",
        flat_data,
        settings.MINRTOUTE_SECRET_KEY,
        signature_time
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f'algorithm="hmac-sha256",credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}",signature="{signature}"',
        "X-Mint-Date": header_time
    }

    try:
        response = requests.post(BASE_URL, json=payload, headers=headers, timeout=60)
        data = response.json()
    except Exception as e:
        return {"status": False, "error": str(e)}

    if not data.get("status"):
        return {
            "status": False,
            "error": data.get("error"),
            "code": data.get("error_code")
        }

    return {
        "status": True,
        "data": data.get("data")
    }