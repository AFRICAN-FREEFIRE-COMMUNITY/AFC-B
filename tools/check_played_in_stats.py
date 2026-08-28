"""Every per-match team-stats payload must expose `played`.

WHY THIS EXISTS (owner report 2026-08-27)
    TournamentTeamMatchStats.played was stored and never SELECTED. Three separate places build the
    per-match stats payload, and all three left it out, so "this team did not play" never reached
    the browser at all. The manual entry screen therefore re-seeded every not-played team as PLAYED
    with no finishing position, and the organizer had to untick them again on every single open
    before the save would be accepted.

    The column had existed the whole time. Only the read was missing, in three copies.

    This is the shape the repo already has a name for: one fact about a domain object, serialised by
    hand in several places, drifting because nothing checks that the copies agree (see
    CLAUDE.md "One contract per domain object" and tools/check_event_contract.py). Prose did not stop
    it happening; a check that fails will.

WHAT IT ASSERTS
    Every queryset over TournamentTeamMatchStats that feeds a per-match `stats` payload names
    `played`, and the hand-built dict in get_all_leaderboard_details_for_event carries it too.

    It is deliberately a TEXT check over the three known sites rather than something clever. The
    failure being prevented is "somebody adds a fourth copy, or removes the key from one", and a
    grep catches exactly that.

RUN
    backend/.venv/Scripts/python.exe tools/check_played_in_stats.py

    Also runs in CI through
    afc_tournament_and_scrims/test_played_round_trip.py::PlayedIsExposedEverywhereTests.
"""
import pathlib
import sys

VIEWS = pathlib.Path(__file__).resolve().parent.parent / "afc_tournament_and_scrims" / "views.py"

# ── choosing the marker, which took two attempts and is the interesting part ──────────────────
# The obvious marker, the joined team name, matched SEVEN blocks: the two real ones, two group
# STANDINGS aggregations (which sum across matches and so have no per-match played, correctly), a
# module-level constant used by those standings queries, and several blocks of commented-out code.
#
# `team_stats_id` is the primary key of ONE TournamentTeamMatchStats row, so it appears only where a
# single match row is being projected. An aggregate cannot carry it. That is what makes it the right
# marker rather than a luckier one: it is true by construction, not by coincidence.
#
# Commented-out lines are skipped for the same reason: this file carries large commented blocks, and
# a checker that fails on dead code teaches people to ignore it.
VALUES_MARKER = '"team_stats_id",'
HAND_BUILT_MARKER = '"placement": team_stat.placement,'
EXPECTED_VALUES_SITES = 2


def main() -> int:
    source = VIEWS.read_text(encoding="utf-8")
    lines = source.splitlines()

    problems = []

    def live(i):
        """True unless the line is commented out. views.py carries large dead blocks."""
        return not lines[i].lstrip().startswith("#")

    # ── the per-match .values() projections ──────────────────────────────────────
    starts = [i for i, line in enumerate(lines) if VALUES_MARKER in line and live(i)]
    if len(starts) != EXPECTED_VALUES_SITES:
        problems.append(
            f"expected {EXPECTED_VALUES_SITES} per-match team-stats .values() blocks, "
            f"found {len(starts)}. "
            "A copy was added or removed; check that the new one selects 'played' too."
        )
    for i in starts:
        block = "\n".join(lines[i : i + 20])
        if '"played"' not in block:
            problems.append(
                f"views.py line {i + 1}: a per-match team-stats .values() does not select 'played'. "
                "Without it the frontend cannot tell that a team did not play."
            )

    # ── the hand-built dict ──────────────────────────────────────────────────────
    hand = [i for i, line in enumerate(lines) if HAND_BUILT_MARKER in line and live(i)]
    if not hand:
        problems.append(
            "the hand-built match_stats dict in get_all_leaderboard_details_for_event was not "
            "found; if it was refactored, update this checker to match."
        )
    for i in hand:
        block = "\n".join(lines[i : i + 6])
        if '"played": team_stat.played' not in block:
            problems.append(
                f"views.py line {i + 1}: the hand-built match_stats dict does not carry 'played'."
            )

    if problems:
        print("FAILED")
        for p in problems:
            print("  -", p)
        return 1

    print(
        f"OK. {len(starts)} per-match team-stats projections and {len(hand)} hand-built dict(s) "
        "all expose 'played'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
