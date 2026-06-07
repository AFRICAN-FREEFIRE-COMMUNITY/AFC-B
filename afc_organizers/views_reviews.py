# afc_organizers/views_reviews.py
# ──────────────────────────────────────────────────────────────────────────────
# Event reviews (ratings + comments) and per-organization performance metrics.
#
# This module holds the "how good was this event / how is this org doing" surface:
#   • Users rate an event 1–5 and leave free-text comments.
#   • The aggregate rating is PUBLIC (anyone can read the average + count).
#   • Individual ratings and comments are ANONYMOUS to the organizer — an organizer
#     only ever sees the aggregate score and de-identified comment text, never who
#     said what. This is enforced here at the serialization boundary (we simply do
#     not emit the commenter's identity), mirroring the EventRating/EventComment
#     model docstrings ("ANONYMOUS to the organizer").
#   • org_metrics rolls every event an organization owns into one flat stat block
#     for the organizer dashboard (events, teams, players, kills, rating).
#
# Why this file mirrors afc_team / afc_tournament_and_scrims and the sibling
# views_organizer.py (and NOT the rankings app):
#   • Function-based @api_view views, one concern per function.
#   • The auth handshake is done inline at the top of every protected view: read the
#     "Authorization" header, require the "Bearer " prefix (400 if missing/malformed),
#     split the token, and hand it to afc_auth's validate_token() (401 if invalid) —
#     the exact pattern views_organizer.py / afc_team/views.py use.
#   • Responses are serialized INLINE as plain dicts (no serializers.py module) so the
#     original developer can read a view top-to-bottom and see the whole shape at once.
#
# Permission gating routes through afc_organizers.permissions (org_can / org_can_event /
# is_platform_org_admin) so the owner / AFC-admin bypass stays in ONE place — it is never
# re-implemented here.
#
# Route mounting lives in afc_organizers/urls.py and is owned by the coordinator — this
# file ONLY defines the view function(s). Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from collections import OrderedDict
from datetime import date

from django.db.models import Avg, Count, Sum
# parse_date turns a "YYYY-MM-DD" query param into a date, or None for absent/malformed
# input — the SAME forgiving posture afc_rankings.admin_audit._parse_date and
# afc_tournament_and_scrims.views use for date filters (a typo degrades to "no filter",
# never a 500).
from django.utils.dateparse import parse_date

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token is imported the SAME way views_organizer.py / afc_team/views.py import
# it (from the auth *views* module) so the auth handshake here is byte-for-byte identical.
# It returns the User for a valid, non-expired session token, or None otherwise.
from afc_auth.views import validate_token

from afc_organizers.models import (
    Organization,
    EventRating,
    EventComment,
)
from afc_organizers.permissions import org_can, org_can_event

# Tournament + stats models live in afc_tournament_and_scrims. We pull in exactly the
# ones org_metrics aggregates over so the field/relation names below are the REAL ones.
from afc_tournament_and_scrims.models import (
    Event,
    Match,
    TournamentTeam,
    TournamentTeamMember,
    RegisteredCompetitors,
    TournamentTeamMatchStats,
    TournamentPlayerMatchStats,
    SoloPlayerMatchStats,
    EventPrizePayout,
    # EventPageView rows are written once per event-detail load by
    # afc_tournament_and_scrims.views.get_event_details (one timestamped row per view,
    # carrying the viewer's user + ip). org_metrics rolls them into view/visitor counts.
    EventPageView,
)


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (DRY — used by several views below)
# ──────────────────────────────────────────────────────────────────────────────
def _require_auth(request):
    """Run the standard Bearer handshake and return (user, error_response).

    Exactly one of the two is non-None:
      • a valid token  → (User, None)
      • missing header  → (None, 400)   ("Authorization header is required")
      • bad prefix      → (None, 400)   ("Invalid token format")
      • invalid token   → (None, 401)   ("Invalid or expired session token.")

    Every PROTECTED view in this file calls this so the 400-vs-401 contract is identical
    to views_organizer.py. The OPTIONAL-auth view (event_rating) deliberately does NOT use
    this — see _optional_user below."""
    session_token = request.headers.get("Authorization")
    if not session_token:
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _optional_user(request):
    """Best-effort identity for endpoints where auth is OPTIONAL (anonymous viewers are
    welcome). Parse the Bearer token IF present and valid, otherwise return None — never
    raise, never 400/401. Used by event_rating so a logged-out viewer still gets the public
    aggregate while a logged-in viewer additionally gets their own my_score."""
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return None
    # Token may still be expired/garbage — validate_token returns None in that case, which
    # is exactly the "treat as anonymous" outcome we want here.
    return validate_token(session_token.split(" ")[1])


def _rating_aggregate(event_id):
    """Return (average_or_None, count) over EventRating for one event, computed in a single
    DB round-trip. average is left as a raw float (callers round/format as their contract
    needs); count is 0 (and average None) when the event has no ratings yet."""
    # EventRating declares no explicit pk, so Django's auto "id" IS the primary key — Count("id")
    # is the model-agnostic way to count rows alongside the Avg in one query.
    agg = EventRating.objects.filter(event_id=event_id).aggregate(
        average=Avg("score"), count=Count("id"),
    )
    return agg["average"], agg["count"]


# ──────────────────────────────────────────────────────────────────────────────
# §1  POST /events/<event_id>/rate  — rate an event 1–5 (AUTH REQUIRED)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def rate_event(request, event_id):
    """Create or update the caller's 1–5 rating for an event. One rating per (event, user)
    — re-rating overwrites the previous score (update_or_create on the unique pair). Returns
    the caller's score plus the fresh public aggregate so the frontend can update in place."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _require_auth(request)
    if err:
        return err

    # ── validate the score is an int in 1..5 ──
    # Coerce defensively: JSON may deliver "4" (string) from some clients; reject anything
    # that is not a whole number in range rather than silently storing junk.
    raw_score = request.data.get("score")
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        return Response(
            {"message": "score must be an integer between 1 and 5."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if score < 1 or score > 5:
        return Response(
            {"message": "score must be an integer between 1 and 5."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── upsert the rating ──
    # unique_together(event, user) means a user has at most ONE rating row per event, so we
    # update_or_create rather than risk a duplicate / IntegrityError on a re-rate.
    EventRating.objects.update_or_create(
        event_id=event_id, user=user, defaults={"score": score},
    )

    # Recompute the aggregate AFTER the upsert so the caller sees the post-write truth.
    average, count = _rating_aggregate(event_id)

    return Response(
        {
            "message": "Rating saved.",
            "my_score": score,
            # round to 1dp for display; None stays None (cannot happen here since we just
            # wrote a row, but kept consistent with event_rating's contract).
            "average": round(average, 1) if average is not None else None,
            "count": count,
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET /events/<event_id>/rating  — public aggregate (+ my_score) (AUTH OPTIONAL)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def event_rating(request, event_id):
    """Return an event's PUBLIC rating aggregate (average rounded to 1dp + count). Auth is
    OPTIONAL: if a valid Bearer token is present we also return the caller's own my_score so
    the UI can pre-fill their stars; if it is missing/invalid we simply return my_score=null.
    Anonymous viewers are NEVER rejected — the aggregate is a public surface."""
    # Optional identity — never 400/401 here. A logged-out viewer just gets my_score=null.
    user = _optional_user(request)

    average, count = _rating_aggregate(event_id)

    # my_score only when we resolved a real user AND they have rated this event.
    my_score = None
    if user is not None:
        my_rating = EventRating.objects.filter(event_id=event_id, user=user).first()
        if my_rating is not None:
            my_score = my_rating.score

    return Response(
        {
            "average": round(average, 1) if average is not None else None,
            "count": count,
            "my_score": my_score,
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  POST /events/<event_id>/comment  — leave a comment on an event (AUTH REQUIRED)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def comment_event(request, event_id):
    """Attach a free-text comment to an event from the current user. Empty / whitespace-only
    text is rejected (400). The comment is stored against the user for moderation, but is
    ANONYMOUS to the organizer when read back (see event_comments below)."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _require_auth(request)
    if err:
        return err

    # ── validate the body text is non-empty ──
    text = (request.data.get("text") or "").strip()
    if not text:
        return Response(
            {"message": "Comment text is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Store with both the event and the author (author kept for moderation only — it is
    # never surfaced to the organizer via the read endpoint).
    EventComment.objects.create(event_id=event_id, user=user, text=text)

    return Response(
        {"message": "Comment posted."},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  GET /events/<event_id>/comments  — organizer reads event comments (AUTH REQUIRED)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def event_comments(request, event_id):
    """Return an event's comments for the organizer, NEWEST FIRST. Gated by
    org_can_event(can_view_reviews) — only the event's own org (or an AFC admin) may read.
    The response is DE-IDENTIFIED: it carries only {text, created_at}, never the commenter's
    identity, consistent with reviews being anonymous to organizers."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _require_auth(request)
    if err:
        return err

    # Resolve the event first so the permission check has the owning org to gate against.
    event = Event.objects.filter(event_id=event_id).first()
    if not event:
        return Response(
            {"message": "Event not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Permission gate — central owner/admin bypass via org_can_event.
    if not org_can_event(user, "can_view_reviews", event):
        return Response(
            {"message": "You do not have permission to view reviews for this event."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Newest first. We intentionally select ONLY text + created_at — the commenter is never
    # exposed (anonymity to the organizer is enforced here at the serialization boundary).
    comments = EventComment.objects.filter(event=event).order_by("-created_at")

    results = [
        {
            "text": comment.text,
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
        }
        for comment in comments
    ]

    return Response({"results": results}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# §5  GET /<slug>/metrics  — per-organization performance metrics (AUTH REQUIRED)
# ──────────────────────────────────────────────────────────────────────────────
# ── §5 helpers (org_metrics only) ───────────────────────────────────────────────
# A hard ceiling on every "top N" / per-event list this endpoint returns. The brief
# asks for rich data but also for it to stay bounded — these lists are sorted + sliced
# server-side so the payload can never balloon, no matter how many events an org runs.
_TOP_N = 10            # top teams / top players leaderboards
_PER_EVENT_LIMIT = 50  # per-event breakdown rows (most orgs run far fewer)

# ── What counts as a COMPLETE registration ─────────────────────────────────────
# "Complete" = the registrant is a CONFIRMED participant in the event (a real entrant the
# organizer can count on), vs "incomplete" = a registration that exists but never resolved
# into a confirmed entrant (pending approval, rejected, withdrawn, left, disqualified).
# These two sets mirror the values the rest of afc_tournament_and_scrims already treats as
# confirmed, so this endpoint stays consistent with how registrations are counted elsewhere:
#   • Solo (RegisteredCompetitors.status): the registration flow confirms an entrant as
#     "registered" (and admin-approval flows use "approved"); see views.py filters such as
#     status__in=["registered", "approved"]. Everything else (pending/rejected/withdrawn/
#     left/disqualified) is a registration that did NOT become a confirmed entrant.
#   • Team (TournamentTeam.status): a confirmed team entry is "active" (see
#     tournament_teams.filter(status="active") across views.py). disqualified/withdrawn/left
#     are entries that did not remain confirmed.
# INCOMPLETE for either shape is simply "a registration row whose status is not in the
# complete set" — we never need to enumerate the incomplete values, we subtract.
_SOLO_COMPLETE_STATUSES = ("registered", "approved")
_TEAM_COMPLETE_STATUSES = ("active",)


def _month_key(d):
    """Bucket a date into a 'YYYY-MM' month key, or None when the date is missing.
    Used to roll events/participants/matches into a monthly trend series."""
    if not d:
        return None
    return f"{d.year:04d}-{d.month:02d}"


def _parse_range(request):
    """Read the optional ?start / ?end query params (each "YYYY-MM-DD") and return
    (start_date_or_None, end_date_or_None).

    Forgiving by design (mirrors afc_rankings.admin_audit._parse_date): a missing OR
    malformed value yields None for that bound, so a typo degrades to "no filter on that
    side" rather than a 500. When BOTH are None the endpoint behaves exactly as before
    (all-time). The bounds are INCLUSIVE day boundaries — callers below apply them as
    `<timestamp>__date__gte=start` / `<timestamp>__date__lte=end` so a plain date captures
    the whole day on a DateTimeField, not just midnight (same trick admin_audit uses)."""
    start = parse_date(request.query_params.get("start") or "")
    end = parse_date(request.query_params.get("end") or "")
    return start, end


def _apply_range(qs, field, start, end):
    """AND a [start, end] INCLUSIVE day-range filter onto `qs` over the date/datetime
    column named by `field` (e.g. "registration_date", "viewed_at", "match_date",
    "created_at"). No-ops for whichever bound is None, so all-time is just "pass None, None".

    Uses the `__date__gte` / `__date__lte` lookups so the filter is evaluated DB-side on the
    DATE part of a DateTimeField — efficient (no Python-side row scan) and identical to the
    convention in afc_rankings.admin_audit.audit_log."""
    if start:
        qs = qs.filter(**{f"{field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lte": end})
    return qs


@api_view(["GET"])
def org_metrics(request, slug):
    """Roll up every event an organization owns into ONE rich metrics payload for the
    organizer Metrics dashboard (app/(organizer)/organizer/metrics/page.tsx, fetched via
    organizersApi.getOrgMetrics(slug, { start, end })). Gated by org_can(can_view_metrics).

    OPTIONAL DATE RANGE (?start=YYYY-MM-DD&?end=YYYY-MM-DD, INCLUSIVE day bounds):
      When either param is present, every TIME-BOUNDED aggregate is restricted to that window
      on the relevant timestamp — registrations by registration_date, matches (and the kills
      derived from them) by Match.match_date, page views by viewed_at, ratings by created_at,
      and the monthly trend buckets by those same registration/match timestamps. When BOTH
      are absent the endpoint is all-time (its original behaviour). NOTE: the set of events
      itself is NOT date-filtered (an org's event roster is small and an event's start_date is
      a single day); instead each event's IN-RANGE activity is measured. The chosen window is
      echoed back as {start, end} (ISO or null) so the frontend can confirm what it received.

    WHAT IT RETURNS (every list is bounded + sorted server-side, so the payload stays small
    no matter how many events the org has run; every count below respects the date range):
      • totals            - events run, by-status / by-type / by-mode / by-tier splits, unique
                            teams + players, total + complete + incomplete registrations,
                            total matches, total kills, prize money awarded, average
                            participants per event, registration fill-rate, completion rate.
      • registrations     - {complete, incomplete, total} across the org (in range). COMPLETE
                            = confirmed entrant (solo "registered"/"approved", team "active");
                            INCOMPLETE = any other registration row (see _*_COMPLETE_STATUSES).
      • page_views        - {total_views, unique_viewers} from EventPageView for the org's
                            events (in range). unique_viewers de-dupes logged-in viewers by
                            user_id and anonymous viewers by ip_address, then sums the two.
      • rating            - average rating + count across the org's events (in range).
      • monthly[]         - registrations / matches / page views per calendar month (the trend
                            series the frontend plots), built from the in-range timestamps.
      • top_teams[]       - the org's most successful teams (by in-range kills, wins alongside).
      • top_players[]     - the org's most active players (by in-range kills). Top _TOP_N.
      • events[]          - a per-event breakdown row enriched with: participants, complete vs
                            incomplete registrations, views + unique viewers, matches, kills,
                            prize, and rating (avg + count) — all measured IN RANGE. Capped at
                            _PER_EVENT_LIMIT, newest first by start_date.

    EFFICIENCY (this repo just fixed several N+1s — keep that discipline):
      Every metric is a single grouped/aggregate query over the event-id set or a
      .values(...).annotate(...) group-by. There is NO per-event loop that hits the DB:
      per-event participant counts, registrations, views, kills, prizes and ratings are each
      computed in ONE grouped query and then merged into the events[] rows in Python by id.

    Aggregates coalesce None→0 so a brand-new org with no events (or an empty window) reports
    clean zeros and empty `events`/`monthly`/`top_*` rather than erroring (the frontend renders
    a calm empty state off the zeros, never an infinite spinner)."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _require_auth(request)
    if err:
        return err

    # Resolve the org by its public slug (404 if missing — same body the rest of the app uses).
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Permission gate — central owner/admin bypass via org_can.
    if not org_can(user, "can_view_metrics", org):
        return Response(
            {"message": "You do not have permission to view metrics for this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── Date-range window (both optional; None,None == all-time, the original behaviour). ──
    # `start`/`end` are inclusive day bounds applied via _apply_range on each timestamped query.
    start, end = _parse_range(request)

    # ── The org's events (one fetch). Every metric below is scoped to these rows. ──
    # We pull the columns we need once and iterate in Python for the splits/trend so we never
    # re-query per event. `max_teams_or_players` is the registration capacity (drives fill-rate).
    events = list(
        Event.objects.filter(organization=org).values(
            "event_id", "event_name", "competition_type", "participant_type",
            "event_mode", "event_status", "start_date", "max_teams_or_players",
            "prizepool_cash_value", "tournament_tier",
        )
    )
    event_ids = [e["event_id"] for e in events]
    events_count = len(event_ids)

    # ════════════════════ §A  Per-event grouped queries (no N+1) ════════════════════
    # Each block below is ONE grouped query keyed by event_id; results land in a dict so the
    # per-event rows + headline totals are assembled in memory, never with a query-per-event.
    # Every TIME-BOUNDED query is wrapped in _apply_range(...) so the [start, end] window (when
    # supplied) is enforced DB-side on the right timestamp. Event-roster facts that have no
    # meaningful per-row timestamp to filter on (capacity, by_status splits) stay all-time.

    # ── Registered teams per event, split COMPLETE vs INCOMPLETE (TournamentTeam.event is a
    # direct FK). Confirmed team entries are status="active" (_TEAM_COMPLETE_STATUSES); any
    # other status is an incomplete entry. Filtered to the window on registration_date so the
    # range reflects "teams that registered in this window". ──
    teams_per_event = {}                 # event_id → total team registrations (in range)
    teams_complete_per_event = {}        # event_id → confirmed (active) team registrations
    _team_reg_qs = _apply_range(
        TournamentTeam.objects.filter(event_id__in=event_ids),
        "registration_date", start, end,
    )
    for row in (
        _team_reg_qs.values("event_id", "status").annotate(c=Count("tournament_team_id"))
    ):
        eid = row["event_id"]
        teams_per_event[eid] = teams_per_event.get(eid, 0) + row["c"]
        if row["status"] in _TEAM_COMPLETE_STATUSES:
            teams_complete_per_event[eid] = teams_complete_per_event.get(eid, 0) + row["c"]

    # ── Solo registrations per event, split COMPLETE vs INCOMPLETE (RegisteredCompetitors.event
    # is a direct FK). Confirmed solo entrants are status in ("registered","approved")
    # (_SOLO_COMPLETE_STATUSES); anything else (pending/rejected/withdrawn/left/disqualified) is
    # incomplete. user_id__isnull=False keeps "a real human registered" the participant unit
    # (unchanged from before). Windowed on registration_date. ──
    solo_regs_per_event = {}             # event_id → total solo registrations (in range)
    solo_complete_per_event = {}         # event_id → confirmed solo registrations
    _solo_reg_qs = _apply_range(
        RegisteredCompetitors.objects.filter(event_id__in=event_ids, user_id__isnull=False),
        "registration_date", start, end,
    )
    for row in _solo_reg_qs.values("event_id", "status").annotate(c=Count("id")):
        eid = row["event_id"]
        solo_regs_per_event[eid] = solo_regs_per_event.get(eid, 0) + row["c"]
        if row["status"] in _SOLO_COMPLETE_STATUSES:
            solo_complete_per_event[eid] = solo_complete_per_event.get(eid, 0) + row["c"]

    # ── Page views per event + unique viewers per event (EventPageView.event is a direct FK;
    # one row per event-detail load, written by get_event_details). Windowed on viewed_at.
    #   • views   = raw row count (every load counts).
    #   • unique  = distinct logged-in users (user_id) PLUS distinct anonymous IPs (ip_address
    #               where user_id is null) — a de-dupe that counts each human ~once whether or
    #               not they were logged in. Two grouped distinct-count queries (logged-in vs
    #               anonymous), merged per event so there is still no per-event DB loop. ──
    _pv_qs = _apply_range(
        EventPageView.objects.filter(event_id__in=event_ids), "viewed_at", start, end,
    )
    views_per_event = {
        row["event_id"]: row["c"]
        for row in _pv_qs.values("event_id").annotate(c=Count("id"))
    }
    # distinct logged-in viewers per event (user_id not null)
    unique_user_views_per_event = {
        row["event_id"]: row["c"]
        for row in _pv_qs.filter(user_id__isnull=False)
        .values("event_id").annotate(c=Count("user_id", distinct=True))
    }
    # distinct anonymous viewers per event (no user_id → de-dupe by ip_address)
    unique_anon_views_per_event = {
        row["event_id"]: row["c"]
        for row in _pv_qs.filter(user_id__isnull=True, ip_address__isnull=False)
        .values("event_id").annotate(c=Count("ip_address", distinct=True))
    }
    # per-event unique viewers = distinct logged-in users + distinct anonymous IPs.
    unique_views_per_event = {
        eid: unique_user_views_per_event.get(eid, 0) + unique_anon_views_per_event.get(eid, 0)
        for eid in set(unique_user_views_per_event) | set(unique_anon_views_per_event)
    }

    # ── Kills per event, summed across BOTH stats tables ──
    # Match → StageGroups (group) → Stages (stage) → Event, so the event id lives at
    # match__group__stage__event_id on both stats tables. We group on that real lookup
    # path and read the key back verbatim (Django returns "<path>" as the dict key). Two
    # grouped queries (team + solo), windowed on the parent Match.match_date, merged below.
    team_kills_per_event = {
        row["match__group__stage__event"]: row["k"] or 0
        for row in _apply_range(
            TournamentTeamMatchStats.objects.filter(
                match__group__stage__event_id__in=event_ids
            ),
            "match__match_date", start, end,
        ).values("match__group__stage__event").annotate(k=Sum("kills"))
    }
    solo_kills_per_event = {
        row["match__group__stage__event"]: row["k"] or 0
        for row in _apply_range(
            SoloPlayerMatchStats.objects.filter(
                match__group__stage__event_id__in=event_ids
            ),
            "match__match_date", start, end,
        ).values("match__group__stage__event").annotate(k=Sum("kills"))
    }

    # ── Matches per event (Match → group → stage → event), windowed on match_date ──
    matches_per_event = {
        row["group__stage__event"]: row["c"]
        for row in _apply_range(
            Match.objects.filter(group__stage__event_id__in=event_ids),
            "match_date", start, end,
        ).values("group__stage__event").annotate(c=Count("match_id"))
    }

    # ── Prize money awarded per event (EventPrizePayout.amount summed) ──
    # No reliable per-payout timestamp to window on, so prize money is reported all-time even
    # under a date range (a payout is a property of the event, not an in-window activity).
    prize_per_event = {
        row["event_id"]: float(row["a"] or 0)
        for row in EventPrizePayout.objects.filter(event_id__in=event_ids)
        .values("event_id").annotate(a=Sum("amount"))
    }

    # ── Rating aggregate per event (average + count), windowed on created_at ──
    rating_per_event = {
        row["event_id"]: {"average": row["avg"], "count": row["c"]}
        for row in _apply_range(
            EventRating.objects.filter(event_id__in=event_ids), "created_at", start, end,
        ).values("event_id").annotate(avg=Avg("score"), c=Count("id"))
    }

    # ── Org-wide page-view headline numbers (in range), de-duped ACROSS events ──
    # Per-event unique counts can't simply be summed for the org headline (a visitor who viewed
    # two of the org's events must count once org-wide), so we recompute org-level distincts in
    # two cheap aggregate queries over the SAME windowed page-view queryset built above.
    total_views = _pv_qs.count()                                  # raw views in range
    org_unique_user_views = (
        _pv_qs.filter(user_id__isnull=False)
        .aggregate(c=Count("user_id", distinct=True))["c"] or 0
    )
    org_unique_anon_views = (
        _pv_qs.filter(user_id__isnull=True, ip_address__isnull=False)
        .aggregate(c=Count("ip_address", distinct=True))["c"] or 0
    )
    # org-wide unique viewers = distinct logged-in users + distinct anonymous IPs (in range).
    unique_viewers = org_unique_user_views + org_unique_anon_views

    # ════════════════════ §B  Distinct teams + players across the org ════════════════════
    # Distinct counts can't come from the per-event sums (a team/player who plays two of the
    # org's events must count once), so we pull the id sets ONCE and union them. All three
    # queries are windowed on registration_date so "unique" tracks the selected range too.

    # Distinct teams = distinct Team behind the org's TournamentTeam rows (in range).
    unique_teams = (
        _apply_range(
            TournamentTeam.objects.filter(event_id__in=event_ids),
            "registration_date", start, end,
        ).values("team_id").distinct().count()
    )

    # Distinct players = union of team-event members + solo-event competitors (None excluded),
    # so a human counted in either registration shape is counted exactly once. TournamentTeamMember
    # has no own timestamp, so we window it via its parent TournamentTeam.registration_date.
    team_player_ids = set(
        _apply_range(
            TournamentTeamMember.objects.filter(
                tournament_team__event_id__in=event_ids, user_id__isnull=False
            ),
            "tournament_team__registration_date", start, end,
        ).values_list("user_id", flat=True)
    )
    solo_player_ids = set(
        _apply_range(
            RegisteredCompetitors.objects.filter(
                event_id__in=event_ids, user_id__isnull=False
            ),
            "registration_date", start, end,
        ).values_list("user_id", flat=True)
    )
    unique_players = len(team_player_ids | solo_player_ids)

    # ════════════════════ §C  Headline totals + splits (in-memory over `events`) ════════════════════
    # NB: the by_status / by_type / by_mode / by_tier splits and capacity describe the org's
    # event ROSTER, which is intentionally NOT date-filtered (an org has few events and these
    # describe the events themselves, not in-window activity). Everything counting registrations
    # / participants / matches / views below already came from the windowed §A dicts.
    by_status, by_type, by_mode, by_tier = {}, {}, {}, {}
    total_capacity = 0
    total_participants = 0          # team-or-solo registrations summed across events (in range)
    completed_count = 0

    # One pass over the org's events fills the splits and the capacity/fill-rate inputs —
    # all without another DB hit.
    for e in events:
        eid = e["event_id"]
        status_key = e["event_status"] or "unknown"
        type_key = e["competition_type"] or "unknown"
        mode_key = e["event_mode"] or "unknown"
        tier_key = e["tournament_tier"] or "unknown"
        by_status[status_key] = by_status.get(status_key, 0) + 1
        by_type[type_key] = by_type.get(type_key, 0) + 1
        by_mode[mode_key] = by_mode.get(mode_key, 0) + 1
        by_tier[tier_key] = by_tier.get(tier_key, 0) + 1
        if status_key == "completed":
            completed_count += 1

        # participants for THIS event = registered teams (team events) + solo registrations.
        # An event is one shape or the other, so summing both is safe (the other is just 0).
        participants = teams_per_event.get(eid, 0) + solo_regs_per_event.get(eid, 0)
        total_participants += participants
        # capacity sums max_teams_or_players for the fill-rate (skip null/0 capacities).
        total_capacity += e["max_teams_or_players"] or 0

    # ── Registration completeness totals (in range), summed across the org's events ──
    # complete = confirmed entrants (team "active" + solo "registered"/"approved");
    # incomplete = every other registration row = total registrations - complete.
    registrations_complete = (
        sum(teams_complete_per_event.values()) + sum(solo_complete_per_event.values())
    )
    registrations_total = total_participants  # team + solo registration rows (in range)
    registrations_incomplete = registrations_total - registrations_complete

    # ── Monthly trend (in range), built from the ACTUAL activity timestamps so the chart moves
    # with the selected window — three grouped TruncMonth queries (registrations by
    # registration_date, matches by match_date, page views by viewed_at), merged by month key.
    # This replaces the old "bucket events by start_date" trend, which ignored the date range.
    from django.db.models.functions import TruncMonth  # local import keeps the header lean

    monthly = OrderedDict()  # 'YYYY-MM' → {registrations, matches, views}

    def _bump(month_dt, key, n):
        """Add n to monthly[YYYY-MM][key], creating the bucket on first touch."""
        mk = _month_key(month_dt)
        if not mk:
            return
        bucket = monthly.setdefault(mk, {"registrations": 0, "matches": 0, "views": 0})
        bucket[key] += n

    # registrations per month = team registrations + solo registrations, both windowed.
    for row in (
        _apply_range(
            TournamentTeam.objects.filter(event_id__in=event_ids), "registration_date", start, end,
        ).annotate(m=TruncMonth("registration_date")).values("m").annotate(c=Count("tournament_team_id"))
    ):
        _bump(row["m"], "registrations", row["c"])
    for row in (
        _apply_range(
            RegisteredCompetitors.objects.filter(event_id__in=event_ids, user_id__isnull=False),
            "registration_date", start, end,
        ).annotate(m=TruncMonth("registration_date")).values("m").annotate(c=Count("id"))
    ):
        _bump(row["m"], "registrations", row["c"])
    # matches per month, windowed on match_date.
    for row in (
        _apply_range(
            Match.objects.filter(group__stage__event_id__in=event_ids), "match_date", start, end,
        ).annotate(m=TruncMonth("match_date")).values("m").annotate(c=Count("match_id"))
    ):
        _bump(row["m"], "matches", row["c"])
    # page views per month, windowed on viewed_at.
    for row in (
        _pv_qs.annotate(m=TruncMonth("viewed_at")).values("m").annotate(c=Count("id"))
    ):
        _bump(row["m"], "views", row["c"])

    # Sort the monthly series chronologically (the frontend plots it left→right oldest→newest).
    monthly_list = [
        {"month": mk, **vals} for mk, vals in sorted(monthly.items(), key=lambda kv: kv[0])
    ]

    # Totals derived from the per-event dicts (all in memory now; all in range).
    total_matches = sum(matches_per_event.values())
    total_kills = sum(team_kills_per_event.values()) + sum(solo_kills_per_event.values())
    total_prize_money = sum(prize_per_event.values())
    registered_teams = sum(teams_per_event.values())

    # average participants per event (guard divide-by-zero for a brand-new org).
    avg_participants = round(total_participants / events_count, 1) if events_count else 0
    # registration fill-rate = how full the org's events get, as a % of summed capacity.
    fill_rate = (
        round((total_participants / total_capacity) * 100, 1) if total_capacity else None
    )
    # completion rate = share of the org's events that have reached "completed" (roster fact,
    # not date-bounded — it reflects the org's events overall).
    completion_rate = (
        round((completed_count / events_count) * 100, 1) if events_count else None
    )

    # ── Org-wide rating aggregate (one query; None→null, 0 count for a fresh org/empty window).
    #    Windowed on created_at so the headline rating tracks the selected range too. ──
    rating_agg = _apply_range(
        EventRating.objects.filter(event_id__in=event_ids), "created_at", start, end,
    ).aggregate(average=Avg("score"), count=Count("id"))
    average_rating = (
        round(rating_agg["average"], 1) if rating_agg["average"] is not None else None
    )
    ratings_count = rating_agg["count"]

    # ════════════════════ §D  Top teams + top players (grouped, then sliced to _TOP_N) ════════════════════
    # Top teams: group the team match-stats by the owning Team and rank by total kills (windowed
    # on match_date). wins = count of TournamentTeam rows flagged is_tournament_winner for that
    # team in this org (a result marker with no timestamp, so it stays all-time). We fetch kills
    # (grouped) and the winner flags (grouped) in two queries, then merge by team_id.
    team_kill_rows = list(
        _apply_range(
            TournamentTeamMatchStats.objects.filter(
                match__group__stage__event_id__in=event_ids
            ),
            "match__match_date", start, end,
        )
        .values("tournament_team__team_id", "tournament_team__team__team_name")
        .annotate(kills=Sum("kills"))
    )
    # wins per team across the org's events (is_tournament_winner is set at result entry).
    team_win_rows = {
        row["team_id"]: row["w"]
        for row in TournamentTeam.objects.filter(
            event_id__in=event_ids, is_tournament_winner=True
        ).values("team_id").annotate(w=Count("tournament_team_id"))
    }
    top_teams = sorted(
        (
            {
                "team_id": r["tournament_team__team_id"],
                "team_name": r["tournament_team__team__team_name"] or "Unknown team",
                "kills": r["kills"] or 0,
                "wins": team_win_rows.get(r["tournament_team__team_id"], 0),
            }
            for r in team_kill_rows
        ),
        key=lambda t: (t["kills"], t["wins"]),
        reverse=True,
    )[:_TOP_N]

    # Top players: kills come from BOTH player-stat shapes. Team-format player kills live on
    # TournamentPlayerMatchStats.player; solo-format kills attach to a RegisteredCompetitors
    # row whose .user is the player. We group each by the underlying user_id, sum kills, then
    # merge the two id→kills maps so a player active in both shapes is summed once.
    # Both player-kill queries are windowed on the parent Match.match_date so the leaderboard
    # tracks the selected range (TournamentPlayerMatchStats reaches the match via team_stats).
    team_player_kill_rows = {
        row["player_id"]: row["k"] or 0
        for row in _apply_range(
            TournamentPlayerMatchStats.objects.filter(
                team_stats__match__group__stage__event_id__in=event_ids
            ),
            "team_stats__match__match_date", start, end,
        ).values("player_id").annotate(k=Sum("kills"))
    }
    solo_player_kill_rows = {
        row["competitor__user_id"]: row["k"] or 0
        for row in _apply_range(
            SoloPlayerMatchStats.objects.filter(
                match__group__stage__event_id__in=event_ids,
                competitor__user_id__isnull=False,
            ),
            "match__match_date", start, end,
        ).values("competitor__user_id").annotate(k=Sum("kills"))
    }
    merged_player_kills = dict(team_player_kill_rows)
    for uid, k in solo_player_kill_rows.items():
        merged_player_kills[uid] = merged_player_kills.get(uid, 0) + k
    # Resolve the top players' usernames in ONE query (avoids a per-player name lookup).
    top_player_ids = [
        uid for uid, _ in sorted(
            merged_player_kills.items(), key=lambda kv: kv[1], reverse=True
        )[:_TOP_N]
        if uid is not None
    ]
    from afc_auth.models import User  # local import keeps the module header import list lean
    username_by_id = {
        u["user_id"]: u["username"]
        for u in User.objects.filter(user_id__in=top_player_ids).values("user_id", "username")
    }
    top_players = [
        {
            "user_id": uid,
            "username": username_by_id.get(uid, "Unknown player"),
            "kills": merged_player_kills.get(uid, 0),
        }
        for uid in top_player_ids
    ]

    # ════════════════════ §E  Per-event breakdown rows (newest first, capped) ════════════════════
    # Sort the org's events newest-first by start_date (undated events sink to the bottom), cap
    # at _PER_EVENT_LIMIT, and stitch in each event's per-event metric from the dicts above —
    # all in memory, no further DB work.
    def _sort_key(e):
        # date.min for undated events so they order last under reverse=True.
        return e["start_date"] or date.min
    sorted_events = sorted(events, key=_sort_key, reverse=True)[:_PER_EVENT_LIMIT]
    event_rows = []
    for e in sorted_events:
        eid = e["event_id"]
        rating = rating_per_event.get(eid, {})
        avg = rating.get("average")
        # complete = confirmed entrants for THIS event (team "active" + solo
        # "registered"/"approved"); incomplete = the remaining registration rows (in range).
        complete_regs = (
            teams_complete_per_event.get(eid, 0) + solo_complete_per_event.get(eid, 0)
        )
        participants = teams_per_event.get(eid, 0) + solo_regs_per_event.get(eid, 0)
        event_rows.append({
            "event_id": eid,
            "event_name": e["event_name"],
            "start_date": e["start_date"].isoformat() if e["start_date"] else None,
            "event_status": e["event_status"],
            "competition_type": e["competition_type"],
            "participant_type": e["participant_type"],
            "tournament_tier": e["tournament_tier"],
            # participants = all registration rows (team+solo) for this event in range.
            "participants": participants,
            "complete_registrations": complete_regs,
            "incomplete_registrations": participants - complete_regs,
            "capacity": e["max_teams_or_players"] or 0,
            # page-view detail for this event (in range): raw views + de-duped unique viewers.
            "views": views_per_event.get(eid, 0),
            "unique_viewers": unique_views_per_event.get(eid, 0),
            "matches": matches_per_event.get(eid, 0),
            "kills": team_kills_per_event.get(eid, 0) + solo_kills_per_event.get(eid, 0),
            "prize_money": prize_per_event.get(eid, 0.0),
            "rating": round(avg, 1) if avg is not None else None,
            "ratings_count": rating.get("count", 0),
        })

    # ════════════════════ Response ════════════════════
    # The flat top-level keys (events_count, registered_teams, registered_players, total_kills,
    # average_rating, ratings_count) are kept verbatim so any older client still works; the rich
    # blocks (totals / registrations / page_views / monthly / top_* / events) power the dashboard.
    # `range` echoes the window the server actually applied so the frontend can confirm it.
    return Response(
        {
            # ── window echo: the [start, end] the server applied (null == no bound) ──
            "range": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },

            # ── back-compat flat keys (unchanged contract for the old card strip) ──
            "events_count": events_count,
            "registered_teams": registered_teams,
            "registered_players": unique_players,
            "total_kills": total_kills,
            "average_rating": average_rating,
            "ratings_count": ratings_count,

            # ── rich totals block ──
            "totals": {
                "events": events_count,
                "completed_events": completed_count,
                "unique_teams": unique_teams,
                "unique_players": unique_players,
                "registered_teams": registered_teams,
                "total_participants": total_participants,
                # registration completeness rolled up (see registrations block for definition).
                "complete_registrations": registrations_complete,
                "incomplete_registrations": registrations_incomplete,
                "total_matches": total_matches,
                "total_kills": total_kills,
                "total_prize_money": round(total_prize_money, 2),
                # page-view headline numbers (in range), surfaced on totals for convenience.
                "total_views": total_views,
                "unique_viewers": unique_viewers,
                "avg_participants_per_event": avg_participants,
                "registration_fill_rate": fill_rate,       # % (null when no capacity known)
                "completion_rate": completion_rate,         # % completed (null for empty org)
                "by_status": by_status,                     # {upcoming, ongoing, completed, ...}
                "by_type": by_type,                         # {tournament, scrims}
                "by_mode": by_mode,                         # {virtual, physical(lan), hybrid}
                "by_tier": by_tier,                         # {tier_1, tier_2, tier_3}
            },

            # ── registration completeness block (in range) ──
            # complete   = confirmed entrants (team status "active" + solo "registered"/"approved")
            # incomplete = every other registration row (pending/rejected/withdrawn/left/disqualified)
            # total      = complete + incomplete = all team+solo registration rows in the window.
            "registrations": {
                "complete": registrations_complete,
                "incomplete": registrations_incomplete,
                "total": registrations_total,
            },

            # ── page-view block (in range), sourced from EventPageView ──
            # total_views    = raw EventPageView rows for the org's events (every load counts).
            # unique_viewers = distinct logged-in users + distinct anonymous IPs, de-duped across
            #                  ALL of the org's events (a visitor who saw two counts once).
            "page_views": {
                "total_views": total_views,
                "unique_viewers": unique_viewers,
            },

            # ── rating summary block ──
            "rating": {
                "average": average_rating,
                "count": ratings_count,
            },

            # ── trend + leaderboards + per-event breakdown ──
            # monthly buckets are built from the IN-RANGE activity timestamps, so the trend moves
            # with the selected window.
            "monthly": monthly_list,    # [{month, registrations, matches, views}]
            "top_teams": top_teams,     # [{team_id, team_name, kills, wins}]
            "top_players": top_players,  # [{user_id, username, kills}]
            "events": event_rows,       # capped per-event rows, newest first (enriched)
            "events_returned": len(event_rows),
            "events_truncated": events_count > len(event_rows),
        },
        status=status.HTTP_200_OK,
    )
