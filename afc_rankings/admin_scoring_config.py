"""
Admin write API for rankings & tiering - the editable Scoring Config surface.

WHAT THIS SURFACE IS FOR
    Every number the scoring engine uses - tier multipliers and win bonuses, the kill and
    placement compression bands, placement points, the finals bonus, all the scrim rules,
    the prize money and social media bands, the ranking tier cutoffs, the player point
    weights, and the participation floors - is editable here, without a deploy. The values
    themselves live as data in ``scoring/tables.py``; this module is the HTTP surface that
    reads, validates, versions, and applies them.

THE FOUR RULES THIS IMPLEMENTS (owner decisions, 2026-08-03)
    1. A change creates a NEW version. ``ScoringConfig`` rows are append-only and never
       edited in place, which is what makes frozen history true rather than promised.
    2. A change is NOT retroactive. It governs work scored after it. The CURRENT season is
       recalculated so it is never half old rules and half new; every other season keeps the
       rules it was scored under, pinned by a ``SeasonScoringConfig`` row.
    3. The admin may CHOOSE additional seasons to apply it to. That path rewrites results
       people have already seen, so the response names every affected season, flags whether
       each is published, and the chosen seasons are recorded in the audit entry.
    4. Head admin only, always audited.

VALIDATION VERSUS CONTRADICTIONS
    A save that would corrupt scoring is REFUSED (400 with an ``errors`` list): a compression
    table with no open top band, a multiplier of zero or less, a tier cutoff no team could
    reach. A config that works but does not do what the author meant is REPORTED and saved:
    two rules that both read "above 100,000" so the second can never fire, bands that overlap,
    ranges no rule covers. Both come from ``scoring/validation.py``, which is pure.

CURRENCY
    Money thresholds are authored in NAIRA while an event's prize pool is stored in the
    event's own currency. That exact mismatch mis-tiered a $400 event on 2026-08-03. Every
    response therefore carries ``field_meta``, which states the currency and unit of every
    editable group, so the UI can never render a money threshold as a bare number.

IDIOM (matches views.py / admin_views.py / admin_tournament_tiers.py)
    * function-based ``@api_view`` views, NOT class-based; no DRF Serializer classes.
    * the auth + audit foundation is REUSED from ``admin_views.py``: ``_auth`` (role gate),
      ``_require_reason`` (mandatory audit reason), ``_audit`` (one RankingAuditLog row).
    * manual-dict serialization, as in ``serializers.py``.

ROUTES (mounted by urls.py under the ``rankings/`` prefix)
    GET  scoring-config/                 -> scoring_config           (read, ranking admins)
    GET  scoring-config/defaults/        -> scoring_config_defaults  (read, ranking admins)
    GET  scoring-config/seasons/         -> scoring_config_seasons   (read, ranking admins)
    GET  scoring-config/versions/<int:>/ -> scoring_config_version   (read, ranking admins)
    POST scoring-config/validate/        -> scoring_config_validate  (read-only check, head admin)
    POST scoring-config/                 -> scoring_config_save      (write, head admin)
"""
import datetime

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .admin_views import _auth, _require_reason, _audit
from .models import ScoringConfig, Season, SeasonScoringConfig, auto_rollover_seasons
from .scoring.tables import FIELD_META, SCHEMA_VERSION, defaults_config
from .scoring.validation import validate_config

# These controls decide every team's rank, so they are head-admin only - narrower than the
# RANKING_ADMIN_ROLES default (head_admin + metrics_admin) used by the data-entry surfaces.
# Reads stay on the wider default so a metrics admin can still SEE the rules in force.
CONFIG_WRITE_ROLES = ("head_admin",)


# ───────────────────────── serialization ─────────────────────────
def serialize_scoring_config(cfg):
    """Manual-dict serialization of one ``ScoringConfig`` row (matches serializers.py)."""
    return {
        "id": cfg.id,
        "version": cfg.version,
        "is_active": cfg.is_active,
        "config": cfg.config,
        "note": cfg.note,
        # created_by is nullable (SET_NULL) - guard the username lookup.
        "created_by": cfg.created_by.username if cfg.created_by_id else None,
        "created_at": cfg.created_at.isoformat(),
    }


def _version_row(cfg, season_counts=None):
    """Lightweight row for the version-history list (no full config blob).

    ``seasons_bound`` tells the admin whether a version is still governing anything, which is
    what makes it obvious that an old version cannot simply be discarded.
    """
    return {
        "id": cfg.id,
        "version": cfg.version,
        "is_active": cfg.is_active,
        "note": cfg.note,
        "created_by": cfg.created_by.username if cfg.created_by_id else None,
        "created_at": cfg.created_at.isoformat(),
        "seasons_bound": (season_counts or {}).get(cfg.id, 0),
    }


def _season_row(season, binding=None, today=None, *, in_default_scope=False):
    """One season as the scope picker and the impact report both need it.

    ``is_closed`` means the season is over and no longer the active one; ``is_frozen`` means
    a quarterly evaluation has already locked its tiers. Both matter because applying a
    change to such a season rewrites results people have already seen, which is why the
    published flags travel alongside them rather than being looked up separately.
    """
    today = today or timezone.localdate()
    is_closed = (not season.is_active) and season.end_date < today
    return {
        "season_id": season.season_id,
        "name": season.name,
        "year": season.year,
        "quarter": season.quarter,
        "start_date": season.start_date.isoformat(),
        "end_date": season.end_date.isoformat(),
        "is_active": season.is_active,
        "is_closed": is_closed,
        "is_frozen": season.scores_frozen_at is not None,
        "tier_eval_run": season.tier_eval_run,
        "rankings_published": season.rankings_published,
        "tiers_published": season.tiers_published,
        # Which rules this season is scored under right now. null version = the shipped
        # defaults; "pinned" false = it simply follows whatever config is active.
        "config_version": (binding.config.version
                           if binding is not None and binding.config_id else None),
        "config_pinned": binding is not None,
        "config_origin": binding.origin if binding is not None else None,
        # True for the season that is always included in a save (the current one).
        "in_default_scope": in_default_scope,
    }


def _bindings_by_season():
    return {
        b.season_id: b
        for b in SeasonScoringConfig.objects.select_related("config").all()
    }


# ───────────────────────── season scope helpers ─────────────────────────
def _current_season():
    """The season a change applies to by default. Calendar-driven, same as recalc.current_season."""
    auto_rollover_seasons()
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


def _resolve_scope(requested_ids):
    """Work out which seasons a save would touch.

    Returns ``(seasons, unknown_ids)``. The CURRENT season is always included - that is the
    owner's default, and it is what stops a season being left half on the old rules and half
    on the new. Anything else is an explicit opt in by the admin.
    """
    current = _current_season()
    seasons, seen = [], set()
    if current is not None:
        seasons.append(current)
        seen.add(current.season_id)

    unknown = []
    for raw in (requested_ids or []):
        try:
            season_id = int(raw)
        except (TypeError, ValueError):
            unknown.append(raw)
            continue
        if season_id in seen:
            continue
        season = Season.objects.filter(pk=season_id).first()
        if season is None:
            unknown.append(season_id)
            continue
        seasons.append(season)
        seen.add(season_id)
    return seasons, unknown


def _impact(seasons, today=None):
    """The affected-season report: every season named, with its published and closed flags.

    ``requires_acknowledgement`` is True when the admin has explicitly opted a CLOSED or
    PUBLISHED season in. The current season never triggers it: recalculating the season in
    progress is the agreed default, and refusing to do it without a confirmation would break
    the safe path. It is still listed with its own flags so the admin can see what it is.
    """
    today = today or timezone.localdate()
    current = _current_season()
    current_id = current.season_id if current else None
    bindings = _bindings_by_season()

    rows, needs_ack = [], False
    for season in seasons:
        row = _season_row(season, bindings.get(season.season_id), today,
                          in_default_scope=(season.season_id == current_id))
        rows.append(row)
        if not row["in_default_scope"] and (
            row["is_closed"] or row["rankings_published"] or row["tiers_published"]
        ):
            needs_ack = True
    return {
        "seasons": rows,
        "requires_acknowledgement": needs_ack,
        # Split out so the UI can word the warning proportionally: published is worse than
        # merely closed, because the standings are already on the public site.
        "published_seasons": [r["season_id"] for r in rows
                              if not r["in_default_scope"]
                              and (r["rankings_published"] or r["tiers_published"])],
        "closed_seasons": [r["season_id"] for r in rows
                           if not r["in_default_scope"] and r["is_closed"]],
    }


def _months_in(season):
    """Every month (first of month) the season's date range covers.

    Monthly ladders are separate rows from the quarterly ones, so applying a change to a
    season has to rebuild both or the month tables keep the old numbers while the quarter
    shows the new ones.
    """
    months, cursor = [], season.start_date.replace(day=1)
    last = season.end_date.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor + datetime.timedelta(days=32)).replace(day=1)
    return months


def _recalculate(seasons):
    """Rebuild the scores of every season in scope, monthly rows and ghost rows included.

    Run synchronously and OUTSIDE the write transaction, exactly like
    ``recalc.run_evaluation`` does its pre-tiering recompute: this is a deliberate admin
    batch action, not the live edit hot path the "recalc is never inline" rule guards, and
    the admin needs to see the result of their own change immediately. Imported locally to
    keep this module free of a load-order cycle with recalc -> aggregation -> models.

    Ghost teams and ghost players are swept too. They are ranked interleaved with the real
    ones, so leaving them on the old rules would put freshly rescored teams next to stale
    ones in the same table.
    """
    from . import recalc, standalone
    from .models import GhostPlayer, GhostTeam

    ghost_team_ids = list(GhostTeam.objects.values_list("ghost_team_id", flat=True))
    ghost_player_ids = list(GhostPlayer.objects.values_list("id", flat=True))

    summary = {"seasons": 0, "months": 0, "ghost_teams": 0, "ghost_players": 0}
    for season in seasons:
        for month in _months_in(season):
            recalc.recalc_month(month)
            for gid in ghost_team_ids:
                standalone.recalc_ghost_team_monthly(gid, month)
            for pid in ghost_player_ids:
                standalone.recalc_ghost_player_monthly(pid, month)
            summary["months"] += 1
        recalc.recalc_season(season)
        for gid in ghost_team_ids:
            standalone.recalc_ghost_team_quarterly(gid, season.season_id)
        for pid in ghost_player_ids:
            standalone.recalc_ghost_player_quarterly(pid, season.season_id)
        summary["seasons"] += 1
    summary["ghost_teams"] = len(ghost_team_ids)
    summary["ghost_players"] = len(ghost_player_ids)
    return summary


# ───────────────────────── read endpoints ─────────────────────────
@api_view(["GET"])
def scoring_config(request):
    """The scoring rules currently in force, plus everything the editor needs to render them.

    Purpose:  populate the admin Scoring Config page.
    Auth:     Bearer SessionToken, head_admin or metrics_admin (read is the wider set).
    Request:  no body, no query parameters.
    Response 200::

        {
          "id": 4 | null,                 # null when no version has been saved yet
          "version": 4 | null,
          "is_active": true,
          "is_default": false,            # true = these are the shipped constants.py values
          "config": { ...the editable blob, see scoring-config/defaults/... },
          "note": "why it was saved",
          "created_by": "username" | null,
          "created_at": "2026-08-03T10:00:00+00:00" | null,
          "schema_version": 2,
          "field_meta": {                 # per group: what the numbers mean and their currency
            "prize_money_points": {"label","unit","currency":"NGN","value_unit","help"}, ...
          },
          "contradictions": [             # advisory, never blocks (see the module docstring)
            {"kind","path","message","entries":[...]}
          ],
          "versions": [                   # newest first, no config blob
            {"id","version","is_active","note","created_by","created_at","seasons_bound"}
          ],
          "seasons": [ ...season rows, see scoring-config/seasons/... ],
          "current_season_id": 12 | null
        }

    Consumed by: the admin Rankings > Scoring Config page (the editor form, the version
    history panel, and the season scope picker are all served from this one call).
    """
    user, err = _auth(request)
    if err:
        return err

    active = ScoringConfig.objects.filter(is_active=True).order_by("-version").first()

    # How many seasons each version still governs - shown in the history list so it is
    # obvious that old versions are load-bearing and not clutter.
    season_counts = {}
    for binding in SeasonScoringConfig.objects.all():
        if binding.config_id:
            season_counts[binding.config_id] = season_counts.get(binding.config_id, 0) + 1
    versions = [_version_row(c, season_counts)
                for c in ScoringConfig.objects.all().order_by("-version")]

    if active:
        body = serialize_scoring_config(active)
        body["is_default"] = False
        blob = active.config
    else:
        # No saved config yet - surface the constants.py defaults as the "current" config.
        blob = defaults_config()
        body = {
            "id": None, "version": None, "is_active": False, "config": blob,
            "note": "", "created_by": None, "created_at": None, "is_default": True,
        }

    checked = validate_config(blob)
    body["schema_version"] = SCHEMA_VERSION
    body["field_meta"] = dict(FIELD_META)
    body["contradictions"] = checked["contradictions"]
    body["versions"] = versions

    today = timezone.localdate()
    current = _current_season()
    bindings = _bindings_by_season()
    body["seasons"] = [
        _season_row(s, bindings.get(s.season_id), today,
                    in_default_scope=(current is not None and s.season_id == current.season_id))
        for s in Season.objects.all().order_by("-year", "-quarter")
    ]
    body["current_season_id"] = current.season_id if current else None
    return Response(body)


@api_view(["GET"])
def scoring_config_defaults(request):
    """The shipped defaults - the "reset to factory settings" payload.

    Purpose:  seed a fresh editor, or let an admin diff the live rules against the originals.
    Auth:     Bearer SessionToken, head_admin or metrics_admin.
    Request:  no body.
    Response 200: ``{"config": {...}, "schema_version": 2, "field_meta": {...}}``.

    The ``config`` object is the canonical editable shape::

        {
          "schema_version": 2,
          "tiers": [{"key":"tier_1","label":"Tier 1","multiplier":2.0,"win_bonus":30,
                     "retired":false}, ...],
          "placement_points": {"1": 12, "2": 9, ...},
          "kill_compression":      [{"max":50,"points":3}, ..., {"max":null,"points":65}],
          "placement_compression": [{"max":50,"points":5}, ..., {"max":null,"points":70}],
          "finals_base": 5,
          "prize_money_points":  [{"max":100000,"points":5}, ..., {"max":null,"points":65}],
          "social_media_points": [{"max":1000,"points":1}, ..., {"max":null,"points":10}],
          "tier_thresholds": {"brackets":[{"min":150,"tier":0},{"min":90,"tier":1},
                                          {"min":40,"tier":2}],
                              "default_tier":3,
                              "labels":{"0":"Elite","1":"Competitive","2":"Rising","3":"Entry"}},
          "scrim": {"weight":0.5,"win_flat":3,"cap_ratio":0.3,"flat_cap":30,
                    "daily_cap":4,"monthly_cap":60},
          "player_weights": {"mvp_pts":5,"finals_pts":3,"team_win_pts":5,
                             "participation_pts":1,"scrim_win_pts":1,"scrim_kill_weight":0.5},
          "participation_floors": {"team_monthly":1,"team_quarterly":2,
                                   "player_monthly":1,"player_quarterly":1}
        }

    A band's ``max`` is an INCLUSIVE upper bound and the last band must be ``null`` (open
    top). ``prize_money_points`` thresholds are in NAIRA - see ``field_meta``.

    Consumed by: the admin Scoring Config page's "Reset to defaults" action.
    """
    user, err = _auth(request)
    if err:
        return err
    return Response({
        "config": defaults_config(),
        "schema_version": SCHEMA_VERSION,
        "field_meta": dict(FIELD_META),
    })


@api_view(["GET"])
def scoring_config_version(request, version):
    """One historical version, in full.

    Purpose:  read the rules a past season was scored under, including entries retired since.
    Auth:     Bearer SessionToken, head_admin or metrics_admin.
    Request:  version number in the path.
    Response 200: the ``serialize_scoring_config`` shape plus
        ``{"field_meta": {...}, "seasons": [ ...season rows bound to this version... ]}``.
    Response 404: ``{"message": "..."}`` when the version does not exist.

    Consumed by: the version history panel's "view" action, and the audit trail when it
    needs to show what a change moved away from.
    """
    user, err = _auth(request)
    if err:
        return err
    cfg = ScoringConfig.objects.filter(version=version).first()
    if not cfg:
        return Response({"message": "Scoring config version not found."},
                        status=status.HTTP_404_NOT_FOUND)
    body = serialize_scoring_config(cfg)
    body["field_meta"] = dict(FIELD_META)
    today = timezone.localdate()
    body["seasons"] = [
        _season_row(b.season, b, today)
        for b in SeasonScoringConfig.objects.filter(config=cfg).select_related("season", "config")
    ]
    return Response(body)


@api_view(["GET"])
def scoring_config_seasons(request):
    """Every season, with the rules it is scored under and whether a change would be safe.

    Purpose:  drive the season scope picker on the save dialog.
    Auth:     Bearer SessionToken, head_admin or metrics_admin.
    Request:  no body.
    Response 200::

        {
          "results": [
            {"season_id":12,"name":"Season 3 2026","year":2026,"quarter":3,
             "start_date":"2026-07-01","end_date":"2026-09-30",
             "is_active":true,"is_closed":false,"is_frozen":false,"tier_eval_run":false,
             "rankings_published":false,"tiers_published":false,
             "config_version":4,"config_pinned":true,"config_origin":"applied",
             "in_default_scope":true}
          ],
          "current_season_id": 12 | null
        }

    ``in_default_scope`` marks the season that is always included in a save. Every other
    season with ``is_closed`` or either published flag set is an unsafe opt in: choosing it
    rewrites standings people have already seen, and the save then requires
    ``acknowledge_published``.

    Consumed by: the admin Scoring Config page's save dialog (scope checkboxes + warnings).
    """
    user, err = _auth(request)
    if err:
        return err
    today = timezone.localdate()
    current = _current_season()
    bindings = _bindings_by_season()
    return Response({
        "results": [
            _season_row(s, bindings.get(s.season_id), today,
                        in_default_scope=(current is not None
                                          and s.season_id == current.season_id))
            for s in Season.objects.all().order_by("-year", "-quarter")
        ],
        "current_season_id": current.season_id if current else None,
    })


# ───────────────────────── validate (writes nothing) ─────────────────────────
@api_view(["POST"])
def scoring_config_validate(request):
    """Check a config and preview which seasons a save would touch. Writes nothing.

    Purpose:  let the editor show problems and the affected-season list BEFORE the admin
              commits, so the confirmation dialog is accurate rather than a guess.
    Auth:     Bearer SessionToken, head_admin only (it exposes the same reasoning as the save).
    Request::

        {"config": { ...full blob... },
         "apply_to_seasons": [9, 12]        # optional; the current season is always included
        }

    Response 200::

        {"valid": true,
         "errors": [{"code","path","message"}],          # non-empty means a save is refused
         "contradictions": [{"kind","path","message","entries":[...]}],
         "impact": {"seasons":[ ...season rows... ],
                    "requires_acknowledgement": false,
                    "published_seasons": [], "closed_seasons": []},
         "unknown_season_ids": []}

    Response 400 when ``config`` is missing or is not an object.

    Consumed by: the admin Scoring Config page, on the Save button (pre-flight) and
    optionally on change with a debounce.
    """
    user, err = _auth(request, roles=CONFIG_WRITE_ROLES)
    if err:
        return err

    config = request.data.get("config")
    if not isinstance(config, dict) or not config:
        return Response({"message": "A 'config' object is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    checked = validate_config(config)
    seasons, unknown = _resolve_scope(request.data.get("apply_to_seasons"))
    return Response({
        "valid": not checked["errors"],
        "errors": checked["errors"],
        "contradictions": checked["contradictions"],
        "impact": _impact(seasons),
        "unknown_season_ids": unknown,
    })


# ───────────────────────── save ─────────────────────────
@api_view(["POST"])
def scoring_config_save(request):
    """Save a NEW scoring config version, apply it to the chosen seasons, and recalculate them.

    Purpose:  the one write on this surface. Never edits an existing version in place.
    Auth:     Bearer SessionToken, head_admin ONLY. These values decide every team's rank.
    Request::

        {"config": { ...full blob, see scoring-config/defaults/... },
         "reason": "at least 10 characters, stored on the version and in the audit log",
         "apply_to_seasons": [9],            # optional extra seasons; current always included
         "acknowledge_published": false,     # required when an opted-in season is closed/published
         "recalculate": true                 # optional, default true; false skips the rebuild
        }

    Response 201::

        {"id","version","is_active","config","note","created_by","created_at",
         "contradictions": [ ...advisory, the save still happened... ],
         "applied_seasons":  [ ...season rows, each with is_closed / rankings_published... ],
         "frozen_seasons":   [ {"season_id","name","config_version"} ],
         "recalculated": {"seasons": 1, "months": 3},
         "audit_id": 88}

    Response 400 ``{"message": ..., "errors": [...]}`` when the config would corrupt scoring,
                 or when the reason is missing, or a season id is unknown.
    Response 403 when the caller is not a head admin.
    Response 409 ``{"message": ..., "impact": {...}}`` when a closed or published season was
                 opted in without ``acknowledge_published``. The body names every affected
                 season so the confirmation can be specific rather than generic.

    WHAT THE WRITE DOES, in order, inside one transaction:
      1. Freezes history: every season with no pin yet is pinned to the config that was
         active BEFORE this save, so it keeps the rules it was scored under. This is what
         makes the change non-retroactive.
      2. Creates the new version, active, deactivating the previous one.
      3. Pins the seasons in scope to the new version.
      4. Writes one audit row recording who, why, the version moved from and to, and every
         season the change was applied to.
    Then, outside the transaction, it recalculates the seasons in scope (monthly rows and
    the quarterly rows), so the current season is never left half on the old rules.

    Consumed by: the admin Scoring Config page's Save action.
    """
    # (1) auth gate - head admin only, narrower than the surface's read gate.
    user, err = _auth(request, roles=CONFIG_WRITE_ROLES)
    if err:
        return err

    # (2) mandatory audit reason (also reused as the version's `note`).
    reason, err = _require_reason(request)
    if err:
        return err

    config = request.data.get("config")
    if not isinstance(config, dict) or not config:
        return Response({"message": "A 'config' object is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    # (3) refuse anything that would corrupt scoring. Contradictions are collected here too
    #     but they are reported with the successful response, not used to block.
    checked = validate_config(config)
    if checked["errors"]:
        return Response(
            {
                "message": "These settings would break scoring, so nothing was saved.",
                "errors": checked["errors"],
                "contradictions": checked["contradictions"],
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # (4) work out the scope and guard the unsafe path.
    seasons, unknown = _resolve_scope(request.data.get("apply_to_seasons"))
    if unknown:
        return Response(
            {"message": f"Unknown season ids: {unknown}.", "unknown_season_ids": unknown},
            status=status.HTTP_400_BAD_REQUEST,
        )
    impact = _impact(seasons)
    if impact["requires_acknowledgement"] and not request.data.get("acknowledge_published"):
        names = ", ".join(
            r["name"] for r in impact["seasons"]
            if not r["in_default_scope"] and (r["is_closed"] or r["rankings_published"]
                                              or r["tiers_published"])
        )
        return Response(
            {
                "message": (
                    f"This change would rewrite results for {names}. Standings people have "
                    f"already seen will change. Send acknowledge_published: true to proceed."
                ),
                "impact": impact,
            },
            status=status.HTTP_409_CONFLICT,
        )

    scope_ids = {s.season_id for s in seasons}

    # (5) the write, atomic so freezing, versioning and pinning can never half-apply.
    with transaction.atomic():
        prev_active = (
            ScoringConfig.objects.select_for_update()
            .filter(is_active=True).order_by("-version").first()
        )
        before = ({"version": prev_active.version, "note": prev_active.note}
                  if prev_active else {"version": None, "note": "(defaults)"})

        # 5a. FREEZE HISTORY. Any season without a pin is pinned to the PREVIOUS config, so
        #     it stays on the rules it was scored under. A season already pinned is left
        #     alone unless it is in scope below. prev_active None pins to the shipped
        #     defaults, which is a real state and not the same as "unpinned".
        pinned_ids = set(SeasonScoringConfig.objects.values_list("season_id", flat=True))
        frozen = []
        for season in Season.objects.exclude(season_id__in=pinned_ids | scope_ids):
            SeasonScoringConfig.objects.create(
                season=season, config=prev_active, bound_by=user,
                origin=SeasonScoringConfig.FROZEN,
                note="Frozen at the rules in force before the following change.",
            )
            frozen.append({
                "season_id": season.season_id,
                "name": season.name,
                "config_version": prev_active.version if prev_active else None,
            })

        # 5b. New version = max + 1, active; every prior row deactivated.
        next_version = (ScoringConfig.objects.aggregate(m=Max("version"))["m"] or 0) + 1
        ScoringConfig.objects.filter(is_active=True).update(is_active=False)
        new_cfg = ScoringConfig.objects.create(
            version=next_version, is_active=True, config=config,
            note=reason, created_by=user,
        )

        # 5c. Pin the seasons in scope to the new version (create or repoint).
        for season in seasons:
            SeasonScoringConfig.objects.update_or_create(
                season=season,
                defaults={
                    "config": new_cfg,
                    "bound_by": user,
                    "origin": SeasonScoringConfig.APPLIED,
                    "note": reason[:255],
                },
            )

        # 5d. Audit. The applied seasons are recorded verbatim so "who changed my June
        #     placement" always has an answer, which is the whole point of the scope rules.
        after = {
            "version": new_cfg.version,
            "note": new_cfg.note,
            "applied_seasons": [
                {"season_id": r["season_id"], "name": r["name"],
                 "rankings_published": r["rankings_published"],
                 "tiers_published": r["tiers_published"],
                 "is_closed": r["is_closed"],
                 "chosen_explicitly": not r["in_default_scope"]}
                for r in impact["seasons"]
            ],
            "frozen_seasons": frozen,
            "acknowledged_published": bool(request.data.get("acknowledge_published")),
            "contradictions": checked["contradictions"],
        }
        current = _current_season()
        entry = _audit(
            user, "scoring_config", "save", reason,
            object_ref=new_cfg.id, before=before, after=after, season=current,
        )

    # (6) rebuild the scores of every season in scope, outside the transaction. This is what
    #     stops the current season being half old rules and half new.
    recalculated = {"seasons": 0, "months": 0}
    if request.data.get("recalculate", True):
        recalculated = _recalculate(seasons)

    body = serialize_scoring_config(new_cfg)
    body["contradictions"] = checked["contradictions"]
    # Recomputed after the write so each row reports the version it is NOW pinned to; the
    # season identity and the published flags are the same either way.
    body["applied_seasons"] = _impact(seasons)["seasons"]
    body["frozen_seasons"] = frozen
    body["recalculated"] = recalculated
    body["audit_id"] = entry.audit_id
    return Response(body, status=status.HTTP_201_CREATED)
