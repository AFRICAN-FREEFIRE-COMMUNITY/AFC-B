"""
afc_shop/wishlist.py
================================================================================
The shop "save for later" / wishlist (owner request 2026-06-29). A buyer saves
products they are interested in and comes back to them.

Endpoints (Bearer -> validate_token, OWNER-SCOPED), registered under shop/ in
afc_shop/urls.py:
  - toggle_wishlist     : POST { product_id } -> add if absent / remove if present;
                          returns { saved: bool } so a heart button can flip in one call.
  - list_my_wishlist    : GET  -> the caller's saved products (newest first), serialized
                          like a storefront card (image + starting price + stock).
  - my_wishlist_ids     : GET  -> just the set of product ids the caller saved, so the shop
                          grid can render each card's heart in the correct on/off state.

HOW IT CONNECTS
  - Model: Wishlist (afc_shop/models.py), unique (user, product).
  - Auth: afc_auth.validate_token (the Bearer caller).
  - Product card shape reuses _abs_url from afc_shop.views so a saved product serialises like
    a storefront product.
  - Consumed by: frontend lib/wishlist.ts, the heart button on ShopClient.tsx product cards +
    ProductDetailPage.tsx, and the saved-items page.
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from .models import Product, Wishlist
from .views import _abs_url  # same absolute-media-URL helper the storefront uses


def _auth_user(request):
    """Bearer -> validate_token. Returns (user, error_response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _serialize_wishlist_product(request, product):
    """A saved product in storefront-card shape: id, name, image, category label, starting
    price (lowest active variant) and whether anything is in stock."""
    variants = list(product.variants.all())
    prices = [v.price for v in variants]
    starting_price = str(min(prices)) if prices else "0"
    in_stock = any(v.is_in_stock() for v in variants)
    return {
        "id": product.id,
        "name": product.name,
        "image": _abs_url(request, product.image),
        "category": product.category.name if product.category_id else (product.product_type or ""),
        "status": product.status,
        "starting_price": starting_price,
        "in_stock": in_stock,
    }


@api_view(["POST"])
def toggle_wishlist(request):
    """POST shop/wishlist/toggle/ — add the product to the caller's wishlist if not already
    saved, or remove it if it is. Body: { product_id }. Returns { saved: bool } reflecting the
    NEW state, so the heart button can flip without a refetch. Idempotent per final state."""
    user, err = _auth_user(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)
    product = Product.objects.filter(id=product_id).first()
    if not product:
        return Response({"message": "Product not found."}, status=404)

    entry = Wishlist.objects.filter(user=user, product=product).first()
    if entry:
        entry.delete()
        return Response({"saved": False, "message": "Removed from saved items."}, status=200)

    Wishlist.objects.create(user=user, product=product)
    return Response({"saved": True, "message": "Saved for later."}, status=201)


@api_view(["GET"])
def list_my_wishlist(request):
    """GET shop/wishlist/ — the caller's saved products (newest first), each in storefront-card
    shape. Consumed by the saved-items page."""
    user, err = _auth_user(request)
    if err:
        return err

    entries = (
        Wishlist.objects.filter(user=user)
        .select_related("product", "product__category")
        .prefetch_related("product__variants")
        .order_by("-created_at")
    )
    products = [_serialize_wishlist_product(request, e.product) for e in entries]
    return Response({"products": products, "count": len(products)}, status=200)


@api_view(["GET"])
def my_wishlist_ids(request):
    """GET shop/wishlist/ids/ — just the product ids the caller has saved, so the shop grid can
    render each card's heart in the correct on/off state in one cheap call."""
    user, err = _auth_user(request)
    if err:
        return err
    ids = list(Wishlist.objects.filter(user=user).values_list("product_id", flat=True))
    return Response({"product_ids": ids}, status=200)
