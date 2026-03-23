# Mintroute Diamond Shop Integration

## Overview

AFC sells Free Fire diamonds to users via the AFC Shop without holding inventory.
When a user purchases a diamond package on the site, the backend:
1. Collects payment from the user at AFC's marked-up price
2. Calls the Mintroute API to purchase the corresponding voucher/top-up at contract price
3. Delivers the voucher pin/code to the user
4. Keeps the margin as profit

This is a **buy-on-demand** model — no pre-purchased stock.

---

## Branch

All work for this feature lives on `shop/mintroute` in both repos.
Do **not** merge to `main`/`master` until fully tested and certified by Mintroute.

---

## Environments

| Environment | Base URL |
|---|---|
| Sandbox | `https://sandbox.mintroute.com/` |
| Production | Provided by Mintroute after sandbox certification |

> **Note:** Sandbox keys and production keys are NOT interchangeable.

---

## Credentials (Store in `.env`, never in code)

```env
# Mintroute API
MINTROUTE_USERNAME=africanff.single
MINTROUTE_ACCESS_KEY=<from_mintroute_onboarding_email>
MINTROUTE_SECRET_KEY=<from_mintroute_onboarding_email>
MINTROUTE_BASE_URL=https://sandbox.mintroute.com  # switch to prod when ready
MINTROUTE_TERMINAL_ID=AFC_WEB_001  # our identifier
```

---

## Authentication

Every API request requires HMAC-SHA256 signing. Steps:

### 1. Build the String to Sign

```
[HTTP_VERB][URL_ENCODED_REQUEST_DATA][TIMESTAMP_YYYYMMDDThhmm]
```

- **HTTP_VERB**: `POST`
- **URL_ENCODED_REQUEST_DATA**: Convert the JSON body to an associative array, then RFC 1738 URL-encode it (`[` → `%5B`, `]` → `%5D`, spaces → `+`)
- **TIMESTAMP**: UTC, format `YYYYMMDDThhmm` (no seconds, no milliseconds) — used in both the signature and the `Authorization` credential

### 2. Generate Signature

```python
import hmac, hashlib, base64

signature = base64.b64encode(
    hmac.new(
        secret_key.encode(),
        string_to_sign.encode(),
        digestmod=hashlib.sha256
    ).digest()
).decode()
```

### 3. Build Request Headers

| Header | Value |
|---|---|
| `Accept` | `application/json` |
| `Content-Type` | `application/json` |
| `Authorization` | `algorithm="hmac-sha256", credential="<ACCESS_KEY>/<YYYYMMDD>", signature="<generated_signature>"` |
| `X-Mint-Date` | `YYYYMMDDThhmmssZ` (UTC, no milliseconds) |

> **Critical:** The date part of `X-Mint-Date` must match the date part in the `Authorization` credential.

---

## API Endpoints

All endpoints use `POST`.

### Voucher Endpoints (`voucher/v2/api/`)

| Endpoint | Purpose |
|---|---|
| `voucher/v2/api/voucher` | Reserve or purchase a voucher |
| `voucher/v2/api/cancel` | Cancel a purchased voucher (product-dependent) |

### Vendor Endpoints (`vendor/api/`)

| Endpoint | Purpose |
|---|---|
| `vendor/api/order_details` | Fetch details for a specific order by `order_id` |
| `vendor/api/get_all_orders` | Fetch all orders within a date range |
| `vendor/api/get_current_balance` | Check AFC's available Mintroute balance |
| `vendor/api/brand` | List brands by category ID |
| `vendor/api/denomination` | List denominations (packages) by brand ID |

> **Recommended:** Set API request timeouts to **60 seconds**.

---

## Free Fire Product (Sandbox)

From the sandbox catalogue provided by Mintroute:

| Field | Value |
|---|---|
| Category | Games |
| Category ID | `3` |
| Brand | Free Fire |
| Brand ID | `113` |
| Denomination | USD 1 (100 Diamonds) Top-Up |
| Denomination ID | `3899` |
| EAN | `2345678918765` |
| Contract Price (Sandbox) | $0.75 USD |

> **Note:** Production product list will be confirmed by Mintroute account manager. EANs do not change as the portfolio expands.

---

## Purchase Flow (Happy Path)

```
User selects diamond package
        ↓
Frontend → POST /shop/mintroute/products/          (list available packages w/ AFC price)
        ↓
User pays (Paystack / payment gateway)
        ↓
Backend verifies payment success
        ↓
Backend → Mintroute reserve (optional, 5 min hold)  POST voucher/v2/api/voucher { request_type: "reserve" }
        ↓
Backend → Mintroute purchase                         POST voucher/v2/api/voucher { request_type: "purchase" }
        ↓
Mintroute returns { pincode, serial_number, barcode }
        ↓
Backend stores order + voucher in DB
        ↓
Backend delivers pincode to user (email + on-screen)
        ↓
User redeems pin in Free Fire
```

---

## API Request Examples

### Reserve a Voucher

```json
POST voucher/v2/api/voucher
{
  "username": "africanff.single",
  "data": {
    "ean": "2345678918765",
    "terminal_id": "AFC_WEB_001",
    "request_type": "reserve"
  }
}
```

**Success Response:**
```json
{
  "status": true,
  "message": "Vouchers reserved successfully",
  "data": {
    "reservation_id": "1233",
    "brand_name": "Free Fire",
    "denomination_name": "USD 1 (100 Diamonds)"
  }
}
```

### Purchase a Voucher

```json
POST voucher/v2/api/voucher
{
  "username": "africanff.single",
  "data": {
    "ean": "2345678918765",
    "location": "AFC_WEBSITE",
    "terminal_id": "AFC_WEB_001",
    "order_id": "<our_unique_order_id>",
    "request_type": "purchase",
    "response_type": "short"
  }
}
```

**Success Response (short):**
```json
{
  "status": true,
  "message": "Vouchers purchased successfully",
  "data": {
    "voucher": {
      "voucher_value": "1",
      "voucher_currency": "USD",
      "pincode": "XXXXXXXXXXXXXX",
      "serial_number": "XXXXXXXXXXXXXXXXX",
      "barcode": "XXXXXXXXXXXXX"
    }
  }
}
```

> **Important:** `order_id` must be unique per transaction (max 24 chars). Use our internal order ID.

### Check Balance

```json
POST vendor/api/get_current_balance
{
  "username": "africanff.single",
  "data": {
    "currency": "USD"
  }
}
```

### Get Free Fire Denominations

```json
POST vendor/api/denomination
{
  "username": "africanff.single",
  "data": {
    "brand_id": "113"
  }
}
```

---

## Rate Limits (CRITICAL)

| Action | Limit |
|---|---|
| Purchases | Max **10 vouchers per 1-minute** sliding window |
| Reservations | Max **5 reservations per 5-minute** window |

Exceeding these limits results in failed requests or **account blockage**.
Our backend must enforce these limits server-side before calling Mintroute.

---

## Pricing & Markup Logic

- Mintroute charges AFC at **contract price** (e.g., $0.75 per 100 diamonds)
- AFC displays products at a **marked-up price** (to be configured by admin)
- Markup is stored in our DB per denomination — not hardcoded
- Displayed price to users is in **NGN** (converted from USD using live or fixed rate — TBD)

Example:
```
Mintroute contract price: $0.75
AFC markup: 20%
AFC cost in USD: $0.90
AFC price to user (at 1600 NGN/USD): ₦1,440
AFC profit: $0.15 per sale
```

---

## Backend Implementation Plan

### New Django App: `afc_mintroute`

**Models:**
- `MintRouteProduct` — stores EAN, brand, denomination, contract_price, afc_price, is_active
- `MintRouteOrder` — stores afc_order_id, mintroute_order_id, user, product, status, pincode, serial_number, created_at

**Services:**
- `mintroute_auth.py` — HMAC signature generation
- `mintroute_client.py` — HTTP client wrapper for all Mintroute endpoints
- `mintroute_service.py` — business logic (reserve → collect payment → purchase → deliver)

**URL endpoints (AFC backend):**
- `GET  /shop/mintroute/products/` — list AFC's diamond packages with marked-up prices
- `POST /shop/mintroute/orders/` — initiate purchase (triggers payment → Mintroute flow)
- `GET  /shop/mintroute/orders/<id>/` — get order status / voucher details
- `GET  /shop/mintroute/balance/` — admin: check Mintroute wallet balance

---

## Frontend Implementation Plan

### New Pages / Components

- `/shop/diamonds/` — Diamond shop listing page (shows Free Fire packages)
- `/shop/diamonds/[id]/` — Product detail / buy page
- `/orders/` — existing orders page updated to show diamond orders with pincode

---

## Error Handling

Key Mintroute error codes to handle:

| Code | Meaning | Our Response |
|---|---|---|
| 1033 | Duplicate order ID | Generate new order ID, retry once |
| 1039 | Insufficient balance | Alert admin, block purchases |
| 1040 | Rate limit exceeded / account blocked | Queue request, alert admin |
| 1057 | Unable to create order | Retry once, then fail gracefully |
| 1082 | Quantity not available | Show "out of stock" to user |
| 1141 | Signature expired | Regenerate signature (clock drift issue) |
| 1149 | Authentication failed | Check keys, alert admin |
| 1410 | Cannot cancel voucher | Inform user, escalate to Mintroute |

---

## Cancellation Policy

Not all vouchers can be cancelled. Free Fire top-ups may or may not support cancellation — confirm with Mintroute account manager before exposing refund option to users.

A voucher that has already been **redeemed cannot be cancelled**.

---

## Certification Checklist (Sandbox → Production)

Mintroute requires completion of the Integration Certification document before going live:

- [ ] Successfully authenticate with HMAC signature
- [ ] Fetch denomination list for Free Fire
- [ ] Reserve a voucher
- [ ] Purchase a voucher
- [ ] Retrieve order details by order_id
- [ ] Retrieve all orders by date range
- [ ] Check current balance
- [ ] Handle error scenarios (insufficient balance, duplicate order ID, rate limit)
- [ ] Submit certification scenarios to `techsupport@mintroute.com`
- [ ] Receive production credentials

---

## Important Notes

- Sandbox credentials are for **testing only** — no real purchases are made
- IP whitelisting is required for production — share static IP(s) with Mintroute before going live
- Maximum 10 purchases per minute — our Celery task queue must throttle accordingly
- All amounts are in **USD** on the Mintroute side
- Contact: `techsupport@mintroute.com` / Account Manager: Shah Faisal (Mintroute DMCC)
