"""
afc_fantasy.pricing - what a player costs in AFC SEEDS, and the sentence that explains it.

THE PROBLEM THIS SOLVES, WITH THE REAL NUMBERS
    Measured across every AFC player with at least 5 maps recorded (254 players, 2,982 stat rows):

        kills per map    min 0.00   p25 0.86   MEDIAN 1.45   p75 2.20   p90 3.19   max 7.00

    The best player gets 4.8x the middle player. Price in proportion to that off a 100-seed pot
    with a squad of 5 (so 20 seeds is the average affordable player) and the best player costs 96
    seeds - the whole pot on one person. Nobody would ever pick him, so the pot has done the
    opposite of its job.

    PRICES ARE NOT A MEASURE OF HOW GOOD SOMEBODY IS. They are the price of a decision. A price is
    right when picking that player is a real choice and wrong when it is obvious either way.

SO: PRICE BY RANK, NOT BY RAW NUMBERS
    Line the players up on per-map scoring, then spread them evenly across a band. The player
    halfway down costs the middle price however large the gap above him is.

    With the default band (8 to 32 seeds, 100-seed pot, squad of 5):

        the 5 best         160 seeds   impossible, and that squad SHOULD be impossible
        the 2 best + 3      64 + 36     possible, but the other three sit below average
        5 average          100 seeds    possible, exactly. The "safe" squad
        5 cheapest          40 seeds    possible, 60 wasted. Nobody does this

    It also survives drift. If the whole scene gets more aggressive and every kill count rises,
    proportional pricing inflates every price at once and the pot stops meaning anything. Rank
    pricing does not care, because somebody is always in the middle.

THE FOUR EDGE CASES, AND WHY EACH IS ANSWERED THIS WAY
    1. No history at all. Most of AFC's player list has never had a stat recorded (377 players have
       match stats out of well over a thousand accounts). Priced at the MIDDLE and badged
       "unproven": cheap-and-unknown is a free lottery ticket everybody takes, and
       expensive-and-unknown punishes a debut nobody could have judged.
    2. Too few maps to mean anything. 7 kills a map over 5 maps might be excellent or might be one
       good night. Below MIN_MAPS_FOR_RANKING (8, the median for AFC's recorded players, so half of
       them clear it) the player is treated as unproven.
    3. Picking a winner should cost something. A great player on a great team scores twice, from
       his own kills and his team's booyah and podium points, so the team carries a premium too -
       ranked on the team's own placement record, NOT on its tier field, which separates almost
       nothing (see the team-premium block below for the numbers). Capped small on purpose: the
       player is what is being bought.
    4. It must be checkable. Every price carries the one line that produced it. A price you can
       check is a price nobody argues with twice, and it is the strongest argument for rank pricing
       over anything cleverer: it explains in one sentence.

HOW IT CONNECTS
    Reads afc_tournament_and_scrims.TournamentPlayerMatchStats (kills, played) and
    TournamentTeamMatchStats (placement, for the team premium). Writes afc_fantasy.PlayerPrice. Called by the admin "open this league"
    endpoint (afc_fantasy.admin_views) and the price_fantasy_league command. Rendered by the squad
    builder, frontend/app/(user)/fantasy/[slug]/build.
"""
from django.db.models import Count, Sum

from afc_tournament_and_scrims.models import TournamentPlayerMatchStats

# ── the band ─────────────────────────────────────────────────────────────────────────────────
# Defaults for a 100-seed pot and a squad of 5. Scaled for any other pot in `band_for`, so a league
# with a 200-seed pot gets the same SHAPE rather than prices that are suddenly all affordable.
DEFAULT_FLOOR_SEEDS = 8
DEFAULT_CEILING_SEEDS = 32
REFERENCE_BUDGET = 100
REFERENCE_SQUAD = 5

# Below this many maps a player has not shown enough for a rank to mean anything. 8 is the median
# map count among AFC players who have any stats at all, so it is a bar half of them clear.
MIN_MAPS_FOR_RANKING = 8

# ── the team premium (owner, 2026-08-17: "shouldn't the team the player is from also affect the
# cost of the player?") ───────────────────────────────────────────────────────────────────────
# Yes, and for a concrete reason: a player on a strong team scores TWICE, from their own kills and
# from their team's booyah and podium points. Two players with identical kill records are not worth
# the same if one of them finishes top three every map.
#
# WHY NOT THE TIER FIELD. The obvious instrument is the team's tier, and it does not work: of the
# 130 teams with a quarterly ranking row, 129 are Tier 3 and exactly ONE is Tier 1
# (afc_rankings.TeamQuarterlyScore, checked 2026-08-17), and afc_team.Team.team_tier is the string
# "3" for all 620 teams. A tier premium would therefore give a bonus to one team and nothing to
# anybody else, which is indistinguishable from having no rule at all.
#
# WHAT DOES WORK is the record itself. Across the 69 teams with 5+ maps recorded:
#     average placement   best 2.56   p25 5.09   MEDIAN 6.20   p75 7.12   worst 10.00
#     booyah rate         median 3.8%   p90 25%   max 50%
# That separates teams properly, it is derived from results already being entered, and it updates
# itself as the scene changes instead of waiting for somebody to re-tier 620 teams by hand.
#
# The premium is RANKED, exactly like the player band, and for the same reason: it is the price of a
# decision, not a measure of merit. Teams are lined up best-to-worst on average placement and spread
# evenly from 0 up to the league's `team_premium_seeds`.
DEFAULT_TEAM_PREMIUM_SEEDS = 6
# Below this many maps a team's record says nothing, so it gets NO premium rather than a guessed
# one. Same principle as an unproven player, and the same trap avoided: a team that played three
# lucky maps should not make its whole roster expensive.
MIN_MAPS_FOR_TEAM_PREMIUM = 5


def band_for(league):
    """(floor, ceiling) in seeds for this league's pot and squad size.

    Scaled from the reference band so the SHAPE holds: whatever the pot, the best player costs
    about a third of it, five average players spend it exactly, and the five best are out of reach.
    A league that doubled its pot without this would find every player affordable, which is the
    same as having no pot.
    """
    if not league.use_budget:
        return 0, 0
    scale = (league.budget_seeds / REFERENCE_BUDGET) * (REFERENCE_SQUAD / max(league.squad_size, 1))
    floor = max(1, round(DEFAULT_FLOOR_SEEDS * scale))
    ceiling = max(floor + 1, round(DEFAULT_CEILING_SEEDS * scale))
    return floor, ceiling


def player_form(player_ids):
    """{player_id: (kills_per_map, maps_played)} from the stats AFC already records.

    Kills and maps only, because those are what actually gets entered (see models.py). Two queries
    for the whole player list rather than one per player: a league with 200 eligible players would
    otherwise open with 200 round trips.
    """
    rows = (
        TournamentPlayerMatchStats.objects
        .filter(player_id__in=player_ids, played=True)
        .values("player_id")
        .annotate(maps=Count("player_stats_id"), kills=Sum("kills"))
    )
    return {
        r["player_id"]: ((r["kills"] or 0) / r["maps"], r["maps"])
        for r in rows if r["maps"]
    }


def team_premiums(team_ids, max_premium):
    """{team_id: seeds} - the premium each team's players carry, from the team's own record.

    Ranked on AVERAGE PLACEMENT (lower is better), so the best team in the pool gets the full
    premium and the worst gets none. Booyah rate is deliberately not blended in: it is largely the
    same information (a team that wins maps has a low average placement), and two overlapping
    measures in one number make the printed reason harder to check, which is the thing that stops
    arguments.

    A team with too little history, or a player with no team at all (solo events), gets 0.
    """
    from django.db.models import Avg, Count

    from afc_tournament_and_scrims.models import TournamentTeamMatchStats

    if not team_ids or max_premium <= 0:
        return {}

    rows = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team__team_id__in=team_ids)
        .values("tournament_team__team_id")
        .annotate(maps=Count("team_stats_id"), avg_place=Avg("placement"))
        .filter(maps__gte=MIN_MAPS_FOR_TEAM_PREMIUM)
    )
    ranked = sorted(
        ((r["tournament_team__team_id"], r["avg_place"]) for r in rows),
        key=lambda row: row[1],           # best (lowest) average placement first
    )
    if not ranked:
        return {}
    if len(ranked) == 1:
        return {ranked[0][0]: max_premium}
    return {
        team_id: round(max_premium * (1 - i / (len(ranked) - 1)))
        for i, (team_id, _) in enumerate(ranked)
    }


def compute_prices(league, entries):
    """The price list for a league. Pure: reads stats, returns dicts, writes nothing.

    `entries` is [(player_id, team), ...] - who is available and which team they are with FOR THIS
    EVENT. Returns [{player_id, team, price_seeds, is_unproven, reason}, ...].

    Keeping this pure is what lets the admin PREVIEW a price list before committing it, and lets
    the tests assert the shape of the band without touching PlayerPrice at all.
    """
    floor, ceiling = band_for(league)
    if not league.use_budget:
        # Free pick: every player is free, and saying so explicitly beats a price list of zeros
        # that looks like a bug.
        return [
            {"player_id": pid, "team": team, "price_seeds": 0, "is_unproven": False,
             "reason": "Free pick league, no prices."}
            for pid, team in entries
        ]

    form = player_form([pid for pid, _ in entries])

    # Only players with enough maps get RANKED. The rest sit at the middle of the band, badged, and
    # are deliberately kept out of the ranking itself: a handful of five-map flukes at the top would
    # push every genuinely proven player down a place.
    ranked = sorted(
        ((pid, form[pid][0], form[pid][1]) for pid, _ in entries
         if pid in form and form[pid][1] >= MIN_MAPS_FOR_RANKING),
        key=lambda row: row[1],
    )
    # position 0..1 up the list; a single ranked player sits at the top of the band, which is right:
    # he is the best there is evidence for.
    positions = {}
    if len(ranked) == 1:
        positions[ranked[0][0]] = 1.0
    elif ranked:
        for i, (pid, _, _) in enumerate(ranked):
            positions[pid] = i / (len(ranked) - 1)

    # The team premium, ranked across the teams actually IN this league (see team_premiums).
    max_team_premium = getattr(league, "team_premium_seeds", DEFAULT_TEAM_PREMIUM_SEEDS)
    premiums = team_premiums(
        {getattr(team, "pk", None) for _, team in entries if team is not None},
        max_team_premium,
    )

    middle = round((floor + ceiling) / 2)
    teams = dict(entries)
    out = []
    for pid, team in entries:
        premium = premiums.get(getattr(team, "pk", None), 0)
        if pid in positions:
            kpm, maps = form[pid]
            base = floor + (ceiling - floor) * positions[pid]
            # The premium is allowed ABOVE the player ceiling rather than capped into it. Capping
            # would silently erase the team bonus for exactly the players it matters most for - the
            # best player on the best team - and quietly make the rule do nothing at the top.
            price = min(ceiling + max_team_premium, round(base) + premium)
            reason = f"{kpm:.1f} kills per map over {maps} maps"
            if premium:
                reason += f", +{premium} for {team.team_name} form"
            out.append({"player_id": pid, "team": team, "price_seeds": max(floor, price),
                        "is_unproven": False, "reason": reason})
        else:
            # An unproven player still carries their team's premium: the team's record is evidence
            # even when the player's is not, and it is the only thing separating two debutants.
            maps = form.get(pid, (0, 0))[1]
            reason = (f"Only {maps} map(s) recorded, not enough to price on" if maps
                      else "No recorded matches yet")
            if premium:
                reason += f", +{premium} for {team.team_name} form"
            out.append({"player_id": pid, "team": teams.get(pid), "price_seeds": middle + premium,
                        "is_unproven": True, "reason": reason})
    return out


def apply_prices(league, entries):
    """Write the price list, LEAVING EVERY HUMAN OVERRIDE ALONE. Returns (written, skipped).

    The formula exists so nobody has to price 250 players by hand. It is not an authority: an admin
    who looked at the list and decided a price was wrong has made a considered decision, and a
    re-run must never quietly undo it. That is the whole reason PlayerPrice.is_overridden exists.
    """
    from .models import PlayerPrice

    overridden = set(
        PlayerPrice.objects.filter(league=league, is_overridden=True)
        .values_list("player_id", flat=True)
    )
    written = skipped = 0
    for row in compute_prices(league, entries):
        if row["player_id"] in overridden:
            skipped += 1
            continue
        PlayerPrice.objects.update_or_create(
            league=league, player_id=row["player_id"],
            defaults={
                "team": row["team"],
                "price_seeds": row["price_seeds"],
                "is_unproven": row["is_unproven"],
                "reason": row["reason"],
                "is_overridden": False,
            },
        )
        written += 1
    return written, skipped
