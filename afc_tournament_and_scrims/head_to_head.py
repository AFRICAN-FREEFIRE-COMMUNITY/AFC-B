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

WHAT "A BRACKET" MEANS (changed 2026-08-13, owner backlog item 21)
  A bracket is the matches of one GROUP, not of one stage. A Clash Squad stage can hold
  several StageGroups rows, each with its own bracket_format, and each runs an independent
  bracket with its own standings and its own winner - "Group A - Knockout" beside
  "Group B - League". Every function here takes a group_id and reads through
  bracket_matches(); group_id=None selects the LEGACY stage-wide bracket (rows with a NULL
  group), which is what everything created before that date looks like.

  The mode therefore lives on StageGroups.bracket_format. FORMAT_FROM_STAGE below is the
  legacy fallback for a stage that still carries the mode in its own format string.

stage_format -> fmt mapping (FORMAT_FROM_STAGE below):
  'cs - knockout'            -> single_elim
  'cs - double elimination'  -> double_elim
  'cs - league'              -> league
  'cs - round robin'         -> round_robin_h2h
  'cs - normal'              -> single_elim   (a "normal" CS stage is a straight knockout)
  'cs'                       -> no stage-level mode at all; ask the group
"""
import datetime
from collections import defaultdict

from django.db import transaction

# scoring.py is the single source of truth for placement-point tables; the D bridge reuses
# its normalizer so a synthetic CS placement scores exactly like a manually-entered BR one.
from . import scoring as scoring_lib
from .models import (
    H2HPlayerStat,
    HeadToHeadMatch,
    Leaderboard,
    Match,
    StageGroups,
    TournamentTeamMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMember,
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
# Sanity cap for a reported set score (round wins). Generous enough for any real best-of format,
# small enough to reject fat-finger typos like "400". See report_result (P2, owner 2026-07-13).
# A stage WITH room settings is additionally capped at that room's best-of - see cs_room.
MAX_ROUND_SCORE = 99

# League points (owner 2026-08-12). Football's 3/1/0: a win is worth more than two draws, which
# is what makes a league table rank the way people expect. Kept as a constant rather than a
# per-event setting until somebody actually asks for a different scale - one number in one place
# is easier to change later than a settings screen nobody uses.
LEAGUE_POINTS = {"win": 3, "draw": 1, "loss": 0}


class BracketError(Exception):
    """Raised for any caller-facing bracket validation failure. The views catch this and
    return its message as a 400, so messages must stay human-readable."""


# ── what "a bracket" means (owner backlog item 21, 2026-08-13) ───────────────────────────────
def bracket_matches(stage, group_id=None):
    """The matches of ONE bracket, as a queryset.

    A Clash Squad stage can hold several independent brackets - one per StageGroups row that has
    a bracket_format - so "a bracket" is the matches of one GROUP, not of one stage. Everything
    that used to read `stage.h2h_matches` goes through here instead, which is what keeps Group A's
    byes, standings, completion and placements from bleeding into Group B's.

    group_id=None means the LEGACY shape: one bracket owned by the whole stage, its matches
    carrying group_id NULL. Passing None therefore selects exactly those rows rather than "all of
    them", so a legacy read stays correct even in a stage that has since gained group brackets.
    """
    return HeadToHeadMatch.objects.filter(stage=stage, group_id=group_id)


def resolve_bracket_group_id(stage, group_id=None):
    """Turn "the bracket of this stage" into a concrete group id.

    Most callers - the overlay renderer, the leaderboard bridge, a test - just mean "this stage's
    bracket" and should not have to know groups exist. This is what lets them keep saying that:

      * an explicit group_id always wins;
      * a stage still holding LEGACY matches (group NULL) resolves to None, so nothing that
        predates 2026-08-13 changes behaviour;
      * a stage with exactly ONE bracket group resolves to that group - the simple case, which is
        almost every Clash Squad stage;
      * a stage split into SEVERAL groups resolves to None, because "the bracket of this stage" is
        genuinely ambiguous there and silently picking Group A would be a lie. Those callers pass
        a group id.
    """
    if group_id:
        return group_id
    if HeadToHeadMatch.objects.filter(stage=stage, group__isnull=True).exists():
        return None
    groups = list(bracket_groups(stage)[:2])
    return groups[0].group_id if len(groups) == 1 else None


def bracket_groups(stage):
    """Every group in `stage` that runs a bracket, in display order.

    A Battle Royale lobby has no bracket_format and never appears here. Used by the endpoints to
    answer "what brackets does this stage have?" and by the FE to draw one card per bracket.
    """
    return stage.groups.exclude(bracket_format="").order_by("group_order", "group_id")


def ensure_bracket_group(stage, fmt, third_place=False):
    """The group a bracket belongs to, creating the single default one when the stage has none.

    WHY THIS EXISTS (owner 2026-08-13): most Clash Squad stages are ONE bracket and the organizer
    should never have to think about groups at all - they pick Clash Squad, pick a mode, done.
    Splitting a stage into several groups (the Champions League shape) is an opt-in for the
    organizers who want it.

    Rather than support both "the bracket hangs off the stage" and "the bracket hangs off a group"
    forever, the simple case quietly gets ONE group. So there is a single shape in the database and
    a single code path, while the screen stays as simple as it was. The auto-created group is named
    "Main bracket" and carries the mode; nothing in the UI shows it unless the organizer turns
    grouping on.

    Returns the StageGroups row. Reuses the stage's existing bracket group when there is exactly
    one, so regenerating never piles up groups.
    """
    existing = list(bracket_groups(stage))
    if len(existing) == 1:
        group = existing[0]
        # Keep the mode in step when a one-bracket stage changes format.
        if group.bracket_format != fmt or group.bracket_third_place != bool(third_place):
            group.bracket_format = fmt
            group.bracket_third_place = bool(third_place)
            group.save(update_fields=["bracket_format", "bracket_third_place"])
        return group
    if existing:
        # Several already exist: the caller must say which one it means.
        raise BracketError(
            "This stage is split into groups, so say which group's bracket you are generating.")

    # None yet. Reuse a plain lobby row if the stage happens to have exactly one (a Clash Squad
    # stage that was built through the Battle Royale wizard before item 21), else create ours.
    plain = list(stage.groups.all()[:2])
    group = plain[0] if len(plain) == 1 else StageGroups.objects.create(
        stage=stage,
        group_name="Main bracket",
        playing_date=stage.start_date,
        playing_time=datetime.time(0, 0),
        teams_qualifying=stage.teams_qualifying_from_stage or 1,
        match_count=0,   # Battle Royale lobby fields; a bracket has no use for them
        match_maps=[],
    )
    group.bracket_format = fmt
    group.bracket_third_place = bool(third_place)
    # It holds a real bracket now, so it is not the hidden bookkeeping anchor any more.
    group.is_synthetic = False
    group.save(update_fields=["bracket_format", "bracket_third_place", "is_synthetic"])
    return group


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
def generate_bracket(stage, team_ids_in_seed_order, fmt, third_place=False, group=None):
    """Build the full HeadToHeadMatch tree for `stage` and return the created matches.

    team_ids_in_seed_order: TournamentTeam pks, BEST team first (index 0 = seed 1). The
    caller (generate endpoint) validates the ids belong to the stage's event and deletes
    any previous bracket; this function only creates.
    fmt: one of VALID_FORMATS (see FORMAT_FROM_STAGE for the stage_format mapping).
    third_place: single elimination only - also create the bronze match between the two
    semifinal losers, so 3rd and 4th are decided instead of shared (owner 2026-08-12).
    Ignored for the other formats: double elimination already separates 3rd from 4th by
    construction, and a league table has no such match.
    """
    ids = list(team_ids_in_seed_order)
    if fmt not in VALID_FORMATS:
        raise BracketError(f"Unknown bracket format '{fmt}'.")
    if len(ids) < 2:
        raise BracketError("At least 2 teams are required to generate a bracket.")
    if fmt == "double_elim" and len(ids) < 3:
        raise BracketError("Double elimination needs at least 3 teams.")

    if fmt in LEAGUE_FORMATS:
        return _generate_league(stage, ids, group=group)
    if fmt == "double_elim":
        return _generate_double_elim(stage, ids, group=group)
    return _generate_single_elim(stage, ids, third_place=third_place, group=group)


def _generate_single_elim(stage, ids, third_place=False, group=None):
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
                stage=stage, group=group, bracket="winners", round_number=r, position=p,
                next_match=nxt, next_match_slot=("a" if p % 2 == 0 else "b") if nxt else None,
            )

    # Optional bronze match (owner 2026-08-12). Needs semifinals to exist, i.e. at least two
    # rounds; a 2-team bracket is only a final and has no 3rd place to play for. The two
    # semifinals (round R-1, positions 0 and 1) drop their LOSERS into it, which is exactly the
    # same wiring double elimination already uses, so _resolve_byes and report_result need no
    # special case: a bye semifinal simply leaves its slot permanently empty and the bronze match
    # resolves as a bye too.
    if third_place and R >= 2:
        third = HeadToHeadMatch.objects.create(
            stage=stage, group=group, bracket="third", round_number=R, position=0)
        for p, slot in ((0, "a"), (1, "b")):
            semi = matches[(R - 1, p)]
            semi.loser_next_match = third
            semi.loser_next_match_slot = slot
            semi.save(update_fields=["loser_next_match", "loser_next_match_slot", "updated_at"])
        matches[("third", 0)] = third

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


def _generate_double_elim(stage, ids, group=None):
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
        stage=stage, group=group, bracket="winners", round_number=R + 1, position=0)

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
                stage=stage, group=group, bracket="losers", round_number=k, position=p,
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
                stage=stage, group=group, bracket="winners", round_number=r, position=p,
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


def _generate_league(stage, ids, group=None):
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
                stage=stage, group=group, bracket="league", round_number=round_number, position=position,
                team_a_id=a, team_b_id=b,
            ))
            position += 1
        # rotate everything but the first entry one step clockwise
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return created


# ── result reporting ─────────────────────────────────────────────────────────────────────────
def write_player_stats(match, player_rows):
    """Replace the per-player lines for ONE Clash Squad set (owner 2026-08-12).

    player_rows: [{"player_id": int, "kills": int, "damage": int, "assists": int,
                   "played": bool, "tournament_team_id": int}, ...]

    Every player must be on the roster of one of the two teams in this match, and the
    tournament_team_id they are filed under must be one of those two teams - otherwise a typo
    could park a line on a team that is not even in the set. Replaces wholesale (delete then
    create) so a correction cannot leave a stale line behind; passing an empty list clears them.

    These rows are the per-SET grain. head_to_head.write_placement_stats sums them per player
    into the single synthetic TournamentPlayerMatchStats row for the stage, which is what the
    player profile, kill tables and afc_rankings actually read. Raises BracketError on any
    validation failure; the view surfaces the message as a 400.
    """
    if player_rows is None:
        return 0
    if not isinstance(player_rows, list):
        raise BracketError("player_stats must be a list of player lines.")

    side_ids = {tid for tid in (match.team_a_id, match.team_b_id) if tid}
    if not side_ids:
        raise BracketError("This match has no teams yet, so player stats cannot be recorded.")

    # Roster per side, from the SAME frozen per-event roster BR entry uses.
    rosters = {
        tid: set(
            TournamentTeamMember.objects
            .filter(tournament_team_id=tid, status__in=("active", "approved"))
            .values_list("user_id", flat=True)
        )
        for tid in side_ids
    }

    cleaned = []
    seen = set()
    for raw in player_rows:
        if not isinstance(raw, dict):
            raise BracketError("Each player line must be an object.")
        try:
            player_id = int(raw.get("player_id"))
            team_id = int(raw.get("tournament_team_id"))
            kills = int(raw.get("kills") or 0)
            damage = int(raw.get("damage") or 0)
            assists = int(raw.get("assists") or 0)
        except (TypeError, ValueError):
            raise BracketError("Player lines need whole numbers for player, team, kills, "
                               "damage and assists.")
        if min(kills, damage, assists) < 0:
            raise BracketError("Kills, damage and assists cannot be negative.")
        if team_id not in side_ids:
            raise BracketError("A player line was filed under a team that is not in this match.")
        if player_id not in rosters[team_id]:
            raise BracketError("A player line was filed for someone who is not on that team's "
                               "roster for this event.")
        if player_id in seen:
            raise BracketError("The same player appears twice in this set.")
        seen.add(player_id)
        cleaned.append(H2HPlayerStat(
            h2h_match=match, tournament_team_id=team_id, player_id=player_id,
            kills=kills, damage=damage, assists=assists,
            played=bool(raw.get("played", True)),
        ))

    with transaction.atomic():
        H2HPlayerStat.objects.filter(h2h_match=match).delete()
        if cleaned:
            H2HPlayerStat.objects.bulk_create(cleaned)
    return len(cleaned)


def award_walkover(match, winner_team_id, result_type="walkover", note="", acting_user=None):
    """Decide a set that was never played: a walkover, a forfeit or a disqualification.

    WHY (owner 2026-08-12): until now the only way to record "the other team never showed" was to
    type a scoreline that never happened, which then fed the round-difference tiebreak and the
    player stats as if a real set had been played. This records the WINNER and marks HOW, leaving
    the scoreline at the minimum the format needs.

    winner_team_id must be one of the two teams in the match. The score is set to the room's
    best-of target (or 1-0 when no room is configured), because a bracket has to advance somebody
    and a league table has to see a win; the result_type is what tells every reader it was not
    played. Advancement, bye cascade and the placement bridge all run exactly as for a real set,
    by delegating to report_result.
    """
    if result_type not in dict(HeadToHeadMatch.RESULT_TYPE_CHOICES) or result_type == "normal":
        raise BracketError("A walkover must be recorded as a forfeit, walkover or disqualification.")
    if match.team_a_id is None or match.team_b_id is None:
        raise BracketError("This match does not have both teams yet.")
    if winner_team_id not in (match.team_a_id, match.team_b_id):
        raise BracketError("The winning team must be one of the two teams in this match.")

    from . import cs_room
    target = cs_room.max_wins_for_match(match) or 1
    if winner_team_id == match.team_a_id:
        sa, sb = target, 0
    else:
        sa, sb = 0, target

    # report_result clears result_type on any ordinary re-report (a real scoreline typed over a
    # forfeit means the set WAS played). This flag tells it not to, since we are the caller that
    # set it deliberately. Transient attribute, never a column.
    match._awarding_walkover = True
    match.result_type = result_type
    match.result_note = (note or "")[:255]
    match.save(update_fields=["result_type", "result_note", "updated_at"])
    return report_result(match, sa, sb, acting_user=acting_user)


def report_result(match, score_a, score_b, acting_user=None, player_stats=None):
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
    # Sanity upper bound (P2, owner 2026-07-13): a CS set is a small number of round wins. Without a
    # cap a fat-finger "40-2" is accepted (winner is still right, but it skews a league's round-diff
    # tiebreak). 99 is generous enough for any real best-of format while catching gross typos.
    if sa > MAX_ROUND_SCORE or sb > MAX_ROUND_SCORE:
        raise BracketError(f"Scores look too large: max {MAX_ROUND_SCORE} round wins per team.")

    # ── best-of, from the room settings (owner 2026-08-12) ──────────────────────────────────
    # Until room settings existed there was no way to say how long a set is, so the only guard
    # was the flat cap above. A room configured for 13 rounds is first-to-7: 9-2 is impossible,
    # and so is 7-7. cs_room resolves match -> stage -> event, and returns None when the event
    # has no room settings at all - in which case nothing below applies and events that predate
    # this feature keep behaving exactly as they did.
    from . import cs_room  # local import: cs_room reads models only, but keep the module graph flat
    best_of_wins = cs_room.max_wins_for_match(match)
    if best_of_wins:
        if max(sa, sb) > best_of_wins:
            raise BracketError(
                f"This room is set to first-to-{best_of_wins}, so a team cannot win more than "
                f"{best_of_wins} rounds. Change the room settings if the set was longer.")
        if sa == best_of_wins and sb == best_of_wins:
            raise BracketError(
                f"Both teams cannot reach {best_of_wins} round wins: the set ends when the first "
                f"one does.")

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
        # A correction that types a real scoreline over a forfeit means the set WAS played, so the
        # marker has to clear itself. award_walkover sets result_type after calling nothing else,
        # then delegates here, so it re-stamps its own value afterwards - see that function.
        if match.result_type != "normal" and not getattr(match, "_awarding_walkover", False):
            match.result_type = "normal"
            match.result_note = ""
            match.save(update_fields=["result_type", "result_note", "updated_at"])

        # Optional per-player lines for this set (owner 2026-08-12). Written inside the same
        # transaction as the score, so a rejected player line rolls the score back too rather
        # than leaving a result recorded with stats that were refused.
        if player_stats is not None:
            write_player_stats(match, player_stats)

        # Advance. On a re-report the slots are deterministic, so writing the (possibly
        # different) new winner/loser simply overwrites the previous propagation.
        if match.next_match_id:
            _set_slot(match.next_match, match.next_match_slot, winner_id)
        if match.loser_next_match_id:
            _set_slot(match.loser_next_match, match.loser_next_match_slot, loser_id)

        # Completing a real match can reveal a downstream bye (a slot that was waiting on
        # us while its partner slot is permanently empty) - cascade those now.
        # Scoped to THIS match's bracket (owner 2026-08-13): a Clash Squad stage can hold
        # several independent brackets, one per group, and a bye in Group A must not be resolved
        # against Group B's tree - nor may finishing Group A's final report the whole stage
        # complete while Group B is still being played.
        all_matches = list(bracket_matches(match.stage, match.group_id)
                           .select_related("next_match", "loser_next_match"))
        _resolve_byes(all_matches)

        # SUB-PROJECT D bridge: the moment the bracket is decided, mirror the placements
        # into TournamentTeamMatchStats so the leaderboard + rankings pipelines see them.
        # Re-running on a corrected final refreshes the same synthetic rows.
        complete = _bracket_complete(all_matches)
        if complete:
            write_placement_stats(match.stage, match.group_id)
    return complete


def _bracket_complete(matches):
    """A league bracket is complete when every match is; an elimination bracket when its
    FINAL (the single winners-bracket match with no next_match: the single-elim final or
    the double-elim grand final) has a decided winner - AND, when the stage has an optional
    third-place match, once that is decided too, so 3rd/4th are settled before we call the
    bracket done and write placements downstream."""
    if not matches:
        return False
    if all(m.bracket == "league" for m in matches):
        return all(m.status == "completed" for m in matches)
    final = next((m for m in matches if m.bracket == "winners" and m.next_match_id is None), None)
    if not (final and final.status == "completed" and final.winner_id):
        return False
    third = next((m for m in matches if m.bracket == "third"), None)
    if third is not None and third.status != "completed":
        return False
    return True


# ── standings ────────────────────────────────────────────────────────────────────────────────
def standings(stage, group_id=None):
    """Rank ONE bracket. Returns a list of
    {tournament_team_id, team_name, placement, wins, draws, losses, rounds_won, rounds_lost,
    points}.

    group_id names which bracket (owner 2026-08-13): a Clash Squad stage can hold several, and
    each one stands alone - its own table, its own winner, its own qualifiers - exactly like a
    Battle Royale group. None = the legacy stage-wide bracket.

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
    # "the standings of this stage" still works for the one-bracket case - see
    # resolve_bracket_group_id for exactly when that is and is not honest.
    group_id = resolve_bracket_group_id(stage, group_id)
    matches = list(
        bracket_matches(stage, group_id).select_related("team_a__team", "team_b__team")
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

    # Per-team W/D/L + round tallies over completed REAL matches (both teams present).
    # DRAWS are counted separately (owner 2026-08-12): a league match may legitimately tie, and
    # until now a drawn set showed up nowhere - a team that played 5 with one draw read "2-2" and
    # looked like it had played 4. Elimination brackets cannot tie, so their draw column is
    # always 0 and the FE hides it.
    tally = {tid: {"wins": 0, "draws": 0, "losses": 0, "rounds_won": 0, "rounds_lost": 0}
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
        else:
            tally[m.team_a_id]["draws"] += 1
            tally[m.team_b_id]["draws"] += 1

    def points(tid):
        """League points for one team: 3 a win, 1 a draw, 0 a loss (LEAGUE_POINTS)."""
        t = tally[tid]
        return t["wins"] * LEAGUE_POINTS["win"] + t["draws"] * LEAGUE_POINTS["draw"]

    def row(tid, placement):
        t = tally[tid]
        return {
            "tournament_team_id": tid,
            "team_name": team_names[tid],
            "placement": placement,
            "wins": t["wins"],
            "draws": t["draws"],
            "losses": t["losses"],
            "rounds_won": t["rounds_won"],
            "rounds_lost": t["rounds_lost"],
            # Only meaningful in a league table; carried on every row so the FE reads one shape.
            "points": points(tid),
        }

    # ── league table ──
    # Ranked on POINTS first (owner 2026-08-12: a league needs a points column, and ranking on
    # raw wins made a team with three draws finish below one that had lost three). Round-win
    # difference then round wins break the tie, exactly as before, and the team name last so the
    # order is stable rather than arbitrary.
    if all(m.bracket == "league" for m in matches):
        ordered = sorted(
            team_names,
            key=lambda tid: (
                -points(tid),
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
    # Key shape: (bracket_rank, -round_number, sub). `sub` exists only for the third-place match,
    # whose two teams must NOT share a placement: its winner is 3rd and its loser 4th, and both
    # sit between the final's loser (2nd) and the quarterfinal losers. Because the bronze match
    # carries round_number R, we file it under the semifinal round R-1 so it sorts in the right
    # gap: (0,-R,0) final loser < (0,-(R-1),0) 3rd < (0,-(R-1),1) 4th < (0,-(R-2),0) QF losers.
    # Semifinal losers themselves are NOT eliminated at the semifinal when a bronze match exists -
    # they have a loser_next_match, so the `continue` below already skips them, and they stay
    # "alive" until the bronze match is played. Everything else keeps sub = 0.
    eliminated = {}  # team_id -> (bracket_rank, -round_number, sub)
    for m in matches:
        if m.status != "completed" or not m.winner_id or not (m.team_a_id and m.team_b_id):
            continue
        if m.loser_next_match_id is not None:
            continue  # double elim winners-bracket loss (or a semifinal feeding the bronze match)
        loser_id = m.team_b_id if m.winner_id == m.team_a_id else m.team_a_id
        if m.bracket == "third":
            eliminated[m.winner_id] = (0, -(m.round_number - 1), 0)  # 3rd
            eliminated[loser_id] = (0, -(m.round_number - 1), 1)     # 4th
            continue
        eliminated[loser_id] = (0 if m.bracket == "winners" else 1, -m.round_number, 0)

    # The two teams waiting on an UNPLAYED bronze match are a special kind of "alive": they can
    # only finish 3rd or 4th, so they must not be listed above the final's loser. Without this,
    # a table read between the final and the bronze match showed the beaten finalist as #4,
    # because the two alive teams consumed slots 2 and 3 first (owner 2026-08-12, seen live).
    pending_bronze = next(
        (m for m in matches if m.bracket == "third" and m.status != "completed"), None)
    bronze_waiting = set()
    if pending_bronze is not None:
        bronze_waiting = {tid for tid in (pending_bronze.team_a_id, pending_bronze.team_b_id) if tid}

    alive = [
        tid for tid in team_names
        if tid != champion_id and tid not in eliminated and tid not in bronze_waiting
    ]

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
    # Finally the pair still waiting on the bronze match: no placement yet, but listed BELOW the
    # decided ones because 3rd/4th is the best either can do.
    for tid in sorted(bronze_waiting, key=lambda t: (-tally[t]["wins"], team_names[t])):
        rows.append(row(tid, None))
    return rows


# ── SUB-PROJECT D BRIDGE ─────────────────────────────────────────────────────────────────────
def write_placement_stats(stage, group_id=None):
    """Mirror a finished bracket's placements into the EXISTING results pipeline.

    group_id names WHICH bracket finished (owner 2026-08-13). When a Clash Squad stage runs
    several group brackets, each writes its placements into ITS OWN group - which is strictly
    more correct than the synthetic anchor below, because the leaderboard has always read
    per-group. Group A's winner is 1st in Group A; nothing is merged across groups.

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
    group_id = resolve_bracket_group_id(stage, group_id)
    placed = [r for r in standings(stage, group_id) if r["placement"]]
    if not placed:
        return 0

    # ── per-player totals for the whole stage (owner 2026-08-12) ────────────────────────────
    # H2HPlayerStat is per SET. Everything downstream (player profiles, kill tables,
    # afc_rankings) reads ONE TournamentPlayerMatchStats row per player per match, and a CS stage
    # has exactly one synthetic match, so sum each player's sets here. A player with no lines at
    # all still gets a row below (kills 0), which is what keeps participation credit working for
    # organizers who only enter set scores.
    player_totals = {}   # user_id -> {"kills": .., "damage": .., "assists": .., "played": bool}
    team_kills = defaultdict(int)  # tournament_team_id -> kills, for the team stat row
    for ps in H2HPlayerStat.objects.filter(h2h_match__in=bracket_matches(stage, group_id)):
        acc = player_totals.setdefault(
            ps.player_id, {"kills": 0, "damage": 0, "assists": 0, "played": False})
        acc["kills"] += ps.kills
        acc["damage"] += ps.damage
        acc["assists"] += ps.assists
        acc["played"] = acc["played"] or ps.played
        team_kills[ps.tournament_team_id] += ps.kills

    # Anchor group: the group whose bracket this IS, when the caller named one - the normal case
    # since 2026-08-13, and the reason no synthetic row is needed for it. Otherwise the legacy
    # path: the stage's first lobby, or a dedicated results group for a pure bracket stage
    # (StageGroups requires the date/time/count fields, hence the stubs).
    group = None
    if group_id:
        group = StageGroups.objects.filter(stage=stage, group_id=group_id).first()
    if group is None:
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
            # Bookkeeping only: nobody plays in this "group". Flagged so the admin Stages tab and
            # every other group surface hides it instead of drawing a lobby card with "Add Teams
            # to Group" on a bracket stage (owner 2026-08-12, finding #12).
            is_synthetic=True,
        )
    elif group.group_name == "Bracket Results" and not group.is_synthetic:
        # Rows created before the flag existed: mark them on the next write so old CS events stop
        # showing the phantom card too, rather than needing a data migration.
        group.is_synthetic = True
        group.save(update_fields=["is_synthetic"])

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
            # match_map is a required CharField; a CS bracket has no BR map. "clash_squad" is not one
            # of the BR map choices (choices are not DB-enforced) so it reads as "clash_squad" rather
            # than mislabelling the set "Bermuda" (P2, owner 2026-07-13). The synthetic match is not
            # shown in the BR editor for a CS stage (guarded out in P1#3); this is the placement anchor.
            "match_map": "clash_squad",
            "result_inputted": True,
            "played_on": stage.end_date,
        },
    )

    written_team_ids = []
    for r in placed:
        points = placement_table.get(r["placement"], 0)
        team_stat, _ = TournamentTeamMatchStats.objects.update_or_create(
            match=synthetic_match,
            tournament_team_id=r["tournament_team_id"],
            defaults={
                "placement": r["placement"],
                # Kills entered per set (H2HPlayerStat) roll up here so a CS team's kill count is
                # real rather than always zero. kill_points STAYS 0 on purpose: a Clash Squad stage
                # is scored on where you finish in the bracket, so kills are a statistic, not a
                # second source of points - adding them would silently change how CS standings
                # rank. total_points therefore remains the placement points alone.
                "kills": team_kills.get(r["tournament_team_id"], 0),
                "damage": 0,
                "assists": 0,
                "placement_points": points,
                "kill_points": 0,
                "total_points": points,
                "played": True,
            },
        )
        written_team_ids.append(r["tournament_team_id"])

        # ── PLAYER-RANKING BRIDGE (owner 2026-07-13: "cs should be both team and player ranking") ──
        # A CS bracket has no per-player kill data (results are team round-wins), but players still
        # earn ranking credit through PARTICIPATION + team-win + finals: afc_rankings._collect_player
        # scores a player on kills + participated + team_won + finals_appearances + mvp, and forces
        # personal_placement_pts=0 (players never score on raw placement). So we write one PLAYED
        # TournamentPlayerMatchStats (kills 0) per ROSTERED member of each placed team, hung off that
        # team's synthetic stat - exactly the row shape a BR match writes - so every CS player counts
        # as having played the event (participation), and the champion's roster gets the team-win
        # bonus. Idempotent: sync to the CURRENT roster, dropping stats for members no longer rostered
        # (a regenerated bracket / roster edit), and a team dropped from `placed` has its team_stat
        # deleted below, which CASCADE-deletes its player rows.
        # The frozen per-event role comes back with the roster in the SAME query, so stamping it
        # costs nothing extra. Without this, Clash Squad and bracket play wrote player rows with
        # role_at_match NULL: the play still scored, but afc_rankings.aggregation skips unstamped
        # rows, so a player whose counted period was all CS got role = NULL and vanished from
        # every role table. Worse, this path REWRITES its rows on each bracket regeneration, so
        # backfill_player_roles would fix the history and then lose it again.
        roster_rows = list(
            TournamentTeamMember.objects
            .filter(tournament_team_id=r["tournament_team_id"], status__in=("active", "approved"))
            .values_list("user_id", "in_game_role")
        )
        roster_user_ids = [uid for uid, _role in roster_rows]
        for uid, role in roster_rows:
            # Real per-set numbers when the organizer entered them, zeroes when they only entered
            # set scores (which still earns participation credit, as before).
            totals = player_totals.get(uid) or {"kills": 0, "damage": 0, "assists": 0}
            TournamentPlayerMatchStats.objects.update_or_create(
                team_stats=team_stat, player_id=uid,
                defaults={
                    "kills": totals["kills"],
                    "damage": totals["damage"],
                    "assists": totals["assists"],
                    "played": True,
                    "role_at_match": role or None,
                },
            )
        TournamentPlayerMatchStats.objects.filter(team_stats=team_stat).exclude(
            player_id__in=roster_user_ids).delete()

    # Refresh semantics: drop synthetic rows for teams no longer placed (e.g. a regenerated
    # bracket with a different field, or a corrected result chain). CASCADE removes their player rows.
    TournamentTeamMatchStats.objects.filter(match=synthetic_match).exclude(
        tournament_team_id__in=written_team_ids).delete()

    return len(written_team_ids)
