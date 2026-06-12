"""
Clash-Squad head-to-head bracket engine (bracket sub-projects C + D).

Until this module, every "cs - ..." Stages.stage_format was a DECORATIVE string: all
results flowed through the BR-shaped TournamentTeamMatchStats (placement + kills) and no
head-to-head structure existed. This module gives the CS formats a real engine, mirroring
how round_robin.py (sub-project B) hosts the BR Round-Robin logic:

  - generate_bracket(stage, team_ids, fmt)  -> build the HeadToHeadMatch tree for a stage
        single_elim       : power-of-2 knockout with byes (higher seeds get the byes)
        double_elim       : winners + losers brackets + a single grand final (no reset)
        league            : every pair plays once (circle method), no advancement links
        round_robin_h2h   : same pairing engine as league (kept as a distinct fmt name so
                            a future "double round robin" league variant can diverge)
  - report_result(match, score_a, score_b)  -> validate, set winner, advance winner/loser
  - standings(stage)                        -> league table OR elimination placements
  - write_placement_stats(stage)            -> SUB-PROJECT D BRIDGE (see its docstring):
        writes one synthetic TournamentTeamMatchStats row per team so the EXISTING
        leaderboard reads (get_all_leaderboard_details_for_event, round_robin standings)
        and the afc_rankings aggregation see CS results with ZERO changes on their side.

HOW IT CONNECTS
  - Model: HeadToHeadMatch (models.py) hanging off Stages; teams are the per-event
    TournamentTeam rows (same identity every other result table uses).
  - Endpoints: head_to_head_views.py (generate / read bracket / report result), wired in
    urls.py under events/stages/<stage_id>/bracket/ and events/h2h-matches/<id>/result/.
  - Consumers: the FE bracket page (GET response shape documented in head_to_head_views),
    plus - indirectly, via write_placement_stats - the event leaderboard UI and the
    afc_rankings pipeline.

stage_format -> fmt mapping (FORMAT_FROM_STAGE below):
  'cs - knockout'            -> single_elim
  'cs - double elimination'  -> double_elim
  'cs - league'              -> league
  'cs - round robin'         -> round_robin_h2h
  'cs - normal'              -> single_elim   (a "normal" CS stage is a straight knockout)
"""
import datetime
from collections import defaultdict

from django.db import transaction

# scoring.py is the single source of truth for placement-point tables; the D bridge reuses
# its normalizer so a synthetic CS placement scores exactly like a manually-entered BR one.
from . import scoring as scoring_lib
from .models import (
    HeadToHeadMatch,
    Leaderboard,
    Match,
    StageGroups,
    TournamentTeamMatchStats,
)

# ── format mapping ───────────────────────────────────────────────────────────────────────────
# The CS stage_format strings already stored on Stages map onto the four bracket engines.
# Callers may also pass an explicit fmt to override (e.g. a BR stage running a tiebreaker
# bracket); the generate endpoint derives fmt from stage_format when none is sent.
FORMAT_FROM_STAGE = {
    "cs - knockout": "single_elim",
    "cs - double elimination": "double_elim",
    "cs - league": "league",
    "cs - round robin": "round_robin_h2h",
    "cs - normal": "single_elim",
}
VALID_FORMATS = ("single_elim", "double_elim", "league", "round_robin_h2h")
# League-family formats share the pairing engine and have no advancement links.
LEAGUE_FORMATS = ("league", "round_robin_h2h")


class BracketError(Exception):
    """Raised for any caller-facing bracket validation failure. The views catch this and
    return its message as a 400, so messages must stay human-readable."""


# ── seeding helpers (pure) ───────────────────────────────────────────────────────────────────
def _bracket_size(n):
    """Smallest power of two >= n (the slot count of the round-1 bracket)."""
    size = 1
    while size < n:
        size *= 2
    return size


def _seed_slots(bracket_size):
    """Standard bracket slot order for `bracket_size` (a power of two).

    Returns the seed number occupying each round-1 slot, in slot order, such that seed 1
    and seed 2 can only meet in the final, 1 and 3 only in the semis, etc. Built by the
    classic doubling expansion: [1] -> [1,2] -> [1,4,2,3] -> [1,8,4,5,2,7,3,6] -> ...
    Adjacent pairs (slots 0+1, 2+3, ...) are the round-1 matches. Because the partner of
    seed s is always (size+1-s), the HIGHEST seed numbers - which don't exist when the
    field is not a power of two - land opposite the LOWEST seeds, which is exactly the
    "higher seeds get the byes" rule.
    """
    order = [1]
    size = 1
    while size < bracket_size:
        size *= 2
        order = [s for seed in order for s in (seed, size + 1 - seed)]
    return order


# ── bye resolution ───────────────────────────────────────────────────────────────────────────
def _set_slot(match, slot, team_id):
    """Write a team into one side of a match ('a' -> team_a, 'b' -> team_b) and save.
    team_id may be None (clears the slot on a re-report that changed the winner)."""
    if slot == "a":
        match.team_a_id = team_id
        match.save(update_fields=["team_a", "updated_at"])
    else:
        match.team_b_id = team_id
        match.save(update_fields=["team_b", "updated_at"])


def _complete_bye(match, winner_id):
    """Auto-complete a match that can never be played: one (or zero) real teams and every
    other slot permanently empty. Convention: score 0-0, winner = the present team (or None
    for a fully vacant match, which only occurs in losers brackets fed by two byes)."""
    match.score_a = 0
    match.score_b = 0
    match.winner_id = winner_id
    match.status = "completed"
    match.save(update_fields=["score_a", "score_b", "winner", "status", "updated_at"])
    # Propagate exactly like a played match: the winner advances; there is no loser to drop.
    if match.next_match_id and winner_id:
        _set_slot(match.next_match, match.next_match_slot, winner_id)


def _resolve_byes(matches):
    """Cascade bye auto-completions across a stage's full match list (in place + saved).

    A slot is PERMANENTLY EMPTY when it holds no team and every match feeding it has
    already completed (a completed feeder that put nothing in the slot produced a None
    winner/loser, i.e. was itself a bye) - or when nothing feeds it at all (an unfilled
    round-1 seed). Whenever a pending match has one real team and a permanently empty
    other slot, it is a bye: complete it and advance the team. Fully-empty matches
    (both slots permanently empty - double-elim losers rounds fed by two byes) complete
    with winner None so the emptiness keeps cascading downstream.

    Called after generation AND after every report_result, because completing a real
    match can reveal a downstream bye (e.g. a losers-bracket slot whose round-robin
    partner never materialized).
    """
    # Who feeds each (match, slot)? Built from the advancement links, so it works for any
    # of the generated shapes without the shapes having to register themselves.
    feeds = defaultdict(list)  # (match_pk, slot) -> [feeder match, ...]
    for m in matches:
        if m.next_match_id:
            feeds[(m.next_match_id, m.next_match_slot)].append(m)
        if m.loser_next_match_id:
            feeds[(m.loser_next_match_id, m.loser_next_match_slot)].append(m)

    def slot_team(m, slot):
        return m.team_a_id if slot == "a" else m.team_b_id

    def permanently_empty(m, slot):
        if slot_team(m, slot):
            return False
        # No feeders -> nothing can ever arrive. Feeders all completed -> whatever they
        # produced is already in the slot; still empty means it stays empty.
        return all(f.status == "completed" for f in feeds.get((m.pk, slot), []))

    changed = True
    while changed:  # cascades: completing one bye can make the next match a bye too
        changed = False
        for m in matches:
            if m.status == "completed":
                continue
            a_id, b_id = m.team_a_id, m.team_b_id
            if a_id and b_id:
                continue  # both teams present: a real, playable match
            if a_id and permanently_empty(m, "b"):
                _complete_bye(m, a_id)
                changed = True
            elif b_id and permanently_empty(m, "a"):
                _complete_bye(m, b_id)
                changed = True
            elif not a_id and not b_id and permanently_empty(m, "a") and permanently_empty(m, "b"):
                _complete_bye(m, None)  # vacant match: completes empty so downstream resolves
                changed = True


# ── generation ───────────────────────────────────────────────────────────────────────────────
def generate_bracket(stage, team_ids_in_seed_order, fmt):
    """Build the full HeadToHeadMatch tree for `stage` and return the created matches.

    team_ids_in_seed_order: TournamentTeam pks, BEST team first (index 0 = seed 1). The
    caller (generate endpoint) validates the ids belong to the stage's event and deletes
    any previous bracket; this function only creates.
    fmt: one of VALID_FORMATS (see FORMAT_FROM_STAGE for the stage_format mapping).
    """
    ids = list(team_ids_in_seed_order)
    if fmt not in VALID_FORMATS:
        raise BracketError(f"Unknown bracket format '{fmt}'.")
    if len(ids) < 2:
        raise BracketError("At least 2 teams are required to generate a bracket.")
    if fmt == "double_elim" and len(ids) < 3:
        raise BracketError("Double elimination needs at least 3 teams.")

    if fmt in LEAGUE_FORMATS:
        return _generate_league(stage, ids)
    if fmt == "double_elim":
        return _generate_double_elim(stage, ids)
    return _generate_single_elim(stage, ids)


def _generate_single_elim(stage, ids):
    """Standard power-of-2 single-elimination knockout.

    For n teams: bracket size P = next power of two, R = log2(P) rounds, all in
    bracket="winners". Round r has P/2^r matches; match p of round r feeds match p//2 of
    round r+1 (slot 'a' when p is even, 'b' when odd). Round-1 teams are placed by
    _seed_slots, so non-existent high seeds become byes opposite the top seeds, then
    _resolve_byes auto-advances them. Matches are created FINAL-FIRST so each next_match
    link can be set at create time (single insert per match)."""
    n = len(ids)
    P = _bracket_size(n)
    R = P.bit_length() - 1  # log2(P)

    matches = {}  # (round, position) -> HeadToHeadMatch
    for r in range(R, 0, -1):  # final first, so next_match exists when round r-1 is created
        for p in range(P >> r):
            nxt = matches.get((r + 1, p // 2))
            matches[(r, p)] = HeadToHeadMatch.objects.create(
                stage=stage, bracket="winners", round_number=r, position=p,
                next_match=nxt, next_match_slot=("a" if p % 2 == 0 else "b") if nxt else None,
            )

    # Round-1 seeding: slot s holds seed slots[s]; seeds beyond n are byes (team None).
    slots = _seed_slots(P)
    for p in range(P // 2):
        seed_a, seed_b = slots[2 * p], slots[2 * p + 1]
        m = matches[(1, p)]
        m.team_a_id = ids[seed_a - 1] if seed_a <= n else None
        m.team_b_id = ids[seed_b - 1] if seed_b <= n else None
        m.save(update_fields=["team_a", "team_b", "updated_at"])

    created = list(matches.values())
    _resolve_byes(created)
    return created


def _generate_double_elim(stage, ids):
    """Winners bracket + losers bracket + a single grand final.

    Structure for bracket size P = 2^R (R >= 2 since we require >= 3 teams):
      WINNERS  rounds 1..R          : P/2^r matches each (same shape as single elim).
      GRAND FINAL                   : bracket="winners", round R+1, position 0
                                      (slot a = WB final winner, slot b = LB final winner).
                                      ONE match only - no bracket reset; the LB winner must
                                      beat the WB winner once. Documented design choice.
      LOSERS rounds 1..2(R-1), for j = 1..R-1 (both rounds of a j-block have P/2^(j+1) matches):
        minor round 2j-1 : j=1 -> pairs of WB round-1 losers (WB R1 match p drops its loser
                           to LB1 match p//2, slot by parity); j>=2 -> pairs of the previous
                           major round's winners (same p//2 + parity rule).
        major round 2j   : slot a = the loser of WB round j+1 match p (1:1 by position),
                           slot b = the winner of minor round 2j-1 match p.
      No cross-bracket seeding rotation is applied in the losers bracket (early rematches
      are possible) - the standard simple construction, kept deliberately minimal.
    """
    n = len(ids)
    P = _bracket_size(n)
    R = P.bit_length() - 1

    # Grand final first (everything ultimately feeds it).
    grand_final = HeadToHeadMatch.objects.create(
        stage=stage, bracket="winners", round_number=R + 1, position=0)

    # Losers bracket, last round first so next links exist at create time.
    lb = {}  # (round, position) -> match
    for k in range(2 * (R - 1), 0, -1):
        j = (k + 1) // 2  # the j-block this round belongs to
        for p in range(P >> (j + 1)):
            if k % 2 == 1:
                # minor round: winner goes to the same block's major round, same position, slot b
                nxt, slot = lb[(k + 1, p)], "b"
            elif j < R - 1:
                # major round (not the LB final): winner pairs up in the next minor round
                nxt, slot = lb[(k + 1, p // 2)], ("a" if p % 2 == 0 else "b")
            else:
                # LB final: winner meets the WB champion in the grand final
                nxt, slot = grand_final, "b"
            lb[(k, p)] = HeadToHeadMatch.objects.create(
                stage=stage, bracket="losers", round_number=k, position=p,
                next_match=nxt, next_match_slot=slot,
            )

    # Winners bracket, final first. Each WB match also carries its loser drop into the LB.
    wb = {}
    for r in range(R, 0, -1):
        for p in range(P >> r):
            nxt = wb.get((r + 1, p // 2)) if r < R else grand_final
            slot = ("a" if p % 2 == 0 else "b") if r < R else "a"
            if r == 1:
                # WB round-1 losers pair up in LB round 1.
                loser_nxt, loser_slot = lb[(1, p // 2)], ("a" if p % 2 == 0 else "b")
            else:
                # WB round r (>=2) losers drop into the major round of block j = r-1, slot a.
                loser_nxt, loser_slot = lb[(2 * (r - 1), p)], "a"
            wb[(r, p)] = HeadToHeadMatch.objects.create(
                stage=stage, bracket="winners", round_number=r, position=p,
                next_match=nxt, next_match_slot=slot,
                loser_next_match=loser_nxt, loser_next_match_slot=loser_slot,
            )

    # Round-1 seeding, identical to single elim.
    slots = _seed_slots(P)
    for p in range(P // 2):
        seed_a, seed_b = slots[2 * p], slots[2 * p + 1]
        m = wb[(1, p)]
        m.team_a_id = ids[seed_a - 1] if seed_a <= n else None
        m.team_b_id = ids[seed_b - 1] if seed_b <= n else None
        m.save(update_fields=["team_a", "team_b", "updated_at"])

    created = [grand_final] + list(lb.values()) + list(wb.values())
    _resolve_byes(created)
    return created


def _generate_league(stage, ids):
    """League / round-robin H2H: every pair plays exactly once, no advancement links.

    Scheduled with the classic CIRCLE METHOD so the matches come out grouped into rounds
    a venue could actually run (each team plays at most once per round): fix the first
    entry, rotate the rest one step per round; with an odd team count a None placeholder
    gives one team a sit-out (no match row) each round. n teams -> n-1 rounds (n even)
    or n rounds (n odd), C(n,2) matches total, all bracket="league"."""
    arr = list(ids)
    if len(arr) % 2 == 1:
        arr.append(None)  # the sit-out marker for odd team counts
    half = len(arr) // 2

    created = []
    for round_number in range(1, len(arr)):
        position = 0
        for i in range(half):
            a, b = arr[i], arr[-1 - i]
            if a is None or b is None:
                continue  # this pairing is the round's sit-out
            created.append(HeadToHeadMatch.objects.create(
                stage=stage, bracket="league", round_number=round_number, position=position,
                team_a_id=a, team_b_id=b,
            ))
            position += 1
        # rotate everything but the first entry one step clockwise
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return created


# ── result reporting ─────────────────────────────────────────────────────────────────────────
def report_result(match, score_a, score_b, acting_user=None):
    """Record a Clash Squad set result (round wins) on `match` and advance the bracket.

    Validates: both teams present, scores are non-negative ints, no ties in elimination
    brackets (winners/losers). League matches may tie (winner stays None; standings count
    it for neither side's wins/losses, but round wins/losses still accrue).

    Re-reporting an already-completed match is ALLOWED (idempotent correction) as long as
    neither downstream match (next_match / loser_next_match) has completed - the new
    winner/loser simply overwrite the slots they previously filled. Once a downstream
    match has completed the correction is refused (the admin must unwind downstream first).

    `acting_user` is accepted for parity with the view layer (it does the permission gate
    and audit); nothing is persisted from it here.

    Returns True when this report COMPLETED the bracket (and the sub-project D bridge,
    write_placement_stats, has been refreshed), else False. Raises BracketError on any
    validation failure - the views surface the message as a 400.
    """
    if match.team_a_id is None or match.team_b_id is None:
        raise BracketError("This match does not have both teams yet (bye or waiting on earlier results).")

    try:
        sa, sb = int(score_a), int(score_b)
    except (TypeError, ValueError):
        raise BracketError("score_a and score_b must be integers (round wins).")
    if sa < 0 or sb < 0:
        raise BracketError("Scores cannot be negative.")

    elimination = match.bracket in ("winners", "losers")
    if elimination and sa == sb:
        raise BracketError("Ties are not allowed in elimination matches: one team must win the set.")

    # Re-report guard: only while nothing downstream has been decided on top of this result.
    if match.status == "completed":
        for downstream in (match.next_match, match.loser_next_match):
            if downstream is not None and downstream.status == "completed":
                raise BracketError(
                    "Cannot change this result: a later match that depends on it is already completed.")

    if sa > sb:
        winner_id, loser_id = match.team_a_id, match.team_b_id
    elif sb > sa:
        winner_id, loser_id = match.team_b_id, match.team_a_id
    else:
        winner_id, loser_id = None, None  # league tie

    with transaction.atomic():
        match.score_a, match.score_b = sa, sb
        match.winner_id = winner_id
        match.status = "completed"
        match.save(update_fields=["score_a", "score_b", "winner", "status", "updated_at"])

        # Advance. On a re-report the slots are deterministic, so writing the (possibly
        # different) new winner/loser simply overwrites the previous propagation.
        if match.next_match_id:
            _set_slot(match.next_match, match.next_match_slot, winner_id)
        if match.loser_next_match_id:
            _set_slot(match.loser_next_match, match.loser_next_match_slot, loser_id)

        # Completing a real match can reveal a downstream bye (a slot that was waiting on
        # us while its partner slot is permanently empty) - cascade those now.
        all_matches = list(HeadToHeadMatch.objects.filter(stage=match.stage)
                           .select_related("next_match", "loser_next_match"))
        _resolve_byes(all_matches)

        # SUB-PROJECT D bridge: the moment the bracket is decided, mirror the placements
        # into TournamentTeamMatchStats so the leaderboard + rankings pipelines see them.
        # Re-running on a corrected final refreshes the same synthetic rows.
        complete = _bracket_complete(all_matches)
        if complete:
            write_placement_stats(match.stage)
    return complete


def _bracket_complete(matches):
    """A league bracket is complete when every match is; an elimination bracket when its
    FINAL (the single winners-bracket match with no next_match: the single-elim final or
    the double-elim grand final) has a decided winner."""
    if not matches:
        return False
    if all(m.bracket == "league" for m in matches):
        return all(m.status == "completed" for m in matches)
    final = next((m for m in matches if m.bracket == "winners" and m.next_match_id is None), None)
    return bool(final and final.status == "completed" and final.winner_id)


# ── standings ────────────────────────────────────────────────────────────────────────────────
def standings(stage):
    """Rank the stage's bracket. Returns a list of
    {tournament_team_id, team_name, placement, wins, losses, rounds_won, rounds_lost}.

    League / round-robin H2H: a table over all completed matches, ranked by match wins,
    then round-win difference, then round wins, then team name (placement = row index + 1;
    ties on all keys order alphabetically). Ties count toward neither wins nor losses.

    Elimination (single/double): placements derive from WHERE each team was knocked out.
    A team's elimination match is the completed match it lost whose loser has nowhere to
    drop (loser_next_match is None) - in single elim that is any loss, in double elim a
    losers-bracket loss or the grand final. Champion is 1st, the final's loser 2nd, and
    teams knocked out in the same round SHARE a placement (semifinal losers share 3rd).
    While the bracket is still running, alive teams carry placement None and sort first.
    Byes never count as wins/losses or rounds.
    """
    matches = list(
        stage.h2h_matches.select_related("team_a__team", "team_b__team")
        .order_by("round_number", "position")
    )
    if not matches:
        return []

    # Collect every real team in the bracket + a display-name map.
    team_names = {}
    for m in matches:
        if m.team_a_id:
            team_names[m.team_a_id] = m.team_a.team.team_name
        if m.team_b_id:
            team_names[m.team_b_id] = m.team_b.team.team_name

    # Per-team W/L + round tallies over completed REAL matches (both teams present).
    tally = {tid: {"wins": 0, "losses": 0, "rounds_won": 0, "rounds_lost": 0}
             for tid in team_names}
    for m in matches:
        if m.status != "completed" or not (m.team_a_id and m.team_b_id):
            continue  # pending, or a bye - byes carry no competitive numbers
        tally[m.team_a_id]["rounds_won"] += m.score_a
        tally[m.team_a_id]["rounds_lost"] += m.score_b
        tally[m.team_b_id]["rounds_won"] += m.score_b
        tally[m.team_b_id]["rounds_lost"] += m.score_a
        if m.winner_id:
            loser_id = m.team_b_id if m.winner_id == m.team_a_id else m.team_a_id
            tally[m.winner_id]["wins"] += 1
            tally[loser_id]["losses"] += 1
        # tie: no win/loss for either side

    def row(tid, placement):
        t = tally[tid]
        return {
            "tournament_team_id": tid,
            "team_name": team_names[tid],
            "placement": placement,
            "wins": t["wins"],
            "losses": t["losses"],
            "rounds_won": t["rounds_won"],
            "rounds_lost": t["rounds_lost"],
        }

    # ── league table ──
    if all(m.bracket == "league" for m in matches):
        ordered = sorted(
            team_names,
            key=lambda tid: (
                -tally[tid]["wins"],
                -(tally[tid]["rounds_won"] - tally[tid]["rounds_lost"]),
                -tally[tid]["rounds_won"],
                team_names[tid],
            ),
        )
        return [row(tid, i + 1) for i, tid in enumerate(ordered)]

    # ── elimination placements ──
    final = next((m for m in matches if m.bracket == "winners" and m.next_match_id is None), None)
    champion_id = final.winner_id if (final and final.status == "completed") else None

    # Each team's elimination point: the lost match with no loser drop. Keyed for ranking:
    # winners-bracket eliminations (single-elim rounds + the grand final) outrank
    # losers-bracket ones, and within a bracket a LATER round means a BETTER finish.
    eliminated = {}  # team_id -> (bracket_rank, -round_number)
    for m in matches:
        if m.status != "completed" or not m.winner_id or not (m.team_a_id and m.team_b_id):
            continue
        if m.loser_next_match_id is not None:
            continue  # double elim winners-bracket loss: the team drops, not out yet
        loser_id = m.team_b_id if m.winner_id == m.team_a_id else m.team_a_id
        eliminated[loser_id] = (0 if m.bracket == "winners" else 1, -m.round_number)

    alive = [tid for tid in team_names if tid != champion_id and tid not in eliminated]

    rows = []
    counter = 1
    if champion_id:
        rows.append(row(champion_id, 1))
        counter = 2
    # Still-alive teams (mid-bracket reads): no placement yet; they occupy the next slots.
    for tid in sorted(alive, key=lambda t: (-tally[t]["wins"], team_names[t])):
        rows.append(row(tid, None))
        counter += 1
    # Knocked-out teams, best finish first; same elimination round shares one placement.
    by_depth = defaultdict(list)
    for tid, key in eliminated.items():
        by_depth[key].append(tid)
    for key in sorted(by_depth):
        group = sorted(by_depth[key], key=lambda t: team_names[t])
        for tid in group:
            rows.append(row(tid, counter))
        counter += len(group)
    return rows


# ── SUB-PROJECT D BRIDGE ─────────────────────────────────────────────────────────────────────
def write_placement_stats(stage):
    """Mirror a finished bracket's placements into the EXISTING results pipeline.

    WHY: the leaderboard reads (get_all_leaderboard_details_for_event, the round_robin
    aggregators) and the afc_rankings aggregation all consume TournamentTeamMatchStats
    rows hanging off a Match in one of the stage's StageGroups - none of them know about
    HeadToHeadMatch. Rather than teach every consumer a second source (sub-project D's
    explicit non-goal), we write ONE synthetic stat row per placed team into ONE synthetic
    Match, and the whole downstream world keeps working unchanged:
      - leaderboard reads aggregate placement_points/kill_points per group -> the bracket
        placements show up as a points table;
      - afc_rankings reads (placement, kills, Match.played_on) -> CS results feed team
        scores exactly like a one-match BR stage.

    CONVENTIONS (so a future reader can spot the synthetic rows):
      - the synthetic Match lives in the stage's FIRST StageGroups row (one is created,
        named "Bracket Results", if the stage has none - CS bracket stages don't otherwise
        need groups) and is flagged with match_number=0 - real matches start at 1, and
        nothing else in the codebase creates match_number 0;
      - each team gets placement = its bracket placement, kills/damage/assists = 0, and
        placement_points from the group's Leaderboard placement table when one is
        configured, else scoring.DEFAULT_PLACEMENT (the same default every manual-entry
        path uses), with total_points = placement_points (no kill component).

    Idempotent + refreshing: re-running (e.g. after a corrected final) updates the same
    rows and removes rows for teams that no longer hold a placement. Returns the number
    of stat rows written. Called automatically by report_result when the bracket
    completes; safe to call again manually.
    """
    placed = [r for r in standings(stage) if r["placement"]]
    if not placed:
        return 0

    # Anchor group: the stage's first lobby, or a dedicated results group for pure
    # bracket stages (StageGroups requires the date/time/count fields, hence the stubs).
    group = stage.groups.order_by("group_id").first()
    if group is None:
        group = StageGroups.objects.create(
            stage=stage,
            group_name="Bracket Results",
            playing_date=stage.start_date,
            playing_time=datetime.time(0, 0),
            teams_qualifying=stage.teams_qualifying_from_stage or 1,
            match_count=0,  # holds only the synthetic match below, no real lobby matches
            match_maps=[],
        )

    # Score placements with the SAME table a manual BR entry on this group would use.
    leaderboard = Leaderboard.objects.filter(stage=stage, group=group).first()
    placement_table = scoring_lib.normalize_placement_points(
        leaderboard.placement_points if leaderboard else None)

    # The synthetic match (match_number=0 convention). played_on = stage end date so the
    # afc_rankings month/quarter bucketing lands the result when the bracket finished.
    synthetic_match, _ = Match.objects.get_or_create(
        group=group,
        match_number=0,
        defaults={
            "leaderboard": leaderboard,
            "match_map": "bermuda",       # required field; meaningless for a CS bracket
            "result_inputted": True,
            "played_on": stage.end_date,
        },
    )

    written_team_ids = []
    for r in placed:
        points = placement_table.get(r["placement"], 0)
        TournamentTeamMatchStats.objects.update_or_create(
            match=synthetic_match,
            tournament_team_id=r["tournament_team_id"],
            defaults={
                "placement": r["placement"],
                "kills": 0,
                "damage": 0,
                "assists": 0,
                "placement_points": points,
                "kill_points": 0,
                "total_points": points,
                "played": True,
            },
        )
        written_team_ids.append(r["tournament_team_id"])

    # Refresh semantics: drop synthetic rows for teams no longer placed (e.g. a regenerated
    # bracket with a different field, or a corrected result chain).
    TournamentTeamMatchStats.objects.filter(match=synthetic_match).exclude(
        tournament_team_id__in=written_team_ids).delete()

    return len(written_team_ids)
