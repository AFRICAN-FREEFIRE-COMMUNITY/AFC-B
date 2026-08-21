"""
Aggregation adapter - ORM → scoring-engine inputs.

The keystone glue: queries real tournament/scrim match stats from
afc_tournament_and_scrims, recomputes raw placement points via the canonical
§4.1 table (NOT the legacy stored column), applies §12 scrim count caps, derives
win/finals from the admin-set markers, builds the engine's frozen dataclasses,
and returns the engine Result plus tiebreaker counts. The recalc layer persists.

Bucketing uses Match.played_on (falls back to match_date entry date).
Registered teams/players only - ghost-team scoring lands in Phase 3.

Driven by recalc.recalc_* (the compute_* entry points below). Reads the rankings
columns on afc_tournament_and_scrims - Match.played_on, Match.mvp,
Stages.is_finals_stage, TournamentTeam.is_tournament_winner /
TournamentTeam.finals_appearances, EventPrizePayout.amount - so changing any of
those models changes the scoring inputs here.
"""
import calendar
import datetime
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from django.db.models import Q, Sum
from django.utils import timezone

from afc_tournament_and_scrims.models import (
    TournamentTeamMatchStats, TournamentPlayerMatchStats, TournamentTeam, EventPrizePayout,
)
from afc_team.models import Team
from .scoring import engine
from .scoring.engine import TournamentInput, ScrimInput, PlayerTournamentInput, PlayerScrimInput
from .scoring.tables import DEFAULT_TABLES, ScoringTables, tables_from_config
from .models import PLAYER_ROLE_CHOICES, ScoringConfig, Season, SeasonScoringConfig, TeamSocialSnapshot


# ───────────────────────── date helpers ─────────────────────────
def month_bounds(month: datetime.date):
    """[first-of-month, first-of-next-month) for a date already on day 1 (or any day)."""
    start = month.replace(day=1)
    last = calendar.monthrange(start.year, start.month)[1]
    end = start + datetime.timedelta(days=last)
    return start, end


def _day_range_q(prefix: str, start: datetime.date, end: datetime.date) -> Q:
    """Filter on play day, preferring Match.played_on, else the match_date entry datetime."""
    start_dt = datetime.datetime.combine(start, datetime.time.min)
    end_dt = datetime.datetime.combine(end, datetime.time.min)
    if timezone.is_naive(start_dt):  # USE_TZ=True → compare against aware bounds
        start_dt = timezone.make_aware(start_dt)
        end_dt = timezone.make_aware(end_dt)
    return (
        Q(**{f"{prefix}__played_on__gte": start, f"{prefix}__played_on__lt": end}) |
        Q(**{f"{prefix}__played_on__isnull": True,
             f"{prefix}__match_date__gte": start_dt, f"{prefix}__match_date__lt": end_dt})
    )


def _event_of_match(match) -> Optional[object]:
    """Resolve a Match to its Event via group→stage→event, else leaderboard→event."""
    grp = getattr(match, "group", None)
    if grp and getattr(grp, "stage", None):
        return grp.stage.event
    lb = getattr(match, "leaderboard", None)
    if lb:
        return lb.event
    return None


def _match_day(match) -> Optional[datetime.date]:
    if match.played_on:
        return match.played_on
    if match.match_date:
        return match.match_date.date()
    return None


# ───────────────────────── counting controls (Result Markers, §16) ─────────────────────────
def _counting_controls(event_ids):
    """event_id → EventCountingControl for the given events. A missing row ⇒ everything counts.

    Fetched once per aggregation pass to avoid an N+1 over the (few) events an entity played.
    """
    from .models import EventCountingControl
    return {c.event_id: c for c in EventCountingControl.objects.filter(event_id__in=event_ids)}


def _excluded_event_ids(event_ids, *, team=None, player=None):
    """Set of event_ids where this team/player is opted out of counting (ResultExclusion)."""
    from .models import ResultExclusion
    qs = ResultExclusion.objects.filter(event_id__in=event_ids)
    qs = qs.filter(team=team) if team is not None else qs.filter(player=player)
    return set(qs.values_list("event_id", flat=True))


def _switched_off_event_ids(controls):
    """Set of event_ids an admin has switched OFF wholesale (EventCountingControl master switch).

    The counterpart of ``_excluded_event_ids``: that one is per ENTITY ("this team's results in
    this event don't count"), this one is per EVENT ("nothing about this event counts, for
    anybody"). Both feed the SAME ``excluded`` set in _collect_team / _collect_player, so a
    switched-off event drops out of the tournament loop, the scrim rows and the role breakdown in
    exactly one place rather than needing a new check at each of them.

    Takes the already-fetched ``{event_id: EventCountingControl}`` map from ``_counting_controls``
    rather than querying again - the rows are read anyway for the component toggles. An event with
    no control row is absent from the map and therefore counts, which is the model's documented
    "no row ⇒ everything counts" default (owner 2026-08-03: everything counts unless switched off).
    """
    return {event_id for event_id, c in controls.items() if not c.counts_toward_rankings}


def _non_counting_prize_q(*, team=None, player=None) -> Q:
    """Q matching EventPrizePayout rows whose event must NOT contribute prize-money points.

    Prize money is the one scoring input that does not come from a match, so it never passes
    through the ``excluded`` set the two _collect_* functions build - it is summed straight off
    EventPrizePayout in compute_team_quarterly / compute_player_quarterly. Without this filter an
    admin could switch an event off (or exclude a disqualified team from it) and still see its
    prize money scoring, which would make both switches a half-truth.

    Matches on the payout's OWN event rather than on the events the entity has match stats for,
    so a payout recorded for an event whose results were never uploaded is filtered too.

    Two reasons an event's prize must not count, mirroring the two switches:
      * the event's master switch is off  (EventCountingControl.counts_toward_rankings False);
      * this specific team/player is excluded from the event (ResultExclusion).
    Used with ``.exclude(...)``, so a payout is dropped when EITHER holds.
    """
    q = Q(event__counting_control__counts_toward_rankings=False)
    if team is not None:
        return q | Q(event__result_exclusions__team=team)
    return q | Q(event__result_exclusions__player=player)


def _unverified_org_event_ids(event_ids):
    """Organizer integrity gate: org-owned events whose results have NOT been verified by an
    AFC admin (Event.rankings_verified is False) do not count toward the official rankings.
    Native AFC events (organization is null) are never returned here, so they always count.

    NOTE (F5, owner 2026-06-19): this gate intentionally does NOT exclude events whose org was later
    SUSPENDED or soft-DELETED. F5 hides a dead org's events from the listings/detail/directory, but
    the owner rule is that RESULTS are ALWAYS retained - a verified result that already counted keeps
    counting in the rankings even after its org is removed. So the divergence from the _ACTIVE_ORG_EVENT
    list filter is deliberate retention, not an oversight; do not add an org-status filter here."""
    from afc_tournament_and_scrims.models import Event
    return set(
        Event.objects.filter(
            event_id__in=event_ids, organization__isnull=False, rankings_verified=False
        ).values_list("event_id", flat=True)
    )


# ───────────────────────── result containers ─────────────────────────
@dataclass
class TeamAgg:
    result: object                 # engine TeamScoreResult / TeamQuarterlyResult
    tournament_wins: int
    total_kills: int
    tournaments_played: int


@dataclass
class PlayerAgg:
    result: object
    total_kills: int
    mvp_count: int
    finals_appearances: int
    tournaments_played: int
    # {role: {"matches": n, "kills": n}} for the period, built from the roles STAMPED on the
    # matches that produced this score (TournamentPlayerMatchStats.role_at_match). Empty when the
    # period holds no role-stamped match, which is the honest answer for staff, ghosts, solo-only
    # play and anything played before the stamping existed. Persisted verbatim by recalc as
    # PlayerMonthlyScore.role_breakdown / PlayerQuarterlyScore.role_breakdown; `primary_role` of it
    # becomes the stored `role` the per-role ladders filter on. Defaulted so the two other
    # constructors of this dataclass (if any appear) do not have to care.
    role_breakdown: dict = None


# ───────────────────────── scrim cap helper (§12) ─────────────────────────
def _apply_scrim_caps(scrim_match_rows, controls=None, tables: ScoringTables = DEFAULT_TABLES):
    """
    scrim_match_rows: list of (day, placement, kills, event_id). Enforce the daily + monthly
    scrim COUNT caps, then return raw (scrim_placement_pts, scrim_kills, scrim_wins) for the
    kept rows. Both caps come from ``tables`` (default 4/day and 60/month, spec §12) so an
    admin can change them from the scoring config without a deploy.

    ``controls``: {event_id: EventCountingControl} from ``_counting_controls``. A scrim event whose
    admin toggles are off contributes nothing for that component (owner 2026-08-03: "admin toggle so
    all events count by default, with admins able to switch individual ones off"). The toggles were
    previously honoured for tournaments only, so a scrim event could not be switched off at all.
    A disabled component is zeroed here, AFTER the day/month caps have chosen which scrims count,
    so turning kills off does not silently promote a later scrim into a freed cap slot.
    """
    controls = controls or {}
    per_day = defaultdict(int)
    total = 0
    placement_pts = 0.0
    kills = 0.0
    wins = 0
    for day, placement, k, event_id in sorted(scrim_match_rows,
                                              key=lambda r: (r[0] or datetime.date.max)):
        if total >= tables.scrim_monthly_cap:
            break
        if day is not None and per_day[day] >= tables.scrim_daily_cap:
            continue
        per_day[day] += 1
        total += 1
        ctrl = controls.get(event_id)
        # count_placement / count_kills / count_winner map onto the scrim components the same way
        # they do for a tournament: placement points, kill points, and the win bonus.
        if not ctrl or ctrl.count_placement:
            placement_pts += engine.placement_points(placement, tables)
        if not ctrl or ctrl.count_kills:
            kills += k
        if placement == 1 and (not ctrl or ctrl.count_winner):
            wins += 1
    return placement_pts, kills, wins


# ───────────────────────── TEAM ─────────────────────────
# Reads the rankings columns on afc_tournament_and_scrims: Match.played_on,
# Stages.is_finals_stage, TournamentTeam.is_tournament_winner /
# TournamentTeam.finals_appearances, EventPrizePayout.amount. Changing those
# models changes the scoring inputs the engine sees.
def _collect_team(team: Team, start: datetime.date, end: datetime.date,
                  tables: ScoringTables = DEFAULT_TABLES):
    """Returns (tournaments: list[TournamentInput], scrims: ScrimInput, win_count, kill_total).

    The scrim side is returned already day/month-capped AND already filtered by the admin counting
    controls, because those controls are keyed by event_id and only this function still knows which
    event each scrim row came from.
    """
    stats = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team__team=team)
        .filter(_day_range_q("match", start, end))
        .select_related("match", "match__group__stage__event", "match__leaderboard__event",
                        "tournament_team", "tournament_team__event")
    )
    tour_events = defaultdict(list)     # event -> [stats]
    scrim_rows = []                     # (day, placement, kills, event_id)
    scrim_event_ids = set()
    for s in stats:
        ev = _event_of_match(s.match)
        if ev is None:
            continue
        if ev.competition_type == "scrims":
            # Carry the event_id so the admin counting controls / exclusions below can reach scrims
            # too - before this they applied to tournaments only and a scrim event had no off switch.
            scrim_rows.append((_match_day(s.match), s.placement, s.kills, ev.event_id))
            scrim_event_ids.add(ev.event_id)
        else:
            tour_events[ev.event_id].append((ev, s))

    # admin counting controls + per-team exclusions (§16, Result Markers surface).
    # Scrim event ids are included so a scrim can be toggled off / excluded exactly like a
    # tournament - the owner's rule is that everything counts by DEFAULT and the admin switches
    # individual ones off (2026-08-03).
    event_ids = list(tour_events.keys()) + list(scrim_event_ids)
    controls = _counting_controls(event_ids)
    excluded = _excluded_event_ids(event_ids, team=team)
    # An event switched off wholesale is treated exactly like an exclusion: it drops out of the
    # tournament loop AND the scrim rows below, so nothing about it reaches the engine.
    excluded |= _switched_off_event_ids(controls)
    # The organizer-verification gate is applied to TOURNAMENTS ONLY, on purpose. Every scrim in
    # production today is org-owned with rankings_verified=False, so folding scrims into this gate
    # would silently switch off every scrim that currently counts - the opposite of the owner's
    # "count by default" rule. Whether an unverified org's SCRIMS should also be gated is an open
    # policy question for the owner, not something to decide here.
    excluded |= _unverified_org_event_ids(list(tour_events.keys()))
    scrim_rows = [r for r in scrim_rows if r[3] not in excluded]

    tournaments = []
    win_count = 0
    kill_total = 0
    for event_id, rows in tour_events.items():
        if event_id in excluded:
            continue  # this team's results in this event are opted out of counting
        ev = rows[0][0]
        # PLACEMENT POINTS, and the one case where they are not derived here (owner 2026-08-21).
        #
        # Normally a row's placement points come from its FINISH in that map, looked up in the
        # admin's own scoring table. An IMPORTED SUMMED row has no per-map finish to look up: the
        # organizer published a standings table, so `placement` is NULL and the only placement
        # figure that exists is the total they published, carried in `placement_points`.
        #
        # The owner's decision is that such an event counts like any other once an admin switches
        # it on. So for an aggregate row the published total is used as-is. WHAT THAT MEANS, stated
        # plainly because it is the thing to check if a ranking ever looks off: those points came
        # from the SOURCE tournament's placement ladder, not AFC's. In practice the two usually
        # agree, because AFC's default ladder (12/9/8/7/6/5/4/3/2/1, DEFAULT_PLACEMENT_POINTS) is
        # the standard Free Fire one that most organizers publish under. Where a source used a
        # different ladder, its teams' placement points arrive on that source's scale.
        #
        # Everything else about the event is still scored by AFC's own configuration: kills, the
        # winner and finals bonuses, the participation rules, and the TIER multiplier, which is the
        # weight applied to the whole event. Only this one input is taken as published.
        raw_placement = sum(
            (s.placement_points or 0) if s.is_aggregate
            else engine.placement_points(s.placement, tables)
            for _, s in rows
        )
        raw_kills = sum(s.kills for _, s in rows)
        tt = TournamentTeam.objects.filter(event_id=event_id, team=team).first()
        won = bool(tt and tt.is_tournament_winner)
        finals = (tt.finals_appearances if tt else 0)
        # a component the admin disabled is zeroed before the engine sees it (engine stays pure)
        ctrl = controls.get(event_id)
        if ctrl:
            if not ctrl.count_winner:
                won = False
            if not ctrl.count_placement:
                raw_placement = 0
            if not ctrl.count_kills:
                raw_kills = 0
        tournaments.append(TournamentInput(
            tier=ev.tournament_tier, raw_placement_pts=raw_placement,
            raw_kills=raw_kills, won=won, finals_appearances=finals,
        ))
        win_count += 1 if won else 0
        kill_total += raw_kills

    # P3: a published, counts_toward_rankings standalone leaderboard contributes one TournamentInput
    # per real-team participant (canonical placement table, same as events), so a standalone result
    # scores through the identical engine path here. Lazy import avoids a load-order cycle
    # (afc_leaderboard.models imports afc_rankings.models). Fold the standalone kills into kill_total
    # too so the §5.4 total_kills tiebreaker stays honest.
    from . import standalone
    sa_inputs = standalone.standalone_team_inputs(team, start, end, tables)
    tournaments += sa_inputs
    kill_total += sum(t.raw_kills for t in sa_inputs)
    sp, sk, sw = _apply_scrim_caps(scrim_rows, controls, tables)
    return tournaments, ScrimInput(sp, sk, sw), win_count, kill_total


# ───────────────────────── scoring config resolution (which rules govern which period) ────────
# THE DJANGO-AWARE HALF of the admin-editable scoring config. The scales themselves live in
# scoring/tables.py, which is pure; deciding WHICH saved version applies to a given month or
# season is a database question, so it belongs here, alongside scrim_flat_cap() below which
# has followed the same pattern since the flat scrim allowance became configurable.
#
# The resolution order, and why:
#   1. The season's own pin (SeasonScoringConfig). A season that has been scored keeps the
#      rules it was scored under, which is what makes an edit non-retroactive. A pin whose
#      config is NULL means "pinned to the shipped defaults", which is NOT the same as no pin.
#   2. Otherwise the active config. A season nobody has pinned yet is being scored for the
#      first time, so today's rules are the right ones.
#   3. Otherwise the shipped constants.py defaults.
#
# Read at call time, never cached, so an admin edit takes effect on the next recalculation
# without a restart - and so a save can never leave a stale config in a worker's memory. The
# cost is one small indexed query per compute call, which is the same order as the existing
# scrim_flat_cap() lookup this replaces.
def config_for_season(season):
    """The ScoringConfig row governing ``season``, or None meaning the shipped defaults."""
    if season is None:
        return ScoringConfig.objects.filter(is_active=True).order_by("-version").first()
    binding = (SeasonScoringConfig.objects
               .filter(season=season).select_related("config").first())
    if binding is not None:
        return binding.config       # may be None: explicitly pinned to the defaults
    return ScoringConfig.objects.filter(is_active=True).order_by("-version").first()


def season_for_month(month: datetime.date):
    """The season whose date range contains this month, or None.

    Monthly ladders are not seasons, but every month sits inside one, so a month inherits
    the season's pinned rules. That is what stops a month inside a closed season being
    re-scored under new rules while the quarter it belongs to stays frozen.
    """
    day = month.replace(day=1)
    return Season.objects.filter(start_date__lte=day, end_date__gte=day).order_by(
        "-year", "-quarter").first()


def resolve_tables(*, season=None, month=None) -> ScoringTables:
    """The ScoringTables that govern a period, ready to hand to the pure scoring engine.

    Fail-soft on purpose: any problem reading or parsing the config falls back to the
    shipped defaults rather than raising, because a scoring run must never die over
    configuration. Validation at SAVE time (scoring/validation.py) is what keeps bad values
    out; this is the last-resort guard for a row edited by hand in the database.
    """
    try:
        if season is None and month is not None:
            season = season_for_month(month)
        cfg = config_for_season(season)
        if cfg is None:
            return DEFAULT_TABLES
        return tables_from_config(cfg.config, version=cfg.version)
    except Exception:
        return DEFAULT_TABLES


def scrim_flat_cap() -> float:
    """The flat scrim allowance an admin has configured, or the shipped default.

    Lives HERE rather than in the scoring engine because that module is deliberately
    Django-free and pure (see its docstring): it must never read the database. This is
    the Django-aware layer, so the lookup belongs here and the value is passed down.

    Kept as its own function because callers outside the compute path use it (and the
    tests pin its fallback behaviour), but it is now a thin read off ``resolve_tables``:
    the compute functions below take the whole ScoringTables, which already carries the
    allowance, so they do not call this.
    """
    return resolve_tables().scrim_flat_cap


def compute_team_monthly(team: Team, month: datetime.date) -> TeamAgg:
    tables = resolve_tables(month=month)
    start, end = month_bounds(month)
    tournaments, scrims, wins, kills = _collect_team(team, start, end, tables)
    result = engine.monthly_team_score(tournaments, scrims, None, tables)
    return TeamAgg(result=result, tournament_wins=wins, total_kills=kills,
                   tournaments_played=len(tournaments))


def compute_team_quarterly(team: Team, season) -> TeamAgg:
    tables = resolve_tables(season=season)
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tournaments, scrims, wins, kills = _collect_team(team, start, end, tables)

    # prize money (§7.2) - sum payouts to this team's tournament-teams in the season window,
    # minus any event switched off or excluded for this team (see _non_counting_prize_q: the
    # payout is keyed off the event, so it does not ride the match-derived ``excluded`` set).
    prize = (EventPrizePayout.objects
             .filter(tournament_team__team=team,
                     created_at__date__gte=start, created_at__date__lt=end)
             .exclude(_non_counting_prize_q(team=team))
             .aggregate(total=Sum("amount"))["total"] or 0)

    # social (§7.3) - quarter snapshot. Only a VERIFIED snapshot contributes points
    # (self-connect → admin verify); an unverified or absent snapshot scores 0.
    snap = TeamSocialSnapshot.objects.filter(team=team, season=season).first()
    followers = snap.combined_followers if (snap and snap.is_verified) else 0

    result = engine.quarterly_team_score(
        tournaments, scrims,
        prize_money_naira=float(prize), combined_followers=followers,
        tables=tables,
    )
    return TeamAgg(result=result, tournament_wins=wins, total_kills=kills,
                   tournaments_played=len(tournaments))


# ───────────────────────── PLAYER ─────────────────────────
def _collect_player(player, start: datetime.date, end: datetime.date,
                    tables: ScoringTables = DEFAULT_TABLES):
    """Build the per-tournament PlayerTournamentInput list + the player's scrim aggregate.

    Returns (tournaments, scrims: PlayerScrimInput, mvp_total, finals_total, kill_total,
    role_breakdown). Like the team path, the scrim side comes back already filtered by the admin
    counting controls / exclusions, since only this function knows which event each scrim row
    belongs to.

    ROLE HISTORY (owner 2026-08-04). Alongside the score, this walks the ``role_at_match`` stamped
    on each of the player's match rows and returns
    ``{role: {"matches": n, "kills": n}}`` for the window. That is the ONLY honest way to say what a
    player was during a past period: the stamp was written from the frozen event roster when the
    result was recorded, so it describes the player as they were then, and no later transfer or role
    change can rewrite it. recalc persists the breakdown and its primary role onto the period score
    row, and ``player_roles.players_by_role`` filters the public role ladders on that stored value
    instead of joining today's ``afc_team.TeamMembers``.

    The breakdown honours exactly the same exclusions as the score it accompanies: a match in an
    excluded / opted-out event contributes no role, and an event whose kills are switched off
    contributes its matches but no kills. That keeps the role-scoped columns consistent with the
    number they sit next to, rather than describing play the score does not reflect.

    Standalone SOLO leaderboards (the ``standalone`` block further down) contribute to the score but
    NOT to the breakdown, and deliberately so: they have no team, so no squad role applies to them.
    A player whose whole period was solo play ends with an empty breakdown and no stored role, which
    is the truth.
    """
    pstats = (
        TournamentPlayerMatchStats.objects
        .filter(player=player, played=True)
        .filter(_day_range_q("team_stats__match", start, end))
        .select_related("team_stats", "team_stats__match",
                        "team_stats__match__group__stage", "team_stats__match__group__stage__event",
                        "team_stats__match__leaderboard__event", "team_stats__tournament_team")
    )
    tour = defaultdict(lambda: {"kills": 0, "ev": None, "finals": 0, "team_won": False, "mvp": 0})
    scrim_rows = []   # (day, kills, is_win, event_id)
    scrim_event_ids = set()
    # One entry per role-stamped match played in the window, tournaments AND scrims alike: a scrim is
    # still a match played in a role. Folded into the breakdown below, once the exclusions are known.
    # (event_id, role, kills)
    role_rows = []
    for ps in pstats:
        match = ps.team_stats.match
        ev = _event_of_match(match)
        if ev is None:
            continue
        # Unstamped rows are skipped rather than defaulted: no role was recorded for that match and
        # inventing one from the current roster is the bug this whole feature removes.
        if ps.role_at_match:
            role_rows.append((ev.event_id, ps.role_at_match, ps.kills))
        if ev.competition_type == "scrims":
            # event_id carried so the counting controls / exclusions below reach scrims too.
            scrim_rows.append((_match_day(match), ps.kills,
                               ps.team_stats.placement == 1, ev.event_id))
            scrim_event_ids.add(ev.event_id)
            continue
        bucket = tour[ev.event_id]
        bucket["ev"] = ev
        bucket["kills"] += ps.kills
        # finals appearance: played a match in a finals stage
        grp = getattr(match, "group", None)
        stage = grp.stage if grp else None
        if stage and getattr(stage, "is_finals_stage", False):
            bucket["finals"] += 1
        # team win for this tournament
        tt = ps.team_stats.tournament_team
        if tt and tt.is_tournament_winner:
            bucket["team_won"] = True
        # MVP for this match
        if match.mvp_id == getattr(player, "user_id", None):
            bucket["mvp"] += 1

    # admin counting controls + per-player exclusions (§16). Scrim event ids are in the lookup so a
    # scrim can be toggled off / excluded exactly like a tournament. The organizer-verification gate
    # stays tournament-only for the reason spelled out in _collect_team.
    event_ids = list(tour.keys()) + list(scrim_event_ids)
    controls = _counting_controls(event_ids)
    excluded = _excluded_event_ids(event_ids, player=player)
    excluded |= _switched_off_event_ids(controls)            # master switch, see _collect_team
    excluded |= _unverified_org_event_ids(list(tour.keys()))  # unverified org events don't count
    scrim_rows = [r for r in scrim_rows if r[3] not in excluded]

    # ── role breakdown: what the player actually played, per role, in this window ──────────────────
    # Built here rather than in the loop above because only now are the exclusions and the per-event
    # counting controls known, and the breakdown must agree with the score it will sit beside:
    #   * an excluded / opted-out event contributes nothing at all (the score ignores it too);
    #   * an event with count_kills switched off still contributes its MATCHES (they were played in
    #     that role) but no kills, mirroring the kills=0 the tournament loop below applies.
    role_breakdown = {}
    for event_id, role, kills in role_rows:
        if event_id in excluded:
            continue
        ctrl = controls.get(event_id)
        bucket = role_breakdown.setdefault(role, {"matches": 0, "kills": 0})
        bucket["matches"] += 1
        bucket["kills"] += 0 if (ctrl and not ctrl.count_kills) else kills

    tournaments = []
    mvp_total = finals_total = kill_total = 0
    for event_id, b in tour.items():
        if event_id in excluded:
            continue  # this player's results in this event are opted out of counting
        ev = b["ev"]
        kills = b["kills"]
        team_won = b["team_won"]
        ctrl = controls.get(event_id)
        if ctrl:
            if not ctrl.count_kills:
                kills = 0
            if not ctrl.count_winner:
                team_won = False
            # count_placement: players score on kills/mvp/finals/team-win/participation,
            # not raw placement (personal_placement_pts is already 0) - nothing to zero here.
        tournaments.append(PlayerTournamentInput(
            tier=ev.tournament_tier, personal_kills=kills, personal_placement_pts=0,
            mvp_count=b["mvp"], finals_appearances=b["finals"],
            team_won=team_won, participated=True,
        ))
        mvp_total += b["mvp"]
        finals_total += b["finals"]
        kill_total += kills

    # P3: a published, counts_toward_rankings SOLO standalone leaderboard contributes one
    # PlayerTournamentInput per real-user participant (kills + participation only, never raw
    # placement - symmetric with the event player path above). Lazy import avoids the load-order
    # cycle; fold the standalone personal_kills into kill_total for the §6.4 tiebreaker.
    from . import standalone
    sa_inputs = standalone.standalone_player_inputs(player, start, end, tables)
    tournaments += sa_inputs
    kill_total += sum(t.personal_kills for t in sa_inputs)

    # Player scrim aggregate, with the same per-event toggles the team path applies: count_kills
    # gates the scrim kills, count_winner gates the scrim win bonus. (count_placement has no player
    # analogue - a player never scores raw placement, see the tournament loop above.)
    s_kills = sum(k for _, k, _, evid in scrim_rows
                  if not (c := controls.get(evid)) or c.count_kills)
    s_wins = sum(1 for _, _, win, evid in scrim_rows
                 if win and (not (c := controls.get(evid)) or c.count_winner))
    return (tournaments, PlayerScrimInput(scrim_kills=s_kills, scrim_wins=s_wins),
            mvp_total, finals_total, kill_total, role_breakdown)


def primary_role(role_breakdown):
    """The one role a period is filed under, from a ``{role: {"matches", "kills"}}`` breakdown.

    A player can genuinely play several roles inside one month or season. The role ladder still has
    to list them exactly once, or the tables stop being a partition of the ladder and a mixed-role
    player is counted twice in two different tab counts. So the period is filed under the role the
    player played MOST of it in, and the full split stays in ``role_breakdown`` so the UI can mark
    the row as mixed and show the role-scoped matches/kills rather than pretending the month was
    single-role.

    Ordering, most significant first: matches played in the role, then kills in it, then the role's
    position in the model's own choice order. The last key is only a determinism tiebreak (an exact
    tie on both counts must not depend on dict iteration order), never a judgement about which role
    is "better".

    Returns None for an empty / missing breakdown, which is what "no role recorded for this period"
    is written as. Shared with the backfill command so the stored role is computed one way only.
    """
    if not role_breakdown:
        return None
    order = {key: index for index, (key, _label) in enumerate(PLAYER_ROLE_CHOICES)}
    return max(
        role_breakdown,
        key=lambda role: (role_breakdown[role].get("matches", 0),
                          role_breakdown[role].get("kills", 0),
                          -order.get(role, len(order))),
    )


def compute_player_monthly(player, month: datetime.date) -> PlayerAgg:
    tables = resolve_tables(month=month)
    start, end = month_bounds(month)
    tournaments, scrims, mvp, finals, kills, roles = _collect_player(player, start, end, tables)
    result = engine.monthly_player_score(tournaments, scrims, tables)
    return PlayerAgg(result=result, total_kills=kills, mvp_count=mvp,
                     finals_appearances=finals, tournaments_played=len(tournaments),
                     role_breakdown=roles)


def compute_player_quarterly(player, season) -> PlayerAgg:
    tables = resolve_tables(season=season)
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tournaments, scrims, mvp, finals, kills, roles = _collect_player(player, start, end, tables)
    # inherited prize money - payouts to any team the player was rostered on (Phase 1: via
    # tournament_team membership), minus any event switched off or excluded for this player
    # (same rule as the team path, see _non_counting_prize_q).
    prize = (EventPrizePayout.objects
             .filter(tournament_team__members__user=player,
                     created_at__date__gte=start, created_at__date__lt=end)
             .exclude(_non_counting_prize_q(player=player))
             .aggregate(total=Sum("amount"))["total"] or 0)
    result = engine.quarterly_player_score(
        tournaments, scrims,
        inherited_prize_money_naira=float(prize),
        tables=tables,
    )
    return PlayerAgg(result=result, total_kills=kills, mvp_count=mvp,
                     finals_appearances=finals, tournaments_played=len(tournaments),
                     role_breakdown=roles)
