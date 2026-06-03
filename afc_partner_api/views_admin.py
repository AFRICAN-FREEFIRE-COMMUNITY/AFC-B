# afc_partner_api/views_admin.py
# ──────────────────────────────────────────────────────────────────────────────
# AFC-staff provisioning + oversight endpoints for the Partner Data API.
#
# This is the surface the platform team (head_admin / partner_admin) uses to stand
# a Partner up and configure EVERYTHING the read API (views_partner.py) later
# enforces: the partner's scope, its 14 resource/field toggles, its rotatable API
# keys, and the per-event publish gate. The read API trusts these views completely —
# a partner can only ever see what an admin granted here.
#
# Convention note (why the code looks like this): this module deliberately mirrors
# afc_organizers/views_admin.py (the closest existing admin surface) and, through it,
# the original hand in afc_team/views.py:
#   * function-based @api_view views, one job each;
#   * USER-SESSION auth done inline by reading the Authorization header and calling
#     validate_token (imported the SAME way afc_team/views.py imports it) — this is
#     the human AFC-staff surface, NOT the X-API-Key partner surface (auth.py);
#   * a single _require_partner_admin gate every view calls first (DRY, and the
#     header-parse/token/role logic lives in exactly one place);
#   * inline dict serialization in each view (no serializers.py);
#   * Response({...}, status=status.HTTP_*) for every return.
#
# SECURITY INVARIANTS this module upholds (spec §9 admin surface):
#   * 403 GATE: every endpoint requires head_admin OR partner_admin (least privilege).
#   * KEY SECRECY: issue_key returns the plaintext key EXACTLY ONCE; the stored row
#     holds only prefix + sha256 hash. No other endpoint ever returns the plaintext
#     (or the hash) — get_partner exposes key METADATA only.
#   * WHITELIST EDIT: edit_partner accepts ONLY PARTNER_TOGGLE_FIELDS + the scope
#     id-lists + allow_all_native_afc; any other key is a 400 (a typo or a malicious
#     body can never set an arbitrary Partner attribute, e.g. status or contact_email).
#
# The coordinator owns route mounting — this file ONLY defines view functions; the
# routes live in admin_urls.py. Full spec: WEBSITE/tasks/partner-api-design.md (§9).
# ──────────────────────────────────────────────────────────────────────────────
from django.utils.text import slugify
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views — import it the SAME way afc_team/views.py
# and afc_organizers/views_admin.py do (confirmed import path).
from afc_auth.views import validate_token

from . import auth
from .models import (
    Partner, PartnerApiKey, PARTNER_TOGGLE_FIELDS,
)


# ──────────────────────────────────────────────────────────────────────────────
# Auth gate
# ──────────────────────────────────────────────────────────────────────────────
# Partner-admin roles. A user holding EITHER of these (as a granular UserRoles row)
# may manage partners. head_admin is the catch-all platform admin; partner_admin is
# the dedicated grant for staff who only run the partner program.
PARTNER_ADMIN_ROLES = ("head_admin", "partner_admin")


def _is_partner_admin(user) -> bool:
    """True for AFC staff entitled to manage partners (head_admin / partner_admin).

    The role lives on the related Roles row, reached through the UserRoles join, so
    we filter ``role__role_name__in`` — NEVER ``role_name__in`` (UserRoles itself has
    no role_name column; that field is on Roles).

    DELIBERATE DIVERGENCE from afc_organizers/permissions.py:is_platform_org_admin:
    that reference gates on TWO factors — ``user.role == "admin"`` AND a granular
    UserRoles row. We intentionally gate on the granular row ALONE here, per the
    Task 7 plan (partner-api-plan.md §"Admin management endpoints", Step 2-3), which
    specifies the helper as ``user.userroles.filter(...).exists()`` with no coarse
    ``User.role`` predicate. Rationale: the granular ``UserRoles`` table is the
    authoritative, fine-grained grant for the partner program (head_admin /
    partner_admin are issued there), so holding one of those rows IS the entitlement
    to manage partners — the coarse ``User.role`` tier is not a prerequisite for it.
    Granting head_admin / partner_admin without the coarse ``admin`` tier is the
    grantor's deliberate choice, and it is honored here.
    """
    return bool(user) and \
        user.userroles.filter(role__role_name__in=PARTNER_ADMIN_ROLES).exists()


# Every endpoint here is partner-admin only, so the header parse + token validation +
# role check is identical across all of them. We resolve it once and let each view
# bail early on the returned Response.
#
# Returns (user, error_response):
#   * (user, None)  → authenticated partner-admin, proceed;
#   * (None, resp)  → stop and return `resp` (400 missing/malformed header,
#                     401 bad/expired token, 403 not a partner admin).
def _require_partner_admin(request):
    # Read the raw Authorization header exactly like the original hand does.
    session_token = request.headers.get("Authorization")

    # 400 when the header is missing entirely — it's a malformed request, not yet an
    # auth failure (matches afc_organizers/views_admin.py wording/shape).
    if not session_token:
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 400 when the scheme is wrong — the token format is the caller's mistake.
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Strip the "Bearer " prefix and resolve the session → user.
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)

    # 401 when the token does not resolve to a live session/user.
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # 403 GATE: a valid login that lacks the partner-admin role is refused.
    if not _is_partner_admin(user):
        return None, Response(
            {"message": "You do not have permission to manage partners."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return user, None


# ──────────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ──────────────────────────────────────────────────────────────────────────────
# _partner_or_404 centralises the "load this partner or 404" lookup so detail/edit/
# suspend/issue-key views all behave identically (404 message lives in one place).
#
# Returns (partner, error_response): exactly one is non-None.
def _partner_or_404(slug):
    partner = Partner.objects.filter(slug=slug).first()
    if not partner:
        return None, Response(
            {"message": "Partner not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return partner, None


def _serialize_partner_summary(partner):
    """Lean partner row for the list view: identity + standing + an active-key count.

    The key count uses the reverse relation so we never hand-join; it counts only
    ACTIVE keys (revoked ones must not inflate the figure the admin sees)."""
    return {
        "partner_id": partner.partner_id,
        "slug": partner.slug,
        "name": partner.name,
        "status": partner.status,
        "active_key_count": partner.api_keys.filter(status="active").count(),
        "created_at": partner.created_at.isoformat() if partner.created_at else None,
    }


def _serialize_partner_detail(partner):
    """Full partner config for the detail view: the summary, plus every toggle the FE
    renders as a switch, the scope id-lists, and contact_email (INTERNAL — this is the
    admin surface, so it is fine here; the partner-facing serializer firewall never
    emits it). Key plaintext/hash are NEVER included — keys are listed separately as
    metadata-only by _serialize_key."""
    out = _serialize_partner_summary(partner)
    # Every resource + field toggle, straight off the row so the FE can bind switches.
    for f in PARTNER_TOGGLE_FIELDS:
        out[f] = getattr(partner, f)
    # Scope: the native-AFC switch + the two grant id-lists (the FE multiselects).
    out["allow_all_native_afc"] = partner.allow_all_native_afc
    out["allowed_events"] = list(partner.allowed_events.values_list("pk", flat=True))
    out["allowed_organizations"] = list(
        partner.allowed_organizations.values_list("pk", flat=True))
    # contact_email is admin-only metadata (never crosses the partner firewall).
    out["contact_email"] = partner.contact_email
    return out


def _serialize_key(key):
    """Metadata-only view of a PartnerApiKey. The plaintext is unrecoverable (only the
    hash is stored) and the hash itself is internal, so NEITHER is ever returned — the
    admin sees the prefix (to identify the key), its label, status, and audit stamps."""
    return {
        "key_id": key.key_id,
        "key_prefix": key.key_prefix,          # safe handle — not the secret
        "label": key.label,
        "status": key.status,
        "rate_limit_per_min": key.rate_limit_per_min,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "created_at": key.created_at.isoformat() if key.created_at else None,
    }


# Small shared paginator (mirrors afc_organizers/views_admin._paginate). List endpoints
# accept ?limit (default 25, max 100) and ?offset, returning {results, total_count,
# has_more} so the admin table never loads the full set at once (best-practice §10).
def _paginate(request, queryset):
    try:
        limit = int(request.GET.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    # Clamp to sane bounds: limit in [1, 100], offset non-negative.
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


# ──────────────────────────────────────────────────────────────────────────────
# 1) create_partner  (POST partners/admin/create/)
# ──────────────────────────────────────────────────────────────────────────────
# Provision a brand-new Partner. Only ``name`` is required; everything else (scope,
# toggles, keys) is configured afterward via edit_partner / issue_key — a fresh
# partner starts with every toggle OFF (least privilege) and an "active" status but
# can read nothing until an admin grants scope + flips toggles.
@api_view(["POST"])
def create_partner(request):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"message": "Partner name is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Optional contact email (internal metadata; never crosses the partner firewall).
    contact_email = request.data.get("contact_email") or ""

    # Derive a unique slug: slugify the name, then suffix "-2", "-3", … until free.
    base_slug = slugify(name) or "partner"
    slug = base_slug
    suffix = 2
    while Partner.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    # created_by records WHICH AFC admin provisioned the partner (audit trail).
    partner = Partner.objects.create(
        name=name,
        slug=slug,
        contact_email=contact_email,
        created_by=user,
    )

    return Response(
        {"message": "Partner created successfully.", "partner": _serialize_partner_summary(partner)},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2) list_partners  (GET partners/admin/list/)
# ──────────────────────────────────────────────────────────────────────────────
# Oversight list of every partner with its active-key count. Supports ?status= (exact)
# and ?search= (name OR slug, case-insensitive). Paginated.
@api_view(["GET"])
def list_partners(request):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    # All partners, newest first (most recently provisioned on top).
    qs = Partner.objects.all().order_by("-created_at")

    # Optional exact status filter (active / suspended).
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    # Optional fuzzy search across name OR slug.
    search = request.GET.get("search")
    if search:
        from django.db.models import Q
        qs = qs.filter(Q(name__icontains=search) | Q(slug__icontains=search))

    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_partner_summary(p) for p in page]

    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3 + 4) partner_detail  (GET = detail, PATCH = edit) at partners/admin/<slug>/
# ──────────────────────────────────────────────────────────────────────────────
# GET detail and PATCH edit share ONE route (the spec maps both to /<slug>/), so a
# single @api_view(["GET", "PATCH"]) dispatcher routes by verb (DRF 405s anything
# else). It delegates to _get_partner / _edit_partner below so each job stays its own
# readable function. (Mirrors the verb-routed single route idiom used in
# afc_organizers/views_design.design_requests.)
@api_view(["GET", "PATCH"])
def partner_detail(request, slug):
    if request.method == "PATCH":
        return _edit_partner(request, slug)
    return _get_partner(request, slug)


# get_partner — full config for one partner: the detail dict (identity + all 14
# toggles + scope id-lists + internal contact_email) plus its keys as METADATA only.
# The plaintext secret is unrecoverable and the hash is internal — neither is ever
# returned here.
def _get_partner(request, slug):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    partner, err = _partner_or_404(slug)
    if err:
        return err

    # Every key (active + revoked) so the admin sees full history; metadata only.
    keys = [_serialize_key(k) for k in partner.api_keys.order_by("-created_at")]

    return Response({
        "partner": _serialize_partner_detail(partner),
        "keys": keys,
    }, status=status.HTTP_200_OK)


# edit_partner — whitelist-validated partial update of a partner's scope + toggles.
# The whitelist IS the security boundary: only the 14 PARTNER_TOGGLE_FIELDS, the two
# scope id-lists (allowed_events / allowed_organizations), and allow_all_native_afc
# may be set. ANY other key in the body is a 400 — so a typo or a malicious payload
# can never set an attribute it shouldn't (e.g. status to bypass suspend, or
# contact_email). True PATCH semantics: only keys actually present in the body are
# touched.
def _edit_partner(request, slug):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    partner, err = _partner_or_404(slug)
    if err:
        return err

    # The complete set of keys the body may carry. Anything outside it is rejected.
    SCOPE_FIELDS = ("allowed_events", "allowed_organizations", "allow_all_native_afc")
    allowed_keys = set(PARTNER_TOGGLE_FIELDS) | set(SCOPE_FIELDS)

    # Reject the WHOLE request if any unknown key is present (fail closed, don't
    # silently ignore — a rejected field tells the admin their payload was wrong).
    unknown = set(request.data.keys()) - allowed_keys
    if unknown:
        return Response(
            {"message": f"Unknown field(s): {', '.join(sorted(unknown))}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── boolean toggles (the 14 PARTNER_TOGGLE_FIELDS) + the native-AFC switch ──
    # bool() coerces whatever truthy/falsy value the client sent into a real boolean.
    for f in PARTNER_TOGGLE_FIELDS:
        if f in request.data:
            setattr(partner, f, bool(request.data[f]))
    if "allow_all_native_afc" in request.data:
        partner.allow_all_native_afc = bool(request.data["allow_all_native_afc"])

    partner.save()

    # ── scope id-lists: replace the M2M set with the supplied ids (true PATCH:
    # only set when the key is present, so omitting it leaves the grants untouched). ──
    if "allowed_events" in request.data:
        ids = request.data.get("allowed_events") or []
        if not isinstance(ids, list):
            return Response({"message": "allowed_events must be a list of event ids."},
                            status=status.HTTP_400_BAD_REQUEST)
        partner.allowed_events.set(ids)
    if "allowed_organizations" in request.data:
        ids = request.data.get("allowed_organizations") or []
        if not isinstance(ids, list):
            return Response({"message": "allowed_organizations must be a list of organization ids."},
                            status=status.HTTP_400_BAD_REQUEST)
        partner.allowed_organizations.set(ids)

    return Response(
        {"message": "Partner updated successfully.", "partner": _serialize_partner_detail(partner)},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5) suspend_partner  (POST partners/admin/<slug>/suspend/)
# ──────────────────────────────────────────────────────────────────────────────
# Reversible freeze / unfreeze. body {suspend: bool}. authenticate_partner already
# refuses every key of a suspended partner, so this is the kill-switch for a partner's
# entire API access without revoking each key individually.
@api_view(["POST"])
def suspend_partner(request, slug):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    partner, err = _partner_or_404(slug)
    if err:
        return err

    # Truthy `suspend` → freeze; falsy → reactivate.
    suspend = request.data.get("suspend")
    partner.status = "suspended" if suspend else "active"
    partner.save(update_fields=["status"])

    return Response(
        {"message": "Partner status updated.", "status": partner.status},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6) issue_key  (POST partners/admin/<slug>/keys/)
# ──────────────────────────────────────────────────────────────────────────────
# Mint a new API key for a partner. auth.generate_key() returns (full, prefix, hash);
# we persist ONLY the prefix + hash and return the FULL plaintext exactly ONCE in this
# response. The plaintext is never stored and can never be re-fetched — this is the
# only moment the admin can copy it (the FE shows a "you won't see this again" dialog).
@api_view(["POST"])
def issue_key(request, slug):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    partner, err = _partner_or_404(slug)
    if err:
        return err

    # Optional human label + an optional per-key rate limit override (defaults to the
    # model default of 60/min when omitted or junk).
    label = (request.data.get("label") or "").strip()
    rate_limit = request.data.get("rate_limit_per_min")
    try:
        rate_limit = int(rate_limit) if rate_limit is not None else 60
    except (TypeError, ValueError):
        rate_limit = 60
    rate_limit = max(1, rate_limit)

    # Generate the credential, store ONLY prefix + hash (the secret is discarded after
    # hashing inside generate_key), and stamp WHICH admin issued it (audit trail).
    full_key, prefix, key_hash = auth.generate_key()
    key = PartnerApiKey.objects.create(
        partner=partner,
        key_prefix=prefix,
        key_hash=key_hash,
        label=label,
        rate_limit_per_min=rate_limit,
        created_by=user,
    )

    # Return the plaintext ONCE alongside the key's metadata. This is the ONLY place
    # `api_key` (the full plaintext) is ever present in any response.
    return Response({
        "message": "API key issued. Copy it now — it will not be shown again.",
        "api_key": full_key,
        "key": _serialize_key(key),
    }, status=status.HTTP_201_CREATED)


# ──────────────────────────────────────────────────────────────────────────────
# 7) revoke_key  (POST partners/admin/keys/<key_id>/revoke/)
# ──────────────────────────────────────────────────────────────────────────────
# Permanently disable a single key. authenticate_partner only matches status="active"
# rows, so a revoked key fails auth immediately (the read API 401s with it). Keyed by
# key_id (not slug) because a key is the addressable thing here; idempotent — revoking
# an already-revoked key is a no-op success.
@api_view(["POST"])
def revoke_key(request, key_id):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    key = PartnerApiKey.objects.filter(key_id=key_id).first()
    if not key:
        return Response({"message": "API key not found."}, status=status.HTTP_404_NOT_FOUND)

    # Idempotent: flip to revoked (re-revoking is harmless).
    key.status = "revoked"
    key.save(update_fields=["status"])

    return Response({"message": "API key revoked."}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 8) publish_event  (POST partners/admin/events/<event_slug>/publish/)
# ──────────────────────────────────────────────────────────────────────────────
# Flip Event.partner_published — the gate the read API's scope predicate applies
# FIRST (scope.py): no partner, however broadly scoped, can see an event until an AFC
# admin publishes it here. body {published: bool}. Imported locally to avoid a
# module-level cross-app import cycle (afc_tournament_and_scrims doesn't import us, and
# we keep it that way).
@api_view(["POST"])
def publish_event(request, event_slug):
    # Auth + partner-admin gate.
    user, err = _require_partner_admin(request)
    if err:
        return err

    from afc_tournament_and_scrims.models import Event
    event = Event.objects.filter(slug=event_slug).first()
    if not event:
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    # Truthy `published` → reachable via the partner API; falsy → withdrawn.
    published = bool(request.data.get("published"))
    event.partner_published = published
    event.save(update_fields=["partner_published"])

    return Response(
        {"message": "Event publish state updated.", "partner_published": event.partner_published},
        status=status.HTTP_200_OK,
    )
