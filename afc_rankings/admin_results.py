"""
Admin write API — Result Markers: per-tournament counting controls + result exclusions (Phase 2).

WHAT THIS COVERS
----------------
Two human-judgement levers over how a *tournament's* results feed the rankings. Both are
read at aggregation time (see ``aggregation.py`` -> ``_counting_controls`` /
``_excluded_event_ids``), so flipping either one and recalculating re-derives the affected
scores. Neither lever mutates a score directly — the score engine stays pure.

  * EventCountingControl  — per-event toggles for whether each scoring COMPONENT counts:
                            ``count_winner`` (winner bonus), ``count_placement`` (placement
                            points), ``count_kills`` (kill points). OneToOne with Event.
                            NO row for an event ⇒ everything counts (defaults all-True).
  * ResultExclusion       — per-event opt-out for ONE team or player: their results in this
                            event don't count at all (e.g. a disqualification / protest).
                            team XOR player (enforced here AND by a DB CheckConstraint).

WHY EVERY WRITE ENQUEUES A RECALC
---------------------------------
A counting toggle or an exclusion changes the *derived* score for whatever entity it
touches, but the score models are only recomputed by the recalc layer. So the pattern below
mirrors admin_prize.py / admin_overrides.py exactly:
    flip the row inside a transaction -> _audit -> enqueue recalc on transaction.on_commit.
We NEVER recalc inline (project rule). We recalc for the *current month* + the *active
season* (recalc.current_month() / recalc.current_season()), consistent with the other
data-entry surfaces — the affected event may span an older month, but the live ranking the
admin is curating is the current one, and enqueue_team/enqueue_player fire both the monthly
and quarterly recalcs harmlessly.

  * event-counting PATCH  -> recalc EVERY team registered in the event (TournamentTeam ->
                            team_id), because a component toggle affects all of them.
  * exclusion create/delete -> recalc just the single freed/excluded team or player.

WHICH EVENTS BELONG TO A SEASON
-------------------------------
Event has no Season FK. A tournament belongs to a season when its ``start_date`` falls in the
season's [start_date, end_date] window — the same date-window approach admin_prize.py uses
for payouts. "Tournament" means ``competition_type != "scrims"`` (the brief's wording; the
model's other value is "tournament"). Scrims never carry counting controls / exclusions in
the rankings sense, so they're excluded from the markers list.

IDIOM (matches the rest of afc_rankings — read views.py / serializers.py / admin_views.py):
  * function-based ``@api_view`` views, NOT class-based; NO DRF Serializer classes.
  * manual-dict serialization via the LOCAL ``serialize_event_markers`` /
    ``serialize_exclusion`` helpers below.
  * the auth + audit foundation is REUSED from ``admin_views.py`` — never reimplemented:
        user, err = _auth(request, roles=...)    # 401/403 short-circuit
        reason, err = _require_reason(request)    # mandatory >= 10-char audit reason (writes only)
        with transaction.atomic(): ...write...
        _audit(user, "tournament_result", "<action>", reason, object_ref=..., before=..., after=...)
  * list endpoints page through ``serializers.paginate`` and return the same
    {"results": [...], "pagination": meta, ...extra} envelope views.py uses.
  * validation errors mirror afc_auth.views: ``Response({"message": "..."}, status=...)``.

object_type is fixed to "tournament_result" for every audit row (a valid
RankingAuditLog.OBJECT_TYPES key), so the §16 audit log filters every Result-Markers change
into one bucket.

Auth: writes are gated on head_admin OR metrics_admin (the default ``_auth`` set,
RANKING_ADMIN_ROLES). The read-only list/detail endpoints still require a valid admin token
(they expose internal config) but skip the reason gate and the audit write.

URL routes returned to the coordinator (mounted under the existing ``rankings/`` prefix):
  GET    admin/results/markers/                  -> results_markers_list   (read-only)
  GET    event-counting/<int:event_id>/          -> event_counting_detail  (read-only)
  PATCH  event-counting/<int:event_id>/          -> event_counting_update
  GET    result-exclusions/                       -> result_exclusions_list (read-only)
  POST   result-exclusions/                       -> result_exclusion_create
  DELETE result-exclusions/<int:exclusion_id>/    -> result_exclusion_delete
"""
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

# Shared auth/audit foundation — do NOT reimplement these here (project rule).
from .admin_views import _auth, _require_reason, _audit
from . import recalc
from . import tasks
from .models import EventCountingControl, ResultExclusion, Season
from .serializers import paginate

# The tournament-side models live in the tournaments app. (We resolve an event's teams to
# recalc via TournamentTeam; per-entity exclusions recalc the single targeted team/player,
# so TournamentPlayerMatchStats is not needed on this surface.)
from afc_tournament_and_scrims.models import Event, TournamentTeam

# Every audit row from this surface buckets under "tournament_result" (RankingAuditLog.OBJECT_TYPES).
AUDIT_OBJECT_TYPE = "tournament_result"

# The three counting toggles, as actual EventCountingControl field names. Used by both the
# serializer (default-True view) and the PATCH handler (which fields a caller may set).
COUNTING_FIELDS = ("count_winner", "count_placement", "count_kills")

# A ResultExclusion targets exactly one of these (team XOR player).
ENTITY_TYPES = ("team", "player")


# ───────────────────────── recalc helpers (enqueue on commit, never inline) ─────────────────────────
def _enqueue_event_team_recalc(event_id):
    """Enqueue a recalc for EVERY team registered in this event, AFTER commit.

    A counting toggle (winner/placement/kills) changes the derived score for all of the
    event's teams, so we recompute each one. We resolve the underlying ``team_id`` from each
    ``TournamentTeam`` (the score models key off Team, not TournamentTeam) and dedupe so a
    team registered twice isn't enqueued twice. Recalc against the current month + active
    season, mirroring admin_prize.py.
    """
    team_ids = (
        TournamentTeam.objects
        .filter(event_id=event_id)
        .values_list("team_id", flat=True)
        .distinct()
    )
    month = recalc.current_month()
    season = recalc.current_season()
    season_id = season.season_id if season else None
    # Snapshot the ids now (the queryset is lazy); the on_commit closure fires post-commit.
    ids = [tid for tid in team_ids if tid]
    transaction.on_commit(
        lambda: [tasks.enqueue_team(tid, month, season_id) for tid in ids]
    )


def _enqueue_entity_recalc(*, team_id=None, player_id=None):
    """Enqueue a recalc for the single team OR player an exclusion touches, AFTER commit.

    Used when a ResultExclusion is created (entity newly excluded) or deleted (entity freed)
    — either way that one entity's score must be re-derived. Current month + active season,
    consistent with the rest of the data-entry surfaces.
    """
    month = recalc.current_month()
    season = recalc.current_season()
    season_id = season.season_id if season else None
    if team_id:
        transaction.on_commit(lambda: tasks.enqueue_team(team_id, month, season_id))
    elif player_id:
        transaction.on_commit(lambda: tasks.enqueue_player(player_id, month, season_id))


# ───────────────────────── local serializers (manual-dict, per house style) ─────────────────────────
def serialize_event_markers(event, control, exclusion_count):
    """One tournament's row for the Result-Markers list.

    Surfaces basic event info plus the counting-control state (defaulting all-True when no
    ``control`` row exists, per the model's "no row ⇒ everything counts" rule) and a count
    of how many ResultExclusions are active for the event. ``team_count`` is the number of
    TournamentTeam rows registered in the event (the entities a counting toggle would recalc).
    """
    return {
        "event_id": event.event_id,
        "event_name": event.event_name,
        # Event has both a date and a tier; expose them so the admin UI can show context.
        "date": event.start_date.isoformat() if event.start_date else None,
        "tier": event.tournament_tier,                       # "tier_1" | "tier_2" | "tier_3"
        "team_count": TournamentTeam.objects.filter(event=event).count(),
        # Counting state: read from the control row, else default True (everything counts).
        "count_winner": control.count_winner if control else True,
        "count_placement": control.count_placement if control else True,
        "count_kills": control.count_kills if control else True,
        # True iff an explicit control row exists (vs the implicit all-True default).
        "has_control_row": control is not None,
        "active_exclusions": exclusion_count,
    }


def serialize_counting_control(event, control):
    """The counting-control state for ONE event (defaults all-True when no row exists).

    Returned by the detail GET and after a PATCH so the caller always sees the resolved
    state, regardless of whether a row was actually persisted yet.
    """
    return {
        "event_id": event.event_id,
        "event_name": event.event_name,
        "count_winner": control.count_winner if control else True,
        "count_placement": control.count_placement if control else True,
        "count_kills": control.count_kills if control else True,
        "has_control_row": control is not None,
        "updated_by": control.updated_by_id if control else None,
        "updated_at": control.updated_at.isoformat() if (control and control.updated_at) else None,
    }


def serialize_exclusion(x):
    """One ResultExclusion as a flat dict (matches the serializers.py manual-dict idiom).

    Surfaces the event, the entity_type and whichever of team/player is set, the human-readable
    names (query-free when the queryset select_related's them), the reason, and creator/date.
    """
    return {
        "exclusion_id": x.id,
        "event_id": x.event_id,
        "event_name": x.event.event_name if x.event_id else None,
        "entity_type": x.entity_type,
        "team_id": x.team_id,
        "team_name": (x.team.team_name if x.team_id else None),
        "player_id": x.player_id,
        "player_username": (x.player.username if x.player_id else None),
        "reason": x.reason,
        "created_by": x.created_by_id,
        "created_at": x.created_at.isoformat() if x.created_at else None,
    }


# ───────────────────────── shared lookups ─────────────────────────
def _get_event(event_id):
    """Resolve an Event by PK, or return (None, 404 Response)."""
    event = Event.objects.filter(pk=event_id).first()
    if not event:
        return None, Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
    return event, None


def _season_event_qs(season):
    """Tournaments belonging to a season: competition_type != "scrims", start_date in window.

    Event has no Season FK, so we use the season's date window on ``start_date`` (the same
    date-window approach admin_prize.py uses for payouts). Scrims are excluded — counting
    controls / exclusions only apply to tournaments in the rankings sense.
    """
    return (
        Event.objects
        .exclude(competition_type="scrims")
        .filter(start_date__gte=season.start_date, start_date__lte=season.end_date)
        .order_by("start_date", "event_id")
    )


# ───────────────────────── GET admin/results/markers/  (read-only) ─────────────────────────
@api_view(["GET"])
def results_markers_list(request):
    """List a season's tournaments with their counting-control state + active-exclusion count.

    Query: ``?season_id=`` — which season's tournaments to list. Defaults to the active season
    when omitted. An unknown season_id returns an empty page (consistent envelope, no 404), so
    the admin UI can render "no tournaments this season" cleanly.

    Read-only: admin token required, but no reason + no audit (foundation's read rule). Paginated
    with the canonical {"results": [...], "pagination": meta, ...extra} envelope.
    """
    user, err = _auth(request)
    if err:
        return err

    # Resolve the season: explicit ?season_id, else the active one.
    season_id = request.GET.get("season_id")
    if season_id:
        season = Season.objects.filter(pk=season_id).first()
        if not season:
            # Unknown season -> empty page (no error), matching admin_prize.py's behaviour.
            return Response({
                "results": [],
                "pagination": {
                    "limit": 25, "offset": 0, "total_count": 0, "has_more": False, "next_offset": None,
                },
                "season_id": season_id,
            })
    else:
        season = recalc.current_season()
        if not season:
            return Response({
                "results": [],
                "pagination": {
                    "limit": 25, "offset": 0, "total_count": 0, "has_more": False, "next_offset": None,
                },
                "season_id": None,
            })

    qs = _season_event_qs(season)
    items, meta = paginate(request, qs)

    # Batch the control rows + per-event active-exclusion counts for just the events on this
    # page (avoids an N+1 over the control / exclusion tables).
    page_event_ids = [ev.event_id for ev in items]
    controls = {
        c.event_id: c
        for c in EventCountingControl.objects.filter(event_id__in=page_event_ids)
    }
    exclusion_counts = {}
    for ex in ResultExclusion.objects.filter(event_id__in=page_event_ids):
        exclusion_counts[ex.event_id] = exclusion_counts.get(ex.event_id, 0) + 1

    return Response({
        "results": [
            serialize_event_markers(ev, controls.get(ev.event_id), exclusion_counts.get(ev.event_id, 0))
            for ev in items
        ],
        "pagination": meta,
        "season_id": season.season_id,
    })


# ───────────────────────── GET event-counting/<event_id>/  (read-only) ─────────────────────────
@api_view(["GET"])
def event_counting_detail(request, event_id):
    """The counting-control state for one event (defaults all-True when no row exists).

    Read-only: admin token required, no reason + no audit.
    """
    user, err = _auth(request)
    if err:
        return err

    event, err = _get_event(event_id)
    if err:
        return err

    control = EventCountingControl.objects.filter(event=event).first()
    return Response(serialize_counting_control(event, control))


# ───────────────────────── PATCH event-counting/<event_id>/  (set toggles) ─────────────────────────
@api_view(["PATCH"])
def event_counting_update(request, event_id):
    """Set an event's counting toggles (count_winner / count_placement / count_kills).

    Body: any subset of the three booleans + the mandatory ``reason``. Partial — only the
    fields present in the body are changed; absent fields keep their current (or default-True)
    value. ``get_or_create`` materialises the control row on first edit (so the implicit
    all-True default becomes explicit), and ``updated_by`` is stamped to the acting admin.

    Because a component toggle affects EVERY team in the event, we enqueue a recalc for each
    registered team (via TournamentTeam) on commit. Audited as ``tournament_result`` /
    ``counting``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    event, err = _get_event(event_id)
    if err:
        return err

    data = request.data

    # Validate any supplied toggle is a real boolean before touching anything. We accept only
    # the three known fields; at least one must be present (an empty PATCH is a no-op error).
    supplied = {f: data.get(f) for f in COUNTING_FIELDS if f in data}
    if not supplied:
        return Response(
            {"message": f"Provide at least one of: {', '.join(COUNTING_FIELDS)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for field, value in supplied.items():
        if not isinstance(value, bool):
            return Response(
                {"message": f"`{field}` must be a boolean (true/false)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    with transaction.atomic():
        # Materialise the control row (defaults all-True) so a first edit makes the implicit
        # default explicit and editable.
        control, _created = EventCountingControl.objects.get_or_create(event=event)
        before = serialize_counting_control(event, control)

        # Apply only the supplied toggles (partial PATCH).
        for field, value in supplied.items():
            setattr(control, field, value)
        control.updated_by = user
        control.save(update_fields=list(supplied.keys()) + ["updated_by", "updated_at"])

        after = serialize_counting_control(event, control)
        _audit(
            user, AUDIT_OBJECT_TYPE, "counting", reason,
            object_ref=f"event:{event.event_id}",
            before=before, after=after, season=recalc.current_season(),
        )
        # Recalc every team in the event — the toggle changed how their results score.
        _enqueue_event_team_recalc(event.event_id)

    return Response(after)


# ───────────────────────── GET result-exclusions/  (read-only) ─────────────────────────
@api_view(["GET"])
def result_exclusions_list(request):
    """List result exclusions. Filter by ``?event_id=`` OR ``?season_id=`` (newest first).

    ``?event_id`` narrows to one event. ``?season_id`` narrows to all of a season's
    tournaments (the same date-window event set the markers list uses). Neither filter ⇒
    all exclusions. Read-only: admin token required, no reason + no audit. Paginated with the
    canonical envelope.
    """
    user, err = _auth(request)
    if err:
        return err

    qs = (ResultExclusion.objects
          .select_related("event", "team", "player")
          .order_by("-created_at", "-id"))

    event_id = request.GET.get("event_id")
    season_id = request.GET.get("season_id")
    if event_id:
        qs = qs.filter(event_id=event_id)
    elif season_id:
        season = Season.objects.filter(pk=season_id).first()
        if not season:
            # Unknown season -> empty page (no error), matching the markers list.
            return Response({
                "results": [],
                "pagination": {
                    "limit": 25, "offset": 0, "total_count": 0, "has_more": False, "next_offset": None,
                },
                "season_id": season_id,
            })
        # Restrict to the events that fall in the season window.
        season_event_ids = _season_event_qs(season).values_list("event_id", flat=True)
        qs = qs.filter(event_id__in=list(season_event_ids))

    items, meta = paginate(request, qs)
    return Response({
        "results": [serialize_exclusion(x) for x in items],
        "pagination": meta,
        "event_id": event_id,
        "season_id": season_id,
    })


# ───────────────────────── POST result-exclusions/  (create) ─────────────────────────
@api_view(["POST"])
def result_exclusion_create(request):
    """Create a result exclusion for ONE team or player in an event.

    Body: ``{ event_id, entity_type ("team"|"player"), team_id|player_id, reason }``.
    Exactly one of team_id / player_id must be set, matching ``entity_type`` (team XOR player —
    enforced here AND by the model's DB CheckConstraint). The (event, team) / (event, player)
    pair is unique at the DB level; a duplicate surfaces as an IntegrityError we translate to
    a clean 400.

    On commit we enqueue a recalc for the excluded entity (its results now stop counting).
    Audited as ``tournament_result`` / ``exclude``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    data = request.data

    # ── event FK ──
    event_id = data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    event = Event.objects.filter(pk=event_id).first()
    if not event:
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    # ── entity_type ──
    entity_type = (data.get("entity_type") or "").strip()
    if entity_type not in ENTITY_TYPES:
        return Response(
            {"message": f"entity_type must be one of: {', '.join(ENTITY_TYPES)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── team XOR player, matched to entity_type ──
    team_id = data.get("team_id")
    player_id = data.get("player_id")
    if entity_type == "team":
        if not team_id:
            return Response({"message": "team_id is required when entity_type is 'team'."},
                            status=status.HTTP_400_BAD_REQUEST)
        if player_id:
            return Response({"message": "Provide team_id only (not player_id) for a team exclusion."},
                            status=status.HTTP_400_BAD_REQUEST)
    else:  # entity_type == "player"
        if not player_id:
            return Response({"message": "player_id is required when entity_type is 'player'."},
                            status=status.HTTP_400_BAD_REQUEST)
        if team_id:
            return Response({"message": "Provide player_id only (not team_id) for a player exclusion."},
                            status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        try:
            exclusion = ResultExclusion.objects.create(
                event=event,
                entity_type=entity_type,
                team_id=team_id if entity_type == "team" else None,
                player_id=player_id if entity_type == "player" else None,
                reason=reason,
                created_by=user,
            )
        except IntegrityError:
            # The unique (event, team) / (event, player) constraint rejected a duplicate, or a
            # bad team/player FK was supplied — surface as a clean 400 rather than a 500.
            return Response(
                {"message": "This team/player is already excluded from this event (or the id is invalid)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-fetch with the related rows so the serializer is query-free for the names.
        exclusion = (ResultExclusion.objects
                     .select_related("event", "team", "player")
                     .get(pk=exclusion.pk))
        after = serialize_exclusion(exclusion)
        _audit(
            user, AUDIT_OBJECT_TYPE, "exclude", reason,
            object_ref=f"event:{event.event_id}:{entity_type}:{team_id or player_id}",
            before={}, after=after, season=recalc.current_season(),
        )
        # Recalc the now-excluded entity — its results stop counting.
        _enqueue_entity_recalc(
            team_id=team_id if entity_type == "team" else None,
            player_id=player_id if entity_type == "player" else None,
        )

    return Response(after, status=status.HTTP_201_CREATED)


# ───────────────────────── DELETE result-exclusions/<exclusion_id>/  (delete) ─────────────────────────
@api_view(["DELETE"])
def result_exclusion_delete(request, exclusion_id):
    """Remove a result exclusion — the freed entity's results count again.

    The audit row's before-snapshot preserves the deleted exclusion (hand-reversible). On
    commit we enqueue a recalc for the freed team/player so its score picks the results back
    up. Audited as ``tournament_result`` / ``unexclude``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    exclusion = (ResultExclusion.objects
                 .select_related("event", "team", "player")
                 .filter(pk=exclusion_id).first())
    if not exclusion:
        return Response({"message": "Result exclusion not found."}, status=status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        before = serialize_exclusion(exclusion)
        # Capture the entity refs BEFORE delete so the post-commit recalc still has them.
        freed_team_id = exclusion.team_id
        freed_player_id = exclusion.player_id
        event_id = exclusion.event_id
        entity_type = exclusion.entity_type
        object_ref = f"event:{event_id}:{entity_type}:{freed_team_id or freed_player_id}"
        exclusion.delete()

        _audit(
            user, AUDIT_OBJECT_TYPE, "unexclude", reason,
            object_ref=object_ref,
            before=before, after={}, season=recalc.current_season(),
        )
        # Recalc the freed entity — its results count again.
        _enqueue_entity_recalc(team_id=freed_team_id, player_id=freed_player_id)

    return Response({"message": "Result exclusion deleted."})
