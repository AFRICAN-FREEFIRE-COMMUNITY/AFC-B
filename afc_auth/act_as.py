# afc_auth/act_as.py
# ──────────────────────────────────────────────────────────────────────────────
# Super-admin "act-as" (god-mode) resolver.
#
# WHAT THIS IS
#   Lets a SUPER ADMIN step INTO any organizer or vendor dashboard and operate as that
#   tenant: create / edit / delete events and products, and see everything the tenant
#   sees. "Super admin" here = head_admin / super_admin / Django superuser ONLY (owner
#   decision 2026-06-29 — deliberately NOT organizer_admin and NOT plain role=="admin").
#
# HOW IT WORKS (the "act-as" header)
#   The frontend axios interceptor (AuthContext) attaches, on EVERY request, the cookies
#   act_as_org / act_as_vendor as request headers:
#       X-Act-As-Org:    <organization slug>
#       X-Act-As-Vendor: <vendor id>
#   set when the admin clicks "Manage as this organizer / vendor" in the admin lists, and
#   cleared on "Exit". (These custom headers are added to CORS_ALLOW_HEADERS in
#   afc/settings.py so the browser preflight lets them through cross-origin.)
#
#   The header only SELECTS a target — it grants NOTHING on its own. Every resolver below
#   verifies is_god_mode_admin(user) FIRST; for anyone else the header is a silent no-op
#   (returns None), so a spoofed header from a normal user can never cross a tenant
#   boundary. Resource ownership stays the TARGET org/vendor; AuditLogMiddleware records
#   the REAL admin as the actor plus an `acting_as` tag (see middleware.py).
#
# WHO CALLS THIS
#   • afc_organizers/views_organizer.get_organization + get_my_organizations — the two
#     org-shell READ endpoints. (Every org MUTATION already bypasses for platform admins
#     via afc_organizers.permissions.org_can_event, so mutations need no change here.)
#   • afc_shop/vendors._require_active_vendor (vendor product CRUD) and
#     afc_shop/fulfilment.vendor_my_orders (the vendor order queue).
#     NOTE: the bank / payout gates (afc_shop/connect.py, afc_shop/paystack_payout.py)
#     intentionally do NOT call into here — bank / payout is OUT of god-mode scope
#     (owner decision 2026-06-29: products + orders yes, bank/payout no).
# ──────────────────────────────────────────────────────────────────────────────

# The ONLY granular roles allowed to act-as. head_admin / super_admin are the top
# platform admins (the same set gated by afc_auth.views.require_head_admin). Plus any
# Django superuser. organizer_admin and plain role=="admin" are intentionally excluded.
_GOD_MODE_ROLES = frozenset({"head_admin", "super_admin"})

# Header names the frontend interceptor sets from the act_as_org / act_as_vendor cookies.
ACT_AS_ORG_HEADER = "X-Act-As-Org"
ACT_AS_VENDOR_HEADER = "X-Act-As-Vendor"


def is_god_mode_admin(user) -> bool:
    """True ONLY for the super admins allowed to act-as any tenant: head_admin /
    super_admin / Django superuser. Everyone else (organizer_admin, plain role=="admin",
    players, anonymous) is False — they can never use the act-as headers."""
    if not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    # Lazy import keeps this module free of any import-time coupling to the large
    # afc_auth.views module (afc_shop / afc_organizers import act_as at load time, and
    # views imports plenty — importing it lazily here avoids any circular-import risk).
    from afc_auth.views import _user_role_names
    return bool(_user_role_names(user) & _GOD_MODE_ROLES)


def _header(request, name):
    """Case-insensitive header read (Django normalizes request.headers) with whitespace
    trimmed; an empty/missing value becomes None."""
    return (request.headers.get(name) or "").strip() or None


def resolve_acting_org(request, user):
    """The Organization a god-mode admin is impersonating via X-Act-As-Org, or None.

    Verifies god-mode FIRST (the header is inert for everyone else), then resolves the
    slug to a real org. Returns None on: no header / caller not god-mode / unknown slug.
    Callers treat a non-None result as "this super admin may act as the owner of this
    org" — they must NOT have already granted access on the header alone."""
    if not is_god_mode_admin(user):
        return None
    slug = _header(request, ACT_AS_ORG_HEADER)
    if not slug:
        return None
    # Lazy import: afc_organizers imports this module, so import its model at call time.
    from afc_organizers.models import Organization
    return Organization.objects.filter(slug=slug).first()


def resolve_acting_vendor(request, user):
    """The Vendor a god-mode admin is impersonating via X-Act-As-Vendor, or None.

    Same god-mode-first rule as resolve_acting_org. Returns the Vendor regardless of its
    active / suspended status — an admin may need to operate a suspended vendor's shop to
    fix it; the product-CRUD draft state machine still applies on top. Returns None on:
    no header / caller not god-mode / non-integer id / unknown vendor."""
    if not is_god_mode_admin(user):
        return None
    raw = _header(request, ACT_AS_VENDOR_HEADER)
    if not raw:
        return None
    try:
        vendor_id = int(raw)
    except (TypeError, ValueError):
        return None
    # Lazy import: afc_shop imports this module, so import its model at call time.
    from afc_shop.models import Vendor
    return Vendor.objects.filter(pk=vendor_id).first()


def acting_as_tag(request):
    """Small dict for AuditLogMiddleware -> metadata['acting_as'], or None when the
    request carries no act-as header. Records the raw target the actor asked to act as
    (org slug / vendor id) so the audit row shows the impersonation alongside the real
    actor. This is a BREADCRUMB, not a gate — the resolvers above are what actually
    enforce access; this only annotates the log."""
    org = _header(request, ACT_AS_ORG_HEADER)
    vendor = _header(request, ACT_AS_VENDOR_HEADER)
    if not org and not vendor:
        return None
    tag = {}
    if org:
        tag["org"] = org[:120]
    if vendor:
        tag["vendor"] = vendor[:40]
    return tag
