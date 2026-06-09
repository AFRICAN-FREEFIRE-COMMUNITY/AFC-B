"""
afc_shop/vendors.py
================================================================================
MARKETPLACE Phase B1 — vendor management + product approval + vendor product CRUD
(spec: WEBSITE/tasks/marketplace-design.md, Phase B1; INVITE-ONLY vendors).

Kept in its own module (like afc_shop/fulfilment.py) so the large legacy views.py
is not churned. This file owns THREE clusters of endpoints, all registered under
shop/ in afc_shop/urls.py:

  A) ADMIN vendor management (require_admin) — INVITE-ONLY: an AFC admin grants a
     partner vendor access by LINKING an existing User. There is NO public "Sell on
     AFC" application and NO pending state on the Vendor (owner decision 2026-06-09).
       - admin_create_vendor        : link a User -> a new active Vendor
       - admin_list_vendors         : list every vendor (the "Manage vendors" table)
       - admin_set_vendor_status    : flip a vendor active <-> suspended
       - admin_assign_product_vendor: set Product.vendor (re-home a product to a vendor)
     Consumed by: the admin shop "Manage vendors" surface.

  B) ADMIN product approval (require_admin) — AFC approves every SUBMITTED vendor
     product before it can reach buyers.
       - admin_list_pending_products: the approval queue (approval_status="submitted")
       - admin_approve_product      : submitted -> approved (+ approved_by)
       - admin_reject_product       : submitted -> rejected (+ rejection_reason)
     Consumed by: the admin shop "Product approvals" surface.

  C) VENDOR product CRUD (gated to the CALLER's own ACTIVE Vendor, using the SAME
     vendor/auth pattern as afc_shop/fulfilment.py) — a vendor manages only their
     own products and can NEVER approve their own product.
       - vendor_my_products   : the caller-vendor's own products (any approval state)
       - vendor_create_product: create a Product owned by the caller's vendor,
                                approval_status="draft", with variants + media like
                                add_product (views.py)
       - vendor_update_product: edit ONLY the caller's own draft/rejected products
       - vendor_submit_product: draft/rejected -> submitted (+ submitted_at)
     Consumed by: the vendor self-serve dashboard (Phase B2 frontend).

HOW IT CONNECTS
  - Models: Vendor (the seller identity), Product (+ the Phase B1 approval fields:
    approval_status / submitted_at / approved_by / rejection_reason added in
    models.py), ProductVariant + ProductMedia (a vendor product carries variants and
    a media gallery, exactly like an admin product). The storefront gate that hides
    unapproved vendor products lives in views.view_active_products.
  - Auth: afc_auth.validate_token (resolve the Bearer caller) + afc_auth.require_admin
    (the admin gate). The vendor gate (_require_active_vendor below) mirrors the
    order/vendor edge used in afc_shop/fulfilment.py (Vendor.user == caller, and the
    vendor must be active).
  - Media limits + helpers (_abs_url, _serialize_media, MAX_IMAGE_BYTES, ...) are
    REUSED from afc_shop.views so a vendor product serialises and validates uploads
    identically to an admin product (no duplicated rules).
"""

import json
import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import User
from afc_auth.views import require_admin, validate_token

from .models import Product, ProductMedia, ProductVariant, ShopChangeLog, Vendor
# Reuse the shop's media helpers + caps so a vendor product behaves exactly like an
# admin product (same absolute-URL builder, same gallery shape, same upload limits).
from .views import (
    ALLOWED_IMAGE_PREFIX,
    ALLOWED_VIDEO_PREFIX,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    _abs_url,
    _attach_media,
    _serialize_category,
    _serialize_media,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared serialisers + the vendor auth gate
# ─────────────────────────────────────────────────────────────────────────────
def _serialize_vendor(vendor):
    """Serialise a Vendor for the admin "Manage vendors" table.

    Includes the linked User (id + username) so the admin can see WHO the vendor
    logs in as, plus a live product count for context before suspending. Mirrors
    the _serialize_category_full shape (flat dict, ids + counts) used elsewhere."""
    return {
        "id": vendor.id,
        "display_name": vendor.display_name,
        "contact_email": vendor.contact_email,
        "whatsapp_number": vendor.whatsapp_number,
        "status": vendor.status,
        # The linked login. user_id is the User PK (afc_auth.User.user_id).
        "user_id": vendor.user_id,
        "username": vendor.user.username if vendor.user_id else None,
        "stripe_account_id": vendor.stripe_account_id,
        "product_count": vendor.products.count(),
        "created_at": vendor.created_at,
    }


def _serialize_vendor_product(request, product):
    """Serialise a Product for the VENDOR dashboard + the admin approval queue.

    Same core shape as views.view_all_products (so the frontend mapping is shared),
    plus the Phase B1 approval fields the vendor/admin need: approval_status,
    submitted_at, rejection_reason, and the owning vendor id/name. Variants + the
    media gallery are included so the vendor can see + edit everything in one place."""
    return {
        "id": product.id,
        "name": product.name,
        "type": product.product_type,
        "category": _serialize_category(product.category),
        "description": product.description,
        "status": product.status,
        "is_limited_stock": product.is_limited_stock,
        "image": _abs_url(request, product.image),
        "media": _serialize_media(request, product),
        # ── Phase B1 approval fields ──
        "approval_status": product.approval_status,
        "submitted_at": product.submitted_at,
        "approved_at": product.approved_at,
        "rejection_reason": product.rejection_reason,
        "vendor_id": product.vendor_id,
        "vendor_name": product.vendor.display_name if product.vendor_id else None,
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
            "created_at": v.created_at,
            "updated_at": v.updated_at,
        } for v in product.variants.all()],
    }


def _require_active_vendor(request):
    """Resolve the Bearer caller and return their ACTIVE Vendor account.

    The vendor gate for cluster C, mirroring the order/vendor edge in
    afc_shop/fulfilment.py (Vendor.user == caller). Returns (user, vendor, error):
      - bad/expired token            -> (None, None, 400/401 Response)
      - caller has no Vendor account -> (None, None, 403 Response)
      - caller's Vendor is suspended -> (None, None, 403 Response)
      - success                      -> (user, vendor, None)

    Used by every vendor CRUD endpoint so the gate is identical everywhere. A
    suspended vendor is blocked here exactly as in fulfilment._authorise (access
    revoked by an admin)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, None, Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, None, Response({"message": "Invalid or expired session token."}, status=401)

    # The caller must own a Vendor account. (A User could in theory have more than
    # one; Phase B1 uses the first, matching fulfilment.vendor_my_orders.)
    vendor = Vendor.objects.filter(user=user).first()
    if not vendor:
        return None, None, Response({"message": "You are not a vendor."}, status=403)

    if vendor.status != "active":
        return None, None, Response({"message": "Your vendor access is suspended."}, status=403)

    return user, vendor, None


def _parse_variants(raw):
    """Normalise an incoming `variants` value into a Python list.

    A multipart request (image rides along) sends nested arrays as a JSON string;
    a JSON-body request sends a real list. Returns (variants, error): on a bad JSON
    string error is a 400 Response. Mirrors the parsing in views.add_product so a
    vendor product is created the same way an admin product is."""
    if isinstance(raw, str):
        try:
            return json.loads(raw), None
        except (ValueError, TypeError):
            return None, Response({"message": "variants must be a valid JSON list."}, status=400)
    return raw, None


# ═════════════════════════════════════════════════════════════════════════════
# A) ADMIN VENDOR MANAGEMENT  (require_admin) — INVITE-ONLY
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def admin_create_vendor(request):
    """
    Grant vendor access to an existing User (INVITE-ONLY; admins create vendors).

    Purpose:  An AFC admin turns a partner's existing account into a marketplace
              vendor. There is NO public application and NO pending state — this is
              the only way a Vendor comes into existence (owner decision 2026-06-09).
    Auth:     require_admin (Bearer session token, role == "admin").
    Request:  { user_id | email, display_name, contact_email?, whatsapp_number? }
              The User is located by user_id (preferred) OR email; display_name is
              required (the shop-facing seller name).
    Response: 201 { message, vendor }  |  400 (missing field / already a vendor)
              | 404 (user not found).
    Consumed by: the admin shop "Manage vendors" surface ("Add vendor" form).

    Sets created_by to the granting admin (audit trail) and status="active".
    """
    admin, err = require_admin(request)
    if err:
        return err

    display_name = (request.data.get("display_name") or "").strip()
    if not display_name:
        return Response({"message": "display_name is required."}, status=400)

    # Locate the User to link by user_id (preferred) or email. We never CREATE a
    # user here — invite-only means linking an existing login, like sponsors/organizers.
    user_id = request.data.get("user_id")
    email = (request.data.get("email") or "").strip()
    target = None
    if user_id:
        target = User.objects.filter(user_id=user_id).first()
    elif email:
        target = User.objects.filter(email__iexact=email).first()
    else:
        return Response({"message": "Provide user_id or email of the user to grant vendor access."}, status=400)

    if not target:
        return Response({"message": "User not found."}, status=404)

    # One active vendor identity per user is enough for Phase B1; refuse a duplicate
    # so an admin does not accidentally create two vendor rows for the same login.
    if Vendor.objects.filter(user=target).exists():
        return Response({"message": "This user is already a vendor."}, status=400)

    vendor = Vendor.objects.create(
        user=target,
        display_name=display_name,
        # Fall back to the user's own email if no explicit contact email is given,
        # so notify_vendor (fulfilment.py) always has somewhere to reach them.
        contact_email=(request.data.get("contact_email") or target.email or "").strip(),
        whatsapp_number=(request.data.get("whatsapp_number") or "").strip(),
        status="active",
        created_by=admin,
    )

    return Response(
        {"message": "Vendor access granted.", "vendor": _serialize_vendor(vendor)},
        status=201,
    )


@api_view(["GET"])
def admin_list_vendors(request):
    """
    List every vendor with their linked login + product count.

    Purpose:  The admin "Manage vendors" table.
    Auth:     require_admin.
    Response: 200 { count, vendors: [ {id,display_name,status,user_id,username,
              product_count,...} ] }.
    Consumed by: the admin shop "Manage vendors" surface.
    """
    admin, err = require_admin(request)
    if err:
        return err

    # select_related the user (each row shows the username) + prefetch products
    # (product_count) so the table renders without N+1 queries.
    qs = (
        Vendor.objects.select_related("user")
        .prefetch_related("products")
        .order_by("-created_at")
    )
    data = [_serialize_vendor(v) for v in qs]
    return Response({"count": len(data), "vendors": data}, status=200)


@api_view(["POST"])
def admin_set_vendor_status(request):
    """
    Activate or suspend a vendor.

    Purpose:  An admin revokes (or restores) a vendor's selling/fulfilment access.
              A suspended vendor is blocked by both the vendor CRUD gate
              (_require_active_vendor) and the fulfilment gate (fulfilment._authorise),
              so they can take no new action while suspended.
    Auth:     require_admin.
    Request:  { vendor_id, status: "active" | "suspended" }.
    Response: 200 { message, vendor }  |  400 (bad status) | 404 (vendor not found).
    Consumed by: the admin shop "Manage vendors" surface (suspend/restore toggle).
    """
    admin, err = require_admin(request)
    if err:
        return err

    vendor_id = request.data.get("vendor_id")
    new_status = request.data.get("status")
    if not vendor_id:
        return Response({"message": "vendor_id is required."}, status=400)
    if new_status not in ("active", "suspended"):
        return Response({"message": "status must be 'active' or 'suspended'."}, status=400)

    vendor = get_object_or_404(Vendor, id=vendor_id)
    vendor.status = new_status
    vendor.save(update_fields=["status"])

    return Response(
        {"message": f"Vendor {new_status}.", "vendor": _serialize_vendor(vendor)},
        status=200,
    )


@api_view(["POST"])
def admin_assign_product_vendor(request):
    """
    Set (or clear) which vendor owns a product.

    Purpose:  An admin re-homes a product to a vendor (e.g. takes an existing AFC
              product into a vendor's catalogue), or clears the link back to
              first-party AFC stock. This is the admin counterpart to a vendor
              creating their own product.
    Auth:     require_admin.
    Request:  { product_id, vendor_id }  — vendor_id null/empty clears the vendor
              (product becomes first-party AFC stock again).
    Response: 200 { message, product_id, vendor_id }  |  404 (product/vendor not found).
    Consumed by: the admin shop product editor / "Manage vendors" surface.

    NOTE: this only sets ownership; it does NOT change approval_status. An admin who
    wants an assigned vendor product live can approve it via admin_approve_product.
    """
    admin, err = require_admin(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    vendor_id = request.data.get("vendor_id")
    if vendor_id:
        vendor = get_object_or_404(Vendor, id=vendor_id)
        product.vendor = vendor
    else:
        # Empty/None clears the link -> back to first-party AFC stock.
        product.vendor = None

    product.save(update_fields=["vendor"])

    return Response(
        {"message": "Product vendor updated.", "product_id": product.id, "vendor_id": product.vendor_id},
        status=200,
    )


# ═════════════════════════════════════════════════════════════════════════════
# B) ADMIN PRODUCT APPROVAL  (require_admin)
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def admin_list_pending_products(request):
    """
    The product approval queue: products a vendor has SUBMITTED for review.

    Purpose:  The admin shop "Product approvals" surface lists everything awaiting a
              decision (approval_status == "submitted").
    Auth:     require_admin.
    Response: 200 { count, products: [ {id,name,approval_status,vendor_id,
              vendor_name,submitted_at,variants,media,...} ] }.
    Consumed by: the admin shop "Product approvals" surface.
    """
    admin, err = require_admin(request)
    if err:
        return err

    qs = (
        Product.objects.filter(approval_status="submitted")
        .select_related("category", "vendor")
        .prefetch_related("variants", "media")
        .order_by("submitted_at", "-created_at")
    )
    data = [_serialize_vendor_product(request, p) for p in qs]
    return Response({"count": len(data), "products": data}, status=200)


@api_view(["POST"])
def admin_approve_product(request):
    """
    Approve a submitted vendor product so it can go live.

    Purpose:  An admin approves a product in the queue. Sets approval_status=
              "approved" (the storefront gate then lets it through, provided
              status="active") + records approved_by for the audit trail and clears
              any prior rejection_reason.
    Auth:     require_admin.
    Request:  { product_id }.
    Response: 200 { message, product_id, approval_status }  |  400 (not in a
              submitted state) | 404 (not found).
    Consumed by: the admin shop "Product approvals" surface (Approve button).

    GUARD: only a "submitted" product may be approved (an admin should be acting on
    the queue, not silently approving a draft). A vendor can never reach this
    endpoint (require_admin), so a vendor cannot approve their own product.
    """
    admin, err = require_admin(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    if product.approval_status != "submitted":
        return Response(
            {"message": f"Only a submitted product can be approved (this one is '{product.approval_status}')."},
            status=400,
        )

    product.approval_status = "approved"
    product.approved_by = admin
    product.approved_at = timezone.now()  # stamp WHEN, for the admin inventory list
    product.rejection_reason = ""  # clear any stale rejection note
    product.save(update_fields=["approval_status", "approved_by", "approved_at", "rejection_reason"])

    # Audit row (reuses the shop change log, like the admin product views).
    ShopChangeLog.objects.create(
        admin_user=admin,
        action="product_updated",
        product=product,
        details={"approval_status": {"old": "submitted", "new": "approved"}},
    )

    return Response(
        {"message": "Product approved.", "product_id": product.id, "approval_status": product.approval_status},
        status=200,
    )


@api_view(["POST"])
def admin_reject_product(request):
    """
    Reject a submitted vendor product with a reason.

    Purpose:  An admin rejects a product in the queue. Sets approval_status=
              "rejected" + stores the reason (shown back to the vendor, who may edit
              and re-submit: rejected -> submitted via vendor_submit_product).
    Auth:     require_admin.
    Request:  { product_id, reason }  — reason is required (the vendor needs to know
              what to fix).
    Response: 200 { message, product_id, approval_status }  |  400 (no reason / not
              submitted) | 404 (not found).
    Consumed by: the admin shop "Product approvals" surface (Reject button + reason).
    """
    admin, err = require_admin(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    reason = (request.data.get("reason") or "").strip()
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)
    if not reason:
        return Response({"message": "A rejection reason is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    if product.approval_status != "submitted":
        return Response(
            {"message": f"Only a submitted product can be rejected (this one is '{product.approval_status}')."},
            status=400,
        )

    product.approval_status = "rejected"
    product.rejection_reason = reason
    product.save(update_fields=["approval_status", "rejection_reason"])

    ShopChangeLog.objects.create(
        admin_user=admin,
        action="product_updated",
        product=product,
        details={"approval_status": {"old": "submitted", "new": "rejected"}, "reason": reason},
    )

    return Response(
        {"message": "Product rejected.", "product_id": product.id, "approval_status": product.approval_status},
        status=200,
    )


# ═════════════════════════════════════════════════════════════════════════════
# C) VENDOR PRODUCT CRUD  (gated to the caller's own ACTIVE Vendor)
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def vendor_my_products(request):
    """
    The caller-vendor's OWN products (every approval state).

    Purpose:  The vendor dashboard product list — shows draft, submitted, approved
              and rejected products so the vendor can see status + any rejection
              reason in one place.
    Auth:     Bearer -> _require_active_vendor (the caller must own an active Vendor).
    Response: 200 { count, products: [ {id,name,approval_status,rejection_reason,
              variants,media,...} ] }.
    Consumed by: the vendor self-serve dashboard (Phase B2 frontend).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    qs = (
        Product.objects.filter(vendor=vendor)
        .select_related("category", "vendor")
        .prefetch_related("variants", "media")
        .order_by("-created_at")
    )
    data = [_serialize_vendor_product(request, p) for p in qs]
    return Response({"count": len(data), "products": data}, status=200)


@api_view(["POST"])
def vendor_create_product(request):
    """
    Create a product OWNED BY the caller's vendor, in "draft".

    Purpose:  A vendor adds a product to their own catalogue. It starts as
              approval_status="draft" (storefront-hidden) and must be submitted, then
              approved by an admin, before it can sell. Created exactly like
              views.add_product (name + variants [+ optional primary image]), but the
              vendor is forced to the CALLER's vendor and approval_status to "draft"
              — a vendor can never create a pre-approved or someone-else's product.
    Auth:     Bearer -> _require_active_vendor.
    Request:  name (required), product_type (slug string, required), description?,
              is_limited_stock?, optional `image` (multipart), variants[] (non-empty:
              each {sku, price, title?, diamonds_amount?, stock_qty?, meta?, is_active?}).
              status defaults to "active" so that, ONCE APPROVED, it is immediately
              sellable; until approval the storefront gate hides it regardless.
    Response: 201 { message, product_id, variant_ids }  |  400 (validation).
    Consumed by: the vendor dashboard "Add product" form (Phase B2 frontend).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    name = request.data.get("name")
    product_type = request.data.get("product_type")
    description = request.data.get("description", "")
    is_limited_stock = str(request.data.get("is_limited_stock", "false")).lower() in ("true", "1", "yes")

    if not name or not product_type:
        return Response({"message": "name and product_type are required."}, status=400)

    variants, err = _parse_variants(request.data.get("variants", []))
    if err:
        return err
    if not isinstance(variants, list) or len(variants) == 0:
        return Response({"message": "variants must be a non-empty list."}, status=400)

    image = request.FILES.get("image")

    # Force ownership + draft state server-side (never trust the client for these).
    product = Product.objects.create(
        name=name,
        description=description,
        product_type=product_type,
        image=image,
        is_limited_stock=is_limited_stock,
        status="active",            # sellable ONCE approved; gate hides it until then
        vendor=vendor,              # forced to the caller's vendor
        approval_status="draft",    # vendor products always start in draft
    )

    created_variants = []
    for v in variants:
        sku = v.get("sku")
        price = v.get("price")
        if not sku or price is None:
            # Roll back the half-built product so a bad variant does not leave an
            # orphan draft behind.
            product.delete()
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

    return Response(
        {"message": "Product created (draft). Submit it for approval when ready.",
         "product_id": product.id, "variant_ids": created_variants},
        status=201,
    )


@api_view(["POST"])
def vendor_update_product(request):
    """
    Edit ONE of the caller-vendor's OWN draft/rejected products.

    Purpose:  A vendor edits a product they have not yet sold-through approval. Only
              the OWNER vendor may edit, and only while the product is "draft" or
              "rejected" (a submitted product is locked pending the admin decision;
              an approved/live product is not editable here in Phase B1). Editing a
              rejected product is how a vendor fixes it before re-submitting.
    Auth:     Bearer -> _require_active_vendor; the product.vendor must equal the
              caller's vendor (else 403 — a vendor cannot edit another vendor's
              product).
    Request:  product_id (required); any of name / description / product_type /
              status / is_limited_stock; optional `image` (multipart); optional
              variants[] (in-place edits keyed by variant id, like views.edit_product).
    Response: 200 { message }  |  400 (not editable) | 403 (not the owner) | 404.
    Consumed by: the vendor dashboard product editor (Phase B2 frontend).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    # OWNERSHIP gate: a vendor may only touch their OWN product.
    if product.vendor_id != vendor.id:
        return Response({"message": "You do not own this product."}, status=403)

    # STATE gate: only draft or rejected products are editable by the vendor. A
    # submitted product is awaiting review; an approved one is live (admin territory).
    if product.approval_status not in ("draft", "rejected"):
        return Response(
            {"message": f"Only a draft or rejected product can be edited (this one is '{product.approval_status}')."},
            status=400,
        )

    # Plain text fields (skip is_limited_stock — coerced below). Mirrors edit_product.
    for field in ["name", "description", "product_type", "status"]:
        if field in request.data:
            setattr(product, field, request.data.get(field))

    if "is_limited_stock" in request.data:
        product.is_limited_stock = str(request.data.get("is_limited_stock")).lower() in ("true", "1", "yes")

    new_image = request.FILES.get("image")
    if new_image:
        product.image = new_image

    product.save()

    # Optional in-place variant edits (only the vendor's own product's variants).
    variants, err = _parse_variants(request.data.get("variants"))
    if err:
        return err
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
            for f in ["sku", "title", "price", "diamonds_amount", "stock_qty", "is_active", "meta"]:
                if f in v:
                    setattr(pv, f, v.get(f))
            pv.save()

    return Response({"message": "Product updated."}, status=200)


# ── vendor media: the multi-image + video gallery, vendor-gated ──────────────────
# These are the VENDOR equivalents of views.add_product_media / delete_product_media
# (which are admin-only). They let a vendor build a gallery (many images + short
# videos) on their OWN product while it is still editable (draft/rejected), reusing the
# SAME validation + caps via views._attach_media so a vendor product behaves identically
# to an admin one. Consumed by the vendor product editor (ProductFormDialog ->
# ProductMediaManager, pointed at these endpoints). The buyer storefront already renders
# product.media (ProductMediaGallery), so anything added here shows once the product is
# approved; the admin approval queue reads the same media to review before approving.

def _vendor_owns_editable_product(vendor, product):
    """Shared gate for the vendor media endpoints: the product must belong to this
    vendor AND be in an editable state (draft/rejected). Returns an error Response if
    not, else None. Mirrors the ownership + state gate in vendor_update_product so media
    edits follow the exact same rules as field edits (no touching a submitted/approved
    product, no touching another vendor's product)."""
    if product.vendor_id != vendor.id:
        return Response({"message": "You do not own this product."}, status=403)
    if product.approval_status not in ("draft", "rejected"):
        return Response(
            {"message": f"Only a draft or rejected product can be edited (this one is '{product.approval_status}')."},
            status=400,
        )
    return None


@api_view(["POST"])
def vendor_add_product_media(request):
    """
    POST /shop/vendor/products/media/add/   multipart: product_id + files[] (image/video)

    Upload one or more images/videos to the gallery of the caller-vendor's OWN
    draft/rejected product. The VENDOR counterpart of views.add_product_media.

    Auth:     Bearer -> _require_active_vendor; the product.vendor must equal the caller's
              vendor and be draft/rejected (else 403/400) -> _vendor_owns_editable_product.
    Request:  multipart -- product_id (required) + one or more files under `files`
              (request.FILES.getlist). Classified image/* vs video/* and capped (images
              <= 5 MB, videos <= 50 MB) by the SHARED views._attach_media.
    Response: 201 { message, media: [ {id,url,media_type,ordering} ] } | 400/403/404.
    Consumed by: ProductFormDialog -> ProductMediaManager (uploadUrl set to this route).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)
    product = get_object_or_404(Product, id=product_id)

    err = _vendor_owns_editable_product(vendor, product)
    if err:
        return err

    files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not files:
        return Response({"message": "No files uploaded."}, status=400)

    created, err = _attach_media(request, product, files)
    if err:
        return err
    return Response({"message": "Media uploaded.", "media": created}, status=201)


@api_view(["POST"])
def vendor_delete_product_media(request):
    """
    POST /shop/vendor/products/media/delete/   body: { media_id }

    Remove a single media item from the caller-vendor's OWN draft/rejected product's
    gallery. The VENDOR counterpart of views.delete_product_media.

    Auth:     Bearer -> _require_active_vendor; the media's product must belong to the
              caller's vendor and be draft/rejected -> _vendor_owns_editable_product.
    Request:  media_id (required).
    Response: 200 { message } | 400/403/404.
    Consumed by: ProductFormDialog -> ProductMediaManager (deleteUrl set to this route).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    media_id = request.data.get("media_id")
    if not media_id:
        return Response({"message": "media_id is required."}, status=400)
    media = get_object_or_404(ProductMedia, id=media_id)

    err = _vendor_owns_editable_product(vendor, media.product)
    if err:
        return err

    media.delete()
    return Response({"message": "Media deleted."}, status=200)


@api_view(["POST"])
def vendor_submit_product(request):
    """
    Submit one of the caller-vendor's draft/rejected products for AFC approval.

    Purpose:  A vendor pushes a product into the admin approval queue. Transition:
              draft|rejected -> submitted (+ stamps submitted_at, clears any prior
              rejection_reason). The product now appears in admin_list_pending_products.
              A vendor can NEVER move it to "approved" — only the admin endpoints do.
    Auth:     Bearer -> _require_active_vendor; product.vendor must equal the
              caller's vendor (else 403).
    Request:  { product_id }.
    Response: 200 { message, product_id, approval_status }  |  400 (already submitted/
              approved) | 403 (not the owner) | 404.
    Consumed by: the vendor dashboard "Submit for approval" button (Phase B2 frontend).
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    if product.vendor_id != vendor.id:
        return Response({"message": "You do not own this product."}, status=403)

    # Only a draft or a rejected (resubmit) product may be submitted.
    if product.approval_status not in ("draft", "rejected"):
        return Response(
            {"message": f"Only a draft or rejected product can be submitted (this one is '{product.approval_status}')."},
            status=400,
        )

    product.approval_status = "submitted"
    product.submitted_at = timezone.now()
    product.rejection_reason = ""  # a fresh submission clears the old rejection note
    product.save(update_fields=["approval_status", "submitted_at", "rejection_reason"])

    return Response(
        {"message": "Product submitted for approval.",
         "product_id": product.id, "approval_status": product.approval_status},
        status=200,
    )
