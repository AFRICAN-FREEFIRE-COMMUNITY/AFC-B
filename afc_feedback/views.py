"""
afc_feedback.views - endpoints for the always-on, reusable site feedback form (backlog item 29).

PURPOSE
    Serve a form's schema to the public widget, accept a submission from ANYONE (logged in or not),
    and give AFC admins a queue to read and triage. The form is data, not code (see models.py), so
    these endpoints are written against "whatever fields this form declares", never against a fixed
    rating-plus-comment shape.

HOUSE IDIOMS (mirrors afc_sponsors.views / afc_leaderboard.views)
    - Function-based @api_view, Bearer SessionToken via afc_auth.views.validate_token.
    - Errors: Response({"message": ...}, status=4xx).
    - Pagination envelope {results, has_more, next_offset, total_count}, limit <= 100, default 25.

THE OPEN WRITE ENDPOINT
    submit_feedback is the only endpoint on AFC that accepts an unauthenticated POST that writes a
    row, because feedback from someone who could NOT sign up is the feedback most worth having. That
    makes it an abuse target, so it is rate limited per sender (see the RATE LIMIT section below),
    every answer is validated and length-capped against the form's own field definitions, and the
    sender's IP is stored only as a salted hash.

ENDPOINTS (mounted at feedback/ via afc/urls.py)
    GET    feedback/forms/<key>/              form_schema         PUBLIC, no auth
    POST   feedback/forms/<key>/submit/       submit_feedback     PUBLIC (auth OPTIONAL), rate limited
    GET    feedback/admin/forms/              admin_list_forms    feedback-admin
    GET    feedback/admin/submissions/        admin_list_submissions   feedback-admin, paginated
    PATCH  feedback/admin/submissions/<id>/   admin_update_submission  feedback-admin

CONSUMED BY
    - frontend components/feedback/FeedbackDialog.tsx  -> form_schema + submit_feedback
      (opened from the Footer link rendered on every public page, components/feedback/FeedbackLauncher.tsx)
    - frontend app/(a)/a/feedback/page.tsx             -> the three admin endpoints
"""
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token

from .models import FeedbackForm, FeedbackField, FeedbackSubmission

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# ── RATE LIMIT ────────────────────────────────────────────────────────────────────────────────
# submit_feedback accepts anonymous writes, so it is limited per SENDER. Same Redis cache and the
# same add()-then-incr() idiom as afc_auth/broadcast_ratelimit.py and afc_partner_api/ratelimit.py
# (django_redis' incr() raises on a missing key, so the bucket must be add()ed first).
#
# Two limits, both needed and each catching what the other misses:
#   COOLDOWN  - a minimum gap between two submissions. Stops a script hammering the endpoint.
#   HOURLY    - a ceiling per clock hour. Stops a slow drip that would walk under the cooldown.
#
# The sender identity is the user id when logged in, otherwise the hashed IP: a signed-in abuser
# cannot dodge the limit by switching network, and an anonymous one is still bounded. Nobody is
# exempt, admins included - an admin has better ways to write to this table than the public form.
# Deliberately generous: a real person sends one piece of feedback, occasionally two.
FEEDBACK_RATE_LIMIT_PER_HOUR = 5
FEEDBACK_COOLDOWN_SECONDS = 30


def _client_ip(request):
    """Best-effort client IP. Honours X-Forwarded-For's FIRST entry (the original client) because AFC
    runs behind a load balancer, and falls back to REMOTE_ADDR."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _client_ip_hash(request):
    """A salted, one-way hash of the client IP.

    We need to RECOGNISE a repeat sender for the rate limit and for spotting one abusive source in the
    admin queue. We do not need to KNOW their address, and a raw IP sitting on a feedback row is PII
    with no purpose. SECRET_KEY is the salt, so the hash is meaningless outside this deployment and
    cannot be reversed with a rainbow table of the ~4 billion IPv4 addresses."""
    ip = _client_ip(request)
    if not ip:
        return ""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode("utf-8")).hexdigest()


def _rate_limit_identity(user, ip_hash):
    """The bucket key suffix identifying this sender: the user id when known, else the hashed IP."""
    if user is not None:
        return f"u{user.user_id}"
    return f"ip{ip_hash or 'unknown'}"


def _hour_key(identity):
    """Per-sender fixed clock-hour bucket. The hour stamp in the key self-rotates the window, so no
    reset job is needed (same trick as broadcast_ratelimit._hour_key)."""
    return f"fb_hr:{identity}:{timezone.now().strftime('%Y%m%d%H')}"


def _cooldown_key(identity):
    return f"fb_cd:{identity}"


def _next_hour_iso():
    """ISO timestamp of the next clock-hour boundary: when the hourly bucket rolls over."""
    nxt = timezone.now().replace(minute=0, second=0, microsecond=0) + timezone.timedelta(hours=1)
    return nxt.isoformat()


def check_feedback_rate(identity):
    """May this sender submit RIGHT NOW? Read-only: does NOT consume a slot.

    Returns (allowed: bool, info: dict). When blocked, info carries `reason` ("cooldown" | "hourly"),
    `resets_at` (ISO, when sending re-opens) and a `message` the frontend can show as-is."""
    cooldown_until = cache.get(_cooldown_key(identity))
    if cooldown_until:
        return False, {
            "reason": "cooldown",
            "resets_at": cooldown_until,
            "message": "You just sent feedback. Please wait a moment before sending more.",
        }

    count = cache.get(_hour_key(identity), 0) or 0
    if count >= FEEDBACK_RATE_LIMIT_PER_HOUR:
        return False, {
            "reason": "hourly",
            "resets_at": _next_hour_iso(),
            "message": (
                f"You have sent {FEEDBACK_RATE_LIMIT_PER_HOUR} pieces of feedback this hour. "
                "Please try again a little later."
            ),
        }

    return True, {"remaining": FEEDBACK_RATE_LIMIT_PER_HOUR - count}


def record_feedback_send(identity):
    """Consume one slot. Called ONLY after a submission is actually stored, so a rejected or invalid
    attempt never costs the sender their allowance."""
    cooldown_end = (
        timezone.now() + timezone.timedelta(seconds=FEEDBACK_COOLDOWN_SECONDS)
    ).isoformat()
    cache.set(_cooldown_key(identity), cooldown_end, FEEDBACK_COOLDOWN_SECONDS)

    hk = _hour_key(identity)
    # add()-then-incr(): django_redis incr() raises ValueError on a missing key. add() is a no-op when
    # the bucket exists and does not reset its TTL, so the hour window stays honest.
    cache.add(hk, 0, 3600)
    try:
        cache.incr(hk)
    except ValueError:
        # TTL-boundary race: the bucket expired between add() and incr(). Re-seed rather than 500.
        cache.set(hk, 1, 3600)


# ── auth helpers ──────────────────────────────────────────────────────────────────────────────
# Granular UserRoles names allowed to READ and TRIAGE feedback. Site feedback is not scoped to any
# one area (an event, the shop, a team), so it is NOT delegated to an area admin: only the roles that
# own the platform as a whole. The coarse support/moderator roles get in via the `role` check in
# is_feedback_admin below, which is how AFC's support staff reach it.
_FEEDBACK_ADMIN_ROLES = ("head_admin", "super_admin")


def _auth_user(request):
    """Resolve the Bearer caller. Returns (user, error_response); exactly one is non-None.

    Used by the three admin endpoints. The public endpoints use _optional_user instead."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response(
            {"message": "Authentication credentials were not provided."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired token."}, status=status.HTTP_401_UNAUTHORIZED
        )
    return user, None


def _optional_user(request):
    """Resolve the caller if they happen to be signed in, otherwise None. NEVER errors.

    This is what makes the feedback widget work for anonymous visitors: a missing, malformed or
    expired token is not a failure here, it just means the submission is stored without attribution."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    try:
        return validate_token(auth.split(" ")[1])
    except Exception:
        return None


def is_feedback_admin(user) -> bool:
    """True if `user` may read and triage the feedback queue: coarse role admin/moderator/support, or
    one of _FEEDBACK_ADMIN_ROLES. Mirrors afc_auth.broadcast_ratelimit.is_broadcast_admin."""
    if not user:
        return False
    if getattr(user, "role", None) in ("admin", "moderator", "support"):
        return True
    try:
        return user.userroles.filter(role__role_name__in=_FEEDBACK_ADMIN_ROLES).exists()
    except Exception:
        return False


# ── serializers ───────────────────────────────────────────────────────────────────────────────
def _serialize_field(field):
    """One question, as the widget needs to render it. `options` and `max_rating` are always present
    so the frontend renderer can branch on field_type without null-checking every extra."""
    return {
        "key": field.key,
        "label": field.label,
        "field_type": field.field_type,
        "required": field.required,
        "placeholder": field.placeholder,
        "help_text": field.help_text,
        "options": field.options or [],
        "max_rating": field.max_rating,
        "max_length": field.max_length,
    }


def _serialize_form(form):
    """A form plus its ordered fields. This is the whole contract the public widget renders from."""
    return {
        "key": form.key,
        "title": form.title,
        "description": form.description,
        "thank_you_message": form.thank_you_message,
        "fields": [_serialize_field(f) for f in form.fields.all()],
    }


def _serialize_submission(sub):
    """One submission for the ADMIN queue.

    `fields_snapshot` is echoed alongside `answers` so the admin page can render the questions as they
    were worded at submit time, even if the form has been edited since."""
    return {
        "id": sub.id,
        "form_key": sub.form.key,
        "form_title": sub.form.title,
        "answers": sub.answers or {},
        "fields_snapshot": sub.fields_snapshot or [],
        "page_path": sub.page_path,
        "locale": sub.locale,
        "user_agent": sub.user_agent,
        # Username only. The admin queue never needs the submitter's email, and not serializing it is
        # cheaper than remembering not to leak it later.
        "username": sub.user.username if sub.user else None,
        "is_anonymous": sub.user_id is None,
        "status": sub.status,
        "admin_note": sub.admin_note,
        "handled_by": sub.handled_by.username if sub.handled_by else None,
        "handled_at": sub.handled_at.isoformat() if sub.handled_at else None,
        "created_at": sub.created_at.isoformat(),
    }


# ── validation ────────────────────────────────────────────────────────────────────────────────
def _clean_page_path(raw):
    """Normalize the client-sent page path to a bare, storable path.

    Drops the query string and fragment: a query string on an AFC URL can carry an invite token or a
    password-reset token, and copying one into a feedback row would persist a credential we were never
    asked to store. Anything that is not a site-relative path is discarded rather than trusted."""
    path = str(raw or "").strip()
    if not path.startswith("/"):
        return ""
    for sep in ("?", "#"):
        if sep in path:
            path = path.split(sep, 1)[0]
    return path[:300]


def _validate_answers(form, raw_answers):
    """Validate the submitted answers against THIS form's declared fields.

    Returns (cleaned_dict, error_message_or_None). Rules, all enforced here rather than trusted from
    the client, since the client is a public web page:
      - unknown keys in the payload are DROPPED (the row stores only declared fields),
      - a required field must be present and non-empty,
      - rating  -> integer within 1..max_rating,
      - choice  -> must be one of the field's declared options,
      - text/textarea -> string, trimmed, truncated to the field's max_length.
    A form with every field optional and nothing filled in is still rejected: an empty submission is
    noise in the queue, never signal."""
    if not isinstance(raw_answers, dict):
        return None, "answers must be an object."

    cleaned = {}
    for field in form.fields.all():
        value = raw_answers.get(field.key)

        # ── absent / blank ──
        if value is None or (isinstance(value, str) and not value.strip()):
            if field.required:
                return None, f"'{field.label}' is required."
            continue

        if field.field_type == FeedbackField.RATING:
            try:
                rating = int(value)
            except (TypeError, ValueError):
                return None, f"'{field.label}' must be a number."
            if rating < 1 or rating > field.max_rating:
                return None, f"'{field.label}' must be between 1 and {field.max_rating}."
            cleaned[field.key] = rating

        elif field.field_type == FeedbackField.CHOICE:
            choice = str(value).strip()
            if choice not in (field.options or []):
                return None, f"'{choice}' is not a valid option for '{field.label}'."
            cleaned[field.key] = choice

        else:  # TEXT / TEXTAREA
            text = str(value).strip()[: field.max_length]
            if not text and field.required:
                return None, f"'{field.label}' is required."
            cleaned[field.key] = text

    if not cleaned:
        return None, "Please fill in at least one field before sending."

    return cleaned, None


# ── PUBLIC endpoints ──────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def form_schema(request, key):
    """GET feedback/forms/<key>/ - the schema the public widget renders.

    PURPOSE  Hand the frontend a form's title, description and ordered fields so a new form can be
             added as DATA without shipping frontend code.
    REQUEST  No body. `key` is the form's public handle, e.g. "site_feedback".
    RESPONSE 200 {"form": {key, title, description, thank_you_message, fields: [...]}}
             404 {"message"} when the key is unknown OR the form is inactive.
    AUTH     None. This is a public page's first call, before the user has decided to say anything.
    CONSUMED BY  frontend components/feedback/FeedbackDialog.tsx (fetched when the dialog opens).

    An INACTIVE form 404s rather than returning is_active:false, so a retired form is simply not
    there as far as any client is concerned. submit_feedback re-checks the flag independently, so
    hiding it here is convenience, not the enforcement.
    """
    form = (
        FeedbackForm.objects.filter(key=key, is_active=True)
        .prefetch_related("fields")
        .first()
    )
    if not form:
        return Response(
            {"message": "This feedback form is not available."}, status=status.HTTP_404_NOT_FOUND
        )
    return Response({"form": _serialize_form(form)}, status=status.HTTP_200_OK)


@api_view(["POST"])
def submit_feedback(request, key):
    """POST feedback/forms/<key>/submit/ - store one filled-in form.

    PURPOSE  Accept feedback from ANY visitor, signed in or not.
    REQUEST  {"answers": {<field_key>: <value>, ...}, "page_path": "/tournaments/x", "locale": "fr"}
             `answers` is validated against the form's own fields (see _validate_answers); unknown
             keys are dropped. `page_path` is stripped to a bare path.
    RESPONSE 201 {"message", "submission_id", "thank_you_message"}
             400 {"message"} validation failed
             404 {"message"} unknown or inactive form
             429 {"message", "reason", "resets_at"} rate limited
    AUTH     OPTIONAL Bearer SessionToken. Sent -> the submission is attributed to that user; absent
             or expired -> stored anonymously. Never rejected for lack of a token.
    CONSUMED BY  frontend components/feedback/FeedbackDialog.tsx (the Send button).

    RATE LIMIT  Per sender (user id when known, else hashed IP): FEEDBACK_COOLDOWN_SECONDS between
    submissions and FEEDBACK_RATE_LIMIT_PER_HOUR per clock hour. The slot is consumed only AFTER the
    row is written, so a validation failure does not eat the visitor's allowance.
    """
    # Inactive forms refuse writes here, independently of form_schema hiding them, so a stale open tab
    # or a scripted client cannot post to a form the owner has retired.
    form = (
        FeedbackForm.objects.filter(key=key, is_active=True)
        .prefetch_related("fields")
        .first()
    )
    if not form:
        return Response(
            {"message": "This feedback form is not available."}, status=status.HTTP_404_NOT_FOUND
        )

    user = _optional_user(request)
    ip_hash = _client_ip_hash(request)
    identity = _rate_limit_identity(user, ip_hash)

    allowed, info = check_feedback_rate(identity)
    if not allowed:
        return Response(
            {
                "message": info["message"],
                "reason": info["reason"],
                "resets_at": info["resets_at"],
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    cleaned, error = _validate_answers(form, request.data.get("answers"))
    if error:
        return Response({"message": error}, status=status.HTTP_400_BAD_REQUEST)

    submission = FeedbackSubmission.objects.create(
        form=form,
        user=user,
        answers=cleaned,
        page_path=_clean_page_path(request.data.get("page_path")),
        # Freeze the questions as they read right now, so editing a label later cannot silently
        # rewrite what a past submitter appears to have been answering.
        fields_snapshot=[
            {"key": f.key, "label": f.label, "field_type": f.field_type}
            for f in form.fields.all()
        ],
        locale=str(request.data.get("locale") or "")[:8],
        user_agent=str(request.headers.get("User-Agent") or "")[:300],
        ip_hash=ip_hash,
    )

    # Consume the slot only now that the row exists.
    record_feedback_send(identity)

    return Response(
        {
            "message": "Thanks, your feedback has been sent.",
            "submission_id": submission.id,
            "thank_you_message": form.thank_you_message,
        },
        status=status.HTTP_201_CREATED,
    )


# ── ADMIN endpoints ───────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_forms(request):
    """GET feedback/admin/forms/ - every form, with its open/total counts.

    PURPOSE  Populate the admin page's "filter by form" control and show at a glance where the
             unhandled feedback is sitting.
    REQUEST  No parameters.
    RESPONSE 200 {"forms": [{key, title, is_active, total_count, open_count}]}
    AUTH     Bearer SessionToken, is_feedback_admin only.
    CONSUMED BY  frontend app/(a)/a/feedback/page.tsx (the form filter + the header counts).
    """
    user, error = _auth_user(request)
    if error:
        return error
    if not is_feedback_admin(user):
        return Response(
            {"message": "You do not have permission to view feedback."},
            status=status.HTTP_403_FORBIDDEN,
        )

    forms = FeedbackForm.objects.annotate(
        total_count=Count("submissions", distinct=True),
        open_count=Count(
            "submissions",
            filter=Q(submissions__status=FeedbackSubmission.OPEN),
            distinct=True,
        ),
    )
    return Response(
        {
            "forms": [
                {
                    "key": f.key,
                    "title": f.title,
                    "is_active": f.is_active,
                    "total_count": f.total_count,
                    "open_count": f.open_count,
                }
                for f in forms
            ]
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def admin_list_submissions(request):
    """GET feedback/admin/submissions/ - the triage queue, newest first.

    PURPOSE  Read what people sent, narrowed to one form and/or one status.
    REQUEST  Query params, all optional:
               form=<key>       only this form
               status=open|handled
               search=<text>    matches the answer text, the page path, or the username
               limit=<1..100>   default 25
               offset=<int>     default 0
    RESPONSE 200 {results: [...], has_more, next_offset, total_count, open_count}
             401/403 {"message"}
    AUTH     Bearer SessionToken, is_feedback_admin only.
    CONSUMED BY  frontend app/(a)/a/feedback/page.tsx (the table).
    """
    user, error = _auth_user(request)
    if error:
        return error
    if not is_feedback_admin(user):
        return Response(
            {"message": "You do not have permission to view feedback."},
            status=status.HTTP_403_FORBIDDEN,
        )

    qs = FeedbackSubmission.objects.select_related("form", "user", "handled_by")

    form_key = (request.GET.get("form") or "").strip()
    if form_key and form_key != "all":
        qs = qs.filter(form__key=form_key)

    status_filter = (request.GET.get("status") or "").strip()
    if status_filter in (FeedbackSubmission.OPEN, FeedbackSubmission.HANDLED):
        qs = qs.filter(status=status_filter)

    search = (request.GET.get("search") or "").strip()
    if search:
        # `answers` is JSON, so this is a substring match on its serialized text. Good enough for
        # "did anyone mention the shop", which is what an admin actually types here, and it avoids a
        # JSON-path query that MySQL and SQLite spell differently (the test suite runs on SQLite).
        qs = qs.filter(
            Q(answers__icontains=search)
            | Q(page_path__icontains=search)
            | Q(user__username__icontains=search)
        )

    total_count = qs.count()
    # The unhandled count for the CURRENT filter, so the header number matches what is on screen.
    open_count = qs.filter(status=FeedbackSubmission.OPEN).count()

    try:
        limit = min(max(int(request.GET.get("limit", DEFAULT_LIMIT)), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    rows = list(qs[offset : offset + limit])
    has_more = offset + len(rows) < total_count

    return Response(
        {
            "results": [_serialize_submission(s) for s in rows],
            "has_more": has_more,
            "next_offset": offset + len(rows) if has_more else None,
            "total_count": total_count,
            "open_count": open_count,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["PATCH"])
def admin_update_submission(request, submission_id):
    """PATCH feedback/admin/submissions/<id>/ - mark one submission handled (or reopen it).

    PURPOSE  Triage. This is the "mark handled" the brief asked for, plus an internal note.
    REQUEST  {"status": "handled"|"open", "admin_note": "..."} - both optional, send either or both.
    RESPONSE 200 {"message", "submission": {...}}
             400 unknown status | 401/403 | 404
    AUTH     Bearer SessionToken, is_feedback_admin only.
    CONSUMED BY  frontend app/(a)/a/feedback/page.tsx (the row's Mark handled / Reopen action).

    IDEMPOTENT: PATCHing status="handled" twice leaves the same end state. handled_by/handled_at are
    stamped on the transition to handled and cleared on reopen, so the audit trail never claims
    someone handled a row that is currently open.
    """
    user, error = _auth_user(request)
    if error:
        return error
    if not is_feedback_admin(user):
        return Response(
            {"message": "You do not have permission to update feedback."},
            status=status.HTTP_403_FORBIDDEN,
        )

    submission = (
        FeedbackSubmission.objects.select_related("form", "user", "handled_by")
        .filter(id=submission_id)
        .first()
    )
    if not submission:
        return Response({"message": "Submission not found."}, status=status.HTTP_404_NOT_FOUND)

    if "status" in request.data:
        new_status = str(request.data.get("status") or "").strip()
        if new_status not in (FeedbackSubmission.OPEN, FeedbackSubmission.HANDLED):
            return Response(
                {"message": "status must be 'open' or 'handled'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        submission.status = new_status
        if new_status == FeedbackSubmission.HANDLED:
            submission.handled_by = user
            submission.handled_at = timezone.now()
        else:
            submission.handled_by = None
            submission.handled_at = None

    if "admin_note" in request.data:
        submission.admin_note = str(request.data.get("admin_note") or "").strip()[:5000]

    submission.save()

    return Response(
        {"message": "Feedback updated.", "submission": _serialize_submission(submission)},
        status=status.HTTP_200_OK,
    )
