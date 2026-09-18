"""
afc_wager.suggest - the settlement suggestion, computed from AFC's own match stats.

WHY: the May branch's "auto-suggestion from the stats reader" never read any stats (the queue was
always empty). Here a market is tied to a real Match, and once `Match.result_inputted` is true the
template's settle rule picks the option the stats say won. A human still confirms (services.
settle_market); this module only proposes, and always says what it looked at.

RULES (MarketTemplate.settle_rule)
    team_placement_1          the TournamentTeamMatchStats row with placement == 1
    team_most_kills           the team row with the most kills (a tie -> no suggestion, evidence
                              names the tied teams, a human decides)
    player_most_kills         the TournamentPlayerMatchStats row with the most kills (same tie rule)
    match_mvp                 Match.mvp
    total_kills_over_under    sum of team kills vs the line: over when strictly greater, under when
                              strictly lower, a push (equal) -> no suggestion, human decides
    manual                    never suggests; the queue shows the market as "manual"

Returns (MarketOption | None, evidence dict). The evidence is stored on the market and shown on the
admin detail and in the queue, so the confirming admin sees the numbers, not just a name.
"""
from django.db.models import Sum

from afc_tournament_and_scrims.models import TournamentPlayerMatchStats, TournamentTeamMatchStats

from .models import MarketTemplate


def compute(market):
    template = market.template
    rule = template.settle_rule
    if rule == MarketTemplate.SETTLE_MANUAL:
        return None, {"rule": rule, "manual": True, "note": "This kind of market is settled by hand."}
    match = market.match
    if match is None:
        return None, {"rule": rule, "manual": True, "note": "No match is linked; settle by hand."}
    if not getattr(match, "result_inputted", False):
        return None, {"rule": rule, "waiting": True, "note": "The match result is not in yet.",
                      "match_id": match.pk}

    options = list(market.options.all())
    by_team = {o.team_id: o for o in options if o.team_id}
    by_player = {o.player_id: o for o in options if o.player_id}
    by_side = {o.side: o for o in options if o.side}
    team_rows = list(TournamentTeamMatchStats.objects.filter(match=match, is_aggregate=False)
                     .select_related("tournament_team"))

    if rule == MarketTemplate.SETTLE_TEAM_PLACEMENT_1:
        winners = [r for r in team_rows if r.placement == 1]
        evidence = {"rule": rule, "match_id": match.pk,
                    "placements": [{"team": r.tournament_team.display_name, "placement": r.placement}
                                   for r in sorted(team_rows, key=lambda r: (r.placement or 999))[:6]]}
        if len(winners) != 1:
            evidence["note"] = "No single team is placed first." if not winners else "More than one team is placed first."
            return None, evidence
        option = by_team.get(winners[0].tournament_team_id)
        evidence["winner"] = winners[0].tournament_team.display_name
        if option is None:
            evidence["note"] = "The winning team is not one of this market's options."
        return option, evidence

    if rule == MarketTemplate.SETTLE_TEAM_MOST_KILLS:
        ranked = sorted(team_rows, key=lambda r: -(r.kills or 0))
        evidence = {"rule": rule, "match_id": match.pk,
                    "kills": [{"team": r.tournament_team.display_name, "kills": r.kills} for r in ranked[:6]]}
        if not ranked:
            evidence["note"] = "No team stats on this match."
            return None, evidence
        top = ranked[0].kills or 0
        tied = [r for r in ranked if (r.kills or 0) == top]
        if len(tied) > 1:
            evidence["note"] = "Tied on kills: " + ", ".join(r.tournament_team.display_name for r in tied)
            return None, evidence
        option = by_team.get(ranked[0].tournament_team_id)
        evidence["winner"] = ranked[0].tournament_team.display_name
        if option is None:
            evidence["note"] = "The top team is not one of this market's options."
        return option, evidence

    if rule == MarketTemplate.SETTLE_PLAYER_MOST_KILLS:
        rows = list(TournamentPlayerMatchStats.objects.filter(team_stats__match=match, team_stats__is_aggregate=False)
                    .select_related("player", "ghost_player"))
        ranked = sorted(rows, key=lambda r: -(r.kills or 0))
        evidence = {"rule": rule, "match_id": match.pk,
                    "kills": [{"player": r.display_name, "kills": r.kills} for r in ranked[:8]]}
        if not ranked:
            evidence["note"] = "No player stats on this match."
            return None, evidence
        top = ranked[0].kills or 0
        tied = [r for r in ranked if (r.kills or 0) == top]
        if len(tied) > 1:
            evidence["note"] = "Tied on kills: " + ", ".join(r.display_name for r in tied)
            return None, evidence
        option = by_player.get(ranked[0].player_id)
        evidence["winner"] = ranked[0].display_name
        if option is None:
            evidence["note"] = "The top player is not one of this market's options."
        return option, evidence

    if rule == MarketTemplate.SETTLE_MATCH_MVP:
        evidence = {"rule": rule, "match_id": match.pk, "mvp": getattr(match.mvp, "username", None)}
        if match.mvp_id is None:
            evidence["note"] = "No MVP recorded on the match."
            return None, evidence
        option = by_player.get(match.mvp_id)
        if option is None:
            evidence["note"] = "The MVP is not one of this market's options."
        return option, evidence

    if rule == MarketTemplate.SETTLE_TOTAL_KILLS_OVER_UNDER:
        total = (TournamentTeamMatchStats.objects.filter(match=match, is_aggregate=False)
                 .aggregate(s=Sum("kills"))["s"] or 0)
        line = market.over_under_line or 0
        evidence = {"rule": rule, "match_id": match.pk, "total_kills": total, "line": line}
        if total == line:
            evidence["note"] = "A push: total kills equal the line. Settle by hand (usually void)."
            return None, evidence
        side = "over" if total > line else "under"
        evidence["winner"] = side
        return by_side.get(side), evidence

    return None, {"rule": rule, "manual": True, "note": "Unknown settle rule; settle by hand."}
