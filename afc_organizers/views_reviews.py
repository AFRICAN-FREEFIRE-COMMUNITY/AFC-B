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
from django.db.models import Avg, Count, Sum

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
    TournamentTeam,
    TournamentTeamMember,
    RegisteredCompetitors,
    TournamentTeamMatchStats,
    SoloPlayerMatchStats,
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
@api_view(["GET"])
def org_metrics(request, slug):
    """Roll up every event an organization owns into one flat stat block for the organizer
    dashboard. Gated by org_can(can_view_metrics). Aggregates are computed over
    Event.objects.filter(organization=org); each .aggregate(Sum/Count) returns 0/None safely
    when there is nothing to count, so a brand-new org reports clean zeros rather than erroring."""
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

    # The set of events this org owns — every metric below is scoped to these event_ids.
    events = Event.objects.filter(organization=org)
    event_ids = list(events.values_list("event_id", flat=True))

    # ── events_count: how many events the org owns ──
    events_count = len(event_ids)

    # ── registered_teams: TournamentTeam rows across the org's events ──
    # TournamentTeam.event is the direct FK, so this is a flat count over the event set.
    registered_teams = TournamentTeam.objects.filter(event_id__in=event_ids).count()

    # ── registered_players: distinct human players across the org's events ──
    # Two registration shapes exist depending on participant_type, and we count DISTINCT
    # users across BOTH so a player is never double-counted:
    #   • team events → TournamentTeamMember.user (its `event` FK points at the same Event)
    #   • solo events → RegisteredCompetitors.user (the per-competitor registration row)
    team_player_ids = set(
        TournamentTeamMember.objects.filter(
            tournament_team__event_id__in=event_ids
        ).values_list("user_id", flat=True)
    )
    solo_player_ids = set(
        RegisteredCompetitors.objects.filter(
            event_id__in=event_ids, user_id__isnull=False
        ).values_list("user_id", flat=True)
    )
    # Union of the two id sets → distinct player count across the org (None ids excluded).
    registered_players = len(
        {pid for pid in team_player_ids if pid is not None} | solo_player_ids
    )

    # ── total_kills: kills across BOTH stats tables for the org's matches ──
    # Match → StageGroups (group) → Stages (stage) → Event, so a match belongs to an event
    # via match__group__stage__event. Both stats tables hang off Match the same way:
    #   • TournamentTeamMatchStats.kills (team-format stats)
    #   • SoloPlayerMatchStats.kills     (solo-format stats)
    # Each .aggregate(Sum) yields None when empty → coalesce to 0 before summing.
    team_kills = TournamentTeamMatchStats.objects.filter(
        match__group__stage__event_id__in=event_ids
    ).aggregate(total=Sum("kills"))["total"] or 0
    solo_kills = SoloPlayerMatchStats.objects.filter(
        match__group__stage__event_id__in=event_ids
    ).aggregate(total=Sum("kills"))["total"] or 0
    total_kills = team_kills + solo_kills

    # ── average_rating + ratings_count: EventRating over the org's events ──
    # Single round-trip; average is None (→ null) and count 0 when there are no ratings.
    rating_agg = EventRating.objects.filter(event_id__in=event_ids).aggregate(
        average=Avg("score"), count=Count("id"),
    )
    average_rating = (
        round(rating_agg["average"], 1) if rating_agg["average"] is not None else None
    )
    ratings_count = rating_agg["count"]

    # Flat dict — the organizer dashboard reads each key directly.
    return Response(
        {
            "events_count": events_count,
            "registered_teams": registered_teams,
            "registered_players": registered_players,
            "total_kills": total_kills,
            "average_rating": average_rating,
            "ratings_count": ratings_count,
        },
        status=status.HTTP_200_OK,
    )
