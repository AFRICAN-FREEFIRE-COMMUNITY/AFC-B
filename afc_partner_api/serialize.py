# afc_partner_api/serialize.py
# ──────────────────────────────────────────────────────────────────────────────
# The partner-facing serialization FIREWALL — the single most security-critical
# module in this app. Every read endpoint passes its ORM objects through one of the
# functions here before anything reaches the wire, so this file is the ONE boundary
# that decides what a partner can ever see.
#
# Two rules, applied to EVERY function (spec §8):
#
#   1. ALLOWLIST, not denylist. A field is emitted ONLY because this code explicitly
#      put it in the output dict. We build small dicts of public handles + dates +
#      status by hand; we NEVER `return model.__dict__` or spread a `.values()` row,
#      because that is exactly how a raw PK / room credential / PII column leaks. If a
#      field is not written here on purpose, it does not exist for the partner.
#
#   2. TOGGLE GATES on stats/details. Public handles (slug, name, in-game id, dates,
#      status, placement-vs-others ordering) are always safe to emit, but every stat
#      or detail field (placements, kills, damage, assists, rosters, maps, prize, mvp)
#      is wrapped in `if partner.include_<x>:` and appears ONLY when that toggle is on.
#      Toggles default OFF (least privilege), so a brand-new partner sees handles only.
#
# What is NEVER emitted, anywhere (the test denylist enforces this):
#   • raw DB PKs            — event_id, match_id, stage_id, group_id,
#                             tournament_team_id, player_id, competitor_id,
#                             leaderboard_id, organization_id
#   • room credentials      — room_id, room_password, room_name
#   • PII / contact         — contact_email, email, full_name/real names, discord_id,
#                             discord_role_id (stage/group/waitlist discord role ids)
#   • internal config/flags — scoring_settings, rankings_verified, is_draft, creator,
#                             partner_published
# `is_native_afc` is derived as `organization_id is None` (a boolean), so partners
# learn an event is a native AFC event WITHOUT ever receiving the raw org PK.
#
# Aggregation note: match/team/standings/player stats are folded from the
# ALREADY-FINALIZED stat rows (TournamentTeamMatchStats for squad/duo events,
# SoloPlayerMatchStats for solo events) — the same rows the admin standings view sums
# (afc_tournament_and_scrims.views.get_all_leaderboard_details_for_event). We reuse
# that summation but strip the result to the public, toggled-on fields only.
# Full spec: WEBSITE/tasks/partner-api-design.md (§8 serialization rules).
# ──────────────────────────────────────────────────────────────────────────────
from django.db.models import Case, Count, IntegerField, Min, Sum, Value, When
from django.db.models.functions import Coalesce

from afc_tournament_and_scrims.models import (
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
)


# ── event ──────────────────────────────────────────────────────────────────────
def serialize_event(ev, partner):
    """Public event card: slug + display fields + dates + status. No PKs, no flags.

    `is_native_afc` is the ONLY thing we expose about ownership — derived from
    organization_id so the raw org PK never crosses the firewall.
    """
    out = {
        "slug": ev.slug,
        "name": ev.event_name,
        "competition_type": ev.competition_type,
        "participant_type": ev.participant_type,
        "tier": ev.tournament_tier,
        "status": ev.event_status,
        "start_date": ev.start_date,
        "end_date": ev.end_date,
        "is_native_afc": ev.organization_id is None,
    }
    # Prize pool is a detail field, gated on include_prize.
    if partner.include_prize:
        out["prize_pool"] = ev.prizepool
    return out


# ── stage ──────────────────────────────────────────────────────────────────────
def serialize_stage(stage, partner):
    """Public stage row: name + 1-based order within the event + dates + status.

    `order` is computed from the stage's position among its event's stages (ordered
    by stage_id, the same ordering the admin standings view uses) rather than exposing
    the raw stage_id — partners get a stable sequence number, never a DB PK.
    """
    # Position of this stage among its siblings, ordered by stage_id (creation order).
    # Counting stages created before-or-at this one yields a 1-based ordinal.
    order = (stage.event.stages.filter(stage_id__lte=stage.stage_id).count())
    out = {
        "stage_name": stage.stage_name,
        "order": order,
        "format": stage.stage_format,
        "status": stage.stage_status,
        "start_date": stage.start_date,
        "end_date": stage.end_date,
    }
    return out


# ── group ──────────────────────────────────────────────────────────────────────
def serialize_group(group, partner):
    """Public group row: name + schedule. No PKs, no discord role id.

    Maps played in the group are a detail field, gated on include_maps.
    """
    out = {
        "group_name": group.group_name,
        "playing_date": group.playing_date,
    }
    if partner.include_maps:
        # match_maps is a plain JSON list of map names (public, no ids).
        out["maps"] = list(group.match_maps or [])
    return out


# ── match ──────────────────────────────────────────────────────────────────────
def serialize_match(match, partner):
    """Public match row: match_number + status. Room credentials are STRIPPED.

    The match carries room_id / room_password / room_name + scoring_settings, none of
    which may ever reach a partner — so we hand-pick only match_number and the public
    result flag, and gate map (include_maps) and mvp (include_mvp) behind toggles.
    """
    out = {
        "match_number": match.match_number,
        "result_inputted": match.result_inputted,
    }
    if partner.include_maps:
        out["map"] = match.match_map
    if partner.include_mvp:
        # MVP is the in-game handle only (or null if none recorded — spec §11 edge case).
        out["mvp"] = match.mvp.username if match.mvp else None
    return out


# ── team ───────────────────────────────────────────────────────────────────────
def serialize_team(tt, partner):
    """One tournament team's public identity + its event-wide aggregated stats.

    `tt` is a TournamentTeam (a team's entry in one event). We fold ALL of that team's
    finalized TournamentTeamMatchStats rows across the event into a single summary, and
    emit each stat ONLY when its toggle is on:
      • include_placements -> best (lowest) placement the team achieved
      • include_kills/damage/assists -> summed across the team's matches
      • include_rosters -> the team's player list (public handles only)
    The team name/tag are always-safe public handles; no team_id / tournament_team_id.
    """
    out = {"team": tt.team.team_name, "team_tag": tt.team.team_tag}

    # Aggregate this team's finalized per-match stat rows once (avoids N queries below).
    agg = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team=tt)
        .aggregate(
            best_placement=Min("placement"),
            kills=Sum("kills"),
            damage=Sum("damage"),
            assists=Sum("assists"),
        )
    )

    if partner.include_placements:
        # Best result the team reached; null if it never recorded a match.
        out["placement"] = agg["best_placement"]
    if partner.include_kills:
        out["kills"] = agg["kills"] or 0
    if partner.include_damage:
        out["damage"] = agg["damage"] or 0
    if partner.include_assists:
        out["assists"] = agg["assists"] or 0
    if partner.include_rosters:
        # Public handles only — username + in-game id, never name/email/discord.
        # Pass `tt` so each roster player's stats are folded ONLY from this team's rows
        # in THIS event (scoped), not the player's lifetime stats across every event.
        out["roster"] = [serialize_player(p, partner, tournament_team=tt) for p in _team_players(tt)]
    return out


def _team_players(tt):
    """Distinct Users who recorded player stats for this tournament team, in a stable
    order. We read the roster from the finalized stat rows (TournamentPlayerMatchStats)
    rather than the registration tables so it reflects who actually played."""
    from afc_auth.models import User

    player_ids = (
        TournamentPlayerMatchStats.objects
        .filter(team_stats__tournament_team=tt)
        .values_list("player_id", flat=True)
        .distinct()
    )
    # order_by username for a deterministic, handle-sorted roster.
    return User.objects.filter(pk__in=list(player_ids)).order_by("username")


# ── player ─────────────────────────────────────────────────────────────────────
def serialize_player(user, partner, tournament_team=None):
    """One player's PUBLIC handle (+ optional folded stats). NEVER full_name/email/discord.

    Always emits the in-game username + in-game id (uid). Stats are folded from the
    player's finalized TournamentPlayerMatchStats rows and gated per toggle.

    `tournament_team` SCOPES the stat fold to a single team-in-one-event. It MUST be
    passed for any per-event payload (rosters, the per-event players endpoint): a
    TournamentPlayerMatchStats row links to its team via team_stats.tournament_team,
    and a tournament_team belongs to exactly one Event — so filtering on it confines
    the aggregate to this player's stats IN THIS EVENT. Without it the aggregate spans
    every event the player ever played (lifetime totals), which would leak wrong,
    cross-event numbers into a per-event response. (Left optional only for a future
    truly-global player view; every current caller passes the team.)
    """
    out = {"username": user.username, "in_game_id": user.uid}

    # Only touch the stat tables if at least one stat toggle is on (avoids a needless query).
    if partner.include_kills or partner.include_damage or partner.include_assists:
        rows = TournamentPlayerMatchStats.objects.filter(player=user)
        if tournament_team is not None:
            # Scope to this team's matches in this event (see docstring).
            rows = rows.filter(team_stats__tournament_team=tournament_team)
        agg = rows.aggregate(kills=Sum("kills"), damage=Sum("damage"), assists=Sum("assists"))
        if partner.include_kills:
            out["kills"] = agg["kills"] or 0
        if partner.include_damage:
            out["damage"] = agg["damage"] or 0
        if partner.include_assists:
            out["assists"] = agg["assists"] or 0
    return out


# ── standings ──────────────────────────────────────────────────────────────────
#
# Ranking metric — why `effective_total`, not the stored `total_points` column:
# the official admin standings view (afc_tournament_and_scrims.views.
# get_all_leaderboard_details_for_event) does NOT trust the persisted total_points
# column — it RECOMPUTES the rank metric on read as
#       effective_total = placement_points + kill_points + bonus_points - penalty_points
# precisely because total_points can be STALE (e.g. a bonus/penalty edited after the
# row was first saved). To keep partner rankings aligned with official AFC standings we
# compute the SAME effective_total here and order by it, with the admin view's leading
# tiebreakers that are well-defined over an event-wide fold:
#       -effective_total, -total_booyah (1st-place finishes), -total_kills.
#
# Honest scope note (so this comment can't drift false like the old one): the admin view
# is computed PER LOBBY/GROUP and carries two extra steps we deliberately do NOT
# replicate in this event-wide partner aggregate — (a) its final `last_match_placement`
# tiebreaker (a per-group "placement in the latest match" subquery) and (b) the
# scoring-mode carry-over overlay. Those are lobby-local; the partner standings are a
# single event-wide ranking. We match the admin's PRIMARY ordering exactly; the residual
# last-match tiebreaker only ever matters when effective_total, booyah, AND kills all tie.


def serialize_standings(event, partner):
    """Event-wide final standings: a ranked list of competitors with public handle +
    toggled stats. Reads from the ALREADY-FINALIZED stat rows and ranks by the same
    recomputed `effective_total` metric the admin standings view uses (see the module
    comment above for why total_points is NOT trusted), then assigns a 1-based `rank`.

    Solo events fold SoloPlayerMatchStats by competitor; squad/duo events fold
    TournamentTeamMatchStats by team. Either way we emit ONLY a public handle, the
    rank, and the toggled-on stat fields — never the underlying competitor/team PK.
    """
    if event.participant_type == "solo":
        return _solo_standings(event, partner)
    return _team_standings(event, partner)


# Booyah = a 1st-place finish. Counting these mirrors the admin view's `total_booyah`
# tiebreaker (Sum of "1 when placement==1 else 0") so partner and official ties break
# the same way.
_BOOYAH = Sum(Case(When(placement=1, then=Value(1)), default=Value(0),
                   output_field=IntegerField()))

# Recomputed-on-read rank metric, identical to the admin view's `effective_total`
# (placement + kill + bonus - penalty). We never order by the stored total_points,
# which can be stale.
_EFFECTIVE_TOTAL = (
    Coalesce(Sum("placement_points"), 0)
    + Coalesce(Sum("kill_points"), 0)
    + Coalesce(Sum("bonus_points"), 0)
    - Coalesce(Sum("penalty_points"), 0)
)


def _team_standings(event, partner):
    # Fold every team's finalized match rows in this event into one summary per team,
    # then rank by recomputed effective_total (admin parity), booyahs, kills — winners
    # first. total_points is still summed only to expose it; it is NOT the sort key.
    rows = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team__event=event)
        .values("tournament_team__team__team_name")
        .annotate(
            effective_total=_EFFECTIVE_TOTAL,
            total_booyah=_BOOYAH,
            total_points=Sum("total_points"),
            kills=Sum("kills"),
            damage=Sum("damage"),
            assists=Sum("assists"),
            best_placement=Min("placement"),
            matches_played=Count("team_stats_id"),
        )
        .order_by("-effective_total", "-total_booyah", "-kills")
    )
    out = []
    for i, r in enumerate(rows, start=1):
        # rank is a derived ordinal; team_name is the public handle. No PKs.
        entry = {"rank": i, "team": r["tournament_team__team__team_name"]}
        _apply_standings_toggles(entry, r, partner)
        out.append(entry)
    return out


def _solo_standings(event, partner):
    rows = (
        SoloPlayerMatchStats.objects
        .filter(match__group__stage__event=event)
        .values("competitor__user__username", "competitor__user__uid")
        .annotate(
            effective_total=_EFFECTIVE_TOTAL,
            total_booyah=_BOOYAH,
            total_points=Sum("total_points"),
            kills=Sum("kills"),
            best_placement=Min("placement"),
            matches_played=Count("id"),
        )
        .order_by("-effective_total", "-total_booyah", "-kills")
    )
    out = []
    for i, r in enumerate(rows, start=1):
        entry = {
            "rank": i,
            "username": r["competitor__user__username"],
            "in_game_id": r["competitor__user__uid"],
        }
        _apply_standings_toggles(entry, r, partner)
        out.append(entry)
    return out


def _apply_standings_toggles(entry, row, partner):
    """Copy ONLY the toggled-on aggregated stats from an annotated standings row into the
    public entry. `row` is a dict from a .values().annotate() queryset; we never spread
    it wholesale (that would leak the competitor/team key), only pull named stats."""
    if partner.include_placements:
        entry["placement"] = row.get("best_placement")
    if partner.include_kills:
        entry["kills"] = row.get("kills") or 0
    # damage/assists only exist on the team aggregate; .get() is None for solo rows.
    if partner.include_damage and "damage" in row:
        entry["damage"] = row.get("damage") or 0
    if partner.include_assists and "assists" in row:
        entry["assists"] = row.get("assists") or 0
