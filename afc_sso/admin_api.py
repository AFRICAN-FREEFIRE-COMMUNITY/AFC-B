# ──────────────────────────────────────────────────────────────────────────────
# AFC-staff provisioning + oversight endpoints for "Sign in with AFC".
#
# WHAT THIS IS: the surface AFC staff (head_admin / partner_admin) use to stand a
# partner SSO application up and to decide, per partner, EXACTLY which player data
# that partner may ever receive. It replaces the interim Django admin screen in
# afc_sso/admin.py with a real API the Next.js admin dashboard can drive.
#
# WHERE IT IS CONSUMED: the admin page titled "API Keys",
#   frontend/app/(a)/a/partners/page.tsx  →  "Sign in with AFC" tab
#   frontend/app/(a)/a/partners/_components/SsoAppsPanel.tsx
#   frontend/lib/sso.ts                    (the typed client for every route below)
# SSO partner apps live beside the Partner Data API partners because they are the
# same idea for a different product: an outside org, approved by AFC, reading a
# deliberately narrow slice of AFC data.
#
# CONVENTION NOTE (why the code looks like this): this module deliberately mirrors
# afc_partner_api/views_admin.py, the closest existing admin surface, and through it
# the original hand in afc_team/views.py:
#   * function-based @api_view views, one job each;
#   * USER-SESSION auth done inline by reading the Authorization header and calling
#     afc_auth.views.validate_token, the same preamble as afc_sso/api.py;
#   * a single _require_sso_admin gate every view calls first;
#   * inline dict serialization in each view (no serializers.py);
#   * Response({...}, status=status.HTTP_*) for every return.
#
# SECURITY INVARIANTS THIS MODULE UPHOLDS
#   * 403 GATE: every endpoint requires head_admin OR partner_admin. Nothing here is
#     reachable by an ordinary player, an organizer or a sponsor.
#   * SECRET SECRECY: django-oauth-toolkit HASHES client_secret on save
#     (oauth2_provider.models.ClientSecretField.pre_save), so the plaintext exists for
#     exactly one moment: while we hold it in memory before saving. create and
#     rotate_secret return it ONCE and nothing else ever returns it, logs it, or can
#     recover it. There is no "show me the secret again" endpoint because there cannot
#     be one.
#   * WHITELIST EDIT: update accepts ONLY the identity fields and the eight share_*
#     toggles. `status` is deliberately NOT editable here - suspend/unsuspend owns it -
#     and neither is client_id, client_secret, client_type, authorization_grant_type,
#     algorithm or skip_authorization. A typo or a hostile body can never reach them.
#   * TOGGLES DEFAULT OFF: a newly created application shares NOTHING. Every share_*
#     field defaults False on the model and this module never flips one implicitly.
#
# HOW A TOGGLE ACTUALLY TAKES EFFECT (the end-to-end trace):
#   admin flips share_email here
#     → AFCSSOApplication.share_email = True                     (afc_sso/models.py)
#     → allowed_scopes() now contains "email"                    (models.TOGGLE_TO_SCOPE)
#     → the partner may REQUEST the email scope at /sso/authorize/ (afc_sso/views.py
#       refuses any scope beyond allowed_scopes())
#     → the player still has to approve it on the consent screen
#     → afc_sso/claims.py releases the claim only if all four gates agree.
# So this API grants a CEILING, never a release. Turning a toggle off lowers the
# ceiling immediately for every future token.
# ──────────────────────────────────────────────────────────────────────────────
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import get_application_model
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from .models import SSO_FIELD_TOGGLES, TOGGLE_TO_SCOPE

Application = get_application_model()  # AFCSSOApplication, via OAUTH2_PROVIDER_APPLICATION_MODEL


# ──────────────────────────────────────────────────────────────────────────────
# Auth gate
# ──────────────────────────────────────────────────────────────────────────────
# The same two roles that manage the Partner Data API manage SSO partners: the two
# products share one admin page and one approval process. head_admin is the catch-all
# platform admin; partner_admin is the dedicated grant for staff who run the partner
# program.
SSO_ADMIN_ROLES = ("head_admin", "partner_admin")


def _is_sso_admin(user) -> bool:
    """True for AFC staff entitled to manage SSO partner applications.

    The role name lives on the related Roles row, reached through the UserRoles join,
    so we filter ``role__role_name__in`` - NEVER ``role_name__in`` (UserRoles itself has
    no role_name column; that field is on Roles). Identical predicate to
    afc_partner_api.views_admin._is_partner_admin, deliberately: the same staff manage
    both halves of the partner program from the same page.
    """
    return bool(user) and \
        user.userroles.filter(role__role_name__in=SSO_ADMIN_ROLES).exists()


def _require_sso_admin(request):
    """Header parse + token validation + role check, resolved once for every view.

    Returns (user, error_response):
      * (user, None)  → authenticated SSO admin, proceed;
      * (None, resp)  → stop and return `resp` (400 missing/malformed header,
                        401 bad/expired token, 403 not an SSO admin).

    Status codes and wording match afc_partner_api.views_admin._require_partner_admin
    and afc_sso.api._require_player so the frontend's one error-toast idiom covers all
    of them.
    """
    session_token = request.headers.get("Authorization")

    # 400 when the header is missing entirely - a malformed request, not yet an auth failure.
    if not session_token:
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 400 when the scheme is wrong - the token format is the caller's mistake.
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from afc_auth.views import validate_token  # local import: avoids an app-loading cycle

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # 403 GATE: a valid login that lacks the role is refused.
    if not _is_sso_admin(user):
        return None, Response(
            {"message": "You do not have permission to manage sign-in partners."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return user, None


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────
# Everything a partner tells us that ends up in a browser redirect or an outbound
# request is validated here rather than trusted. A redirect URI in particular is the
# field that turns a mistake into a phishing tool, so it is the strictest.
_url_validator = URLValidator(schemes=["http", "https"])

# http:// is allowed ONLY for these hosts, so a partner can develop against their own
# machine. Everything a real player is ever redirected to must be https.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _require_object_body(request):
    """Returns an error Response when the body is not a JSON object, else None.

    Every mutating view below reads the body with .get()/.keys(). DRF hands those
    straight through, so a body that parsed as a list or a bare string would raise an
    AttributeError deep inside the view and surface as a 500. One check up front turns
    that into the 400 it actually is.
    """
    if not hasattr(request.data, "get") or not hasattr(request.data, "keys"):
        return Response(
            {"message": "Request body must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def _clean_url(value, field_label, require_https=True):
    """Validate one optional absolute URL. Returns (cleaned, error_message).

    An empty value is legal for every optional URL field on the model (they are all
    blank=True), and comes back as "" so the caller can store it as-is.
    """
    value = (value or "").strip()
    if not value:
        return "", None
    try:
        _url_validator(value)
    except ValidationError:
        return None, f"{field_label} must be a valid URL."
    if require_https and not value.lower().startswith("https://"):
        host = value.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host not in _LOCAL_HOSTS:
            return None, f"{field_label} must use https."
    return value, None


def _clean_redirect_uris(value):
    """Validate the space-separated redirect_uris list. Returns (cleaned, error_message).

    django-oauth-toolkit stores redirect URIs as one whitespace-separated string and
    matches the incoming redirect_uri against that list exactly (afc_sso/views.py refuses
    anything that does not match, without redirecting). We accept either a list or a
    string from the client and normalise to the single space-separated string the library
    expects, so the admin UI can use a textarea with one URI per line.

    At least one URI is required: an application with none can never complete a sign-in.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value]
    else:
        parts = str(value or "").split()
    parts = [p for p in parts if p]

    if not parts:
        return None, "At least one redirect URI is required."

    for uri in parts:
        cleaned, err = _clean_url(uri, f"Redirect URI '{uri}'")
        if err:
            return None, err
    return " ".join(parts), None


# ──────────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ──────────────────────────────────────────────────────────────────────────────
def _application_or_404(application_id):
    """Load one application or hand back the 404 Response. Exactly one is non-None."""
    application = Application.objects.filter(pk=application_id).first()
    if not application:
        return None, Response(
            {"message": "Sign-in partner not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return application, None


def _serialize_summary(application):
    """Lean row for the list table: identity, standing, client id, and how many of the
    eight data toggles are on (so an admin scanning the table can see at a glance which
    partner has been granted the most)."""
    return {
        "application_id": application.pk,
        "name": application.name,
        "display_name": application.display_name or application.name,
        "status": application.status,
        # The client id is PUBLIC by design (it travels in the authorize URL). The secret
        # is not, and never appears in any serializer.
        "client_id": application.client_id,
        "shared_field_count": sum(
            1 for f in SSO_FIELD_TOGGLES if getattr(application, f, False)
        ),
        "created_at": application.created.isoformat() if application.created else None,
    }


def _serialize_detail(application):
    """Full config for the edit form: the summary plus the identity URLs, the redirect
    URI list, and every share_* toggle as a boolean keyed by its field name.

    `scopes` is the derived read-only view of the toggles: the OIDC scope set this
    partner is permitted to ask for, straight from AFCSSOApplication.allowed_scopes().
    It is returned so the admin UI can show what the toggles actually amount to without
    reimplementing the mapping. client_secret is NOT here and cannot be - only its hash
    is stored.
    """
    out = _serialize_summary(application)
    out["logo_url"] = application.logo_url
    out["homepage_url"] = application.homepage_url
    out["deletion_webhook_url"] = application.deletion_webhook_url
    # The library stores these space-separated; the admin UI edits them one per line.
    out["redirect_uris"] = application.redirect_uris
    for f in SSO_FIELD_TOGGLES:
        out[f] = getattr(application, f, False)
    out["scopes"] = sorted(application.allowed_scopes())
    return out


def _paginate(request, queryset):
    """?limit (default 25, max 100) + ?offset → (page, total_count, has_more).

    Same shared paginator as afc_partner_api.views_admin._paginate, so the admin table
    binds to one response shape across both tabs of the API Keys page.
    """
    try:
        limit = int(request.GET.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


# ──────────────────────────────────────────────────────────────────────────────
# 1 + 2) sso_applications  (GET = list, POST = create) at sso/admin/apps/
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "POST"])
@authentication_classes([])
def sso_applications(request):
    """List every "Sign in with AFC" partner application, or create one.

    PURPOSE: fills, and adds rows to, the "Sign in with AFC" tab of the admin API Keys
    page (frontend/app/(a)/a/partners/page.tsx).

    AUTH: `Authorization: Bearer <SessionToken>`, and the caller must hold head_admin or
    partner_admin. See _require_sso_admin for the 400/401/403 shapes.

    WHY @authentication_classes([]): these routes live under /sso/, where
    SSOSessionTokenMiddleware sets request.user from the `auth_token` cookie. DRF's
    default SessionAuthentication would then see an authenticated user and run its CSRF
    check, which 403s every POST/PATCH from a browser that also holds that cookie (it
    does whenever the frontend and API share a host, as in local dev). Identical reason,
    identical fix, as afc_sso/api.py.

    ── GET (list) ──
    REQUEST: optional ?search= (name / display name / client id, case-insensitive),
    ?status= (active | suspended), ?limit= (default 25, max 100), ?offset=.
    RESPONSE 200:
        {"results": [{"application_id": 3, "name": "Partner Org",
                      "display_name": "Partner Org", "status": "active",
                      "client_id": "abc123...", "shared_field_count": 2,
                      "created_at": "2026-08-03T10:00:00+00:00"}],
         "total_count": 1, "has_more": false}

    ── POST (create) ──
    REQUEST: {"name": "Partner Org",                 # required, internal identifier
              "redirect_uris": "https://partner.test/cb",   # required, string or list
              "display_name": "Partner Org",         # optional, shown to the player
              "logo_url": "...", "homepage_url": "...",     # optional, https
              "deletion_webhook_url": "..."}         # optional, https
    Toggles are NOT accepted here on purpose: a new partner starts with every share_*
    field OFF (least privilege) and is granted data afterwards, deliberately, from the
    edit form. That also keeps "created" and "granted access" as two separate, auditable
    admin actions.

    RESPONSE 201:
        {"message": "...", "client_secret": "<plaintext>", "application": {...detail...}}
    THE PLAINTEXT SECRET IS IN THIS RESPONSE AND NOWHERE ELSE, EVER. The stored column
    holds a hash of it (ClientSecretField.pre_save), so it cannot be recovered later by
    this API, by the Django admin, or by reading the database. If it is lost, the only
    remedy is rotate_sso_client_secret below.
    """
    user, err = _require_sso_admin(request)
    if err:
        return err

    if request.method == "GET":
        qs = Application.objects.all().order_by("-created")

        status_filter = request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        search = request.GET.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(display_name__icontains=search)
                | Q(client_id__icontains=search)
            )

        page, total_count, has_more = _paginate(request, qs)
        return Response(
            {
                "results": [_serialize_summary(a) for a in page],
                "total_count": total_count,
                "has_more": has_more,
            },
            status=status.HTTP_200_OK,
        )

    # ── POST: create ──
    err = _require_object_body(request)
    if err:
        return err

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"message": "Partner name is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    redirect_uris, err_msg = _clean_redirect_uris(request.data.get("redirect_uris"))
    if err_msg:
        return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)

    # display_name is what the PLAYER reads on the consent screen, so it must never be
    # empty; fall back to the internal name rather than showing them a blank sentence.
    display_name = (request.data.get("display_name") or "").strip() or name

    logo_url, err_msg = _clean_url(request.data.get("logo_url"), "Logo URL")
    if err_msg:
        return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
    homepage_url, err_msg = _clean_url(request.data.get("homepage_url"), "Homepage URL")
    if err_msg:
        return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
    webhook_url, err_msg = _clean_url(
        request.data.get("deletion_webhook_url"), "Deletion webhook URL")
    if err_msg:
        return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)

    # Generate the secret OURSELVES rather than letting the model default fire, because
    # the default is generated inside save() and hashed on the way to the column - by the
    # time create() returns, the plaintext is gone. Holding it here is the only way to
    # show it to the admin once.
    secret = generate_client_secret()

    application = Application.objects.create(
        name=name,
        display_name=display_name,
        logo_url=logo_url,
        homepage_url=homepage_url,
        deletion_webhook_url=webhook_url,
        redirect_uris=redirect_uris,
        client_secret=secret,
        # ── Protocol shape, fixed by AFC and not admin-editable ──
        # CONFIDENTIAL + authorization-code + RS256 is the only combination AFC supports:
        # a server-side partner exchanging a code (with PKCE, see settings PKCE_REQUIRED)
        # for an RS256-signed ID token. Implicit and password grants are not offered.
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        algorithm=Application.RS256_ALGORITHM,
        # Audit trail: which AFC staff member provisioned this partner. `user` on the
        # library's Application model is a plain nullable FK with no other meaning here.
        user=user,
        # status defaults to "active" and every share_* toggle defaults to False, so a
        # brand-new partner can sign a player in and learn nothing about them but that
        # the sign-in succeeded.
    )

    return Response(
        {
            "message": "Sign-in partner created. Copy the client secret now, it will not be shown again.",
            "client_secret": secret,
            "application": _serialize_detail(application),
        },
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3 + 4) sso_application_detail  (GET = detail, PATCH = update) at
#        sso/admin/apps/<application_id>/
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "PATCH"])
@authentication_classes([])
def sso_application_detail(request, application_id):
    """Read or update one partner application, including the eight data toggles.

    PURPOSE: backs the edit dialog on the "Sign in with AFC" tab. This is where an admin
    decides what a partner may learn about a player, so it is the most consequential
    endpoint in this module.

    AUTH: as sso_applications above (Bearer SessionToken + head_admin / partner_admin).

    ── GET ──
    RESPONSE 200: {"application": {...summary fields...,
                                   "logo_url": "", "homepage_url": "",
                                   "deletion_webhook_url": "",
                                   "redirect_uris": "https://partner.test/cb",
                                   "share_profile": false, ... all eight ...,
                                   "scopes": ["openid"]}}
    `scopes` is derived from the toggles (AFCSSOApplication.allowed_scopes()) and is
    read-only. There is no client_secret field, by construction.

    ── PATCH ──
    REQUEST: any subset of
      identity  name, display_name, logo_url, homepage_url, redirect_uris,
                deletion_webhook_url
      toggles   share_profile, share_email, share_freefire_uid, share_team,
                share_history, share_stats, share_ranking, share_standing
    True PATCH semantics: only keys actually present are touched.

    THE WHITELIST IS THE SECURITY BOUNDARY. Any key outside the list above is a 400 for
    the WHOLE request rather than being silently ignored, so a typo cannot look like it
    worked. In particular `status` is refused here - suspend/unsuspend owns it, so
    freezing a partner is always a deliberate, separate action - and client_id,
    client_secret, client_type, authorization_grant_type, algorithm and
    skip_authorization are unreachable.

    RESPONSE 200: {"message": "...", "application": {...detail...}}
    """
    user, err = _require_sso_admin(request)
    if err:
        return err

    application, err = _application_or_404(application_id)
    if err:
        return err

    if request.method == "GET":
        return Response({"application": _serialize_detail(application)},
                        status=status.HTTP_200_OK)

    # ── PATCH: whitelist-validated partial update ──
    err = _require_object_body(request)
    if err:
        return err

    IDENTITY_FIELDS = (
        "name", "display_name", "logo_url", "homepage_url",
        "redirect_uris", "deletion_webhook_url",
    )
    allowed_keys = set(IDENTITY_FIELDS) | set(SSO_FIELD_TOGGLES)

    unknown = set(request.data.keys()) - allowed_keys
    if unknown:
        return Response(
            {"message": f"Unknown field(s): {', '.join(sorted(unknown))}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── identity: plain text fields ──
    if "name" in request.data:
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"message": "Partner name is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        application.name = name
    if "display_name" in request.data:
        # Blank display_name falls back to the internal name, for the same reason as at
        # creation: the consent screen must never show the player a nameless request.
        application.display_name = (
            (request.data.get("display_name") or "").strip() or application.name
        )

    # ── identity: URL fields, each validated ──
    url_fields = (
        ("logo_url", "Logo URL"),
        ("homepage_url", "Homepage URL"),
        ("deletion_webhook_url", "Deletion webhook URL"),
    )
    for field, label in url_fields:
        if field in request.data:
            cleaned, err_msg = _clean_url(request.data.get(field), label)
            if err_msg:
                return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
            setattr(application, field, cleaned)

    if "redirect_uris" in request.data:
        cleaned, err_msg = _clean_redirect_uris(request.data.get("redirect_uris"))
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        application.redirect_uris = cleaned

    # ── the eight data toggles ──
    # bool() coerces whatever truthy/falsy value the client sent into a real boolean, so
    # a JSON string or a 0/1 cannot leave a non-boolean in a column that gates data release.
    for field in SSO_FIELD_TOGGLES:
        if field in request.data:
            setattr(application, field, bool(request.data[field]))

    application.save()

    return Response(
        {
            "message": "Sign-in partner updated.",
            "application": _serialize_detail(application),
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5) suspend_sso_application  (POST sso/admin/apps/<application_id>/suspend/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
def suspend_sso_application(request, application_id):
    """Freeze or unfreeze one partner's ability to sign players in.

    PURPOSE: the Suspend / Unsuspend control on the "Sign in with AFC" tab. This is the
    kill switch for a partner AFC no longer trusts, without deleting the application (and
    so without breaking the account links every player already has with them, which a
    delete would).

    AUTH: as above (Bearer SessionToken + head_admin / partner_admin).

    REQUEST: {"suspend": true} to freeze, {"suspend": false} to restore.
    RESPONSE 200: {"message": "...", "status": "suspended"}

    WHAT SUSPENSION ACTUALLY DOES: AFCAuthorizationView (afc_sso/views.py) refuses every
    authorization request from an application whose is_active_partner() is False, so no
    new sign-in and no new token can be obtained. Access tokens the partner already holds
    are NOT deleted by this call and keep working until they expire, which is why this is
    a freeze rather than a revocation. Reversible by design: flipping it back restores the
    partner exactly as it was, toggles included.
    """
    user, err = _require_sso_admin(request)
    if err:
        return err

    application, err = _application_or_404(application_id)
    if err:
        return err

    err = _require_object_body(request)
    if err:
        return err

    # Truthy `suspend` → freeze; falsy → reactivate. Same shape as
    # afc_partner_api.views_admin.suspend_partner so both tabs behave identically.
    application.status = "suspended" if request.data.get("suspend") else "active"
    application.save()

    return Response(
        {"message": "Sign-in partner status updated.", "status": application.status},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6) rotate_sso_client_secret  (POST sso/admin/apps/<application_id>/rotate-secret/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
def rotate_sso_client_secret(request, application_id):
    """Issue a new client secret for one partner and invalidate the old one.

    PURPOSE: the Rotate secret action on the "Sign in with AFC" tab. Two situations call
    for it: the partner leaked or lost their secret, or AFC is rotating credentials as
    routine hygiene.

    AUTH: as above (Bearer SessionToken + head_admin / partner_admin).

    REQUEST: no body.
    RESPONSE 200: {"message": "...", "client_secret": "<plaintext>",
                   "application": {...detail...}}

    THE OLD SECRET STOPS WORKING THE MOMENT THIS RETURNS. Only one hash is stored, so
    replacing it is the invalidation - there is no grace period and no second live
    secret. Until the partner deploys the new value, every token exchange and every
    refresh they attempt fails, which is why the admin UI puts this behind a confirm
    that says exactly that.

    AS AT CREATION, THE PLAINTEXT IS RETURNED ONCE. It is hashed on save and cannot be
    read back afterwards by anything.

    Access tokens the partner already holds are untouched: a bearer token is not tied to
    the client secret. If the goal is to cut a partner off right now, suspend them (which
    stops new authorizations) - and remember that individual players can always cut one
    partner off themselves from Connected apps (afc_sso/api.py).
    """
    user, err = _require_sso_admin(request)
    if err:
        return err

    application, err = _application_or_404(application_id)
    if err:
        return err

    # Same one-moment-only handling as create: hold the plaintext, let save() hash it.
    secret = generate_client_secret()
    application.client_secret = secret
    application.save()

    return Response(
        {
            "message": "Client secret rotated. Copy it now, it will not be shown again.",
            "client_secret": secret,
            "application": _serialize_detail(application),
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 7) sso_scope_catalogue  (GET sso/admin/scopes/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def sso_scope_catalogue(request):
    """The eight toggles, in order, each paired with the OIDC scope it unlocks and the
    exact sentence the PLAYER is shown on the consent screen.

    PURPOSE: lets the admin UI label each switch with the promise the player reads, so
    staff grant data knowing precisely what the partner will be told. The frontend also
    carries its own translated copy of these sentences (messages/*/ssoAdmin.json) because
    the admin dashboard is localized through next-intl, not Django; this endpoint is the
    canonical source those keys are copied from, and the place to look when they drift.

    AUTH: as above (Bearer SessionToken + head_admin / partner_admin) - it describes the
    grant model, so it is staff-only like the rest of this module.

    REQUEST: none.
    RESPONSE 200:
        {"toggles": [{"field": "share_profile", "scope": "profile",
                      "description": "Your in-game name, avatar, country and language"}, ...]}

    The description comes from settings.OAUTH2_PROVIDER["SCOPES"], the same catalogue
    afc_sso.claims.describe_scopes reads for the consent screen and the player's Connected
    apps page, so the three can never disagree. The order is SSO_FIELD_TOGGLES order,
    which is the order the model declares them in.
    """
    user, err = _require_sso_admin(request)
    if err:
        return err

    from django.conf import settings

    catalogue = settings.OAUTH2_PROVIDER["SCOPES"]
    return Response(
        {
            "toggles": [
                {
                    "field": field,
                    "scope": TOGGLE_TO_SCOPE[field],
                    # str() resolves the gettext_lazy proxy now (it is not JSON
                    # serializable while lazy), exactly as describe_scopes does.
                    "description": str(catalogue.get(TOGGLE_TO_SCOPE[field], "")),
                }
                for field in SSO_FIELD_TOGGLES
            ]
        },
        status=status.HTTP_200_OK,
    )
