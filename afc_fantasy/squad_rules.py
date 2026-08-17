"""
afc_fantasy.squad_rules - whether a squad is legal, and if not, WHY in a sentence a fan can act on.

WHY THIS RETURNS REASONS AND NOT A BOOLEAN
    The same lesson afc_polls.eligibility learned: "Save" being greyed out generates support
    tickets. A refusal has to say what the rule is, what YOURS is, and what would fix it. So every
    check returns a per-rule breakdown, and the squad builder renders the whole list - passing rules
    included - because a fan seeing "3 of 5 picked, 62 of 100 seeds spent" understands the game in a
    way an error message can never teach them.

WHY IT IS CHECKED ON THE SERVER, ALWAYS
    Whatever the builder does is a courtesy. This runs again inside the save, before anything is
    written, because a squad that breaks the budget is not a cosmetic problem: it wins.

THE RULES, AND WHERE EACH NUMBER COMES FROM (spec section 3)
    squad size          the admin's choice, 4 to 6
    max per team        the admin's choice. THE rule that stops every fan entering the same squad
    exactly one captain the one pick that makes two identical squads finish differently
    within budget       only when the league uses a budget at all
    picks are eligible  a player who is not in this event cannot be picked
    league is open      a locked league takes no edits, from anyone, ever

HOW IT CONNECTS
    Reads afc_fantasy.PlayerPrice (what each pick costs and which team they are with) and the
    settings on FantasyLeague. Called by afc_fantasy.views.save_squad before it writes, and by
    the squad builder through the same endpoint's dry-run. Writes nothing.
"""


def check_squad(league, picks, prices_by_player):
    """Is this squad legal? Returns {"ok": bool, "spent": int, "rules": [...]}.

    `picks` is [{"player_id": int, "is_captain": bool}, ...] exactly as the client sends it.
    `prices_by_player` is {player_id: PlayerPrice}, which is also how eligibility is decided: a
    player with no price row is not in this league's pool, so "unknown player" and "not in this
    event" are the same refusal and cannot disagree with each other.

    Every rule is reported with `ok`, a `label` naming the rule and a `detail` stating the fan's own
    position against it. `ok` on the envelope is the AND of them, computed here so no caller can
    accidentally save a squad that failed a rule it forgot to look at.
    """
    rules = []
    player_ids = [p.get("player_id") for p in picks]

    # ── the league must be open ───────────────────────────────────────────────────────────────
    # First, because every other message is noise if picks are already final.
    rules.append({
        "key": "league_open",
        "ok": not league.is_locked,
        "label": "Picks are still open",
        "detail": ("Picks are final for this league." if league.is_locked
                   else "You can still change your squad."),
    })

    # ── every pick must be a player in this league's pool ─────────────────────────────────────
    unknown = [pid for pid in player_ids if pid not in prices_by_player]
    rules.append({
        "key": "players_available",
        "ok": not unknown,
        "label": "Every pick is playing in this event",
        "detail": ("All your picks are in this event." if not unknown
                   else f"{len(unknown)} of your picks are not playing in this event."),
    })

    # ── no duplicates ─────────────────────────────────────────────────────────────────────────
    # Checked explicitly rather than left to the database constraint: the same player twice would
    # double every point they score, and a fan deserves to be told that rather than shown a 500.
    duplicates = len(player_ids) != len(set(player_ids))
    rules.append({
        "key": "no_duplicates",
        "ok": not duplicates,
        "label": "No player picked twice",
        "detail": ("Each player appears once." if not duplicates
                   else "You have picked the same player more than once."),
    })

    # ── squad size ────────────────────────────────────────────────────────────────────────────
    size_ok = len(picks) == league.squad_size
    rules.append({
        "key": "squad_size",
        "ok": size_ok,
        "label": f"Pick exactly {league.squad_size} players",
        "detail": f"You have picked {len(picks)} of {league.squad_size}.",
    })

    # ── how many from one team ────────────────────────────────────────────────────────────────
    # The setting that stops everyone entering the same squad. Players with no team recorded are
    # not grouped together: "no team" is missing data, not a club, and treating it as one would
    # refuse a legal squad for a reason the fan cannot see or fix.
    per_team = {}
    for pid in player_ids:
        price = prices_by_player.get(pid)
        team_id = getattr(price, "team_id", None) if price else None
        if team_id is not None:
            per_team[team_id] = per_team.get(team_id, 0) + 1
    worst = max(per_team.values(), default=0)
    rules.append({
        "key": "max_per_team",
        "ok": worst <= league.max_per_team,
        "label": f"At most {league.max_per_team} players from any one team",
        "detail": (f"Your biggest group from one team is {worst}." if per_team
                   else "No team limits reached yet."),
    })

    # ── exactly one captain ───────────────────────────────────────────────────────────────────
    captains = sum(1 for p in picks if p.get("is_captain"))
    rules.append({
        "key": "one_captain",
        "ok": captains == 1,
        "label": "Choose exactly one captain",
        "detail": ("Captain chosen." if captains == 1
                   else f"You have {captains} captains selected."),
    })

    # ── the budget ────────────────────────────────────────────────────────────────────────────
    # Only counted for known players, so an unknown pick is reported once (above) rather than
    # producing a second, confusing "you are under budget" that contradicts it.
    spent = sum(prices_by_player[pid].price_seeds for pid in player_ids if pid in prices_by_player)
    if league.use_budget:
        rules.append({
            "key": "within_budget",
            "ok": spent <= league.budget_seeds,
            "label": f"Stay within {league.budget_seeds} AFC SEEDS",
            "detail": f"You have spent {spent} of {league.budget_seeds} seeds.",
        })

    return {"ok": all(r["ok"] for r in rules), "spent": spent, "rules": rules}
