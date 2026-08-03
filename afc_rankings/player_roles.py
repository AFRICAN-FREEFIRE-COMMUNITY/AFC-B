"""
Public read API for the PER-ROLE player ladders ("sniper rankings, rusher rankings...").

WHAT THIS IS, AND WHAT IT IS DELIBERATELY NOT
    A role ladder is a FILTER over the existing player ladder. It is not a second scoring
    system. Every player keeps the exact score ``afc_rankings.recalc`` already wrote for the
    period; a role table simply shows the subset of players who play that role, renumbered
    1..N so the numbering means something inside the table.

    That choice is the whole design, so it is worth saying why. A role-specific SCORE would
    need role-specific inputs (a sniper stat, a rusher stat) and nothing in the pipeline
    records them: ``PlayerMonthlyScore`` / ``PlayerQuarterlyScore`` hold kills, MVPs, finals,
    team wins, participation and scrims, none of which are role-flavoured. Inventing weights
    per role would produce numbers no result could justify, and two players with identical
    performances would score differently for a reason nobody could point at. A filter is
    honest: the sniper ladder says "of the players who play sniper, here is the order",
    which is exactly what was asked for and is derivable from data that actually exists.

WHERE THE ROLE COMES FROM, AND THE ONE CAVEAT
    ``afc_team.TeamMembers.in_game_role`` - rusher / support / grenader / sniper, set on the
    roster and editable only while the transfer window is open (afc_team.views). A user
    belongs to at most one team (``unique_member_one_team``), so a player has at most one
    role and no row is ever counted twice.

    CAVEAT, stated rather than hidden: the role is the player's role TODAY, not the role
    they held when the points were earned. Role history is not stored anywhere, so this is
    the only answer available. In practice a role can only change during the transfer
    window, which is also when rosters move, so the drift is small and the alternative
    (silently pretending we know their July role) would be worse.

    Two groups are absent from every role table, both correctly:
      * players with no ``in_game_role`` (staff roles - coach, manager, analyst - and anyone
        on no roster at all). They have no role, so no role table is theirs.
      * GHOST players. A ghost is an unclaimed historical name with no roster row, therefore
        no role. They still appear in the unfiltered ladder, exactly as they do today.

PUBLISH GATING IS THE SAME AS THE MAIN LADDER
    This endpoint reuses ``views._resolve_month`` / ``_season_of_month`` /
    ``_resolve_quarterly_season`` / ``_period_meta`` rather than re-deriving them, so a role
    table can never leak an unpublished period the main ladder is still hiding. Importing
    the private helpers from ``views`` is the established idiom here (admin_publish.py and
    admin_evaluation.py do the same).

AUTH
    None, deliberately. This is a PUBLIC read and it mirrors ``afc_rankings.views``, whose
    ladders are open to everyone. The Bearer + ``validate_token`` preamble in this codebase
    belongs to the admin write surfaces (see ``admin_scoring_config.py``); adding it here
    would put the public rankings page behind a login.

ROUTES (mounted by urls.py under the ``rankings/`` prefix)
    GET players/by-role/  -> players_by_role   (public)

Consumed by: the public rankings page's player ladder role tabs
(frontend app/(user)/rankings/page.tsx, via ``rankingsApi.playersByRole`` in lib/rankings.ts).
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_team.models import TeamMembers

from . import serializers as S
from . import views as V
from .models import PlayerMonthlyScore, PlayerQuarterlyScore

# The role catalog, straight off the model so a new choice added there shows up here without
# a second edit. Order is the model's order, which is the order the tabs render in.
ROLE_CHOICES = tuple(TeamMembers.IN_GAME_ROLE_CHOICES)
ROLE_KEYS = tuple(key for key, _label in ROLE_CHOICES)

# The sentinel the tab bar sends for "everybody", so the frontend never has to special-case a
# missing parameter. Anything else unrecognised is treated the same way (no filter) rather than
# 400ing: a stale bookmark should show the ladder, not an error.
ROLE_ALL = "all"

MONTHLY, QUARTERLY = "monthly", "quarterly"


def _player_ids_with_role(role):
    """The user ids currently rostered in ``role``.

    One query, materialised, because it is used both to filter the ladder and (for the counts)
    to intersect with the scored population. ``unique_member_one_team`` guarantees a user
    appears at most once, so this is a set of distinct ids by construction.
    """
    return set(
        TeamMembers.objects.filter(in_game_role=role).values_list("member_id", flat=True)
    )


def _role_counts(qs):
    """``{role: how many players in ``qs`` play it}`` - the numbers on the role tabs.

    ``qs`` is the UNFILTERED ladder queryset for the period, so a count is "how many scored
    players hold this role", not "how many players hold this role" - a role with 40 rostered
    players but nobody scored this month correctly reads 0, and the tab says so instead of
    opening onto an empty table.

    Two queries total regardless of how many roles exist: the scored player ids, then every
    (member, role) pair among them.
    """
    scored_ids = set(qs.exclude(player_id=None).values_list("player_id", flat=True))
    counts = {key: 0 for key in ROLE_KEYS}
    if not scored_ids:
        return counts
    pairs = (TeamMembers.objects
             .filter(member_id__in=scored_ids)
             .exclude(in_game_role=None)
             .values_list("in_game_role", flat=True))
    for role in pairs:
        if role in counts:
            counts[role] += 1
    return counts


def _catalog(counts):
    """The role tab bar as data: key, the model's English label, and the count.

    The label is shipped so a client that has no translation for a NEW role still renders
    something readable; the frontend translates the known four and falls back to this.
    """
    return [
        {"role": key, "label": str(label), "player_count": counts.get(key, 0)}
        for key, label in ROLE_CHOICES
    ]


def _rerank_within_role(rows, offset):
    """Renumber a page of role-filtered rows 1..N WITHIN the role.

    The rows arrive in global rank order, and the global rank is ordinal with no ties
    (``recalc.rerank_player_month`` / ``rerank_player_quarter`` number 1..N straight down the
    sorted ladder), so position in the filtered list IS the within-role rank. ``offset``
    carries the pagination offset so page 2 continues 26, 27, ... rather than restarting at 1.

    ``rank`` is OVERWRITTEN rather than supplemented on purpose: a role table that showed the
    global rank would number its rows 3, 17, 24 and read as a broken list. The global number
    is not lost - it moves to ``overall_rank``, so a row can still say "12th overall".
    """
    for index, row in enumerate(rows, start=offset + 1):
        row["overall_rank"] = row["rank"]
        row["rank"] = index
    return rows


def _unranked(rows):
    """The unfiltered ladder, with ``overall_rank`` mirrored on so both shapes match.

    Keeping the key present in every response means the client renders one row component
    instead of branching on whether a role is selected.
    """
    for row in rows:
        row["overall_rank"] = row["rank"]
    return rows


@api_view(["GET"])
def players_by_role(request):
    """The player ladder for one in-game role, or the whole ladder, plus the role tab counts.

    Purpose:  drive the per-role player tables on the public rankings page ("sniper rankings,
              rusher rankings"). One call returns both the table and the tab bar, so selecting
              a role is a single request and the counts never disagree with the rows.
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
          "results": [ ...player rows... ], # player_monthly / player_quarterly shape, PLUS:
                                            #   rank         = rank WITHIN the role (1..N)
                                            #   overall_rank = the rank on the full ladder
          "pagination": {"limit","offset","total_count","has_more","next_offset"},
          "month": "2026-07-01",            # monthly only
          "season": { ...season... },
          "published": true,                # false = the period is gated, results is empty
          "is_current_period": true,
          "current_season": { ...season... }
        }

    An unknown ``role`` is treated as "all" rather than refused, so a stale link degrades to
    the full ladder instead of an error page.

    RANK SEMANTICS, the one thing a caller must not get wrong: inside a role table ``rank`` is
    the rank AMONG PLAYERS OF THAT ROLE. The player ranked 1 as a sniper may be 24th overall;
    ``overall_rank`` carries that number so both can be shown.

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

    # The tab counts come from the UNFILTERED ladder, so they describe the period rather than
    # the current selection and stay stable while the user clicks between roles. They are
    # skipped entirely for a gated period: a count is a fact about rows we are not allowed to
    # serve, so publishing it would leak through the gate ("14 snipers scored this month" from
    # a season the public cannot see). All zeroes is the honest answer there.
    counts = _role_counts(base) if published else {key: 0 for key in ROLE_KEYS}

    envelope = {
        "role": role,
        "period": period,
        "roles": _catalog(counts),
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
        # Ghost rows have no roster and therefore no role; filtering on player_id__in drops
        # them here, which is correct - see the module docstring.
        qs = qs.filter(player_id__in=_player_ids_with_role(role))

    items, meta = S.paginate(request, qs)
    rows = [serialize(x) for x in items]
    rows = _rerank_within_role(rows, meta["offset"]) if role is not None else _unranked(rows)

    # Tiers are a second, independent publish gate on the quarterly ladder (see
    # views._gated_quarterly): hide the badge until an admin publishes the tiers, or a role
    # table would show tiers the main ladder is still withholding.
    if period == QUARTERLY and not season.tiers_published:
        for row in rows:
            row["tier"] = None
            row["tier_label"] = None

    return Response({**envelope, "results": rows, "pagination": meta, "published": True})
