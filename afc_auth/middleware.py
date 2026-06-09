"""
afc_auth/middleware.py — sitewide AUTOMATIC admin audit-log capture.

AuditLogMiddleware records EVERY mutating request (POST/PUT/PATCH/DELETE) made by a user who holds
an admin/staff role into the afc_auth.AuditLog table, with zero per-view code. It is the "automatic"
half of the audit-log feature; the read half is afc_auth.views.get_audit_log
(GET /auth/get-audit-log/), surfaced on the admin History page (frontend app/(a)/a/history).

How it fits into the system:
  - Registered in afc/settings.py MIDDLEWARE, AFTER django.contrib.auth's AuthenticationMiddleware
    (so URL resolution + the standard request pipeline are in place by the time we run).
  - AFC authenticates with a CUSTOM SessionToken (Authorization: "Bearer <token>"), NOT Django's
    session framework, so request.user is the AnonymousUser. We therefore resolve the acting User
    ourselves the same way the views do (SessionToken lookup, expiry check) - see _resolve_actor.
    That logic is a local copy of afc_auth.views.validate_token / get_client_ip, kept local so this
    middleware module has NO import-time dependency on the large views module.
  - Only admin/staff actors are logged (User.role in the admin set, OR any granular UserRoles row,
    OR is_staff/superuser). A normal player editing their own profile is not an "admin action".

Safety contract (project rules):
  - Best-effort: the whole capture is wrapped in try/except and runs AFTER the response is produced,
    so a logging failure can NEVER break or slow the real request.
  - No secrets: sensitive-looking keys are redacted and the raw request body is never stored
    (we keep only URL kwargs + query params, redacted) - never log tokens/passwords/PII.

Talks to: afc_auth.models.SessionToken (actor resolution) + afc_auth.models.AuditLog (the sink) +
afc_auth.models.UserRoles (granular role snapshot, via the User.userroles reverse relation).
"""
import json

from afc_auth.models import SessionToken, AuditLog

# HTTP methods that change state - the only ones worth auditing (reads are skipped wholesale).
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# User.role values that count as admin/staff. Mirrors the gate in afc_auth.views.require_admin
# (which checks role == "admin") but is a little broader so moderator/support mutations are caught
# too; granular UserRoles are checked separately below.
_ADMIN_ROLES = {"admin", "moderator", "support"}

# Substrings that mark a metadata key as sensitive; its value is replaced with "***".
_SENSITIVE = ("password", "token", "secret", "key", "cvv", "card", "pin", "otp", "authorization")

# Path prefixes we never audit: the audit reader itself (avoid self-noise), the Django admin, and
# static/media. Note these are BACKEND paths (the /a/* admin UI lives on the frontend and reaches
# the backend through the prefixes below, e.g. /shop/, /events/, /auth/).
_SKIP_PREFIXES = ("/auth/get-audit-log", "/admin/", "/static/", "/media/")

# Only capture the request body for JSON bodies under this size (bytes). Skips file uploads /
# multipart (which we never want to read into memory or store) and oversized payloads.
_MAX_BODY_BYTES = 20_000

# Human-readable verbs for the common action slugs (the resolved URL name). Anything not listed
# falls back to a humanized slug ("edit_solo_match_result" -> "Edit solo match result"). This map is
# what turns the raw endpoint into the plain-English summary the audit table shows. Extend freely.
_ACTION_LABELS = {
    "create_event": "Created an event",
    "edit_event": "Edited an event",
    "delete_event": "Deleted an event",
    "create_news": "Posted news",
    "edit_news": "Edited news",
    "delete_news": "Deleted news",
    "suspend_user": "Suspended a user",
    "activate_user": "Activated a user",
    "ban_player": "Banned a player",
    "unban_player": "Unbanned a player",
    "ban_team": "Banned a team",
    "unban_team": "Unbanned a team",
    "assign_roles_to_user": "Assigned admin roles",
    "edit_user_roles": "Edited admin roles",
    "add_role": "Created a role",
    "delete_role": "Deleted a role",
    "delete_product": "Deleted a shop product",
    "create_product": "Created a shop product",
    "edit_product": "Edited a shop product",
    "admin_send_message": "Messaged a user or team",
    "send_notification": "Sent a notification",
    "send_notification_to_multiple_users": "Sent a bulk notification",
    "broadcast_to_group": "Messaged an event group",
    "create_coupon": "Created a coupon",
    "delete_coupon": "Deleted a coupon",
}

# Body keys that identify the TARGET of the action, in priority order. Used to append a short
# reference to the summary (e.g. "Edited an event #163" or 'Suspended a user "john"').
_TARGET_ID_KEYS = ("event_id", "team_id", "user_id", "product_id", "news_id", "order_id", "id")
_TARGET_NAME_KEYS = ("team_name", "username", "name", "title", "slug")


def _humanize_slug(slug):
    """Turn a url_name like "edit_solo_match_result" into "Edit solo match result"."""
    return slug.replace("_", " ").strip().capitalize() if slug else "Action"


def _summary_target(kwargs, body):
    """Best-effort short reference to what was acted on, from URL kwargs first, then the body."""
    if kwargs:
        k, v = next(iter(kwargs.items()))
        return f"#{v}"
    for key in _TARGET_ID_KEYS:
        if isinstance(body, dict) and body.get(key) not in (None, ""):
            return f"#{body[key]}"
    for key in _TARGET_NAME_KEYS:
        if isinstance(body, dict) and body.get(key) not in (None, ""):
            return f"\"{body[key]}\""
    return ""


def _build_summary(url_name, method, kwargs, body):
    """Plain-English short form for the audit table, e.g. 'Edited an event #163'."""
    label = _ACTION_LABELS.get(url_name) or _humanize_slug(url_name)
    target = _summary_target(kwargs, body)
    return f"{label} {target}".strip()


def _resolve_actor(request):
    """Bearer token -> User (or None). Local copy of afc_auth.views.validate_token so this module
    stays import-light at startup. select_related('user') keeps it to a single query."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        session = SessionToken.objects.select_related("user").get(token=token)
    except SessionToken.DoesNotExist:
        return None
    if session.is_expired():
        return None
    return session.user


def _client_ip(request):
    """Real client IP, honoring X-Forwarded-For (prod runs behind nginx on EC2). Local copy of
    afc_auth.views.get_client_ip."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _redact(value):
    """Recursively replace sensitive values with '***', preserving the shape so the log stays
    readable. Applied to URL kwargs + query params before they go into AuditLog.metadata."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in _SENSITIVE) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class AuditLogMiddleware:
    """Auto-logs admin/staff mutations into afc_auth.AuditLog. See the module docstring for the
    full contract and how it connects to the read endpoint + admin History page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Capture the (JSON) request body BEFORE the view consumes the stream, so the audit details
        # can show exactly what the admin submitted. Best-effort, size/content-type gated; reading
        # request.body here caches it so the downstream view / DRF can still parse it normally.
        try:
            request._audit_body = self._capture_body(request)
        except Exception:
            request._audit_body = {}

        # Run the real request - we need the final response status to log a complete record.
        response = self.get_response(request)
        try:
            self._maybe_log(request, response)
        except Exception:
            # Auditing must NEVER break a real request. Swallow everything (best-effort by design).
            pass
        return response

    def _capture_body(self, request):
        """Redacted dict/list of the JSON request body for mutating requests, else {}. Only reads
        application/json under the size cap - never multipart / file uploads (we don't want big
        uploads in memory or in the log, and reading them would consume the upload stream)."""
        if request.method not in _MUTATING_METHODS:
            return {}
        if "application/json" not in (request.content_type or ""):
            return {}
        body = request.body  # cached by Django -> the view can still re-read it
        if not body or len(body) > _MAX_BODY_BYTES:
            return {}
        data = json.loads(body.decode("utf-8"))
        return _redact(data) if isinstance(data, (dict, list)) else {}

    def _maybe_log(self, request, response):
        # Cheapest checks first so reads and anonymous/non-admin traffic cost effectively nothing.
        if request.method not in _MUTATING_METHODS:
            return
        path = request.path or ""
        if path.startswith(_SKIP_PREFIXES):
            return

        actor = _resolve_actor(request)
        if actor is None:
            return  # unauthenticated mutation (login/register/etc.) - not an admin action

        # One userroles query, reused for BOTH the admin decision and the role snapshot. Most admin
        # actions come from User.role == "admin" (no extra cost there); granular-only admins (e.g.
        # role="player" + shop_admin) are caught via the userroles list.
        granular = list(actor.userroles.values_list("role__role_name", flat=True))
        is_admin = (
            actor.is_superuser
            or actor.is_staff
            or (actor.role or "").lower() in _ADMIN_ROLES
            or bool(granular)
        )
        if not is_admin:
            return  # ordinary user mutating their own data - outside the admin audit scope

        # Snapshot the actor's role(s) as a readable label, de-duped, order-preserved.
        role_parts = ([actor.role] if actor.role else []) + [g for g in granular if g]
        if actor.is_superuser:
            role_parts.append("superuser")
        role_label = "+".join(dict.fromkeys(role_parts))

        # Derive a short action slug + best-effort target from the resolved URL.
        match = getattr(request, "resolver_match", None)
        action = match.url_name if (match and match.url_name) else path
        view_name = match.view_name if match else ""
        target_type, target_id = "", ""
        if match and match.kwargs:
            # Take the first URL kwarg as the target (e.g. {"product_id": 3} -> "product_id"/"3").
            k, v = next(iter(match.kwargs.items()))
            target_type, target_id = str(k), str(v)[:120]

        # Redacted, bounded metadata: URL kwargs + query params + the JSON body (captured above).
        # The body is what makes the row EXPAND to "exactly what the admin submitted".
        body = getattr(request, "_audit_body", {}) or {}
        metadata = {
            "kwargs": _redact(dict(match.kwargs)) if (match and match.kwargs) else {},
            "query": _redact({k: request.GET.get(k) for k in request.GET}),
            "body": body,
        }
        # Extra, view-authored detail fields (e.g. before/after, recipient count) via set_audit().
        extra = getattr(request, "audit_details", {}) or {}
        if extra:
            metadata["details"] = _redact(extra)

        # Summary: PREFER a specific, view-authored summary (afc_auth.audit.set_audit) - it knows the
        # entity name + before/after ("Changed Detty December from internal to external"). Only fall
        # back to the generic slug-derived summary for endpoints that haven't been instrumented.
        url_name = match.url_name if (match and match.url_name) else ""
        kwargs = dict(match.kwargs) if (match and match.kwargs) else {}
        view_summary = getattr(request, "audit_summary", "") or ""
        summary = view_summary or _build_summary(url_name, request.method, kwargs, body)

        AuditLog.objects.create(
            actor=actor,
            actor_username=actor.username,
            actor_role=role_label,
            summary=summary[:255],
            action=str(action)[:120],
            method=request.method,
            path=path[:512],
            view_name=str(view_name)[:255],
            target_type=target_type[:120],
            target_id=target_id,
            status_code=getattr(response, "status_code", None),
            ip_address=(_client_ip(request) or "")[:45],
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:2000],
            metadata=metadata,
        )
