"""
Public read API for the PER-ROLE player ladders ("sniper rankings, rusher rankings...").

WHAT THIS IS, AND WHAT IT IS DELIBERATELY NOT
    A role ladder is a FILTER over the existing player ladder. It is not a second scoring system.
    Every player keeps the exact score ``afc_rankings.recalc`` already wrote for the period; a role
    table shows the subset of players who played that role, renumbered 1..N so the numbering means
    something inside the table.

    That choice is the whole design, so it is worth saying why, plainly. A role-specific SCORE would
    need role-specific inputs, and the match pipeline records exactly ONE per-player statistic:
    kills. ``TournamentPlayerMatchStats`` also carries damage, assists, deaths, knockdowns,
    headshots, revives_received and survival_seconds, but every production write path leaves them
    zero: the .log upload and the legacy image upload hardcode damage/assists to 0, and the rich
    fields are filled only by the 3D-room debugger-log ingest (``debugger_ingest.py``), which has
    never run against real data. ``SoloPlayerMatchStats`` records placement and kills and nothing
    else. So there is no sniper statistic, no rusher statistic, no support statistic to weight.
    Inventing weights per role would produce numbers no result could justify, and two players with
    identical performances would score differently for a reason nobody could point at. A filter is
    honest: the sniper ladder says "of the players who played sniper, here is the order".

    What a role table CAN report beyond the shared score is what the player really did IN THAT ROLE:
    ``role_matches`` and ``role_kills``, both scoped to the role and both derived from stamped
    facts. That is the honest ceiling until the pipeline captures more.

WHERE THE ROLE COMES FROM: STORED, NOT RE-DERIVED
    ``PlayerMonthlyScore.role`` / ``PlayerQuarterlyScore.role``, written by ``recalc`` from the roles
    stamped on the matches that produced the score. The chain, end to end:

        afc_team.TeamMembers.in_game_role            the LIVE club role, changes over time
          -> TournamentTeamMember.in_game_role       FROZEN when the player is put on an event roster
          -> TournamentPlayerMatchStats.role_at_match  stamped when a result is recorded
          -> PlayerMonthlyScore.role / role_breakdown  aggregated per period by recalc
          -> this endpoint                            filters and counts on the stored value

    This module used to join ``TeamMembers`` directly, which meant a role table described the
    present rather than the period it claimed to describe: a player who was a sniper in July and is
    a rusher today appeared in July's RUSHER table. Reading the stored value fixes that, and because
    the stamp is copied from the FROZEN per-event roster rather than the live one, re-uploading or
    editing an old result reproduces the old role instead of rewriting it.

    A player can play several roles in one period. The period is filed under the role they played
    MOST of it in (``aggregation.primary_role``), so each player appears in exactly one role table
    and the tables stay a partition of the ladder with nobody counted twice. The full split lives in
    ``role_breakdown``, which is what ``role_is_mixed`` and the role-scoped columns are read from, so
    a mixed-role player is disclosed rather than flattened.

WHO HAS NO ROLE, AND WHY THAT IS NOT A BUG
    ``role`` is NULL, and the player is absent from every role table while still present on the
    unfiltered ladder, when the period holds no role-stamped match. That is the truth for:
      * staff - coach, manager and analyst hold no ``in_game_role`` at all;
      * players on no roster;
      * GHOST players - an unclaimed historical name has no roster row, so it can have no role;
      * a period spent entirely on SOLO / standalone leaderboards, where no squad role applies;
      * anything played before the stamping existed (see the ``backfill_player_roles`` command,
        which deliberately leaves finished periods empty rather than stamping today's role onto
        them).
    The last case is why the response carries ``role_coverage``: a period with no stored role data
    must SAY so instead of serving a filtered table that looks authoritative.

PUBLISH GATING IS THE SAME AS THE MAIN LADDER
    This endpoint reuses ``views._resolve_month`` / ``_season_of_month`` /
    ``_resolve_quarterly_season`` / ``_period_meta`` rather than re-deriving them, so a role table
    can never leak an unpublished period the main ladder is still hiding. Importing the private
    helpers from ``views`` is the established idiom here (admin_publish.py and admin_evaluation.py
    do the same).

AUTH
    None, deliberately. This is a PUBLIC read and it mirrors ``afc_rankings.views``, whose ladders
    are open to everyone. The Bearer + ``validate_token`` preamble in this codebase belongs to the
    admin write surfaces (see ``admin_scoring_config.py``); adding it here would put the public
    rankings page behind a login.

ROUTES (mounted by urls.py under the ``rankings/`` prefix)
    GET players/by-role/  -> players_by_role   (public)

Consumed by: the public rankings page's player ladder role tabs
(frontend app/(user)/rankings/page.tsx, via ``rankingsApi.playersByRole`` in lib/rankings.ts).
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import serializers as S
from . import views as V
from .models import PLAYER_ROLE_CHOICES, PlayerMonthlyScore, PlayerQuarterlyScore

# The role catalog. It lives on the score models (PLAYER_ROLE_CHOICES, itself kept in lockstep with
# afc_team.TeamMembers.IN_GAME_ROLE_CHOICES by a test) so a new choice shows up here without a second
# edit. Order is the model's order, which is the order the tabs render in.
ROLE_CHOICES = tuple(PLAYER_ROLE_CHOICES)
ROLE_KEYS = tuple(key for key, _label in ROLE_CHOICES)

# The sentinel the tab bar sends for "everybody", so the frontend never has to special-case a
# missing parameter. Anything else unrecognised is treated the same way (no filter) rather than
# 400ing: a stale bookmark should show the ladder, not an error.
ROLE_ALL = "all"

MONTHLY, QUARTERLY = "monthly", "quarterly"


def _role_counts(qs):
    """``{role: how many players in ``qs`` play it}`` - the numbers on the role tabs.

    ``qs`` is the UNFILTERED ladder queryset for the period, so a count is "how many scored players
    are FILED under this role for this period", not "how many players hold this role today". A role
    with 40 rostered players but nobody who played it this month correctly reads 0, and the tab says
    so instead of opening onto an empty table.

    One query, off the stored column. Rows with no role (NULL) simply fall out, which is why the
    counts never sum to the ladder size and why ``_role_coverage`` reports the gap separately.
    """
    counts = {key: 0 for key in ROLE_KEYS}
    for role in qs.exclude(role=None).values_list("role", flat=True):
        if role in counts:
            counts[role] += 1
    return counts


def _role_coverage(qs, counts):
    """How much of this period actually HAS a stored role, so the UI can be honest about it.

    Returns ``{"players_with_role", "players_scored", "has_role_data"}``. The frontend shows a
    notice when ``has_role_data`` is false and the period simply predates the role stamping: without
    it, a month recorded before the feature shipped would render four empty role tabs that look like
    "nobody played these roles" rather than "we did not record this back then".

    ``players_with_role`` is summed from the tab counts rather than re-queried, so the notice and
    the tabs can never disagree.
    """
    with_role = sum(counts.values())
    return {
        "players_with_role": with_role,
        "players_scored": qs.count(),
        "has_role_data": with_role > 0,
    }


def _catalog(counts):
    """The role tab bar as data: key, the model's English label, and the count.

    The label is shipped so a client that has no translation for a NEW role still renders something
    readable; the frontend translates the known four and falls back to this.
    """
    return [
        {"role": key, "label": str(label), "player_count": counts.get(key, 0)}
        for key, label in ROLE_CHOICES
    ]


def _rerank_within_role(rows, offset):
    """Renumber a page of role-filtered rows 1..N WITHIN the role.

    The rows arrive in global rank order, and the global rank is ordinal with no ties
    (``recalc.rerank_player_month`` / ``rerank_player_quarter`` number 1..N straight down the sorted
    ladder), so position in the filtered list IS the within-role rank. ``offset`` carries the
    pagination offset so page 2 continues 26, 27, ... rather than restarting at 1.

    ``rank`` is OVERWRITTEN rather than supplemented on purpose: a role table that showed the global
    rank would number its rows 3, 17, 24 and read as a broken list. The global number is not lost -
    it moves to ``overall_rank``, so a row can still say "12th overall".
    """
    for index, row in enumerate(rows, start=offset + 1):
        row["overall_rank"] = row["rank"]
        row["rank"] = index
    return rows


def _unranked(rows):
    """The unfiltered ladder, with ``overall_rank`` mirrored on so both shapes match.

    Keeping the key present in every response means the client renders one row component instead of
    branching on whether a role is selected.
    """
    for row in rows:
        row["overall_rank"] = row["rank"]
    return rows


@api_view(["GET"])
def players_by_role(request):
    """The player ladder for one in-game role, or the whole ladder, plus the role tab counts.

    Purpose:  drive the per-role player tables on the public rankings page ("sniper rankings,
              rusher rankings"). One call returns the table, the tab bar and the coverage notice, so
              selecting a role is a single request and the three can never disagree.
    Auth:     none. Public read, same as every ladder in ``views.py`` (see the module docstring).
    Request (query parameters, all optional)::

        role      "rusher" | "support" | "grenader" | "sniper" | "all"   (default "all")
        period    "monthly" | "quarterly"                               (default "monthly")
        month     "YYYY-MM"    monthly only; same resolution as players/monthly/
        season_id int          quarterly only; same resolution as players/quarterly/
        limit     1..100 (default 25), offset - the standard pagination pair

    Response 200::

        {
          "role": "sniper" | null,          # null = the unfiltered ladder
          "period": "monthly",
          "roles": [                        # the tab bar, ALWAYS every role, even at 0
            {"role": "rusher", "label": "Rusher", "player_count": 12}, ...
          ],
          "role_coverage": {                # how much of the period has a STORED role
            "players_with_role": 37, "players_scored": 44, "has_role_data": true
          },
          "results": [ ...player rows... ], # player_monthly / player_quarterly shape, PLUS:
                                            #   rank          = rank WITHIN the role (1..N)
                                            #   overall_rank  = the rank on the full ladder
                                            #   role          = the period's stored role, or null
                                            #   role_matches  = matches played IN that role
                                            #   role_kills    = kills IN that role
                                            #   role_is_mixed = played 2+ roles this period
          "pagination": {"limit","offset","total_count","has_more","next_offset"},
          "month": "2026-07-01",            # monthly only
          "season": { ...season... },
          "published": true,                # false = the period is gated, results is empty
          "is_current_period": true,
          "current_season": { ...season... }
        }

    An unknown ``role`` is treated as "all" rather than refused, so a stale link degrades to the
    full ladder instead of an error page.

    RANK SEMANTICS, the one thing a caller must not get wrong: inside a role table ``rank`` is the
    rank AMONG PLAYERS OF THAT ROLE. The player ranked 1 as a sniper may be 24th overall;
    ``overall_rank`` carries that number so both can be shown.

    ROLE SEMANTICS, the second thing: the role is the one the player held DURING the period, read
    from the stored column, not the role they hold today. ``role_coverage.has_role_data`` false
    means the period predates the stamping and the caller must say so rather than present four empty
    role tabs as fact.

    Consumed by: app/(user)/rankings/page.tsx (RankingsView, the player role tabs) through
    ``rankingsApi.playersByRole`` in lib/rankings.ts.
    """
    role = (request.GET.get("role") or ROLE_ALL).strip().lower()
    if role not in ROLE_KEYS:
        role = None                                   # "all", blank, or anything unrecognised
    period = (request.GET.get("period") or MONTHLY).strip().lower()
    if period != QUARTERLY:
        period = MONTHLY

    # ── resolve the period and its publish gate exactly as the main ladder does ──
    if period == MONTHLY:
        month = V._resolve_month(request, PlayerMonthlyScore)
        season = V._season_of_month(request, month)
        base = (PlayerMonthlyScore.objects.filter(month=month)
                .select_related("player", "ghost_player").order_by("rank"))
        serialize = S.player_monthly
        period_keys = {"month": month.isoformat()}
    else:
        month = None
        season = V._resolve_quarterly_season(request)
        base = (PlayerQuarterlyScore.objects.filter(season=season)
                .select_related("player", "ghost_player").order_by("rank")
                if season else PlayerQuarterlyScore.objects.none())
        serialize = S.player_quarterly
        period_keys = {}

    published = bool(season and season.rankings_published)

    # The tab counts come from the UNFILTERED ladder, so they describe the period rather than the
    # current selection and stay stable while the user clicks between roles. They are skipped
    # entirely for a gated period: a count is a fact about rows we are not allowed to serve, so
    # publishing it would leak through the gate ("14 snipers scored this month" from a season the
    # public cannot see). All zeroes is the honest answer there, and the coverage block is zeroed
    # with them so a gated period never advertises how much role data it holds either.
    counts = _role_counts(base) if published else {key: 0 for key in ROLE_KEYS}
    coverage = (_role_coverage(base, counts) if published
                else {"players_with_role": 0, "players_scored": 0, "has_role_data": False})

    envelope = {
        "role": role,
        "period": period,
        "roles": _catalog(counts),
        "role_coverage": coverage,
        **period_keys,
        "season": S.season(season) if season else None,
        **V._period_meta(season),
    }

    # ── the publish gate. Identical to views._gated_monthly / _gated_quarterly: nothing is
    #    served for a season whose rankings are not published yet. The tab bar still ships
    #    (all zeroes), so the UI keeps its shape instead of collapsing to nothing.
    if not published:
        return Response({**envelope, "results": [],
                         "pagination": {"total_count": 0, "has_more": False},
                         "published": False})

    qs = base
    if role is not None:
        # Filter on the STORED role for this period, not on a join against today's roster - that
        # join is the bug this replaced. Rows with role=NULL (staff, ghosts, solo-only periods,
        # play from before the stamping) fall out here, which is correct: no role was recorded for
        # them, so no role table is theirs. They stay on the unfiltered ladder.
        qs = qs.filter(role=role)

    items, meta = S.paginate(request, qs)
    rows = [serialize(x) for x in items]
    rows = _rerank_within_role(rows, meta["offset"]) if role is not None else _unranked(rows)

    # Tiers are a second, independent publish gate on the quarterly ladder (see
    # views._gated_quarterly): hide the badge until an admin publishes the tiers, or a role table
    # would show tiers the main ladder is still withholding.
    if period == QUARTERLY and not season.tiers_published:
        for row in rows:
            row["tier"] = None
            row["tier_label"] = None

    return Response({**envelope, "results": rows, "pagination": meta, "published": True})
