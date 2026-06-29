from decimal import Decimal
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.views import require_admin, validate_token
from afc_leaderboard import models
from .models import Cart, CartItem, Category, Coupon, Fulfillment, Order, OrderItem, Product, ProductMedia, ProductVariant, Redemption, ShopChangeLog
from afc_auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.db.models import Sum, Count, F
from django.shortcuts import get_object_or_404
from datetime import timedelta
import hmac
import hashlib
import json
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.shortcuts import get_object_or_404
from decimal import Decimal
import uuid
import requests
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
import requests
from decimal import Decimal
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.conf import settings
from django.utils import timezone
from django.db.models import Count
from decimal import Decimal
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.utils import timezone


# Create your views here.

# ─────────────────────────────────────────────────────────────────────────────
# Shop media limits + helpers (shared by the product + media views below).
#
# Server-side caps so a buyer-facing gallery never ingests an oversized file.
# The frontend also limits client-side, but these are the authoritative checks.
# ─────────────────────────────────────────────────────────────────────────────
MAX_IMAGE_BYTES = 5 * 1024 * 1024        # 5 MB per product image
MAX_VIDEO_BYTES = 50 * 1024 * 1024       # 50 MB per product video

# Accepted upload content-type prefixes, keyed by media_type. Used to classify
# and reject uploads in add_product_media.
ALLOWED_IMAGE_PREFIX = "image/"
ALLOWED_VIDEO_PREFIX = "video/"


def _abs_url(request, file_field):
    """
    Build an absolute media URL for an Image/FileField, or None if empty.
    Mirrors the request.build_absolute_uri(...) pattern used in afc_team.views
    so the frontend always receives a fully-qualified URL it can render.
    """
    if not file_field:
        return None
    try:
        return request.build_absolute_uri(file_field.url)
    except Exception:
        return None


def _serialize_media(request, product):
    """
    Serialise a product's media gallery (images + videos) into the array shape
    the frontend ShopClient / ProductDetailPage consume:
        [{ id, url, media_type, ordering }, ...]
    Ordered by `ordering` then id (the model Meta ordering), so the primary
    image (ordering=0) comes first.
    """
    return [
        {
            "id": m.id,
            "url": _abs_url(request, m.file),
            "media_type": m.media_type,
            "ordering": m.ordering,
        }
        for m in product.media.all()
    ]


def _serialize_category(category):
    """
    Serialise a Category into the shape the frontend category tabs / admin list
    consume. Returns None when a product has no structured category yet (legacy
    diamond rows) so the frontend can fall back to the legacy product_type.
    """
    if not category:
        return None
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "is_physical": category.is_physical,
        "is_active": category.is_active,
    }


def get_changes(old_obj, new_data, fields):
    changes = {}

    for field in fields:
        old_val = getattr(old_obj, field, None)
        new_val = new_data.get(field, old_val)

        if str(old_val) != str(new_val):
            changes[field] = {
                "old": old_val,
                "new": new_val
            }

    return changes


@api_view(["POST"])
def add_product(request):
    """
    Create a product + its variants in one request.

    Purpose:  Admin creates any catalogue product (diamonds OR physical goods).
    Auth:     require_admin (Bearer session token, role == "admin").
    Request:  name, product_type (legacy slug string), OR category_id /
              category_slug (preferred), description, is_limited_stock, status,
              optional `image` (multipart file), variants[] (list of dicts).
    Response: 201 { message, product_id, variant_ids }.
    Consumed by: AddProductModal.tsx on /a/shop/inventory.

    GENERALISED: the old hard-coded `product_type in [diamonds|bundle|gun_skin]`
    whitelist is gone. Any non-empty type/category is accepted so the shop can
    sell arbitrary categories created via the Category CRUD. `product_type` is
    kept in sync with the chosen category's slug for backward-compat readers.
    """
    admin, err = require_admin(request)
    if err: return err

    name = request.data.get("name")
    product_type = request.data.get("product_type")
    description = request.data.get("description", "")
    is_limited_stock = str(request.data.get("is_limited_stock", "false")).lower() in ("true", "1", "yes")
    status_val = request.data.get("status", "active")

    # Resolve the structured category (preferred) by id or slug. Falls back to
    # the legacy `product_type` string when no category is supplied (back-compat).
    category = None
    category_id = request.data.get("category_id")
    category_slug = request.data.get("category_slug")
    if category_id:
        category = Category.objects.filter(id=category_id).first()
    elif category_slug:
        category = Category.objects.filter(slug=category_slug).first()

    # Keep product_type in sync with the category slug so every legacy code path
    # (e.g. the frontend `product.type` filter) keeps working unchanged.
    if category:
        product_type = category.slug

    variants = request.data.get("variants", [])  # list of {sku,title,price,diamonds_amount,stock_qty,meta}

    # When the request is multipart (because an image file rides along), nested
    # arrays arrive as a JSON string. Parse it back into a list so both the
    # JSON-body and multipart-body callers reach the same code path.
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except (ValueError, TypeError):
            return Response({"message": "variants must be a valid JSON list."}, status=400)

    # GENERALISED guard: require a name + a non-empty type/category (no whitelist).
    if not name or not product_type:
        return Response({"message": "name and a product_type or category are required."}, status=400)

    if not isinstance(variants, list) or len(variants) == 0:
        return Response({"message": "variants must be a non-empty list."}, status=400)

    # Optional primary image (multipart). Additional images/videos are uploaded
    # separately via add_product_media after the product exists.
    image = request.FILES.get("image")

    product = Product.objects.create(
        name=name,
        description=description,
        product_type=product_type,
        category=category,
        image=image,
        is_limited_stock=is_limited_stock,
        status=status_val
    )

    created_variants = []
    for v in variants:
        sku = v.get("sku")
        price = v.get("price")
        if not sku or price is None:
            return Response({"message": "Each variant needs sku and price."}, status=400)

        pv = ProductVariant.objects.create(
            product=product,
            sku=sku,
            title=v.get("title", ""),
            price=price,
            diamonds_amount=int(v.get("diamonds_amount") or 0),
            meta=v.get("meta") or {},
            stock_qty=int(v.get("stock_qty") or 0),
            is_active=bool(v.get("is_active", True)),
        )
        created_variants.append(pv.id)
        ShopChangeLog.objects.create(
            admin_user=admin,
            action="variant_created",
            product=product,
            variant=pv,
            details={
                "sku": pv.sku,
                "price": str(pv.price),
                "stock_qty": pv.stock_qty
            }
        )

    return Response({
        "message": "Product created.",
        "product_id": product.id,
        "variant_ids": created_variants
    }, status=201)


@api_view(["GET"])
def view_all_products(request):
    admin, err = require_admin(request)
    if err: return err

    # prefetch media + category alongside variants so the gallery and category
    # badge render without N+1 queries.
    # Exclude "archived" products: archive is the soft-deleted state used by
    # delete_product when a product has order history and cannot be hard-deleted, so a
    # "deleted" product must not reappear in the admin catalog (owner request 2026-06-09).
    # "active" + "inactive" (hidden-but-kept, via deactivate_product) still show.
    qs = (
        Product.objects.exclude(status="archived")
        .order_by("-created_at")
        .select_related("category", "vendor", "approved_by")
        .prefetch_related("variants", "media")
    )

    data = []
    for p in qs:
        data.append({
            "id": p.id,
            "name": p.name,
            "type": p.product_type,           # legacy slug string (back-compat)
            "category": _serialize_category(p.category),  # structured category or None
            "description": p.description,
            "status": p.status,
            "is_limited_stock": p.is_limited_stock,
            "image": _abs_url(request, p.image),          # primary card thumbnail
            "media": _serialize_media(request, p),        # full image+video gallery
            # ── Marketplace ownership + approval (owner request 2026-06-09) ──
            # The admin inventory list previously showed only `status` (active/inactive),
            # so a VENDOR product still awaiting approval read as "active" with no owner.
            # Surface who owns it + its approval lifecycle so an admin can tell a pending
            # vendor submission apart from a live first-party product. vendor_id is NULL for
            # first-party AFC stock (diamonds etc.); approval_status defaults to "approved"
            # for those, so they read as approved/live as before.
            "vendor_id": p.vendor_id,
            "vendor_name": p.vendor.display_name if p.vendor_id else None,
            "approval_status": p.approval_status,
            "submitted_at": p.submitted_at,
            "approved_by": p.approved_by.username if p.approved_by_id else None,
            "approved_at": p.approved_at,
            "rejection_reason": p.rejection_reason,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "variants": [{
                "id": v.id,
                "sku": v.sku,
                "title": v.title,
                "price": str(v.price),
                "diamonds_amount": v.diamonds_amount,
                "stock_qty": v.stock_qty,
                "is_active": v.is_active,
                "in_stock": v.is_in_stock(),
                "ean": v.ean,
                "created_at": v.created_at,
                "updated_at": v.updated_at,
            } for v in p.variants.all()]
        })
    return Response({"products": data}, status=200)


@api_view(["GET"])
def view_active_products(request):
    """
    PUBLIC storefront product list -- only ACTIVE products, no auth required.

    Purpose:  Drives the user-facing shop (/shop). The admin list `view_all_products`
              is `require_admin` and returns every status, so the storefront could only
              load for admins (regular players got 403, anonymous 401). This is the public
              counterpart: same posture as view_active_categories / view_product_details.
    Auth:     public (no token).
    Response: 200 { products: [...] }  -- identical item shape to view_all_products so the
              frontend mapping is unchanged; only sellable products are returned.
    Consumed by: ShopClient.tsx (app/(user)/shop/_components/ShopClient.tsx).

    MARKETPLACE GATE (Phase B1): an UNAPPROVED vendor product must never reach a
    buyer. So the storefront returns only:
        status == "active"  AND  (vendor IS NULL OR approval_status == "approved")
    - vendor IS NULL    -> first-party AFC stock (diamonds, existing goods). These
                           are unaffected: Product.approval_status defaults to
                           "approved", but we allow them through on the NULL-vendor
                           branch regardless, so legacy rows are never filtered out.
    - approval_status="approved" -> the only state in which a VENDOR product is live.
    A vendor product in draft / submitted / rejected is hidden here even if an admin
    (or the vendor) set its status to "active". The gate is enforced in the DB query
    (Q below) so it cannot be bypassed by the serialiser.
    """
    from django.db.models import Q
    qs = (
        Product.objects.filter(status="active")
        .filter(Q(vendor__isnull=True) | Q(approval_status="approved"))
        .order_by("-created_at")
        .select_related("category")
        .prefetch_related("variants", "media")
    )

    data = []
    for p in qs:
        data.append({
            "id": p.id,
            "name": p.name,
            "type": p.product_type,
            "category": _serialize_category(p.category),
            "description": p.description,
            "status": p.status,
            "is_limited_stock": p.is_limited_stock,
            "image": _abs_url(request, p.image),
            "media": _serialize_media(request, p),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            # storefront only needs sellable variants; an inactive variant is hidden here.
            "variants": [{
                "id": v.id,
                "sku": v.sku,
                "title": v.title,
                "price": str(v.price),
                "diamonds_amount": v.diamonds_amount,
                "stock_qty": v.stock_qty,
                "is_active": v.is_active,
                "in_stock": v.is_in_stock(),
                "ean": v.ean,
                "created_at": v.created_at,
                "updated_at": v.updated_at,
            } for v in p.variants.all() if v.is_active]
        })
    return Response({"products": data}, status=200)


@api_view(["POST"])
def edit_product(request):
    """
    Update a product's fields, its category, optional image, and (optionally)
    its variants.

    Purpose:  Admin edits any catalogue product.
    Auth:     require_admin.
    Request:  product_id (required); any of name / description / product_type /
              status / is_limited_stock; optional category_id|category_slug;
              optional `image` (multipart); optional variants[] for in-place
              variant edits.
    Response: 200 { message }.
    Consumed by: /a/shop/inventory/[id]/page.tsx edit form.
    """
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    old_data = {
        "name": product.name,
        "description": product.description,
        "product_type": product.product_type,
        "status": product.status,
        "is_limited_stock": product.is_limited_stock,
    }

    # plain text product fields (skip is_limited_stock — coerced below)
    for field in ["name", "description", "product_type", "status"]:
        if field in request.data:
            setattr(product, field, request.data.get(field))

    # is_limited_stock may arrive as a "true"/"false" string under multipart, so
    # coerce it to a real bool rather than assigning the raw string.
    if "is_limited_stock" in request.data:
        product.is_limited_stock = str(request.data.get("is_limited_stock")).lower() in ("true", "1", "yes")

    # Re-point the structured category if supplied, and keep product_type in sync
    # with its slug so legacy readers (e.g. the frontend type filter) stay correct.
    category_id = request.data.get("category_id")
    category_slug = request.data.get("category_slug")
    if category_id or category_slug:
        new_category = None
        if category_id:
            new_category = Category.objects.filter(id=category_id).first()
        elif category_slug:
            new_category = Category.objects.filter(slug=category_slug).first()
        if new_category:
            product.category = new_category
            product.product_type = new_category.slug

    # Replace the primary image only when a new file is actually uploaded.
    new_image = request.FILES.get("image")
    if new_image:
        product.image = new_image

    product.save()

    new_data = request.data

    changes = get_changes(product, new_data, old_data.keys())

    if changes:
        ShopChangeLog.objects.create(
            admin_user=admin,
            action="product_updated",
            product=product,
            details=changes
        )

    # variant updates (optional)
    variants = request.data.get("variants")  # list of {id, ...fields}
    # Parse a JSON string back to a list when the request came in as multipart.
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except (ValueError, TypeError):
            return Response({"message": "variants must be a valid JSON list."}, status=400)
    if variants is not None:
        if not isinstance(variants, list):
            return Response({"message": "variants must be a list."}, status=400)

        for v in variants:
            vid = v.get("id")
            if not vid:
                continue
            pv = ProductVariant.objects.filter(id=vid, product=product).first()
            if not pv:
                continue

            old_variant = {
                "sku": pv.sku,
                "title": pv.title,
                "price": pv.price,
                "diamonds_amount": pv.diamonds_amount,
                "stock_qty": pv.stock_qty,
                "is_active": pv.is_active,
                "ean": pv.ean,
            }

            for f in ["sku", "title", "price", "diamonds_amount", "stock_qty", "is_active", "meta", "ean"]:
                if f in v:
                    setattr(pv, f, v.get(f))
            pv.save()
            
            changes = get_changes(pv, v, old_variant.keys())

            if changes:
                ShopChangeLog.objects.create(
                    admin_user=admin,
                    action="variant_updated",
                    product=product,
                    variant=pv,
                    details=changes
                )

    return Response({"message": "Product updated."}, status=200)


@api_view(["POST"])
def add_product_variant(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    sku = request.data.get("sku")
    price = request.data.get("price")
    if not sku or price is None:
        return Response({"message": "sku and price are required."}, status=400)

    pv = ProductVariant.objects.create(
        product=product,
        sku=sku,
        title=request.data.get("title", ""),
        price=price,
        diamonds_amount=int(request.data.get("diamonds_amount") or 0),
        meta=request.data.get("meta") or {},
        stock_qty=int(request.data.get("stock_qty") or 0),
        is_active=bool(request.data.get("is_active", True)),
        ean=request.data.get("ean"),
    )

    return Response({
        "message": "Variant added to product.",
        "variant_id": pv.id
    }, status=201)


@api_view(["POST"])
def delete_product_variant(request):
    admin, err = require_admin(request)
    if err: 
        return err
    variant_id = request.data.get("variant_id")
    if not variant_id:
        return Response({"message": "variant_id is required."}, status=400)
    variant = get_object_or_404(ProductVariant, id=variant_id)

    old_data = {
        "sku": variant.sku,
        "price": str(variant.price),
        "stock_qty": variant.stock_qty
    }

    variant.delete()

    ShopChangeLog.objects.create(
        admin_user=admin,
        action="variant_deleted",
        details=old_data
    )
    return Response({"message": "Product variant deleted."}, status=200)


@api_view(["POST"])
def delete_product(request):
    """
    Delete a catalog product.

    Behaviour (owner request 2026-06-09: "delete should actually delete, not just soft
    delete"). A delete now HARD-deletes the product (its row, and via the CASCADE on
    ProductVariant.product / ProductMedia.product its variants and media) whenever that is
    safe. The ONLY thing that blocks a real delete is order history: OrderItem.variant is a
    PROTECT FK (see afc_shop/models.py OrderItem.variant), so a product whose variant has
    ever been ordered cannot be removed without destroying that order record. In that one
    case we fall back to archiving (status="archived") and tell the admin why. Archived
    products are also excluded from the admin catalog list (view_all_products), so a
    "deleted" product disappears from the catalog either way.

    Request:  { product_id }
    Response: 200 { message, deleted: bool }  (deleted=false means it was archived because
              it has order history rather than truly removed)
    Auth:     require_admin (shop_admin / head_admin).
    Consumed by: the admin shop catalog delete action (app/(a)/a/shop, ProductsTab delete
    button -> POST /shop/delete-product/). The FE shows res.data.message and refetches the
    list, so a hard-deleted or archived product both vanish from the admin catalog.
    """
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    product_name = product.name

    # Try a REAL delete first. ProductVariant.product + ProductMedia.product are CASCADE, so
    # deleting the product removes its variants and media too. But OrderItem.variant is
    # PROTECT, so if any variant was ever ordered Django raises ProtectedError and nothing
    # is deleted -- we then archive instead to preserve that order history.
    from django.db.models.deletion import ProtectedError
    try:
        product.delete()
        ShopChangeLog.objects.create(
            admin_user=admin,
            action="product_deleted",
            details={"product_id": product_id, "name": product_name},
        )
        return Response({"message": "Product deleted.", "deleted": True}, status=200)
    except ProtectedError:
        # Referenced by past orders: keep the row (so those orders stay intact) but archive
        # it so it leaves the catalog. This is the only case delete cannot be a true delete.
        product.status = "archived"
        product.save(update_fields=["status"])
        ShopChangeLog.objects.create(
            admin_user=admin,
            action="product_archived",
            details={"product_id": product_id, "name": product_name,
                     "reason": "has order history (a variant is referenced by past orders)"},
        )
        return Response({
            "message": ("This product has order history, so it was archived instead of "
                        "deleted to keep those order records. It no longer shows in the catalog."),
            "deleted": False,
        }, status=200)


@api_view(["POST"])
def deactivate_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    old_status = product.status
    product.status = "inactive"
    product.save(update_fields=["status"])


    ShopChangeLog.objects.create(
        admin_user=admin,
        action="product_updated",
        product=product,
        details={
            "status": {
                "old": old_status,
                "new": "active"
            }
        }
    )

    return Response({"message": "Product deactivated (hidden from customers)."}, status=200)


@api_view(["POST"])
def activate_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    old_status = product.status
    product.status = "active"
    product.save(update_fields=["status"])


    ShopChangeLog.objects.create(
        admin_user=admin,
        action="product_updated",
        product=product,
        details={
            "status": {
                "old": old_status,
                "new": "active"
            }
        }
    )

    return Response({"message": "Product activated."}, status=200)


@api_view(["GET"])
def view_current_stock_status(request):
    admin, err = require_admin(request)
    if err: return err

    qs = ProductVariant.objects.select_related("product").all().order_by("product__name")

    data = []
    for v in qs:
        data.append({
            "product_id": v.product.id,
            "product_name": v.product.name,
            "variant_id": v.id,
            "sku": v.sku,
            "variant_title": v.title,
            "is_limited_stock": v.product.is_limited_stock,
            "stock_qty": v.stock_qty,
            "in_stock": v.is_in_stock(),
            "active": v.is_active and v.product.status == "active",
        })
    return Response({"stock": data}, status=200)


@api_view(["GET"])
def view_all_orders(request):
    admin, err = require_admin(request)
    if err: return err

    # select_related("user", "coupon") so the related rows are fetched in the same
    # query AND so a dangling FK surfaces predictably rather than via a lazy hit
    # mid-serialization.
    qs = (
        Order.objects.all()
        .select_related("user", "coupon")
        .prefetch_related("items__variant__product")
        .order_by("-created_at")[:500]
    )

    data = []
    for o in qs:
        # Guard: o.user / o.coupon are non-deferred FKs, but on prod an order can
        # reference a user/coupon row that was hard-deleted outside the ORM. The
        # `if o.user` truthiness check does NOT catch that: resolving the relation
        # raises User.DoesNotExist (RelatedObjectDoesNotExist), which would 500 the
        # whole list. Resolve each FK defensively so one bad row degrades to null
        # instead of taking down the entire admin orders view.
        try:
            username = o.user.username if o.user_id else None
        except Exception:
            username = None
        try:
            coupon_code = o.coupon.code if o.coupon_id else None
        except Exception:
            coupon_code = None

        data.append({
            "order_id": o.id,
            "user_id": o.user_id,
            "username": username,
            "status": o.status,
            "subtotal": str(o.subtotal),
            "discount_total": str(o.discount_total),
            "total": str(o.total),
            "coupon_code": coupon_code,
            "created_at": o.created_at,
            "items": [{
                "variant_id": it.variant_id,
                "product": it.product_name_snapshot,
                "variant": it.variant_title_snapshot,
                "qty": it.quantity,
                "unit_price": str(it.unit_price),
                "line_total": str(it.line_total),
            } for it in o.items.all()]
        })
    return Response({"orders": data}, status=200)


def _orders_in_range(start_dt, end_dt):
    # select_related the FKs so resolving o.user / o.coupon during serialization
    # does not fire a lazy per-row query (and so a dangling FK surfaces here, not
    # mid-loop). The today/week/month summaries all reuse this.
    return (
        Order.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt)
        .select_related("user", "coupon")
    )


def _serialize_order_summary(o):
    """
    Flat summary row for the today/week/month order lists.

    Resolves the user + coupon FKs DEFENSIVELY: on prod an order can point at a
    user/coupon that was hard-deleted outside the ORM. `o.user.username if o.user
    else None` does NOT protect against that, because resolving the relation
    raises User.DoesNotExist (RelatedObjectDoesNotExist) which bubbles up as an
    unhandled error and 500s the endpoint. Reading via the FK id and catching the
    lookup keeps one bad row from taking down the whole report.
    """
    try:
        username = o.user.username if o.user_id else None
    except Exception:
        username = None
    try:
        coupon_code = o.coupon.code if o.coupon_id else None
    except Exception:
        coupon_code = None

    return {
        "order_id": o.id,
        "user_id": o.user_id,
        "username": username,
        "status": o.status,
        "subtotal": str(o.subtotal),
        "discount_total": str(o.discount_total),
        "total": str(o.total),
        "coupon_code": coupon_code,
        "created_at": o.created_at,
    }


@api_view(["GET"])
def orders_today(request):
    admin, err = require_admin(request)
    if err: return err

    orders = _orders_in_range(
        timezone.now().replace(hour=0, minute=0, second=0, microsecond=0),
        timezone.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    )
    data = [_serialize_order_summary(o) for o in orders]
    return Response({"orders": data}, status=200)


@api_view(["GET"])
def orders_this_week(request):
    admin, err = require_admin(request)
    if err: return err

    orders = _orders_in_range(
        timezone.now() - timedelta(days=timezone.now().weekday()),
        timezone.now()
    )
    data = [_serialize_order_summary(o) for o in orders]
    return Response({"orders": data}, status=200)


@api_view(["GET"])
def orders_this_month(request):
    admin, err = require_admin(request)
    if err: return err

    orders = _orders_in_range(
        timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        timezone.now()
    )
    data = [_serialize_order_summary(o) for o in orders]
    return Response({"orders": data}, status=200)


@api_view(["GET"])
def view_all_coupons(request):
    admin, err = require_admin(request)
    if err: return err

    qs = Coupon.objects.all().order_by("-id")
    data = [{
        "id": c.id,
        "code": c.code,
        "discount_type": c.discount_type,
        "discount_value": str(c.discount_value),
        "active": c.is_active,
        "start_at": c.start_at,
        "end_at": c.end_at,
        "min_order_amount": str(c.min_order_amount),
        "max_uses": c.max_uses,
        "used_count": c.used_count,
        "is_valid_now": c.is_valid_now(),
    } for c in qs]

    return Response({"coupons": data}, status=200)


@api_view(["POST"])
def create_coupon(request):
    admin, err = require_admin(request)
    if err: return err

    code = (request.data.get("code") or "").strip().upper()
    discount_type = request.data.get("discount_type")
    discount_value = request.data.get("discount_value")

    if not code or discount_type not in ["percent", "fixed"] or discount_value is None:
        return Response({"message": "code, discount_type, discount_value are required."}, status=400)

    #check if similar code has ever been used before, if yes and it's inactive, we can allow reuse but if it's active we should reject
    existing = Coupon.objects.filter(code=code).first()
    if existing:
        return Response({"message": "A coupon with this code already exists."}, status=400)
    

    c = Coupon.objects.create(
        code=code,
        discount_type=discount_type,
        discount_value=Decimal(str(discount_value)),
        is_active=bool(request.data.get("active", True)),
        min_order_amount=Decimal(str(request.data.get("min_order_amount", "0"))),
        max_uses=request.data.get("max_uses") or None,
        start_at=request.data.get("start_at") or None,  # if you're sending ISO string, parse it properly
        end_at=request.data.get("end_at") or None,
    )

    return Response({"message": "Coupon created.", "coupon_id": c.id}, status=201)


@api_view(["GET"])
def view_product_details(request):
    """
    Public product detail (no auth) used by both the user detail page and the
    admin edit page. Now returns the structured category, the primary image,
    and the full media gallery (images + videos) alongside the variants.
    Consumed by: ProductDetailPage.tsx (user) + /a/shop/inventory/[id] (admin).
    """
    # admin, err = require_admin(request)
    # if err: return err
    product_id = request.GET.get("product_id")

    product = get_object_or_404(
        Product.objects.select_related("category").prefetch_related("variants", "media"),
        id=product_id,
    )

    data = {
        "id": product.id,
        "name": product.name,
        "type": product.product_type,                  # legacy slug string
        "category": _serialize_category(product.category),
        "description": product.description,
        "status": product.status,
        "is_limited_stock": product.is_limited_stock,
        "image": _abs_url(request, product.image),     # primary image
        "media": _serialize_media(request, product),   # image+video gallery
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "variants": [{
            "id": v.id,
            "sku": v.sku,
            "title": v.title,
            "price": str(v.price),
            "diamonds_amount": v.diamonds_amount,
            "stock_qty": v.stock_qty,
            "is_active": v.is_active,
            "in_stock": v.is_in_stock(),
            "meta": v.meta,
        } for v in product.variants.all()]
    }
    return Response({"product": data}, status=200)


@api_view(["POST"])
def add_to_cart(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # ---------------- INPUT ----------------
    variant_id = request.data.get("variant_id")
    quantity = request.data.get("quantity", 1)
    coupon_code = request.data.get("coupon_code", "").strip().upper()

    if not variant_id:
        return Response({"message": "variant_id is required."}, status=400)

    try:
        quantity = int(quantity)
    except:
        return Response({"message": "quantity must be a number."}, status=400)

    if quantity <= 0:
        return Response({"message": "quantity must be at least 1."}, status=400)

    variant = get_object_or_404(ProductVariant, id=variant_id)

    # ---------------- VALIDATION ----------------
    if not variant.is_active:
        return Response({"message": "This product variant is not available."}, status=400)

    if variant.product.status != "active":
        return Response({"message": "This product is not available."}, status=400)

    if not variant.is_in_stock():
        return Response({"message": "This item is out of stock."}, status=400)

    # ---------------- STOCK CHECK ----------------
    if variant.product.is_limited_stock:
        if quantity > variant.stock_qty:
            return Response({
                "message": f"Only {variant.stock_qty} left in stock."
            }, status=400)
        
    
    # Coupon validation (if provided)
    coupon = None
    if coupon_code:
        coupon = Coupon.objects.filter(code=coupon_code).first()
        if not coupon:
            return Response({"message": "Invalid coupon code."}, status=400)
        if not coupon.is_valid_now():
            return Response({"message": "This coupon is not valid at the moment."}, status=400)

    # ---------------- CREATE / UPDATE CART ----------------
    with transaction.atomic():

        cart, _ = Cart.objects.get_or_create(user=user)

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            variant=variant,
            defaults={"quantity": quantity},
            coupon=coupon
        )

        if not created:
            new_qty = cart_item.quantity + quantity

            if variant.product.is_limited_stock and new_qty > variant.stock_qty:
                return Response({
                    "message": f"Cannot add more. Only {variant.stock_qty} available."
                }, status=400)

            cart_item.quantity = new_qty
            cart_item.coupon = coupon
            cart_item.save(update_fields=["quantity", "coupon"])

    # ---------------- RESPONSE SUMMARY ----------------
    items = cart.items.select_related("variant__product")

    subtotal = Decimal("0.00")
    response_items = []

    for item in items:
        line_total = item.variant.price * item.quantity
        subtotal += line_total

        response_items.append({
            "cart_item_id": item.id,
            "variant_id": str(item.variant.id),
            "product_name": item.variant.product.name,
            "variant_title": item.variant.title,
            "unit_price": str(item.variant.price),
            "quantity": item.quantity,
            "line_total": str(line_total),
            "coupon": item.coupon.code if item.coupon else None,
        })

    return Response({
        "message": "Item added to cart.",
        "cart": {
            "cart_id": cart.id,
            "items": response_items,
            "subtotal": str(subtotal),
            "total_items": items.count(),
        }
    }, status=200)


@api_view(["GET"])
def get_my_cart(request):
    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart = Cart.objects.filter(user=user).first()

    if not cart:
        return Response({
            "cart": {
                "cart_id": None,
                "items": [],
                "subtotal": "0.00",
                "total_items": 0
            }
        }, status=200)

    items = cart.items.select_related("variant__product")

    subtotal = Decimal("0.00")
    response_items = []

    for item in items:
        line_total = item.variant.price * item.quantity
        subtotal += line_total

        response_items.append({
            "cart_item_id": item.id,
            "variant_id": str(item.variant.id),
            "product_name": item.variant.product.name,
            "variant_title": item.variant.title,
            "unit_price": str(item.variant.price),
            "quantity": item.quantity,
            "line_total": str(line_total),
            "in_stock": item.variant.is_in_stock(),
            "coupon": item.coupon.code if item.coupon else None,
        })

    return Response({
        "cart": {
            "cart_id": cart.id,
            "items": response_items,
            "subtotal": str(subtotal),
            "total_items": items.count(),
        }
    }, status=200)


@api_view(["POST"])
def remove_from_cart(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart_item_id = request.data.get("cart_item_id")
    if not cart_item_id:
        return Response({"message": "cart_item_id is required."}, status=400)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart not found."}, status=404)

    cart_item = CartItem.objects.filter(id=cart_item_id, cart=cart).first()
    if not cart_item:
        return Response({"message": "Item not found in your cart."}, status=404)

    cart_item.delete()

    return Response({"message": "Item removed from cart."}, status=200)


@api_view(["POST"])
def update_cart_item_quantity(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart_item_id = request.data.get("cart_item_id")
    quantity = request.data.get("quantity")

    if not cart_item_id or quantity is None:
        return Response({"message": "cart_item_id and quantity are required."}, status=400)

    try:
        quantity = int(quantity)
    except:
        return Response({"message": "quantity must be a number."}, status=400)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart not found."}, status=404)

    cart_item = CartItem.objects.select_related("variant__product").filter(
        id=cart_item_id,
        cart=cart
    ).first()

    if not cart_item:
        return Response({"message": "Item not found in your cart."}, status=404)

    variant = cart_item.variant

    if quantity <= 0:
        cart_item.delete()
        return Response({"message": "Item removed from cart."}, status=200)

    if variant.product.is_limited_stock and quantity > variant.stock_qty:
        return Response({
            "message": f"Only {variant.stock_qty} available in stock."
        }, status=400)

    cart_item.quantity = quantity
    cart_item.save(update_fields=["quantity"])

    return Response({"message": "Cart updated successfully."}, status=200)


@api_view(["POST"])
def clear_cart(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart already empty."}, status=200)

    cart.items.all().delete()

    return Response({"message": "Cart cleared successfully."}, status=200)



TAX_RATE = Decimal("0.075")  # 7.5%


from decimal import Decimal, ROUND_HALF_UP

# TAX_RATE = Decimal("0.10")  # 10%


# @api_view(["POST"])
# def buy_now(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid token"}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid session"}, status=401)

#     items = request.data.get("items", [])

#     if not isinstance(items, list) or not items:
#         return Response({"message": "Items are required."}, status=400)

#     # -------- Customer Info --------
#     required_fields = [
#         "first_name", "last_name", "email",
#         "phone_number", "address", "city",
#         "state", "postcode"
#     ]

#     for field in required_fields:
#         if not request.data.get(field):
#             return Response({"message": f"{field} is required."}, status=400)

#     subtotal = Decimal("0.00")
#     total_tax = Decimal("0.00")
#     total_discount = Decimal("0.00")

#     order_items_to_create = []

#     # -------- Validate & Calculate --------
#     for item in items:
#         variant_id = item.get("variant_id")
#         quantity = int(item.get("quantity", 1))
#         coupon_code = item.get("coupon_code")

#         if quantity <= 0:
#             return Response({"message": "Invalid quantity."}, status=400)

#         try:
#             variant = ProductVariant.objects.select_related("product").get(
#                 id=variant_id,
#                 is_active=True
#             )
#         except ProductVariant.DoesNotExist:
#             return Response({"message": f"Product {variant_id} not found."}, status=404)

#         if not variant.is_in_stock():
#             return Response({"message": f"{variant.title} is out of stock."}, status=400)

#         if variant.product.is_limited_stock and variant.stock_qty < quantity:
#             return Response({"message": f"Insufficient stock for {variant.title}."}, status=400)

#         unit_price = variant.price
#         base_price = (unit_price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

#         # -------- TAX (Before Discount) --------
#         tax_amount = (base_price * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

#         discount_amount = Decimal("0.00")
#         applied_coupon = None

#         # -------- COUPON --------
#         if coupon_code:
#             try:
#                 coupon = Coupon.objects.get(code=coupon_code, is_active=True)
#             except Coupon.DoesNotExist:
#                 return Response({"message": f"Coupon {coupon_code} invalid."}, status=400)

#             if not coupon.is_valid_now():
#                 return Response({"message": f"Coupon {coupon_code} expired or invalid."}, status=400)

#             if base_price < coupon.min_order_amount:
#                 return Response({"message": f"Coupon {coupon_code} minimum not reached."}, status=400)

#             if coupon.discount_type == "percent":
#                 discount_amount = (base_price * (coupon.discount_value / Decimal("100"))).quantize(Decimal("0.01"))
#             else:
#                 discount_amount = coupon.discount_value

#             # Prevent over-discount
#             if discount_amount > base_price:
#                 discount_amount = base_price

#             applied_coupon = coupon

#         line_total = base_price + tax_amount - discount_amount

#         subtotal += base_price
#         total_tax += tax_amount
#         total_discount += discount_amount

#         order_items_to_create.append({
#             "variant": variant,
#             "quantity": quantity,
#             "unit_price": unit_price,
#             "line_total": line_total,
#             "coupon": applied_coupon,
#             "discount": discount_amount
#         })

#     grand_total = subtotal + total_tax - total_discount

#     # -------- Create Order --------
#     with transaction.atomic():

#         order = Order.objects.create(
#             user=user,
#             subtotal=subtotal,
#             discount_total=total_discount,
#             total=grand_total,
#             status="pending",
#             # coupon_code=applied_coupon.code if applied_coupon else None,  # item-level coupons
#             first_name=request.data.get("first_name"),
#             last_name=request.data.get("last_name"),
#             email=request.data.get("email"),
#             phone_number=request.data.get("phone_number"),
#             address=request.data.get("address"),
#             city=request.data.get("city"),
#             state=request.data.get("state"),
#             postcode=request.data.get("postcode"),
#         )

#         order_items = []
#         for item in order_items_to_create:
#             order_items.append(
#                 OrderItem(
#                     order=order,
#                     variant=item["variant"],
#                     quantity=item["quantity"],
#                     unit_price=item["unit_price"],
#                     line_total=item["line_total"],
#                     product_name_snapshot=item["variant"].product.name,
#                     variant_title_snapshot=item["variant"].title or item["variant"].sku,
#                     coupon_code=item["coupon"].code if item["coupon"] else None
#                 )
#             )

#         OrderItem.objects.bulk_create(order_items)

#     # -------- Paystack Init --------
#     reference = f"PS_{uuid.uuid4().hex}"
#     order.paystack_reference = reference
#     order.save(update_fields=["paystack_reference"])

#     headers = {
#         "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#         "Content-Type": "application/json",
#     }

#     payload = {
#         "email": request.data.get("email"),
#         "amount": int(grand_total * 100),
#         "reference": reference,
#         "callback_url": settings.PAYSTACK_CALLBACK_URL,
#         "metadata": {
#             "order_id": str(order.id),
#             "user_id": user.user_id,
#         }
#     }

#     response = requests.post(
#         "https://api.paystack.co/transaction/initialize",
#         headers=headers,
#         json=payload
#     )

#     data = response.json()

#     if not data.get("status"):
#         order.status = "failed"
#         order.save(update_fields=["status"])
#         return Response({"message": "Payment initialization failed."}, status=400)

#     return Response({
#         "authorization_url": data["data"]["authorization_url"],
#         "reference": reference,
#         "order_id": order.id,
#         "subtotal": str(subtotal),
#         "tax": str(total_tax),
#         "discount": str(total_discount),
#         "total": str(grand_total)
#     }, status=200)


@api_view(["POST"])
def buy_now(request):
    auth = request.headers.get("Authorization")

    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session"}, status=401)

    items = request.data.get("items", [])
    if not items:
        return Response({"message": "Items required"}, status=400)

    required_fields = ["first_name","last_name","email","phone_number","address","city","state","postcode"]

    for field in required_fields:
        if not request.data.get(field):
            return Response({"message": f"{field} is required"}, status=400)

    subtotal = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_discount = Decimal("0.00")

    order_items_to_create = []

    for item in items:
        variant = ProductVariant.objects.filter(id=item["variant_id"], is_active=True).first()
        if not variant:
            return Response({"message": "Invalid product"}, status=404)

        quantity = int(item.get("quantity", 1))

        if quantity <= 0:
            return Response({"message": "Invalid quantity"}, status=400)

        if variant.product.is_limited_stock and variant.stock_qty < quantity:
            return Response({"message": "Insufficient stock"}, status=400)

        base_price = (variant.price * quantity).quantize(Decimal("0.01"))
        tax = (base_price * TAX_RATE).quantize(Decimal("0.01"))

        subtotal += base_price
        total_tax += tax

        order_items_to_create.append({
            "variant": variant,
            "quantity": quantity,
            "unit_price": variant.price,
            "line_total": base_price + tax
        })

    # ── Coupon discount (order-level), applied SERVER-SIDE ──────────────────────────
    # The cart checkout (CartDetails.tsx buy-now) attaches the applied coupon to each
    # item as coupon_code (the product page can carry one too). The amount we charge must
    # NEVER trust the client, so we re-validate the coupon here and reduce the order total.
    # Previously this was missing: the discount only showed on the client and the user was
    # still charged the full subtotal+tax via Paystack. On payment success,
    # verify_paystack_payment + paystack_webhook increment this coupon's used_count.
    coupon = None
    coupon_code = ""
    for _it in items:
        _cc = (_it.get("coupon_code") or "").strip()
        if _cc:
            coupon_code = _cc.upper()
            break

    discount = Decimal("0.00")
    if coupon_code:
        coupon = Coupon.objects.filter(code=coupon_code).first()
        if not coupon:
            return Response({"message": "Invalid coupon code."}, status=400)
        if not coupon.is_valid_now():
            return Response({"message": "This coupon is not valid at the moment."}, status=400)
        if subtotal < coupon.min_order_amount:
            return Response(
                {"message": f"This coupon needs a minimum order of {coupon.min_order_amount}."},
                status=400,
            )
        # percent -> share of the pre-tax subtotal; fixed -> flat amount. Cap at subtotal.
        if coupon.discount_type == "percent":
            discount = (subtotal * (coupon.discount_value / Decimal("100"))).quantize(Decimal("0.01"))
        else:
            discount = coupon.discount_value
        discount = min(discount, subtotal)  # never push the order below zero

    grand_total = (subtotal + total_tax - discount).quantize(Decimal("0.01"))
    if grand_total < Decimal("0.00"):
        grand_total = Decimal("0.00")

    with transaction.atomic():
        order = Order.objects.create(
            user=user,
            subtotal=subtotal,
            tax=total_tax,
            discount_total=discount,
            coupon=coupon,
            total=grand_total,
            status="pending",
            first_name=request.data.get("first_name"),
            last_name=request.data.get("last_name"),
            email=request.data.get("email"),
            phone_number=request.data.get("phone_number"),
            address=request.data.get("address"),
            city=request.data.get("city"),
            state=request.data.get("state"),
            postcode=request.data.get("postcode"),
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                variant=i["variant"],
                quantity=i["quantity"],
                unit_price=i["unit_price"],
                line_total=i["line_total"],
                # snapshot the applied coupon code per line (the order-level coupon FK above
                # is the source of truth; this is the historical per-item record).
                coupon_code=(coupon.code if coupon else None),
            )
            for i in order_items_to_create
        ])

    # ── saved delivery info (owner request 2026-06-29) ──
    # If the buyer ticked "save my info" (save_delivery_info) we persist a reusable
    # SavedDeliveryProfile; if they picked an existing saved entry (saved_profile_id) we link
    # to it. Best-effort: a saved-profile hiccup must never fail the order. The link is
    # persisted by the order.save() just below. See afc_shop/delivery.py.
    from afc_shop.delivery import attach_delivery_profile
    attach_delivery_profile(order, user, request.data)

    # PAYSTACK INIT
    reference = f"PS_{uuid.uuid4().hex}"

    order.paystack_reference = reference
    order.save()

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
        json={
            "email": order.email,
            "amount": int(order.total * 100),
            "reference": reference,
            "callback_url": settings.PAYSTACK_CALLBACK_URL,
            "metadata": {"order_id": str(order.id)}
        }
    ).json()

    if not response.get("status"):
        return Response({"message": "Payment init failed"}, status=400)

    return Response({
        "authorization_url": response["data"]["authorization_url"],
        "reference": reference,
        "order_id": order.id
    })


# -------- MINTROUTE V1 TEST ---------


from .services.mintroute import get_brands, get_denominations, purchase_voucher

# @api_view(["POST"])
# def verify_paystack_payment(request):
#     reference = request.data.get("reference")

#     if not reference:
#         return Response({"message": "reference is required."}, status=400)

#     headers = {
#         "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#     }

#     verify_url = f"https://api.paystack.co/transaction/verify/{reference}"
#     response = requests.get(verify_url, headers=headers, timeout=30)
#     paystack_response = response.json()

#     if not paystack_response.get("status"):
#         return Response({"message": "Verification failed."}, status=400)

#     data = paystack_response.get("data", {})

#     if data.get("status") != "success":
#         return Response({"message": "Payment not successful."}, status=400)

#     metadata = data.get("metadata", {})
#     order_id = metadata.get("order_id")

#     order = Order.objects.select_related("user").prefetch_related(
#         "items__variant__product"
#     ).filter(id=order_id).first()

#     if not order:
#         return Response({"message": "Order not found."}, status=404)

#     if order.status == "paid":
#         return Response({"message": "Already verified."}, status=200)

#     expected_amount_kobo = int(order.total * 100)

#     if data.get("amount") != expected_amount_kobo:
#         return Response({"message": "Amount mismatch."}, status=400)

#     # -------- Extract Paystack Info --------
#     transaction_id = str(data.get("id"))
#     payment_channel = data.get("channel")  # card / bank / ussd / transfer
#     paid_at = data.get("paid_at")

#     # Optional: card info
#     authorization = data.get("authorization", {})
#     card_type = authorization.get("card_type")
#     bank = authorization.get("bank")

#     payment_method = payment_channel

#     if payment_channel == "card" and card_type:
#         payment_method = f"{card_type} ({bank})"

#     # -------- Process Order --------
#     with transaction.atomic():

#         # Prevent double processing
#         if order.status == "paid":
#             return Response({"message": "Already processed"}, status=200)

#         transaction_id = str(data.get("id"))
#         payment_channel = data.get("channel")

#         order.status = "paid"
#         order.paystack_transaction_id = transaction_id
#         order.payment_method = payment_channel
#         order.paid_at = timezone.now()
#         order.save(update_fields=[
#             "status",
#             "paystack_transaction_id",
#             "payment_method",
#             "paid_at"
#         ])

#         # ✅ Increment Coupon Usage
#         if order.coupon:
#             order.coupon.used_count += 1
#             order.coupon.save(update_fields=["used_count"])

#             # Create redemption record (avoid duplicates)
#             if not Redemption.objects.filter(
#                 coupon=order.coupon,
#                 redeemed_by=order.user,
#                 redeemed_at__isnull=False,
#                 order_amount=order.total
#             ).exists():

#                 Redemption.objects.create(
#                     coupon=order.coupon,
#                     product_variant=order.items.first().variant,
#                     redeemed_by=order.user,
#                     redeemed_at=timezone.now(),
#                     order_amount=order.total,
#                     savings=order.discount_total
#                 )

#         # Reduce stock
#         for item in order.items.all():
#             variant = item.variant
#             if not variant.ean:
#                 raise Exception("EAN not configured for this product variant")
#             if variant.product.is_limited_stock:
#                 if variant.stock_qty < item.quantity:
#                     order.status = "failed"
#                     order.save(update_fields=["status"])
#                     return Response({"message": "Stock inconsistency"}, status=400)

#                 variant.stock_qty -= item.quantity
#                 variant.save(update_fields=["stock_qty"])

#         # Create fulfillment
#         # for item in order.items.all():
#         #     Fulfillment.objects.create(
#         #         order=order,
#         #         item=item,
#         #         status="queued"
#         #     )

        

#         for item in order.items.all():
#             fulfillment = Fulfillment.objects.create(
#                 order=order,
#                 item=item,
#                 status="processing"
#             )

#             try:
#                 response = purchase_voucher(item.variant, order)

#                 if response.get("status"):
#                     voucher = response["data"]["voucher"]

#                     fulfillment.status = "delivered"
#                     fulfillment.provider_payload = voucher
#                     fulfillment.save()

#                 else:
#                     fulfillment.status = "failed"
#                     fulfillment.notes = response.get("error")
#                     fulfillment.save()

#             except Exception as e:
#                 fulfillment.status = "failed"
#                 fulfillment.notes = str(e)
#                 fulfillment.save()


#     return Response({
#         "message": "Payment verified successfully.",
#         "order_id": order.id,
#         "transaction_id": transaction_id,
#         "payment_method": payment_method,
#         "status": "paid"
#     }, status=200)


@api_view(["POST"])
def verify_paystack_payment(request):
    reference = request.data.get("reference")

    if not reference:
        return Response({"message": "Reference required"}, status=400)

    verify = requests.get(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}
    ).json()

    if not verify.get("status"):
        return Response({"message": "Verification failed"}, status=400)

    data = verify["data"]

    if data["status"] != "success":
        return Response({"message": "Payment not successful"}, status=400)

    order_id = data["metadata"]["order_id"]

    order = Order.objects.prefetch_related("items__variant").filter(id=order_id).first()

    if not order:
        return Response({"message": "Order not found"}, status=404)

    if order.status == "paid":
        return Response({"message": "Already processed"}, status=200)

    with transaction.atomic():

        order.status = "paid"
        order.paystack_transaction_id = str(data["id"])
        order.payment_method = data["channel"]
        order.paid_at = timezone.now()
        order.save()

        # Count the coupon use now that the order is paid. The already-paid guard above
        # makes this run exactly once per order (verify + webhook can both fire).
        if order.coupon_id:
            order.coupon.used_count = (order.coupon.used_count or 0) + 1
            order.coupon.save(update_fields=["used_count"])

        for item in order.items.all():
            for _ in range(item.quantity):  # IMPORTANT FIX

                fulfillment = Fulfillment.objects.create(
                    order=order,
                    item=item,
                    status="processing"
                )

                response = purchase_voucher(item.variant, order)

                if response.get("status"):
                    fulfillment.status = "delivered"
                    fulfillment.provider_payload = response["data"]
                else:
                    fulfillment.status = "failed"
                    fulfillment.notes = f"{response["status"]} {response["error"]} {response["code"]}"

                fulfillment.save()

    # ── Marketplace fulfilment hook (Phase A) ──────────────────────────────────
    # If this order contains a vendor (marketplace) product, start the order
    # fulfilment lifecycle: set state="received", email the buyer, notify the
    # vendor. No-op + idempotent for pure diamond/AFC orders. Lives outside the
    # transaction above (it only reads + emails) and never raises into this path.
    from .fulfilment import notify_order_paid
    notify_order_paid(order)

    return Response({"message": "Payment verified"})




# @api_view(["POST"])
# @csrf_exempt
# def paystack_webhook(request):

#     # -------- Verify Signature --------
#     signature = request.headers.get("x-paystack-signature")
#     body = request.body

#     computed_signature = hmac.new(
#         settings.PAYSTACK_SECRET_KEY.encode(),
#         body,
#         hashlib.sha512
#     ).hexdigest()

#     if signature != computed_signature:
#         return Response({"message": "Invalid signature"}, status=400)

#     payload = json.loads(body)
#     event = payload.get("event")

#     if event != "charge.success":
#         return Response({"message": "Event ignored"}, status=200)

#     data = payload.get("data", {})
#     reference = data.get("reference")
#     metadata = data.get("metadata", {})

#     order_id = metadata.get("order_id")

#     try:
#         order = Order.objects.select_related().prefetch_related("items__variant__product").get(id=order_id)
#     except Order.DoesNotExist:
#         return Response({"message": "Order not found"}, status=404)

#     # Prevent double processing
#     if order.status == "paid":
#         return Response({"message": "Already processed"}, status=200)

#     with transaction.atomic():

#         # Prevent double processing
#         if order.status == "paid":
#             return Response({"message": "Already processed"}, status=200)

#         transaction_id = str(data.get("id"))
#         payment_channel = data.get("channel")

#         order.status = "paid"
#         order.paystack_transaction_id = transaction_id
#         order.payment_method = payment_channel
#         order.paid_at = timezone.now()
#         order.save(update_fields=[
#             "status",
#             "paystack_transaction_id",
#             "payment_method",
#             "paid_at"
#         ])

#         # ✅ Increment Coupon Usage
#         if order.coupon:
#             order.coupon.used_count += 1
#             order.coupon.save(update_fields=["used_count"])

#             # Create redemption record (avoid duplicates)
#             if not Redemption.objects.filter(
#                 coupon=order.coupon,
#                 redeemed_by=order.user,
#                 redeemed_at__isnull=False,
#                 order_amount=order.total
#             ).exists():

#                 Redemption.objects.create(
#                     coupon=order.coupon,
#                     product_variant=order.items.first().variant,
#                     redeemed_by=order.user,
#                     redeemed_at=timezone.now(),
#                     order_amount=order.total,
#                     savings=order.discount_total
#                 )

#         # Reduce stock
#         for item in order.items.all():
#             variant = item.variant
#             if variant.product.is_limited_stock:
#                 if variant.stock_qty < item.quantity:
#                     order.status = "failed"
#                     order.save(update_fields=["status"])
#                     return Response({"message": "Stock inconsistency"}, status=400)

#                 variant.stock_qty -= item.quantity
#                 variant.save(update_fields=["stock_qty"])

#         # Create fulfillment
#         for item in order.items.all():
#             fulfillment = Fulfillment.objects.create(
#                 order=order,
#                 item=item,
#                 status="processing"
#             )

#             try:
#                 response = purchase_voucher(item.variant, order)

#                 if response.get("status"):
#                     voucher = response["data"]["voucher"]

#                     fulfillment.status = "delivered"
#                     fulfillment.provider_payload = voucher
#                     fulfillment.save()

#                 else:
#                     fulfillment.status = "failed"
#                     fulfillment.notes = response.get("error")
#                     fulfillment.save()

#             except Exception as e:
#                 fulfillment.status = "failed"
#                 fulfillment.notes = str(e)
#                 fulfillment.save()

#     return Response({"message": "Payment processed successfully"}, status=200)


@api_view(["POST"])
@csrf_exempt
def paystack_webhook(request):
    # A public webhook receives whatever Paystack (or a probe / bad caller) posts,
    # so it must tolerate a missing signature header, an unset secret key, and an
    # empty or malformed JSON body. Every one of those is bad INPUT, not a server
    # fault, so we answer 400 and never let it bubble up to a 500.

    signature = request.headers.get("x-paystack-signature")

    # Guard: missing signature header. Without it the request cannot be a genuine
    # Paystack webhook, so reject before doing any work. (Prevents the later
    # `signature != computed` from silently passing on a None secret env.)
    if not signature:
        return Response({"message": "Missing signature"}, status=400)

    # Guard: PAYSTACK_SECRET_KEY may be unset in the environment (os.getenv -> None).
    # Calling .encode() on None raises AttributeError ('NoneType' has no attribute
    # 'encode'); treat an unconfigured key as a request we cannot verify -> 400.
    secret = settings.PAYSTACK_SECRET_KEY
    if not secret:
        return Response({"message": "Webhook verification unavailable"}, status=400)

    computed = hmac.new(
        secret.encode(),
        request.body,
        hashlib.sha512
    ).hexdigest()

    if signature != computed:
        return Response({"message": "Invalid signature"}, status=400)

    # Guard: an empty or non-JSON body makes json.loads raise (ValueError /
    # JSONDecodeError). Bad payload -> 400, never 500.
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return Response({"message": "Invalid payload"}, status=400)
    if not isinstance(payload, dict):
        return Response({"message": "Invalid payload"}, status=400)

    if payload.get("event") != "charge.success":
        return Response({"message": "Ignored"}, status=200)

    # Guard: `data` / `data.metadata.order_id` may be absent on a malformed event.
    # Using payload["data"] / data["metadata"]["order_id"] would raise KeyError
    # (TypeError if `data` is not a dict). Read defensively and 400 if order_id
    # is missing rather than letting the lookup explode.
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return Response({"message": "Invalid payload"}, status=400)
    metadata = data.get("metadata") or {}
    order_id = metadata.get("order_id") if isinstance(metadata, dict) else None
    if not order_id:
        return Response({"message": "order_id is required"}, status=400)

    order = Order.objects.prefetch_related("items__variant").filter(id=order_id).first()

    if not order or order.status == "paid":
        return Response({"message": "Already processed"}, status=200)

    with transaction.atomic():

        order.status = "paid"
        # Guard: read id/channel with .get() so a verified-but-sparse payload
        # cannot raise KeyError here after the order has already been located.
        order.paystack_transaction_id = str(data.get("id") or "")
        order.payment_method = data.get("channel") or ""
        order.paid_at = timezone.now()
        order.save()

        # Count the coupon use now that the order is paid. The already-paid guard above
        # makes this run exactly once per order (webhook + verify can both fire).
        if order.coupon_id:
            order.coupon.used_count = (order.coupon.used_count or 0) + 1
            order.coupon.save(update_fields=["used_count"])

        for item in order.items.all():
            for _ in range(item.quantity):

                fulfillment = Fulfillment.objects.create(
                    order=order,
                    item=item,
                    status="processing"
                )

                response = purchase_voucher(item.variant, order)

                if response.get("status"):
                    fulfillment.status = "delivered"
                    fulfillment.provider_payload = response["data"]
                else:
                    fulfillment.status = "failed"
                    fulfillment.provider_payload = {"status": f"{response["status"]}", "error": f"{response["error"]}", "code": f"{response["code"]}"}


                fulfillment.save()

    return Response({"message": "Webhook processed"})


@api_view(["GET"])
def test_denom(request):
    result = get_denominations(113)
    print("RESULT:", result)
    return Response(result)


@api_view(["GET"])
def test_brands(request):
    result = get_brands(2)
    print("RESULT:", result)
    return Response(result)


@api_view(["GET"])
def get_all_fulfillments(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)
    
    fulfillments = Fulfillment.objects.all().order_by("-id")

    data = []

    for f in fulfillments:
        data.append({
            "order_id": f.order.id,
            "status": f.status,
            "notes": f.notes,
            "provider_payload": f.provider_payload,
            "created_at": f.created_at,
        })

    return Response({"fulfillments": data}, status=200)


@api_view(["GET"])
def get_my_orders(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # prefetch items -> variant -> product so the new per-item product thumbnail (below) is
    # resolved without an N+1 across orders. OrderItem.variant is PROTECTed, so item.variant
    # and item.variant.product are always present.
    orders = (
        Order.objects.filter(user=user)
        .order_by("-created_at")
        .prefetch_related("items__variant__product")
    )

    data = []
    for order in orders:
        data.append({
            "order_id": order.id,
            "status": order.status,
            "subtotal": str(order.subtotal),
            "total": str(order.total),
            "created_at": order.created_at,
            "tax": str(order.tax),
            "items": [{
                "product_name": item.product_name_snapshot,
                "variant_title": item.variant_title_snapshot,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
                # product thumbnail for the my-orders list (owner request 2026-06-29) so the
                # buyer sees the picture of what they ordered, not just the name. Consumed by
                # frontend OrdersClient.tsx (the Items cell + the "+N more" tooltip).
                "product_image": _abs_url(request, item.variant.product.image),
            } for item in order.items.all()]
        })

    return Response({"orders": data}, status=200)


@api_view(["GET"])
def get_order_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    order_id = request.GET.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related("user").prefetch_related("items__variant__product").get(
            id=order_id,
            user=user
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)

    data = {
        "order_id": order.id,
        "status": order.status,
        "subtotal": str(order.subtotal),
        "total": str(order.total),
        "created_at": order.created_at,
        "tax": str(order.tax),
        # "voucher": item.fulfillment_records.first().provider_payload if item.fulfillment_records.exists() else None,
        "items": [{
            "product_name": item.product_name_snapshot,
            "variant_title": item.variant_title_snapshot,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
        } for item in order.items.all()]
    }

    return Response({"order": data}, status=200)



@api_view(["GET"])
def get_order_details_for_admin(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    order_id = request.GET.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related("user").prefetch_related("items__variant__product").get(
            id=order_id
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)

    data = {
        "order_id": order.id,
        "user_id": order.user.user_id,
        "username": order.user.username if order.user else None,
        "status": order.status,
        "subtotal": str(order.subtotal),
        "total": str(order.total),
        "created_at": order.created_at,
        "tax": str(order.tax),
        "first_name": order.first_name,
        "last_name": order.last_name,
        "email": order.email,
        "phone_number": order.phone_number,
        "address": order.address,
        "city": order.city,
        "state": order.state,
        "date": order.created_at.date(),
        "status": order.status,
        "transaction_id": order.paystack_transaction_id,
        "reference": order.paystack_reference,
        "items": [{
            "product_name": item.product_name_snapshot,
            "variant_title": item.variant_title_snapshot,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
        } for item in order.items.all()]
    }

    return Response({"order": data}, status=200)


@api_view(["POST"])
def mark_order_as_paid(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    order_id = request.data.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related().prefetch_related("items__variant__product").get(
            id=order_id
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)
    
    old_status = order.status

    if order.status == "paid":
        return Response({"message": "Order is already marked as paid."}, status=200)

    with transaction.atomic():
        order.status = "paid"
        order.save(update_fields=["status"])

        ShopChangeLog.objects.create(
            admin_user=user,
            action="order_status_updated",
            order=order,
            details={
                "status": {
                    "old": old_status,
                    "new": "paid"
                }
            }
        )

        # Reduce stock
        for item in order.items.all():
            variant = item.variant
            if variant.product.is_limited_stock:
                old_stock = variant.stock_qty
                variant.stock_qty -= item.quantity
                variant.save(update_fields=["stock_qty"])

                ShopChangeLog.objects.create(
                    action="variant_updated",
                    variant=variant,
                    details={
                        "stock_qty": {
                            "old": old_stock,
                            "new": variant.stock_qty,
                            "reason": "order_purchase",
                            "order_id": order.id
                        }
                    }
                )

    return Response({"message": "Order marked as paid successfully."}, status=200)


@api_view(["POST"])
def delete_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.delete()

    return Response({"message": "Coupon deleted successfully."}, status=200)


@api_view(["POST"])
def deactivate_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.is_active = False
    coupon.save(update_fields=["is_active"])

    return Response({"message": "Coupon deactivated successfully."}, status=200)


@api_view(["POST"])
def activate_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.is_active = True
    coupon.save(update_fields=["is_active"])

    return Response({"message": "Coupon activated successfully."}, status=200)


@api_view(["POST"])
def edit_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)
    
    old_coupon = {
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value,
        "max_uses": coupon.max_uses,
        "min_order_amount": coupon.min_order_amount,
        "end_at": coupon.end_at,
        "description": coupon.description
    }

    code = request.data.get("code")
    discount_type = request.data.get("discount_type")
    discount_value = request.data.get("discount_value")
    max_uses = request.data.get("max_uses")
    min_order_amount = request.data.get("min_order_amount")
    expiry_date = request.data.get("expiry_date")
    description = request.data.get("description")

    if code:
        coupon.code = code
    if discount_type in ["percentage", "fixed"]:
        coupon.discount_type = discount_type
    if discount_value:
        try:
            coupon.discount_value = Decimal(discount_value)
        except:
            return Response({"message": "Invalid discount_value."}, status=400)
    if max_uses is not None:
        try:
            coupon.max_uses = int(max_uses)
        except:
            return Response({"message": "Invalid max_uses."}, status=400)
    if min_order_amount is not None:
        try:
            coupon.min_order_amount = Decimal(min_order_amount)
        except:
            return Response({"message": "Invalid min_order_amount."}, status=400)
    if expiry_date:
        try:
            coupon.end_at = timezone.datetime.fromisoformat(expiry_date)
        except:
            return Response({"message": "Invalid expiry_date format."}, status=400)
    if description is not None:
        coupon.description = description
        
    coupon.save(update_fields=[
        "code", "discount_type", "discount_value", "max_uses",
        "min_order_amount", "end_at", "description"
    ])

    old_coupon = {
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value,
        "max_uses": coupon.max_uses,
        "min_order_amount": coupon.min_order_amount,
        "end_at": coupon.end_at,
        "description": coupon.description
    }

    return Response({"message": "Coupon updated successfully."}, status=200)


@api_view(["POST"])
def get_total_customer_savings(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)


    # get total savings without using Sum aggregation to avoid Decimal issues
    total_savings = Decimal("0.00")
    redemptions = Redemption.objects.filter(coupon=coupon).values_list("savings", flat=True)
    for savings in redemptions:
        if savings:
            total_savings += savings

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_customer_savings": str(total_savings)
    }, status=200)


@api_view(["POST"])
def get_total_coupon_uses(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    total_uses = Redemption.objects.filter(coupon=coupon).count()

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_uses": total_uses
    }, status=200)


@api_view(["POST"])
def get_total_revenue_generated(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    total_revenue = Redemption.objects.filter(coupon=coupon).aggregate(
        total_revenue=models.Sum("final_amount_after_discount")
    )["total_revenue"] or Decimal("0.00")

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_revenue_generated_from_coupon": str(total_revenue)
    }, status=200)


@api_view(["POST"])
def get_weekly_usage_and_saving_generated(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    one_week_ago = timezone.now() - timezone.timedelta(days=7)

    weekly_data = Redemption.objects.filter(
        coupon=coupon,
        redeemed_at__gte=one_week_ago
    ).annotate(
        day=models.functions.TruncDay("redeemed_at")
    ).values("day").annotate(
        total_uses=models.Count("id"),
        total_savings=models.Sum("savings_amount")
    ).order_by("day")

    result = []
    for entry in weekly_data:
        result.append({
            "date": entry["day"].date(),
            "uses": entry["total_uses"],
            "savings": str(entry["total_savings"] or Decimal("0.00"))
        })

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "weekly_usage_and_saving": result
    }, status=200)


@api_view(["GET"])
def get_coupon_conversion_rate(request):
    # The registered route (urls.py:45) has no path converter, so Django never
    # passes a `slug` positional arg -> the old signature raised
    # TypeError: missing 1 required positional argument 'slug' at dispatch (500).
    # Read the coupon slug from the query string instead: GET ?slug=<coupon-slug>.
    slug = request.GET.get("slug")
    if not slug:
        return Response({"message": "slug query param is required."}, status=400)

    coupon = get_object_or_404(Coupon, slug=slug)

    # Orders since the coupon became active. NOTE: the Coupon model has no
    # `created_at` field, so the previous `else coupon.created_at` fallback was a
    # latent AttributeError (-> 500) for any coupon with start_at unset (the
    # default, since start_at is nullable). When start_at is None we cannot anchor
    # on a creation time, so count all orders (no date filter) rather than crash.
    orders_qs = Order.objects.all()
    if coupon.start_at:
        orders_qs = orders_qs.filter(created_at__gte=coupon.start_at)
    total_orders = orders_qs.count()

    if total_orders == 0:
        return Response({
            "coupon": coupon.code,
            "conversion_rate": "0%",
            "used_orders": 0,
            "total_orders_since_creation": 0
        })

    used_orders = Order.objects.filter(
        coupon=coupon,
        status="paid"
    ).count()

    conversion_rate = (Decimal(used_orders) / Decimal(total_orders)) * 100

    return Response({
        "coupon": coupon.code,
        "used_orders": used_orders,
        "total_orders_since_creation": total_orders,
        "conversion_rate_percent": round(conversion_rate, 2)
    })


@api_view(["POST"])
def get_coupon_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    data = {
        "id": coupon.id,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "max_uses": coupon.max_uses,
        "used_count": coupon.used_count,
        "min_order_amount": str(coupon.min_order_amount),
        "expiry_date": coupon.end_at,
        "is_active": coupon.is_active,
        "description": coupon.description
    }

    return Response({"coupon_details": data}, status=200)


@api_view(["POST"])
def get_coupon_details_with_code(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_code = request.data.get("coupon_code")
    if not coupon_code:
        return Response({"message": "coupon_code is required."}, status=400)

    try:
        coupon = Coupon.objects.get(code=coupon_code)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    data = {
        "id": coupon.id,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "max_uses": coupon.max_uses,
        "used_count": coupon.used_count,
        "min_order_amount": str(coupon.min_order_amount),
        "expiry_date": coupon.end_at,
        "is_active": coupon.is_active,
        "description": coupon.description
    }

    return Response({"coupon_details": data}, status=200)

# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY CRUD  (admin-managed product categories)
#
# These power the "Manage Categories" surface on /a/shop/inventory and the user
# shop category tabs. They generalise the shop beyond the old diamond-only set.
#   - view_all_categories      (admin)  : full list incl. inactive + product count
#   - view_active_categories   (public) : active categories for the user shop tabs
#   - create_category / edit_category / delete_category (admin)
# Auth + response conventions mirror the product/coupon views above.
# ═════════════════════════════════════════════════════════════════════════════

def _serialize_category_full(category):
    """
    Admin-facing category serialisation: includes inactive state, ordering, and
    a live product count so the admin table can show usage before deleting.
    """
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
        "is_physical": category.is_physical,
        "is_active": category.is_active,
        "ordering": category.ordering,
        "product_count": category.products.count(),
        "created_at": category.created_at,
        "updated_at": category.updated_at,
    }


@api_view(["GET"])
def view_all_categories(request):
    """
    List every category (active + inactive) with product counts.

    Purpose:  Admin category management table.
    Auth:     require_admin.
    Response: 200 { categories: [ {id,name,slug,is_physical,is_active,ordering,product_count,...} ] }.
    Consumed by: ManageCategoriesModal on /a/shop/inventory.
    """
    admin, err = require_admin(request)
    if err: return err

    qs = Category.objects.all().prefetch_related("products")
    data = [_serialize_category_full(c) for c in qs]
    return Response({"categories": data}, status=200)


@api_view(["GET"])
def view_active_categories(request):
    """
    List only active categories, ordered for display.

    Purpose:  Drives the user shop category tabs (replaces the hard-coded
              frontend `shopProductTypes = ["diamonds"]`).
    Auth:     public (no token) -- same posture as view_product_details.
    Response: 200 { categories: [ {id,name,slug,is_physical,is_active} ] }.
    Consumed by: ShopClient.tsx tab bar.
    """
    qs = Category.objects.filter(is_active=True)
    data = [_serialize_category(c) for c in qs]
    return Response({"categories": data}, status=200)


@api_view(["POST"])
def create_category(request):
    """
    Create a new product category.

    Purpose:  Admin adds a category (e.g. "Jerseys") so products can be filed
              under it and it appears as a user shop tab.
    Auth:     require_admin.
    Request:  name (required), description, is_physical (bool), is_active (bool),
              ordering (int).
    Response: 201 { message, category }.
    Consumed by: ManageCategoriesModal "Add category" form.
    """
    admin, err = require_admin(request)
    if err: return err

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"message": "name is required."}, status=400)

    # Reject duplicates up front for a clean message (the unique constraint would
    # otherwise surface as a 500 IntegrityError).
    if Category.objects.filter(name__iexact=name).exists():
        return Response({"message": "A category with this name already exists."}, status=400)

    category = Category.objects.create(
        name=name,
        description=request.data.get("description", ""),
        is_physical=str(request.data.get("is_physical", "true")).lower() in ("true", "1", "yes"),
        is_active=str(request.data.get("is_active", "true")).lower() in ("true", "1", "yes"),
        ordering=int(request.data.get("ordering") or 0),
    )

    return Response(
        {"message": "Category created.", "category": _serialize_category_full(category)},
        status=201,
    )


@api_view(["POST"])
def edit_category(request):
    """
    Update an existing category.

    Purpose:  Admin renames a category or toggles its visibility / shipping flag.
    Auth:     require_admin.
    Request:  category_id (required); any of name / description / is_physical /
              is_active / ordering.
    Response: 200 { message, category }.
    Consumed by: ManageCategoriesModal inline edit.

    NOTE: we keep the existing slug stable on rename so product_type values that
    already reference it (and any saved links) do not break.
    """
    admin, err = require_admin(request)
    if err: return err

    category_id = request.data.get("category_id")
    if not category_id:
        return Response({"message": "category_id is required."}, status=400)

    category = get_object_or_404(Category, id=category_id)

    name = request.data.get("name")
    if name is not None:
        name = name.strip()
        if not name:
            return Response({"message": "name cannot be empty."}, status=400)
        # Guard against renaming onto another category's name.
        if Category.objects.filter(name__iexact=name).exclude(id=category.id).exists():
            return Response({"message": "Another category already uses this name."}, status=400)
        category.name = name

    if "description" in request.data:
        category.description = request.data.get("description", "")
    if "is_physical" in request.data:
        category.is_physical = str(request.data.get("is_physical")).lower() in ("true", "1", "yes")
    if "is_active" in request.data:
        category.is_active = str(request.data.get("is_active")).lower() in ("true", "1", "yes")
    if "ordering" in request.data:
        try:
            category.ordering = int(request.data.get("ordering") or 0)
        except (ValueError, TypeError):
            return Response({"message": "ordering must be a number."}, status=400)

    category.save()

    return Response(
        {"message": "Category updated.", "category": _serialize_category_full(category)},
        status=200,
    )


@api_view(["POST"])
def delete_category(request):
    """
    Delete a category.

    Purpose:  Admin removes an unused category.
    Auth:     require_admin.
    Request:  category_id (required).
    Response: 200 { message }  |  400 if products still reference it.
    Consumed by: ManageCategoriesModal delete action.

    SAFETY: refuse to delete a category that still has products attached so the
    admin does not silently orphan the catalogue. (Product.category is SET_NULL,
    so a forced delete would only null the link -- but blocking is clearer.)
    """
    admin, err = require_admin(request)
    if err: return err

    category_id = request.data.get("category_id")
    if not category_id:
        return Response({"message": "category_id is required."}, status=400)

    category = get_object_or_404(Category, id=category_id)

    product_count = category.products.count()
    if product_count > 0:
        return Response(
            {"message": f"Cannot delete: {product_count} product(s) still use this category. Reassign or remove them first."},
            status=400,
        )

    category.delete()
    return Response({"message": "Category deleted."}, status=200)


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCT MEDIA  (multi-image + video gallery per product)
#
# A product can carry many images and videos via the ProductMedia table. These
# endpoints upload and remove individual media items; the gallery itself is
# returned inside view_all_products / view_product_details as a `media` array.
#   - add_product_media          (admin)  : multipart upload, one or many files
#   - delete_product_media       (admin)  : remove a single media item
#   - vendor_add/delete (afc_shop/vendors.py) : the VENDOR-gated equivalents, which
#     reuse _attach_media below so both audiences share identical validation.
# Size limits are enforced here (the authoritative server-side check).
# ═════════════════════════════════════════════════════════════════════════════

def _attach_media(request, product, files):
    """Validate + persist uploaded `files` as ProductMedia rows on `product`.

    SHARED by the admin upload (add_product_media) and the VENDOR upload
    (afc_shop/vendors.py vendor_add_product_media) so both classify by content-type,
    enforce the SAME caps (images <= MAX_IMAGE_BYTES, videos <= MAX_VIDEO_BYTES), and
    append after any existing gallery items (ordering continues from the current count).

    Returns (created, error_response):
      - on success: created = [ {id,url,media_type,ordering}, ... ], error_response = None
      - on a bad/oversized/unsupported file: created = None, error_response = a DRF Response
        to return as-is (any rows created before the bad file in the same batch remain;
        callers send one batch at a time so this is acceptable).
    """
    existing = product.media.count()
    created = []
    for index, f in enumerate(files):
        content_type = (getattr(f, "content_type", "") or "").lower()
        # Classify by content-type and validate size against the matching cap.
        if content_type.startswith(ALLOWED_IMAGE_PREFIX):
            media_type = "image"
            if f.size > MAX_IMAGE_BYTES:
                return None, Response(
                    {"message": f"Image '{f.name}' exceeds the 5 MB limit."}, status=400)
        elif content_type.startswith(ALLOWED_VIDEO_PREFIX):
            media_type = "video"
            if f.size > MAX_VIDEO_BYTES:
                return None, Response(
                    {"message": f"Video '{f.name}' exceeds the 50 MB limit."}, status=400)
        else:
            return None, Response(
                {"message": f"Unsupported file type for '{f.name}'. Only images and videos are allowed."},
                status=400)
        media = ProductMedia.objects.create(
            product=product, file=f, media_type=media_type, ordering=existing + index)
        created.append({
            "id": media.id,
            "url": _abs_url(request, media.file),
            "media_type": media.media_type,
            "ordering": media.ordering,
        })
    return created, None


@api_view(["POST"])
def add_product_media(request):
    """
    Upload one or more media files (images + videos) for a product.

    Purpose:  Admin attaches a gallery to a product.
    Auth:     require_admin.
    Request:  multipart -- product_id (required) + one or more files under the
              field name `files` (use request.FILES.getlist). Each file is
              classified by its content-type (image/* or video/*) and capped
              (images <= 5 MB, videos <= 50 MB).
    Response: 201 { message, media: [ {id,url,media_type,ordering} ] }.
    Consumed by: the media uploader in AddProductModal / the edit product page.
    """
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    # Accept the field under `files` (multi) or fall back to a single `file`.
    files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not files:
        return Response({"message": "No files uploaded."}, status=400)

    created, err = _attach_media(request, product, files)
    if err:
        return err
    return Response({"message": "Media uploaded.", "media": created}, status=201)


@api_view(["POST"])
def delete_product_media(request):
    """
    Delete a single media item from a product's gallery.

    Purpose:  Admin removes one image/video.
    Auth:     require_admin.
    Request:  media_id (required).
    Response: 200 { message }.
    Consumed by: the media uploader's per-item remove button.
    """
    admin, err = require_admin(request)
    if err: return err

    media_id = request.data.get("media_id")
    if not media_id:
        return Response({"message": "media_id is required."}, status=400)

    media = get_object_or_404(ProductMedia, id=media_id)
    media.delete()
    return Response({"message": "Media deleted."}, status=200)
