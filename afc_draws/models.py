"""
afc_draws/models.py - the GROUP DRAW (owner 2026-09-12, Phase 1).

WHAT IT IS, in plain English: instead of the organizer pressing "seed to groups" and the site
dealing teams into groups at random, the organizer can open a DRAW for a stage. Every team (or solo
player) of that stage then turns over one face-down card on the event page and the card says which
group they are in. Whoever has not picked by the close time is dealt into the cards that are left.

WHY TWO MODELS
    StageDraw   one per stage: the window (draft -> open -> closed), who opened it, and the SEAL.
    DrawCard    one per competitor slot: the card number the player sees, the group it hides, and
                once taken, who took it, for which competitor, when, and whether it was a real pick
                or the auto-fill at close. A pick writes the ordinary StageGroupCompetitor row too,
                so standings, rooms, broadcasts and every existing reader keep working unchanged.

THE SEAL (why a draw cannot be rigged after the fact)
    The card-to-group mapping is decided ONCE at creation, shuffled server-side. Before anyone can
    pick, the board shows sha256(salt + mapping). At close the salt and the mapping are published,
    so anybody can recompute the hash and see that the mapping shown is the one that was sealed.
    The salt is never returned while the draw is open; with it, the mapping could be brute-forced
    from the hash (there are only so many arrangements).

FAIRNESS OF THE MAPPING
    Cards are dealt to groups as evenly as the seeder would (competitor i -> group i % g), then the
    ORDER of the cards is shuffled. So a 48-team stage with 4 groups always has exactly 12 cards per
    group; picking early or late changes nothing about which groups remain possible, only which
    numbers are still on the table.

HOW IT CONNECTS
    - Written by afc_draws/views.py (create / open / pick / close / reset).
    - Read by the public board (GET draws/<id>/board/) rendered on the event page
      (frontend GroupDrawBoard) and the organizer card on the edit page's Actions tab.
    - Guards the ordinary seeders: afc_tournament_and_scrims.views.seed_stage_competitors_to_groups
      and the team variant refuse while a draw for that stage is open (afc_draws.services.draw_is_open).
    - Phase 2 (AFC Seeds) adds rerolls and peeks as DrawAction rows against these cards; nothing
      here has to change for that.
"""
from django.conf import settings
from django.db import models


class StageDraw(models.Model):
    STATUS_DRAFT = "draft"      # cards dealt and sealed, nobody can pick yet
    STATUS_OPEN = "open"        # picks accepted until closes_at
    STATUS_CLOSED = "closed"    # every competitor placed; salt + mapping public
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    draw_id = models.AutoField(primary_key=True)
    # One draw per stage. Reset deletes the row (and its cards) rather than reusing it, so a
    # re-created draw gets a fresh seal.
    stage = models.OneToOneField(
        "afc_tournament_and_scrims.Stages", on_delete=models.CASCADE, related_name="draw",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    # The seal. commitment = sha256(salt + ":" + canonical JSON of [[card_number, group_id], ...]).
    # salt is only ever serialised once status is closed (see views.serialize_board).
    commitment = models.CharField(max_length=64)
    salt = models.CharField(max_length=64)

    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # What happens to whoever has not picked when the draw closes (owner 2026-09-12: "the
    # admin/organizers decides if they want to randomize the teams/players who did not pick").
    # True: they are dealt the remaining cards at random, as before. False: they stay unplaced in
    # the stage pool and the organizer seeds or moves them by hand; the board lists them.
    # Set when the draw opens, changeable while it is open (services.update_window).
    auto_place_at_close = models.BooleanField(default=True)
    # The last time the organizer sent "you have not picked yet" to the stragglers
    # (services.remind): one reminder per draw per REMIND_EVERY, so a nervous organizer cannot
    # spam a captain's inbox.
    last_reminder_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="draws_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "afc_draws_stagedraw"

    def __str__(self):
        return f"Draw {self.draw_id} for stage {self.stage_id} ({self.status})"


class DrawCard(models.Model):
    VIA_PICK = "pick"       # the competitor turned this card over themselves
    VIA_AUTO = "auto"       # dealt at close because they never picked
    VIA_CHOICES = [(VIA_PICK, "Picked"), (VIA_AUTO, "Auto-placed")]

    card_id = models.AutoField(primary_key=True)
    draw = models.ForeignKey(StageDraw, on_delete=models.CASCADE, related_name="cards")
    # 1-based, what the player sees on the board.
    number = models.PositiveIntegerField()
    # The group this card hides. Fixed at creation and part of the sealed mapping.
    stage_group = models.ForeignKey(
        "afc_tournament_and_scrims.StageGroups", on_delete=models.CASCADE, related_name="draw_cards",
    )

    # Who holds the card. Exactly one of tournament_team / player once taken; both NULL while the
    # card is still face down. Mirrors StageGroupCompetitor's team-or-player shape.
    tournament_team = models.ForeignKey(
        "afc_tournament_and_scrims.TournamentTeam", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="draw_cards",
    )
    player = models.ForeignKey(
        "afc_tournament_and_scrims.RegisteredCompetitors", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="draw_cards",
    )
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="draw_cards_picked",
    )
    picked_at = models.DateTimeField(null=True, blank=True)
    via = models.CharField(max_length=6, choices=VIA_CHOICES, null=True, blank=True)

    class Meta:
        db_table = "afc_draws_drawcard"
        constraints = [
            models.UniqueConstraint(fields=["draw", "number"], name="uniq_draw_card_number"),
            # A competitor holds at most one card per draw. MySQL treats NULLs as distinct inside a
            # unique index, so the untaken cards (both NULL) never collide with each other.
            models.UniqueConstraint(fields=["draw", "tournament_team"], name="uniq_draw_card_team"),
            models.UniqueConstraint(fields=["draw", "player"], name="uniq_draw_card_player"),
        ]
        ordering = ["number"]

    @property
    def is_taken(self) -> bool:
        return self.tournament_team_id is not None or self.player_id is not None

    def __str__(self):
        return f"Card {self.number} of draw {self.draw_id}"
