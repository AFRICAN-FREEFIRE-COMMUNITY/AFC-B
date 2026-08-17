"""
afc_fantasy.models - the AFC Fantasy League.

WHAT THE FEATURE IS
    A fan picks a squad of REAL players from a REAL AFC event before it starts. When those players
    play their actual matches, the fan scores from what they did. Everyone who entered sits on one
    table. Highest total wins. The fan never plays Free Fire; they pick, then they watch.

    Spec: WEBSITE/tasks/fantasy-league-spec.md. That document is written for the owner in plain
    English and is the authority on every default in this file. Where a number here looks
    arbitrary, the spec says why it is that number.

THE ONE CONSTRAINT THAT SHAPED EVERYTHING
    The only per-player figures AFC reliably records are KILLS and sometimes MVP (owner, 2026-08-16).
    Damage, assists, deaths and headshots have columns and are mostly empty, because filling them in
    by hand for every player in every match is work nobody has time for. So the scoring below uses
    kills, MVP, the team's placement, and whether the player was fielded - and nothing else. A
    number nobody entered is worse than a number we chose not to use: it would make a fan's score
    depend on whether an organizer happened to fill in a column.

WHAT IS DELIBERATELY *NOT* STORED
    A squad's score. It is recomputed from current match stats every time it is read
    (see scoring.py). AFC corrects results after the fact - a kill count is fixed, a team is
    disqualified - and a stored total would quietly disagree with the results page it came from.
    FantasyPoints is a CACHE of that computation, rebuilt on demand, never typed and never trusted
    over the stats it derives from.

HOW IT CONNECTS
    Event / Match / TournamentTeamMatchStats / TournamentPlayerMatchStats (afc_tournament_and_scrims)
        the results that were being entered anyway; the whole scoring input.
    afc_auth.audience                    who may enter, via the spec on FantasyLeague.eligibility_spec.
    TournamentTeamMatchStats.placement   the team premium in a player's price, ranked on the
                                         team's own record (NOT the tier field, which today
                                         puts 129 of 130 teams in the same bucket).
    Frontend: app/(user)/fantasy/**      league page, squad builder, my squad.
              app/(a)/a/fantasy/**       create and manage a league.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class FantasyLeague(models.Model):
    """One fantasy competition attached to one AFC event.

    EVERY RULE OF THE GAME IS A COLUMN HERE, not a constant in code, because the owner asked for
    admins to choose (spec section 4): squad size, how many players may come from one team, what
    the captain multiplies by, budget or free pick, and whether entry is free, sponsored or paid.
    A league that has opened must never have these changed underneath its entrants, which is what
    `is_locked` guards.
    """

    # ── how a squad may be built ──────────────────────────────────────────────────────────────
    # 4 to 6 (owner). 5 is the recommended default and the reason is worth keeping: a Free Fire
    # squad is 4, so a size of 4 would let somebody copy one real team and make no decision at all.
    MIN_SQUAD_SIZE = 4
    MAX_SQUAD_SIZE = 6

    ENTRY_CHOICES = [
        ("free", "Free to enter, no prize"),
        ("sponsored", "Free to enter, prize put up by a sponsor or AFC"),
        ("paid", "Entry fee, prize built from entries"),
    ]
    SCOPE_CHOICES = [
        ("event", "One event"),
        ("stage", "One stage"),
        ("season", "A whole ranking season"),
    ]
    STATUS_CHOICES = [
        ("draft", "Draft, not visible to fans"),
        ("open", "Open, fans can enter and edit their squad"),
        ("locked", "Locked, picks are final and scoring has begun"),
        ("settled", "Finished, the result is published"),
    ]

    league_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    description = models.TextField(blank=True, default="")

    # WHAT IT IS ATTACHED TO. `event` is required even for a season-scoped league in step 1: the
    # spec ships per-event first (section 10) and widening the scope later must not require
    # rewriting rows that already exist.
    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="fantasy_leagues",
    )
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default="event")

    # ── the admin's choices (spec section 3 and 4) ────────────────────────────────────────────
    squad_size = models.PositiveSmallIntegerField(default=5)
    # The single most important setting for stopping every fan entering the same squad. At 2,
    # nobody can clone the tournament favourite; they have to find a good player on a weaker team.
    max_per_team = models.PositiveSmallIntegerField(default=2)
    # Stored as an integer of tenths (20 = 2.0x) so the multiplier is exact. A FloatField would
    # make two squads on 1.5x differ in the last decimal place, and a fantasy table that cannot be
    # reproduced exactly is a fantasy table nobody trusts.
    captain_multiplier_tenths = models.PositiveSmallIntegerField(default=20)

    # Budget on means every player has a price and the squad must fit the pot. Off means free pick,
    # constrained only by max_per_team.
    use_budget = models.BooleanField(default=True)
    # The pot, in AFC SEEDS (the fantasy currency, owner 2026-08-16). Not real money, cannot be
    # bought, transferred or cashed out. 100 because it makes prices readable at a glance: a
    # 22-seed player is obviously about a fifth of a squad.
    budget_seeds = models.PositiveIntegerField(default=100)

    # ── how much the TEAM affects a player's price (owner, 2026-08-17) ────────────────────────
    # A player on a strong team scores twice: from their own kills, and from their team's booyah and
    # podium points. So the team carries a premium on top of the player's own price, ranked across
    # the teams in this league on their actual placement record. 6 seeds on a 100-seed pot is
    # roughly a fifth of the player band, which is enough to make "who do they play for" a real
    # consideration without letting one strong roster take over the price list. Set to 0 to price
    # purely on the individual. See afc_fantasy.pricing.team_premiums, which also explains why the
    # team TIER field cannot be used for this.
    team_premium_seeds = models.PositiveSmallIntegerField(default=6)

    entry_type = models.CharField(max_length=10, choices=ENTRY_CHOICES, default="free")
    # Only meaningful when entry_type="paid". Kept alongside a currency because AFC charges in
    # several (see afc_auth.currencies), and a bare number would be read as naira by half the site.
    entry_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    entry_fee_currency = models.CharField(max_length=8, blank=True, default="")

    # WHO MAY ENTER. Same dict shape afc_auth.audience parses for polls and broadcasts, so the
    # audience an admin picks here is literally the audience they would pick to announce it.
    # Empty means anyone with an account.
    eligibility_spec = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="draft", db_index=True)
    # When picks stop being editable. Null means "the first match of the event starts it", which is
    # the default the spec describes; a stored time lets an admin lock earlier or later.
    locks_at = models.DateTimeField(null=True, blank=True)
    # Stamped the moment picks actually locked, so "when did this become final" is a fact rather
    # than a comparison somebody has to redo against a clock.
    locked_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="fantasy_leagues_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["event", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

    @property
    def captain_multiplier(self):
        """The multiplier as a number, e.g. 2.0. Stored in tenths; see the field comment."""
        return self.captain_multiplier_tenths / 10

    @property
    def is_locked(self):
        """Picks are final. True from the moment scoring begins and forever after: a settled
        league is still locked, because 'can I edit my squad' must not answer yes on a league that
        finished last month."""
        return self.status in ("locked", "settled")

    def should_lock_now(self, now=None):
        """Has this league reached its lock time? Read by the lock task and by every squad write.

        A league with no `locks_at` locks when the event starts, which is what the spec promises
        ("when the first match of the event starts, picks lock"). Checking it on every squad write
        as well as on a schedule means a late entry cannot slip in between two runs of the task.
        """
        if self.status != "open":
            return False
        now = now or timezone.now()
        deadline = self.locks_at or self.event.event_start_time
        return bool(deadline and now >= deadline)


class FantasyScoringRules(models.Model):
    """What each thing a player does is worth, for ONE league.

    A ROW, NOT CONSTANTS, because the spec promises "every one of these numbers is a setting an
    admin can change per league" (section 5). It also means a league that has already been played
    keeps the rules it was played under: changing next month's scoring cannot rewrite last month's
    table, which it would if these lived in code.

    The defaults, and why (spec section 5):
      kills are 2, not 1     - kills are the figure AFC records most reliably, so the game should
                               reward the thing that can actually be measured.
      a booyah is 5          - otherwise everyone picks fraggers on weak teams and the strongest
                               players in the scene are worth nothing.
      MVP is 5               - rare, and it rewards the all-round game a kill count misses.
      playing at all is 1    - a benched player should score less than one who played and did
                               nothing, and it makes "will they be fielded" part of the decision.
      damage is 0            - the column exists and is mostly empty. It is here at zero so that if
                               AFC ever records damage properly it is a number an admin types, not
                               a migration and a rebuild. Old leagues keep scoring exactly as they
                               did, because 0 changes nothing.
    """

    league = models.OneToOneField(FantasyLeague, on_delete=models.CASCADE, related_name="scoring")
    points_per_kill = models.IntegerField(default=2)
    points_booyah = models.IntegerField(default=5)
    points_top3 = models.IntegerField(default=2)
    points_mvp = models.IntegerField(default=5)
    points_played = models.IntegerField(default=1)
    # Per 1000 damage, so a whole number stays usable. 0 until AFC records damage reliably.
    points_per_1k_damage = models.IntegerField(default=0)

    def __str__(self):
        return f"scoring for {self.league_id}"


class PlayerPrice(models.Model):
    """What one player costs, in AFC SEEDS, in one league.

    PRICES ARE NOT A MEASURE OF HOW GOOD SOMEBODY IS. They are the price of a DECISION. Measured on
    the live data (spec section 6): across the 254 players with 5+ maps recorded, the best player
    gets 4.8x the kills per map of the middle one. Price proportionally off a 100-seed pot and the
    best player costs 96 seeds, so nobody can ever pick him and he might as well not exist. So
    players are priced by WHERE THEY RANK, spread evenly across a band, which keeps the star
    affordable-but-costly and survives the whole scene's kill counts drifting up or down.

    FROZEN WHEN THE LEAGUE OPENS and never moved while it runs. A player getting cheaper mid-event
    after a bad game would mean two fans paid different amounts for the same pick, and the table
    would stop meaning anything.

    `reason` is not decoration. Somebody will say their favourite is priced wrong, and a price you
    can check is a price nobody argues with twice. It holds the one line that produced the number
    ("1.9 kills per map over 12 maps, Tier 2 team") and the squad builder prints it under the price.
    """

    league = models.ForeignKey(FantasyLeague, on_delete=models.CASCADE, related_name="prices")
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fantasy_prices",
    )
    # The team the player is with FOR THIS EVENT. Stored rather than looked up, because rosters
    # change and a squad built in week one must still be explainable in week three.
    team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="fantasy_prices",
    )
    price_seeds = models.PositiveIntegerField()
    # True when the player has too little recorded history to rank (spec: fewer than 8 maps, which
    # is the median for AFC's recorded players, so it is a bar half of them clear). Priced at the
    # middle of the band and BADGED, because cheap-and-unknown is a free lottery ticket everybody
    # takes and expensive-and-unknown punishes a debut nobody could have judged.
    is_unproven = models.BooleanField(default=False)
    reason = models.CharField(max_length=200, blank=True, default="")
    # Set when a human typed the price. The auto-pricer must never overwrite a considered decision,
    # so a re-run skips every overridden row.
    is_overridden = models.BooleanField(default=False)

    class Meta:
        # One price per player per league. Two would mean the squad builder shows whichever the
        # database happened to return first.
        constraints = [
            models.UniqueConstraint(fields=["league", "player"], name="uniq_fantasy_price"),
        ]
        indexes = [models.Index(fields=["league", "price_seeds"])]
        ordering = ["-price_seeds", "player_id"]

    def __str__(self):
        return f"{self.player_id} = {self.price_seeds} seeds in league {self.league_id}"


class FantasySquad(models.Model):
    """One person's entry into one league.

    `spent_seeds` is stored, unlike the SCORE. The two look similar and are not: what a squad cost
    is a fact about the moment it was saved, fixed by the prices in force then, and it must not
    move if a price is later corrected. A score is a live consequence of results that get corrected
    all the time, so it is always recomputed. Storing the first and recomputing the second is the
    difference between a receipt and a running total.
    """

    squad_id = models.AutoField(primary_key=True)
    league = models.ForeignKey(FantasyLeague, on_delete=models.CASCADE, related_name="squads")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fantasy_squads",
    )
    # What the fan called their team. Free text; falls back to their username on screen.
    squad_name = models.CharField(max_length=80, blank=True, default="")
    spent_seeds = models.PositiveIntegerField(default=0)
    # Stamped when picks locked, so an entry can prove it was complete in time.
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One squad per person per league (spec section 9: "somebody enters twice with two
        # accounts" is answered by this plus, for a paid league, the payment tied to the entry).
        constraints = [
            models.UniqueConstraint(fields=["league", "user"], name="uniq_fantasy_squad_per_user"),
        ]
        indexes = [models.Index(fields=["league", "-created_at"])]

    def __str__(self):
        return f"{self.user_id}'s squad in league {self.league_id}"


class SquadPick(models.Model):
    """One player in one squad, and whether they are the captain.

    STORES THE PLAYER ID, NEVER THE NAME. AFC players rename themselves often, and the platform has
    been bitten by name-keyed references before. A rename must not be able to break a squad.

    `price_seeds` is copied in at save time rather than read through to PlayerPrice: it is what this
    fan actually paid, and an admin correcting a price afterwards must not retroactively change what
    somebody's squad cost.
    """

    pick_id = models.AutoField(primary_key=True)
    squad = models.ForeignKey(FantasySquad, on_delete=models.CASCADE, related_name="picks")
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fantasy_picks",
    )
    is_captain = models.BooleanField(default=False)
    price_seeds = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            # The same player twice in one squad would double every point they score.
            models.UniqueConstraint(fields=["squad", "player"], name="uniq_squad_pick"),
        ]
        ordering = ["-is_captain", "pick_id"]

    def __str__(self):
        return f"{self.player_id}{' (C)' if self.is_captain else ''} in squad {self.squad_id}"


class FantasyPoints(models.Model):
    """A CACHE of what a squad scored in one match. Never typed, never authoritative.

    Why a cache and not a total: results get corrected on AFC all the time, and the spec promises
    fantasy scores always follow the current results. So scoring.py recomputes from
    TournamentPlayerMatchStats and REPLACES these rows. Deleting the whole table would cost nothing
    but a slow page.

    What it buys: a leaderboard of 300 squads over 12 matches is 3,600 additions from live stats on
    every page load. This turns that into one indexed read, and `computed_at` says how fresh it is
    so the page can be honest about a correction that has not been reflected yet.
    """

    league = models.ForeignKey(FantasyLeague, on_delete=models.CASCADE, related_name="points")
    squad = models.ForeignKey(FantasySquad, on_delete=models.CASCADE, related_name="points")
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match", on_delete=models.CASCADE, related_name="fantasy_points",
    )
    points = models.IntegerField(default=0)
    # The per-player split behind `points`, so "my squad" can show WHERE a score came from without
    # recomputing: {player_id: {"kills": 6, "booyah": true, "mvp": false, "points": 23}}.
    breakdown = models.JSONField(default=dict, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["squad", "match"], name="uniq_fantasy_points"),
        ]
        indexes = [models.Index(fields=["league", "squad"])]

    def __str__(self):
        return f"squad {self.squad_id} scored {self.points} in match {self.match_id}"
