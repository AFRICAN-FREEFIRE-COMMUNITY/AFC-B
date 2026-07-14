# afc_tournament_and_scrims/final_standings.py
# ──────────────────────────────────────────────────────────────────────────────
# OFFICIAL FINAL STANDINGS for a (possibly multi-stage) event.
#
# Owner rule (2026-07-14, DYNASTY CUP GRAND FINALS SSA):
#   "For any multistage event, [placement] should come from the LAST STAGE that team
#    played in; teams that reached the last stage should show differently; and the prize
#    should combine all money the team earned from that event across all stages and groups."
#
# So a team's FINAL PLACEMENT = its rank in the LAST STAGE it actually played. A team that
# advanced to a deeper stage ranks ABOVE every team eliminated in an earlier stage. Inside a
# single stage, teams are ordered by that stage's OFFICIAL standings — the exact table the
# site shows for that stage: base cumulative points + configured tie-breakers, then the
# Point-Rush carry-over fold and the Champion-Point pin overlaid the same way
# get_all_leaderboard_details_for_event / get_event_details render them. So "Nth place" here
# always matches the finals leaderboard a viewer sees on the tournament page.
#
# For a single-stage event this collapses to that one stage's standings (== the whole event).
#
# WHY THIS EXISTS (the bug it fixes):
#   `final_placement` (afc_team) and the prize auto-sync (prize_sync) both ranked teams with
#   `_aggregate_team_standings(match__group__stage__event=event)` — i.e. SUMMED points across
#   ALL stages (SEMI + RUSH POINT + GRAND FINALS merged). That ranked a team by cumulative
#   event points instead of by how far it advanced / how it placed in the deciding stage, so a
#   team that finished mid-table in the Grand Finals could show 4th overall (and draw the
#   4th-place prize) purely on carried semifinal points.
#
# CONNECTS TO:
#   - afc_team.views.get_team_details -> per-event `final_placement` + `reached_final_stage`
#     (the "Final placement" cell + a "Finalist / Reached <finals>" marker in the UI).
#   - afc_tournament_and_scrims.prize_sync.sync_event_prize_payouts -> maps each prize
#     distribution (event / stage / group) onto the matching standings, position -> team.
#   Both call event_final_standings(event) so placement and prize ALWAYS agree.
#
# Reuses (never duplicates) the site's scoring primitives:
#   - round_robin.cumulative_standings / group_standings  (base tie-broken table)
#   - views._final_stage_for_event                        (which stage is the decider)
#   - views._carry_over_for_stage                         (Point-Rush bonus fold)
#   - scoring.champion_for_group                          (Champion-Point pin)
# Lazy (function-scope) imports throughout: this module is imported BY prize_sync + afc_team,
# and views.py imports round_robin/prize_sync, so a module-load import of views would cycle.
# ──────────────────────────────────────────────────────────────────────────────
from collections import defaultdict


def _champion_id_for_group(group, threshold, carry_over):
    """Champion-Point winner for ONE lobby, replayed exactly like the results builder.

    Mirrors get_all_leaderboard_details_for_event's champion replay (views.py ~13914): walk the
    group's matches in play order, feed each team's per-match effective points (the stored
    total_points for that map) through scoring.champion_for_group with the Point-Rush carry-over
    as the head start. Returns the champion's tournament_team_id, or None if the rule never fires.
    """
    from .models import Match, TournamentTeamMatchStats
    from . import scoring as scoring_lib

    replay = []
    for m in Match.objects.filter(group=group).order_by("match_number"):
        rows = [
            {"id": s.tournament_team_id, "placement": s.placement, "points": s.total_points}
            for s in TournamentTeamMatchStats.objects.filter(match=m)
        ]
        replay.append({"rows": rows})
    return scoring_lib.champion_for_group(replay, threshold, carry_over=dict(carry_over or {}))


def official_stage_standings(stage):
    """The ordered team list for ONE stage, matching what the site shows for that stage.

    Base = cumulative_standings(stage) (team rows summed across the stage's lobbies, with the
    event's configured tie-breakers already applied). We then overlay, in the SAME order the
    results builder does:
      (1) Point-Rush carry-over — add each team's banked bonus into effective_total, re-sort on
          the exact DB tiebreak chain, then re-apply config tie-breakers.
      (2) Champion-Point pin — for a SINGLE-lobby stage (a champion is defined per lobby), pin
          the crowned team to the top. Multi-lobby stages keep the merged points order (a
          per-lobby champion has no single meaning once lobbies are merged; deciding stages are
          single-lobby in practice, e.g. a Grand Finals).
    Returns the list of standings dicts (each carries `tournament_team_id`), in final order.
    """
    from .round_robin import cumulative_standings, apply_tie_breakers

    rows = cumulative_standings(stage)
    if not rows:
        return rows
    event = stage.event
    ptype = event.participant_type

    # (1) Point-Rush carry-over (same on-read bonus the leaderboard folds for this stage).
    from .views import _carry_over_for_stage
    carry = _carry_over_for_stage(stage, ptype)
    changed = False
    for r in rows:
        bonus = carry.get(r["tournament_team_id"], 0)
        r["carry_over_points"] = bonus
        if bonus:
            r["effective_total"] = int(r.get("effective_total", 0)) + bonus
            changed = True
    if changed:
        rows.sort(key=lambda r: (
            -int(r.get("effective_total", 0)),
            -int(r.get("total_booyah", 0)),
            -int(r.get("total_kills", 0)),
            int(r.get("last_match_placement", 999)),
            r.get("team_name") or "",
        ))
        rows = apply_tie_breakers(rows, event, stage, None)

    # (2) Champion-Point pin (single-lobby stages only; matches the results builder's pin).
    if getattr(stage, "champion_point_enabled", False) and stage.champion_point_threshold:
        groups = list(stage.groups.all())
        if len(groups) == 1:
            champ = _champion_id_for_group(groups[0], int(stage.champion_point_threshold), carry)
            if champ is not None:
                # Stable pin: champion to the front, everyone else keeps the order above.
                rows.sort(key=lambda r: 0 if r["tournament_team_id"] == champ else 1)
    return rows


def event_final_standings(event):
    """Tiered final standings for an event: rank by LAST STAGE PLAYED, deeper stages first.

    Returns a 4-tuple:
      ordered          : list of {tournament_team_id, rank, stage_id, stage_name,
                         reached_final_stage} in final order (rank 1..N).
      rank_by_tt       : {tournament_team_id: rank}  (1-based).
      reached_final_ids: set of tournament_team_id that actually PLAYED the final stage.
      final_stage      : the Stages row that decides the event (is_finals_stage, else the
                         highest stage_order), or None if the event has no stages.

    Tiering: stages are ordered canonically (stage_order, start_date, stage_id); "deeper" =
    later in that order. Each team is placed in the tier of the DEEPEST stage it has any match
    stats in, and ordered WITHIN that tier by official_stage_standings for that stage. Tiers are
    concatenated deepest-first, so every finalist outranks every team knocked out earlier.
    A single-stage event yields exactly that stage's official standings.
    """
    from .models import Stages, TournamentTeamMatchStats
    from .views import _final_stage_for_event

    stages = list(
        Stages.objects.filter(event=event).order_by("stage_order", "start_date", "stage_id")
    )
    if not stages:
        return [], {}, set(), None

    final_stage = _final_stage_for_event(event)
    stage_by_id = {s.stage_id: s for s in stages}
    depth = {s.stage_id: i for i, s in enumerate(stages)}  # later canonical order = deeper

    # Deepest stage each team actually played (distinct team x stage rows that have stats).
    last_stage_of = {}  # tt_id -> Stages
    played = (
        TournamentTeamMatchStats.objects
        .filter(match__group__stage__event=event, tournament_team__isnull=False)
        .values_list("tournament_team_id", "match__group__stage_id")
        .distinct()
    )
    for tt_id, st_id in played:
        st = stage_by_id.get(st_id)
        if st is None:
            continue
        cur = last_stage_of.get(tt_id)
        if cur is None or depth[st_id] > depth[cur.stage_id]:
            last_stage_of[tt_id] = st

    teams_by_stage = defaultdict(set)
    for tt_id, st in last_stage_of.items():
        teams_by_stage[st.stage_id].add(tt_id)

    # Concatenate tiers deepest-first; order each tier by that stage's official standings.
    ordered = []
    for st in sorted(stages, key=lambda s: depth[s.stage_id], reverse=True):
        tier_ids = teams_by_stage.get(st.stage_id)
        if not tier_ids:
            continue
        seen = set()
        for r in official_stage_standings(st):
            tid = r["tournament_team_id"]
            if tid in tier_ids and tid not in seen:
                ordered.append({"tournament_team_id": tid, "stage_id": st.stage_id,
                                "stage_name": st.stage_name})
                seen.add(tid)
        # Defensive: any tier team missing from the stage table (shouldn't happen) goes last.
        for tid in tier_ids - seen:
            ordered.append({"tournament_team_id": tid, "stage_id": st.stage_id,
                            "stage_name": st.stage_name})

    # "Reached the final stage" is only a meaningful distinction for a MULTI-stage event (in a
    # single-stage event every team that played trivially "reached the last stage", so we flag none —
    # the UI would otherwise badge everyone). final_placement still populates for single-stage events.
    final_id = final_stage.stage_id if final_stage else None
    reached_final_ids = (
        set(teams_by_stage.get(final_id, set())) if (final_id and len(stages) > 1) else set()
    )

    rank_by_tt = {}
    for i, row in enumerate(ordered, start=1):
        row["rank"] = i
        row["reached_final_stage"] = (row["stage_id"] == final_id)
        rank_by_tt[row["tournament_team_id"]] = i

    return ordered, rank_by_tt, reached_final_ids, final_stage
