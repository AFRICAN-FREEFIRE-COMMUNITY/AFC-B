"""
afc_fantasy.scoring - what a squad scored, computed from the results AFC already records.

THE RULE THIS FILE EXISTS TO KEEP
    A fantasy score is NEVER typed and never stored as the truth. It is recomputed from the current
    match stats every time, because AFC corrects results after the fact: a kill count is fixed, a
    team is disqualified, a whole match is re-uploaded. A stored total would quietly disagree with
    the results page it came from, and the first person to notice would be a fan who lost.

    FantasyPoints rows are a CACHE of this computation. Deleting the whole table costs a slow page
    and nothing else.

WHAT IT READS, AND WHY ONLY THIS
    TournamentPlayerMatchStats  kills, played           per player, per match
    TournamentTeamMatchStats    placement               1 is a booyah, 2 and 3 are the podium
    Match.mvp                   the map's MVP, if set

    That is the complete list of what AFC reliably records (owner, 2026-08-16). Damage, assists and
    deaths have columns and are mostly empty. Scoring on a column nobody fills would make a fan's
    total depend on whether an organizer had a spare five minutes, so the damage rule ships at 0
    points and waits for the data rather than the other way round.

THE CAPTAIN
    Multiplies EVERYTHING that player scored, not just their kills. It is the one choice that makes
    two identical squads finish differently, which is what makes it worth making.

HOW IT CONNECTS
    Reads afc_tournament_and_scrims (Match, TournamentTeamMatchStats, TournamentPlayerMatchStats).
    Writes afc_fantasy.FantasyPoints. Called by recompute_league (the admin action, the post-result
    hook and the fantasy_recompute command). Rendered by the league table and "my squad" pages,
    frontend/app/(user)/fantasy/[slug].
"""
from django.db import transaction

from afc_tournament_and_scrims.models import Match, TournamentPlayerMatchStats

from .models import FantasyPoints, FantasySquad


def _match_ids_for(league):
    """Every match whose result counts for this league, oldest first.

    Scoped to the league's EVENT. A season-scoped league widens this later; keeping the lookup in
    one function is what makes that a change here rather than in three call sites.
    """
    return list(
        Match.objects.filter(group__stage__event=league.event)
        .order_by("match_id").values_list("match_id", flat=True)
    )


def score_player_match(rules, kills, placement, played, is_mvp, damage=0):
    """What ONE player scored in ONE match, before any captain multiplier.

    Pure arithmetic on numbers the caller has already fetched, so the rules can be tested without a
    database and the same function serves both the live table and the "what if" preview.

    A player who was not fielded scores NOTHING, not even the participation point: the point exists
    to reward being picked and playing, so paying it to a benched player would defeat it.
    """
    if not played:
        return 0
    total = rules.points_played
    total += kills * rules.points_per_kill
    if placement == 1:
        total += rules.points_booyah
    elif placement in (2, 3):
        total += rules.points_top3
    if is_mvp:
        total += rules.points_mvp
    if rules.points_per_1k_damage:
        total += (damage // 1000) * rules.points_per_1k_damage
    return total


def _stats_by_match(league, player_ids):
    """{match_id: {player_id: (kills, placement, played, damage)}} for the league's players.

    ONE query for the whole league rather than one per squad per match. A league with 300 squads
    over 12 matches would otherwise be 3,600 round trips on a page load; here it is one.
    """
    rows = (
        TournamentPlayerMatchStats.objects
        .filter(player_id__in=player_ids,
                team_stats__match__group__stage__event=league.event)
        .values("player_id", "kills", "damage", "played",
                "team_stats__placement", "team_stats__match_id")
    )
    out = {}
    for r in rows:
        out.setdefault(r["team_stats__match_id"], {})[r["player_id"]] = (
            r["kills"] or 0, r["team_stats__placement"], r["played"], r["damage"] or 0,
        )
    return out


def recompute_league(league):
    """Rebuild every squad's cached points for this league. Returns the number of rows written.

    REPLACES rather than adds: a corrected result must be able to LOWER a score, which an
    accumulating write could never do. That is why this uses update_or_create per (squad, match)
    and then deletes any cached row whose match no longer produces a score.
    """
    rules = league.scoring
    squads = list(
        FantasySquad.objects.filter(league=league).prefetch_related("picks")
    )
    if not squads:
        return 0

    player_ids = {pick.player_id for squad in squads for pick in squad.picks.all()}
    stats = _stats_by_match(league, player_ids)
    # MVP is on the match, not the player row, so it is fetched once per match rather than looked
    # up per player.
    mvp_by_match = dict(
        Match.objects.filter(match_id__in=stats.keys())
        .values_list("match_id", "mvp_id")
    )

    written = 0
    with transaction.atomic():
        for squad in squads:
            picks = list(squad.picks.all())
            live_matches = set()
            for match_id, per_player in stats.items():
                total = 0
                breakdown = {}
                for pick in picks:
                    row = per_player.get(pick.player_id)
                    if row is None:
                        # Picked but not in this match: their team was knocked out, or they were
                        # not fielded and no row was written. Nothing scored, and no entry in the
                        # breakdown, so "my squad" shows the gap honestly.
                        continue
                    kills, placement, played, damage = row
                    is_mvp = mvp_by_match.get(match_id) == pick.player_id
                    base = score_player_match(rules, kills, placement, played, is_mvp, damage)
                    points = (round(base * league.captain_multiplier) if pick.is_captain else base)
                    total += points
                    breakdown[str(pick.player_id)] = {
                        "kills": kills, "placement": placement, "mvp": is_mvp,
                        "captain": pick.is_captain, "points": points,
                    }
                if not breakdown:
                    continue
                live_matches.add(match_id)
                FantasyPoints.objects.update_or_create(
                    squad=squad, match_id=match_id,
                    defaults={"league": league, "points": total, "breakdown": breakdown},
                )
                written += 1
            # Drop cached rows for matches this squad no longer scores in. Without this, a result
            # that was deleted or a player removed from a match would leave points on the table
            # that nothing in the current data supports.
            FantasyPoints.objects.filter(squad=squad).exclude(match_id__in=live_matches).delete()
    return written


def standings(league):
    """The table: [{squad, total, matches}, ...], highest first.

    Ties are broken by the highest-scoring captain, then by who entered first - the rule the spec
    requires to be written down BEFORE a league opens (section 8), because deciding it afterwards
    with money attached is how a community turns on you. It is applied here whether or not there is
    a prize, so the free leagues behave the same way and the rule is familiar by the time it counts.
    """
    from django.db.models import Sum, Count

    totals = (
        FantasyPoints.objects.filter(league=league)
        .values("squad_id")
        .annotate(total=Sum("points"), matches=Count("match_id"))
    )
    by_squad = {row["squad_id"]: row for row in totals}
    squads = FantasySquad.objects.filter(league=league).select_related("user").prefetch_related("picks")

    rows = []
    for squad in squads:
        agg = by_squad.get(squad.squad_id, {"total": 0, "matches": 0})
        captain = next((p for p in squad.picks.all() if p.is_captain), None)
        captain_points = 0
        if captain:
            captain_points = sum(
                fp.breakdown.get(str(captain.player_id), {}).get("points", 0)
                for fp in squad.points.all()
            )
        rows.append({
            "squad": squad,
            "total": agg["total"] or 0,
            "matches": agg["matches"] or 0,
            "captain_points": captain_points,
        })
    rows.sort(key=lambda r: (-r["total"], -r["captain_points"], r["squad"].created_at))
    for i, row in enumerate(rows, start=1):
        row["position"] = i
    return rows
