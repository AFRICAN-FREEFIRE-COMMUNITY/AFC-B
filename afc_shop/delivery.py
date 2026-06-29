"""
afc_shop/delivery.py
================================================================================
Saved delivery info (owner request 2026-06-29) + the SUPER-ADMIN-ONLY view of all
collected customer delivery PII.

TWO surfaces, kept in this one module (like fulfilment.py / vendors.py so the big
views.py is not churned), all registered under shop/ in afc_shop/urls.py:

  A) USER saved delivery profiles (Bearer -> validate_token, OWNER-SCOPED) — a buyer
     saves their delivery/contact details at checkout and reuses them next time:
       - list_my_delivery_profiles    : the picker feed (default first)
       - create_delivery_profile      : save a new entry (+ optional default)
       - update_delivery_profile      : edit one of the caller's entries
       - delete_delivery_profile      : remove one
       - set_default_delivery_profile : flip which entry is the default
     Plus two helpers the checkout views call:
       - persist_delivery_profile(user, data, ...) : create/refresh a profile from a
         checkout payload (used when the buyer ticks "save my info")
       - attach_delivery_profile(order, user, data): best-effort link of an Order to a
         saved profile (saved_profile_id) or to a freshly-saved one (save_delivery_info)
     Consumed by: frontend CartDetails.tsx (the checkout picker + "save my info") and the
     /profile/addresses manage page.

  B) SUPER-ADMIN delivery-info view (require_head_admin = head_admin/super_admin/superuser
     ONLY — NOT plain role=="admin", NOT shop_admin) — browse every customer's collected
     delivery PII, sourced from ORDER rows (every checkout already snapshots the address
     onto its Order, so no extra table is read):
       - admin_list_delivery_info   : masked, searchable, paginated list (POST so the
         AuditLogMiddleware logs each browse)
       - admin_reveal_delivery_info : full unmasked record for one order (POST so each
         reveal is audited per-record)
     Consumed by: frontend /a/shop/customers (super-admin-only).

HOW IT CONNECTS
  - Models: SavedDeliveryProfile + Order.saved_profile FK (afc_shop/models.py).
  - Auth: afc_auth.validate_token (the Bearer caller) + afc_auth.require_head_admin (the
    super-admin gate, same one guarding the audit log).
  - Audit: admin_* are POST so afc_auth.middleware.AuditLogMiddleware records who browsed /
    revealed which record (sentence templates added in that middleware's _ACTION_SENTENCES).
"""
import logging

from django.db import transaction
from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token, require_head_admin
from .models import Order, SavedDeliveryProfile

logger = logging.getLogger(__name__)

# How many saved profiles a single user may keep. Soft cap enforced on create so the picker
# stays manageable and the table cannot be spammed.
MAX_PROFILES_PER_USER = 10

# The delivery snapshot fields shared by a checkout payload, an Order, and a profile.
_DELIVERY_FIELDS = (
    "first_name", "last_name", "email", "phone_number",
    "address", "city", "state", "postcode",
)
# postcode is optional everywhere (matches SavedDeliveryProfile.postcode blank=True).
_REQUIRED_PROFILE_FIELDS = (
    "first_name", "last_name", "email", "phone_number", "address", "city", "state",
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────
def _serialize_profile(p):
    """The full owner-facing shape of one saved profile (the owner sees their own PII)."""
    return {
        "id": p.id,
        "label": p.label,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "email": p.email,
        "phone_number": p.phone_number,
        "address": p.address,
        "city": p.city,
        "state": p.state,
        "postcode": p.postcode,
        "is_default": p.is_default,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _make_sole_default(user, profile):
    """Make `profile` the user's ONLY default. MySQL has no partial unique index, so default
    uniqueness is enforced here: clear is_default on the user's other rows, set it on this one.
    Wrapped by callers in a transaction so the two writes are atomic."""
    SavedDeliveryProfile.objects.filter(user=user).exclude(pk=profile.pk).update(is_default=False)
    if not profile.is_default:
        profile.is_default = True
        profile.save(update_fields=["is_default"])


def _paginate(data, default_limit=25, max_limit=100):
    """Parse limit/offset from a dict (request body for the admin POST reads) with safe
    bounds — limit defaults to 25, hard-capped at 100, offset floors at 0."""
    try:
        limit = int(data.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(data.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)


def _mask_email(email):
    """j***@example.com — keep the first character + the domain, hide the rest."""
    e = (email or "").strip()
    if "@" not in e:
        return e
    name, _, domain = e.partition("@")
    return f"{(name[0] if name else '')}***@{domain}"


def _mask_phone(phone):
    """***123 — show only the last 3 digits."""
    p = (phone or "").strip()
    return f"***{p[-3:]}" if len(p) > 3 else "***"


# ──────────────────────────────────────────────────────────────────────────────
# Checkout helpers (called by buy_now / stripe_buy_now)
# ──────────────────────────────────────────────────────────────────────────────
def persist_delivery_profile(user, data, label=""):
    """Create or refresh a SavedDeliveryProfile for `user` from a checkout payload `data`
    (the same 8 delivery fields the order carries). Returns the profile, or None if the
    required fields are missing (never raises into the checkout path). Enforces the per-user
    cap; the first profile a user saves becomes their default automatically."""
    if any(not (data.get(f) or "").strip() for f in _REQUIRED_PROFILE_FIELDS):
        return None  # incomplete payload -> nothing to save (checkout still proceeds)

    existing = SavedDeliveryProfile.objects.filter(user=user)
    # Reuse an identical existing entry (same address + phone) instead of piling up dupes.
    dupe = existing.filter(
        address=data.get("address", ""), phone_number=data.get("phone_number", ""),
    ).first()
    if dupe:
        return dupe
    if existing.count() >= MAX_PROFILES_PER_USER:
        # At the cap: do not create more (the buyer can prune on the manage page). The order
        # still goes through; we just don't save another reusable entry.
        return None

    is_first = not existing.exists()
    profile = SavedDeliveryProfile.objects.create(
        user=user,
        label=(label or data.get("delivery_label") or "")[:60],
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        email=data.get("email", ""),
        phone_number=data.get("phone_number", ""),
        address=data.get("address", ""),
        city=data.get("city", ""),
        state=data.get("state", ""),
        postcode=data.get("postcode", ""),
        is_default=is_first,  # first saved entry is the default
    )
    return profile


def attach_delivery_profile(order, user, data):
    """Best-effort link of `order` to a saved profile, set on order.saved_profile WITHOUT
    saving (the caller saves the order anyway). Either:
      - saved_profile_id present -> link to that existing (owner-scoped) profile, or
      - save_delivery_info truthy -> persist a new profile and link to it.
    Any failure is swallowed: a saved-profile hiccup must NEVER fail a checkout."""
    try:
        saved_id = data.get("saved_profile_id")
        if saved_id:
            profile = SavedDeliveryProfile.objects.filter(id=saved_id, user=user).first()
            if profile:
                order.saved_profile = profile
            return
        if data.get("save_delivery_info"):
            profile = persist_delivery_profile(user, data)
            if profile:
                order.saved_profile = profile
    except Exception:  # never break checkout over a saved-profile write
        logger.exception("attach_delivery_profile failed for user %s", getattr(user, "user_id", None))


# ──────────────────────────────────────────────────────────────────────────────
# A) USER saved delivery profiles (owner-scoped CRUD)
# ──────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    """Bearer -> validate_token. Returns (user, error_response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


@api_view(["GET"])
def list_my_delivery_profiles(request):
    """GET shop/delivery-profiles/ — the caller's saved delivery entries (default first).
    Powers the checkout picker (CartDetails.tsx) and the /profile/addresses manage page."""
    user, err = _auth_user(request)
    if err:
        return err
    profiles = SavedDeliveryProfile.objects.filter(user=user)
    return Response({"profiles": [_serialize_profile(p) for p in profiles]}, status=200)


@api_view(["POST"])
def create_delivery_profile(request):
    """POST shop/delivery-profiles/create/ — save a new delivery entry for the caller.
    Body: the 7 required delivery fields (+ optional postcode, label, is_default). The first
    entry a user saves becomes the default; passing is_default makes this the sole default."""
    user, err = _auth_user(request)
    if err:
        return err

    data = request.data
    missing = [f for f in _REQUIRED_PROFILE_FIELDS if not (data.get(f) or "").strip()]
    if missing:
        return Response({"message": f"Missing required field(s): {', '.join(missing)}."}, status=400)

    if SavedDeliveryProfile.objects.filter(user=user).count() >= MAX_PROFILES_PER_USER:
        return Response(
            {"message": f"You can save at most {MAX_PROFILES_PER_USER} delivery addresses."},
            status=400,
        )

    with transaction.atomic():
        is_first = not SavedDeliveryProfile.objects.filter(user=user).exists()
        profile = SavedDeliveryProfile.objects.create(
            user=user,
            label=(data.get("label") or "")[:60],
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            email=data.get("email", ""),
            phone_number=data.get("phone_number", ""),
            address=data.get("address", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            postcode=data.get("postcode", ""),
        )
        # Become the default if requested, or if it is the user's first saved entry.
        if data.get("is_default") or is_first:
            _make_sole_default(user, profile)

    return Response({"profile": _serialize_profile(profile)}, status=201)


@api_view(["POST"])
def update_delivery_profile(request):
    """POST shop/delivery-profiles/update/ — edit one of the caller's saved entries.
    Body: { profile_id, ...any delivery fields, label, is_default }. Cross-user id -> 404."""
    user, err = _auth_user(request)
    if err:
        return err

    profile = SavedDeliveryProfile.objects.filter(
        id=request.data.get("profile_id"), user=user,
    ).first()
    if not profile:
        return Response({"message": "Saved address not found."}, status=404)

    data = request.data
    # Apply only the delivery fields actually present (partial update). label too.
    for f in _DELIVERY_FIELDS:
        if f in data:
            setattr(profile, f, data.get(f) or "")
    if "label" in data:
        profile.label = (data.get("label") or "")[:60]

    with transaction.atomic():
        profile.save()
        if data.get("is_default"):
            _make_sole_default(user, profile)

    return Response({"profile": _serialize_profile(profile)}, status=200)


@api_view(["POST"])
def delete_delivery_profile(request):
    """POST shop/delivery-profiles/delete/ — remove one of the caller's saved entries.
    If the deleted entry was the default, the next most-recent entry becomes the default."""
    user, err = _auth_user(request)
    if err:
        return err

    profile = SavedDeliveryProfile.objects.filter(
        id=request.data.get("profile_id"), user=user,
    ).first()
    if not profile:
        return Response({"message": "Saved address not found."}, status=404)

    was_default = profile.is_default
    with transaction.atomic():
        profile.delete()
        if was_default:
            # Promote the next most-recent entry so the user always has a default if any remain.
            nxt = SavedDeliveryProfile.objects.filter(user=user).order_by("-updated_at").first()
            if nxt:
                _make_sole_default(user, nxt)

    return Response({"message": "Saved address deleted."}, status=200)


@api_view(["POST"])
def set_default_delivery_profile(request):
    """POST shop/delivery-profiles/set-default/ — make one of the caller's entries the default."""
    user, err = _auth_user(request)
    if err:
        return err

    profile = SavedDeliveryProfile.objects.filter(
        id=request.data.get("profile_id"), user=user,
    ).first()
    if not profile:
        return Response({"message": "Saved address not found."}, status=404)

    with transaction.atomic():
        _make_sole_default(user, profile)

    return Response({"message": "Default address updated."}, status=200)


# ──────────────────────────────────────────────────────────────────────────────
# B) SUPER-ADMIN delivery-info view (require_head_admin — head_admin/super_admin ONLY)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_list_delivery_info(request):
    """POST shop/admin/delivery-info/ — masked, searchable, paginated list of collected
    customer delivery PII, sourced from ORDER rows. POST (not GET) so AuditLogMiddleware
    records every super-admin browse. require_head_admin (head_admin/super_admin/superuser
    only). Body: { q?, date_from?, date_to?, limit?<=100 def25, offset? }.
    Consumed by: frontend /a/shop/customers (super-admin-only)."""
    admin, err = require_head_admin(request)
    if err:
        return err

    data = request.data or {}
    qs = Order.objects.select_related("user", "saved_profile").order_by("-created_at")

    q = (data.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(email__icontains=q) | Q(phone_number__icontains=q)
            | Q(address__icontains=q) | Q(city__icontains=q) | Q(state__icontains=q)
        )
    if data.get("date_from"):
        qs = qs.filter(created_at__date__gte=data["date_from"])
    if data.get("date_to"):
        qs = qs.filter(created_at__date__lte=data["date_to"])

    total_count = qs.count()
    limit, offset = _paginate(data)
    rows = qs[offset:offset + limit]

    results = [{
        "order_id": o.id,
        "user_id": o.user.user_id if o.user_id else None,
        "username": o.user.username if o.user_id else None,
        "first_name": o.first_name,
        "last_name": o.last_name,
        # PII masked in the list — full values only via the audited reveal endpoint below.
        "email": _mask_email(o.email),
        "phone_number": _mask_phone(o.phone_number),
        "city": o.city,
        "state": o.state,
        "status": o.status,
        "total": str(o.total),
        "saved_profile_id": o.saved_profile_id,
        "created_at": o.created_at,
    } for o in rows]

    return Response({
        "results": results,
        "total_count": total_count,
        "has_more": offset + limit < total_count,
        "next_offset": offset + limit if offset + limit < total_count else None,
    }, status=200)


@api_view(["POST"])
def admin_reveal_delivery_info(request):
    """POST shop/admin/delivery-info/reveal/ — the FULL unmasked delivery record for one
    order. POST so each reveal is audited per-record. require_head_admin. Body: { order_id }.
    Consumed by: the "Reveal full details" control on /a/shop/customers."""
    admin, err = require_head_admin(request)
    if err:
        return err

    order = (
        Order.objects.select_related("user", "saved_profile")
        .filter(id=request.data.get("order_id")).first()
    )
    if not order:
        return Response({"message": "Order not found."}, status=404)

    return Response({"record": {
        "order_id": order.id,
        "user_id": order.user.user_id if order.user_id else None,
        "username": order.user.username if order.user_id else None,
        "first_name": order.first_name,
        "last_name": order.last_name,
        "email": order.email,
        "phone_number": order.phone_number,
        "address": order.address,
        "city": order.city,
        "state": order.state,
        "postcode": order.postcode,
        "status": order.status,
        "total": str(order.total),
        "saved_profile_id": order.saved_profile_id,
        "created_at": order.created_at,
    }}, status=200)
