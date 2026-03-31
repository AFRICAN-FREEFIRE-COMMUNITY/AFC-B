# import hmac
# import hashlib
# import base64
# import urllib.parse
# from datetime import datetime, timezone


# def generate_signature(http_method, data_dict, secret_key, timestamp):
#     encoded_data = urllib.parse.urlencode(data_dict, doseq=True)

#     string_to_sign = f"{http_method}{encoded_data}{timestamp}"

#     digest = hmac.new(
#         secret_key.encode(),
#         string_to_sign.encode(),
#         hashlib.sha256
#     ).digest()

#     return base64.b64encode(digest).decode()


# def flatten_data(payload):
#     flat = {}
#     for key, value in payload.items():
#         if isinstance(value, dict):
#             for k, v in value.items():
#                 flat[f"{key}[{k}]"] = v
#         else:
#             flat[key] = value
#     return flat


# import requests
# import uuid
# from django.conf import settings


# BASE_URL = "https://sandbox.mintroute.com/voucher/v2/api/voucher"


# def purchase_voucher(variant, order):

#     now = datetime.now(timezone.utc)

#     signature_time = now.strftime("%Y%m%dT%H%M")
#     header_time = now.strftime("%Y%m%dT%H%M%SZ")
#     date_only = now.strftime("%Y%m%d")

#     payload = {
#         "username": settings.MINTROUTE_USERNAME,
#         "data": {
#             "ean": variant.ean,
#             "location": "UK",
#             "terminal_id": "WEB001",
#             "order_id": f"ORD-{order.id}-{uuid.uuid4().hex[:6]}",
#             "request_type": "purchase",
#             "response_type": "short"
#         }
#     }

#     flat_data = flatten_data(payload)

#     signature = generate_signature(
#         "POST",
#         flat_data,
#         settings.MINTROUTE_SECRET_KEY,
#         signature_time
#     )

#     headers = {
#         "Accept": "application/json",
#         "Content-Type": "application/json",
#         "Authorization": f'algorithm="hmac-sha256", credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}", signature="{signature}"',
#         "X-Mint-Date": header_time
#     }

#     try:
#         response = requests.post(BASE_URL, json=payload, headers=headers, timeout=60)

#         print("STATUS CODE:", response.status_code)
#         print("RAW RESPONSE:", response.text)

#         try:
#             data = response.json()
#         except Exception as e:
#             return {
#                 "status": False,
#                 "error": "Invalid JSON from provider",
#                 "status_code": response.status_code,
#                 "raw_response": response.text
#             }
#         # data = response.json()
#     except Exception as e:
#         return {"status": False, "error": str(e)}

#     if not data.get("status"):
#         return {
#             "status": False,
#             "error": data.get("error"),
#             "code": data.get("error_code")
#         }

#     return {
#         "status": True,
#         "data": data.get("data")
#     }


# import requests
# from datetime import datetime
# from django.conf import settings
# import logging

# logger = logging.getLogger(__name__)

# # logger.error("FUNCTION CALLED")
# # logger.error(f"PAYLOAD: {payload}")


# DENOM_URL = "https://sandbox.mintroute.com/voucher/v2/api/denomination"


# def get_denominations(brand_id):
#     logger.error("FUNCTION CALLED")

#     now = datetime.now(timezone.utc)

#     signature_time = now.strftime("%Y%m%dT%H%M")
#     header_time = now.strftime("%Y%m%dT%H%M%SZ")
#     date_only = now.strftime("%Y%m%d")

#     payload = {
#         "username": settings.MINTROUTE_USERNAME,
#         "data": {
#             "brand_id": str(brand_id)
#         }
#     }
#     logger.error("PAYLOAD: %s", payload)

#     flat_data = flatten_data(payload)

#     signature = generate_signature(
#         "POST",
#         flat_data,
#         settings.MINTROUTE_SECRET_KEY,
#         signature_time
#     )

#     headers = {
#     "Accept": "application/json",
#     "Content-Type": "application/json",  # ← IMPORTANT
#     "Authorization": f'algorithm="hmac-sha256", credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}", signature="{signature}"',
#     "X-Mint-Date": header_time
# }

#     logger.error("PAYLOAD: %s", payload)
#     logger.error("FLAT DATA: %s", flat_data)
#     logger.error("SIGNATURE: %s", signature)
#     logger.error("X-MINT-DATE: %s", header_time)

#     response = requests.post(DENOM_URL, json=payload, headers=headers)

#     logger.error("RAW DENOM RESPONSE: %s", response.text)

#     data = response.json()

#     if str(data.get("status")).lower() != "true":
#         return {"status": False, "error": data.get("error")}

#     return {
#         "status": True,
#         "data": data.get("data")
#     }


import hmac
import hashlib
import base64
import urllib.parse

def build_encoded_string(data_dict):
    parts = []

    for key, value in data_dict.items():
        encoded_key = urllib.parse.quote(str(key), safe='')
        encoded_value = urllib.parse.quote(str(value), safe='')

        parts.append(f"{encoded_key}={encoded_value}")

    return "&".join(parts)

def generate_signature(http_method, data_dict, secret_key, timestamp):
    # encoded_data = urllib.parse.urlencode(data_dict, doseq=True)
    encoded_data = urllib.parse.urlencode(
        data_dict,
        doseq=True,
        quote_via=urllib.parse.quote_plus   # 🔥 IMPORTANT
    )

    # 🚨 NO NEWLINES
    string_to_sign = f"{http_method}{encoded_data}{timestamp}"

    signature = base64.b64encode(
        hmac.new(
            secret_key.encode(),              # ✅ FIX
            string_to_sign.encode(),          # ✅ FIX
            hashlib.sha256
        ).digest()
    ).decode()                                # ✅ FIX

    return signature


def flatten_data(payload):
    flat = {}

    # Order MUST match payload exactly
    flat["username"] = payload["username"]

    for key, value in payload["data"].items():
        flat[f"data[{key}]"] = value

    return flat


import requests
from datetime import datetime, timezone
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

DENOM_URL = "https://sandbox.mintroute.com/voucher/v2/api/denomination"


def get_denominations(brand_id):
    now = datetime.now(timezone.utc)

    signature_time = now.strftime("%Y%m%dT%H%M")
    header_time = now.strftime("%Y%m%dT%H%M%SZ")
    date_only = now.strftime("%Y%m%d")

    payload = {
        "username": settings.MINTROUTE_USERNAME,
        "data": {
            "brand_id": str(brand_id),
            "location": "UK",          # 🔥 ADD THIS
            "terminal_id": "WEB001"    # 🔥 ADD THIS
        }
    }

    # flat_data = flatten_data(payload)

    # signature = generate_signature(
    #     "POST",
    #     flat_data,
    #     settings.MINTROUTE_SECRET_KEY,
    #     signature_time
    # )

    # headers = {
    #     "Accept": "application/json",
    #     "Content-Type": "application/json",
    #     "Authorization": f'algorithm="hmac-sha256",credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}",signature="{signature}"',
    #     "X-Mint-Date": header_time
    # }

    flat_data = flatten_data(payload)

    signature = generate_signature(
        "POST",
        flat_data,
        settings.MINTROUTE_SECRET_KEY,
        signature_time
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f'algorithm="hmac-sha256", credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}", signature="{signature}"',
        "X-Mint-Date": header_time
    }

    # DEBUG
    logger.error("STRING TO SIGN DATA: %s", flat_data)
    logger.error("SIGNATURE: %s", signature)

    # response = requests.post(DENOM_URL, json=payload, headers=headers)
    # encoded_data = urllib.parse.urlencode(flat_data)

    # response = requests.post(
    #     DENOM_URL,
    #     data=encoded_data,   # 🔥 NOT json=
    #     headers=headers
    # )

    response = requests.post(DENOM_URL, json=payload, headers=headers, timeout=60)


    logger.error("RAW RESPONSE: %s", response.text)

    try:
        data = response.json()
    except:
        return {"status": False, "error": "Invalid JSON response"}

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


import uuid

BASE_URL = "https://sandbox.mintroute.com/voucher/v2/api/voucher"


def purchase_voucher(variant, order):
    now = datetime.now(timezone.utc)

    signature_time = now.strftime("%Y%m%dT%H%M")
    header_time = now.strftime("%Y%m%dT%H%M%SZ")
    date_only = now.strftime("%Y%m%d")

    payload = {
        "username": settings.MINTROUTE_USERNAME,
        "data": {
            "ean": "2345678918765",  # ✅ FROM YOUR TABLE
            "location": "UK",
            "terminal_id": "WEB001",
            "order_id": "ORD-TEST-12345",
            "request_type": "purchase",
            "response_type": "short"
        }
    }
    flat_data = flatten_data(payload)

    signature = generate_signature(
        "POST",
        flat_data,
        settings.MINTROUTE_SECRET_KEY,
        signature_time
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f'algorithm="hmac-sha256", credential="{settings.MINTROUTE_ACCESS_KEY}/{date_only}", signature="{signature}"',
        "X-Mint-Date": header_time
    }

    response = requests.post(BASE_URL, json=payload, headers=headers, timeout=60)

    print("RAW RESPONSE:", response.text)

    try:
        data = response.json()
    except:
        return {"status": False, "error": "Invalid JSON response"}

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