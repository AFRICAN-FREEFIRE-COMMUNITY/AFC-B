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

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SUMMARY GENERATION (the "What happened" column on /a/history + the Settings History tab).
#
# Three tiers, tried in order by _build_summary:
#   1. _ACTION_SENTENCES - per-endpoint TEMPLATES that read the submitted identifiers (URL kwargs,
#      JSON body, query params) and produce a full plain sentence naming the action AND its object,
#      e.g. "Viewed admin details of the event dynasty-cup-mozambique". Add new admin endpoints HERE.
#   2. _ACTION_LABELS - simple verb labels for older endpoints; the target ref ("#163" / '"john"')
#      is appended automatically (legacy behavior, kept so existing rows/tests stay stable).
#   3. _generic_sentence - the catch-all: verb heuristics on the slug (get_/list_ -> "Viewed",
#      create_/add_ -> "Created", edit_/update_ -> "Updated", delete_/remove_ -> "Deleted") plus the
#      most identifying submitted field, so even brand-new unmapped endpoints read as a sentence,
#      never as a raw action key.
#
# A view can still beat all three with afc_auth.audit.set_audit() (it knows names + before/after).
# Everything below reads REDACTED data only (kwargs/body/query are redacted before summary building),
# so a secret can never leak into a summary.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Tier 2: human-readable verbs for the common action slugs (the resolved URL name). The target ref
# is appended by _summary_target ("Edited an event #163"). Prefer adding NEW endpoints to
# _ACTION_SENTENCES below - templates there can name the object properly.
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
    """Best-effort short reference to what was acted on, from URL kwargs first, then the body.
    Used by the tier-2 _ACTION_LABELS path only (legacy '#163' / '"john"' suffix style)."""
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


# ── tier-1/tier-3 building blocks: pull identifiers out of the (redacted) request ────────────────

def _field(ctx, *keys):
    """First non-empty value for `keys` (priority order), each looked up in URL kwargs, then the
    JSON body, then the query params. `ctx` is the {"kwargs":…, "body":…, "query":…} dict built in
    _maybe_log from the SAME redacted data that goes into AuditLog.metadata. Returns "" if nothing
    matches, so templates can chain fallbacks."""
    for key in keys:
        for source in (ctx.get("kwargs"), ctx.get("body"), ctx.get("query")):
            if isinstance(source, dict) and source.get(key) not in (None, ""):
                return source[key]
    return ""


def _fmt_id(value, quote=False):
    """Format an identifier for use inside a sentence:
      7 / "7"                 -> "#7"             (numeric ids get the house # prefix)
      "dynasty-cup-mozambique"-> as-is            (slug-like, reads fine bare)
      "Detty December"        -> '"Detty December"' (free text is quoted; quote=True forces it)
    Empty / non-scalar values -> "" so callers can detect "no identifier"."""
    if value in (None, ""):
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        return f"#{text}"
    if quote or " " in text:
        return f"\"{text}\""
    return text


def _noun(ctx, noun, *keys):
    """'event' + best identifier among keys -> "event #163" / "event dynasty-cup-mozambique".
    With no identifier it degrades to just "event" so the sentence still reads naturally."""
    ident = _fmt_id(_field(ctx, *keys))
    return f"{noun} {ident}".strip()


def _list_len(ctx, key):
    """Length of a submitted list field (e.g. source_event_ids), or None if absent/not a list."""
    body = ctx.get("body")
    value = body.get(key) if isinstance(body, dict) else None
    return len(value) if isinstance(value, list) else None


# ── tier-1 multi-branch templates (kept as defs so the dict below stays readable) ────────────────

def _event_link_create(ctx):
    """events/<id>/links/create/ - body carries target_event_id + qualify_count (default 2)."""
    sentence = (f"Created a qualification link from {_noun(ctx, 'event', 'event_id')} "
                f"to {_noun(ctx, 'event', 'target_event_id')}")
    top_n = _field(ctx, "qualify_count")
    return f"{sentence} (top {top_n} qualify)" if top_n else sentence


def _event_link_decide(ctx):
    """events/links/<id>/decide/ - body.action drives the verb (see event_links.decide)."""
    verbs = {
        "allow": "Allowed a qualifying team into the target event",
        "reject": "Rejected a qualifying team from the target event",
        "decline": "Declined a qualification spot",
        "replace_next": "Replaced a declined team with the next eligible team",
        "undo": "Undid the last decision on a qualification",
    }
    head = verbs.get(_field(ctx, "action"), "Decided on a qualification")
    link = _fmt_id(_field(ctx, "link_id"))
    return f"{head} (qualification link {link})" if link else head


def _import_competitors(ctx):
    """events/<id>/import-competitors/ - body.source_event_ids is the list of source events."""
    n = _list_len(ctx, "source_event_ids")
    source = f"{n} event{'s' if n != 1 else ''}" if n else "other events"
    return f"Merged competitors from {source} into {_noun(ctx, 'event', 'event_id')}"


def _h2h_generate(ctx):
    """events/stages/<id>/bracket/generate/ - body.team_ids is the seed-ordered team list."""
    sentence = f"Generated a head-to-head bracket for {_noun(ctx, 'stage', 'stage_id')}"
    n = _list_len(ctx, "team_ids")
    return f"{sentence} ({n} teams)" if n else sentence


def _h2h_result(ctx):
    """events/h2h-matches/<id>/result/ - body carries score_a / score_b."""
    sentence = f"Reported the result of head-to-head match {_fmt_id(_field(ctx, 'match_id'))}".strip()
    score_a, score_b = _field(ctx, "score_a"), _field(ctx, "score_b")
    if score_a != "" and score_b != "":
        return f"{sentence} ({score_a} - {score_b})"
    return sentence


def _sponsor_submission_decide(ctx):
    """sponsors/submissions/<id>/decide/ - body.action: approve|reject|reject_final|undo."""
    sub = _noun(ctx, "sponsor engagement submission", "submission_id")
    verbs = {
        "approve": f"Approved {sub}",
        "reject": f"Rejected {sub}",
        "reject_final": f"Rejected {sub} and released the registration slot",
        "undo": f"Undid the last decision on {sub}",
    }
    return verbs.get(_field(ctx, "action"), f"Decided on {sub}")


def _blacklist_create(ctx):
    """organizers/blacklists/ POST - body carries team_id + duration_days/start/end + reason."""
    sentence = f"Blacklisted {_noun(ctx, 'team', 'team_id')}"
    days = _field(ctx, "duration_days")
    return f"{sentence} for {days} days" if days else sentence


def _blacklist_decide_lift(ctx):
    """organizers/blacklists/lift-requests/<id>/decide/ - body.decision: approve|reject."""
    req = _noun(ctx, "blacklist lift request", "request_id")
    verbs = {"approve": f"Approved {req}", "reject": f"Rejected {req}"}
    return verbs.get(_field(ctx, "decision"), f"Decided on {req}")


def _payout_action(verb, suffix=""):
    """Factory for the admin payout endpoints (shop/admin/payouts/*): the body narrows the scope
    to one payout, one vendor, or everything owed."""
    def template(ctx):
        payout = _fmt_id(_field(ctx, "payout_id"))
        if payout:
            return f"{verb} vendor payout {payout}{suffix}"
        vendor = _fmt_id(_field(ctx, "vendor_id"))
        if vendor:
            return f"{verb} all owed payouts for vendor {vendor}{suffix}"
        return f"{verb} all owed vendor payouts{suffix}"
    return template


def _blacklist_lookup(ctx):
    """organizers/blacklist-lookup/ - query carries team_id OR user_id (read endpoint today; only
    ever logged if it gains a mutating verb, kept mapped so that day still reads as a sentence)."""
    team, user = _fmt_id(_field(ctx, "team_id")), _fmt_id(_field(ctx, "user_id"))
    target = f"team {team}" if team else (f"player {user}" if user else "a team or player")
    return f"Looked up the blacklist history of {target}"


# Tier 1: full-sentence templates per url_name. Each value is a callable(ctx) -> str, where ctx is
# {"kwargs":…, "body":…, "query":…} (all redacted). These produce the explanatory sentences the
# owner asked for ("Viewed admin details of the event dynasty-cup-mozambique"), naming both the
# action and its object. Connects to: afc_tournament_and_scrims/{event_links,head_to_head_views}.py,
# afc_sponsors/{views,engagements}.py, afc_organizers/views_blacklist*.py, afc_shop/{connect,
# paystack_payout}.py and afc_tournament_and_scrims/views.py (the admin POST-reads).
_ACTION_SENTENCES = {
    # ── events: admin detail reads (POST bodies, so the middleware logs them) ──
    "get_event_details_for_admin": lambda c: (
        f"Viewed admin details of the {_noun(c, 'event', 'slug', 'event_id', 'event_name')}"
    ),

    # ── event linking / qualification chains (event_links.py) ──
    "create_event_link": _event_link_create,
    "fire_event_link": lambda c: (
        f"Fired qualification {_noun(c, 'link', 'link_id')} "
        "(sent its qualifying teams to the target event)"
    ),
    "decide_event_link": _event_link_decide,
    "cancel_event_link": lambda c: f"Cancelled qualification {_noun(c, 'link', 'link_id')}",
    "import_event_competitors": _import_competitors,

    # ── clash-squad head-to-head brackets (head_to_head_views.py) ──
    "generate_h2h_bracket": _h2h_generate,
    "report_h2h_match_result": _h2h_result,

    # ── sponsor system P1-P4 (afc_sponsors) ──
    "sponsors_create": lambda c: f"Created the {_noun(c, 'sponsor', 'name')}",
    "sponsors_edit": lambda c: f"Updated the {_noun(c, 'sponsor', 'name', 'sponsor_id')}",
    "sponsors_add_member": lambda c: (
        f"Added {_noun(c, 'user', 'user_id')} as {_field(c, 'role') or 'member'} "
        f"of {_noun(c, 'sponsor', 'sponsor_id')}"
    ),
    "sponsors_remove_member": lambda c: (
        f"Removed {_noun(c, 'member', 'member_id')} from {_noun(c, 'sponsor', 'sponsor_id')}"
    ),
    "sponsors_attach_event": lambda c: (
        f"Attached {_noun(c, 'event', 'event_id')} to {_noun(c, 'sponsor', 'sponsor_id')}"
    ),
    "sponsors_detach_event": lambda c: (
        f"Detached {_noun(c, 'event', 'event_id')} from {_noun(c, 'sponsor', 'sponsor_id')}"
    ),
    "sponsors_configure_sponsorship": lambda c: (
        f"Updated the engagement requirements of {_noun(c, 'sponsor', 'sponsor_id')} "
        f"for {_noun(c, 'event', 'event_id')}"
    ),
    "sponsors_decide_submission": _sponsor_submission_decide,
    "sponsors_resubmit_submission": lambda c: (
        f"Resubmitted {_noun(c, 'sponsor engagement submission', 'submission_id')}"
    ),

    # ── organizer blacklists (views_blacklist.py + views_blacklist_lookup.py) ──
    "organizers_blacklists": _blacklist_create,  # POST creates; the GET list is never audited
    "organizers_blacklist_lift": lambda c: f"Lifted {_noun(c, 'blacklist', 'blacklist_id')}",
    "organizers_blacklist_request_lift": lambda c: (
        f"Requested a lift of {_noun(c, 'blacklist', 'blacklist_id')}"
    ),
    "organizers_blacklist_decide_lift": _blacklist_decide_lift,
    "organizers_blacklist_lookup": _blacklist_lookup,
    "organizers_admin_blacklists": lambda c: "Viewed the sitewide blacklist dashboard",

    # ── marketplace vendor payouts (connect.py + paystack_payout.py) ──
    "admin_release_owed_payouts": _payout_action("Released"),
    "admin_retry_owed_paystack_payouts": _payout_action("Retried", " via Paystack"),
}


# ── tier 3: the generic fallback - verb heuristics so unmapped endpoints still read as sentences ──

# Slug prefixes -> plain-English verb. Checked in order; the matched prefix is stripped and the
# rest of the slug becomes the object phrase ("get_event_details_for_admin" -> "Viewed event
# details for admin"). Endpoints whose slug carries no known prefix keep the humanized slug.
_VERB_PREFIXES = (
    (("get_", "list_", "view_", "fetch_"), "Viewed"),
    (("create_", "add_"), "Created"),
    (("edit_", "update_"), "Updated"),
    (("delete_", "remove_"), "Deleted"),
)

# Identifier keys for the fallback, most-identifying first: human-readable names/slugs beat bare
# numeric ids ("dynasty-cup-mozambique" says more than "#163"). Searched across body + query; the
# first URL kwarg is used between the two groups (it is the route's own target).
_FALLBACK_NAME_KEYS = ("slug", "name", "title", "username", "team_name", "event_name")
_FALLBACK_ID_KEYS = (
    "event_id", "team_id", "user_id", "product_id", "news_id", "order_id", "sponsor_id",
    "link_id", "stage_id", "match_id", "submission_id", "blacklist_id", "payout_id",
    "vendor_id", "id",
)


def _best_identifier(ctx):
    """The most identifying submitted field, formatted for a sentence. Names/slugs first (quoted
    for clarity since the fallback sentence is mechanical), then the first URL kwarg, then ids."""
    for key in _FALLBACK_NAME_KEYS:
        for source in (ctx.get("body"), ctx.get("query")):
            if isinstance(source, dict) and source.get(key) not in (None, ""):
                ident = _fmt_id(source[key], quote=True)
                if ident:
                    return ident
    kwargs = ctx.get("kwargs")
    if isinstance(kwargs, dict) and kwargs:
        ident = _fmt_id(next(iter(kwargs.values())))
        if ident:
            return ident
    return _fmt_id(_field(ctx, *_FALLBACK_ID_KEYS))


def _generic_sentence(url_name, ctx):
    """Readable sentence for an endpoint nobody has mapped yet: verb heuristic + object phrase +
    best identifier, e.g. "Viewed event details for admin \"dynasty-cup-mozambique\"". Guarantees
    the audit table never shows a raw action key for future endpoints."""
    slug = (url_name or "").strip()
    verb, rest = "", slug
    for prefixes, candidate in _VERB_PREFIXES:
        match = next((p for p in prefixes if slug.startswith(p)), None)
        if match:
            verb, rest = candidate, slug[len(match):]
            break
    phrase = rest.replace("_", " ").strip()
    sentence = f"{verb} {phrase}".strip() if (verb and phrase) else _humanize_slug(slug)
    ident = _best_identifier(ctx)
    return f"{sentence} {ident}".strip() if ident else sentence


def _build_summary(url_name, method, ctx):
    """Plain-English sentence for the audit table. Tier 1 (sentence templates) -> tier 2 (legacy
    labels + target ref) -> tier 3 (generic verb-heuristic fallback). `ctx` carries the REDACTED
    {"kwargs","body","query"} dicts; templates are best-effort and must never raise upward, so a
    template error simply drops through to the next tier."""
    template = _ACTION_SENTENCES.get(url_name)
    if template:
        try:
            sentence = template(ctx)
            if sentence:
                return sentence
        except Exception:
            pass  # a malformed body must never kill the log row - fall through to tier 2/3
    label = _ACTION_LABELS.get(url_name)
    if label:
        body = ctx.get("body") if isinstance(ctx.get("body"), dict) else {}
        target = _summary_target(ctx.get("kwargs") or {}, body)
        return f"{label} {target}".strip()
    return _generic_sentence(url_name, ctx)


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
    readable. Applied to URL kwargs + query params before they go into AuditLog.metadata.

    Also coerces non-JSON-native scalars (UUID / datetime / date / Decimal) to strings so the row
    can be stored: AuditLog.metadata is a JSONField, and URL kwargs from typed path converters carry
    e.g. a UUID (the ``<uuid:ghost_team_id>`` rankings routes) that json.dumps cannot encode. Without
    this, AuditLog.objects.create would raise mid-INSERT (silently swallowed in prod, but it poisons
    the surrounding transaction in tests). Keeping the coercion HERE means every metadata path
    (kwargs / query / body) is JSON-safe in one place.
    """
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in _SENSITIVE) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    # JSON-native scalars pass through unchanged; everything else (UUID, datetime, date, Decimal, ...)
    # is stringified so the JSONField can store it.
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


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
        # entity name + before/after ("Changed Detty December from internal to external"). Otherwise
        # build one from the three-tier mapping above (sentence templates -> labels -> generic
        # verb-heuristic fallback). The ctx reuses the REDACTED metadata dicts so a secret value can
        # never surface in a summary.
        url_name = match.url_name if (match and match.url_name) else ""
        ctx = {"kwargs": metadata["kwargs"], "body": body, "query": metadata["query"]}
        view_summary = getattr(request, "audit_summary", "") or ""
        summary = view_summary or _build_summary(url_name, request.method, ctx)

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
