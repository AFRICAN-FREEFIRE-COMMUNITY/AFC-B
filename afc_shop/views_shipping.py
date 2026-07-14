"""
afc_shop/views_shipping.py
──────────────────────────
Shipping rate-quote endpoint (provider-agnostic). Owner ask 2026-06-29.

ONE endpoint:
  POST /shop/shipping/quote/
    Auth:    Bearer session token (same as buy_now / get_my_cart).
    Body:    { address, city, state, postcode, items: [{variant_id, quantity}] }
             (the same delivery fields buy_now.required_fields validate, plus the cart).
    Returns: { enabled, couriers: [{courier_id, name, fee, currency, eta, service_code}],
               request_token }
             enabled=False when no shipping provider is configured -> the FE courier
             picker (CartDetails.tsx) renders nothing and checkout is unchanged.

WHY a thin view: all provider logic lives in afc_shop.services.shipping (quote_rates),
so this view only authenticates, shapes the address/items, and serializes the RateQuote.
Mirrors the Bearer-auth pattern of buy_now (afc_shop/views.py). The chosen courier's
fee is later charged via Order.shipping_fee and booked by services.shipping.book_shipment
on payment success (wired alongside the provider client).
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from .services.shipping import quote_rates


@api_view(["POST"])
def shipping_quote(request):
    # Bearer auth, identical to buy_now (afc_shop/views.py:1406).
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session"}, status=401)

    # The delivery address the courier quotes against (same field names buy_now uses).
    address = {
        "address": request.data.get("address", ""),
        "city": request.data.get("city", ""),
        "state": request.data.get("state", ""),
        "postcode": request.data.get("postcode", ""),
        "country": request.data.get("country", "Nigeria"),
    }
    items = request.data.get("items", []) or []

    # quote_rates never raises: a missing provider / courier-API hiccup -> enabled=False,
    # so this endpoint always 200s and the checkout page degrades gracefully.
    quote = quote_rates(address, items)
    return Response(quote.to_dict(), status=200)
