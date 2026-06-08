import uuid
from django.db import models
from afc_team.models import Team, TeamMembers
from django.conf import settings
from django.utils.text import slugify

# ---------------- Event ----------------
class Event(models.Model):
    COMPETITION_TYPE_CHOICES = [
        ("tournament", "Tournament"),
        ("scrims", "Scrims")
    ]

    PARTICIPANT_TYPE_CHOICES = [
        ("solo", "Solo"),
        ("duo", "Duo"),
        ("squad", "Squad")
    ]

    EVENT_TYPE_CHOICES = [
        ("internal", "Internal"),
        ("external", "External")
    ]

    EVENT_MODE_CHOICES = [
        ("virtual", "Online"),
        ("physical(lan)", "Physical(LAN)"),
        ("hybrid", "Hybrid")
    ]

    EVENT_STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("ongoing", "Ongoing"),
        ("completed", "Completed")
    ]

    TOURNAMENT_TIER_CHOICES = [
        ("tier_1", "Tier 1"),
        ("tier_2", "Tier 2"), 
        ("tier_3", "Tier 3")
    ]

    REG_RESTRICTION_CHOICES = [
        ("none", "No Restriction"),
        ("by_region", "By Region"),
        ("by_country", "By Country"),
    ]

    RESTRICTION_MODE_CHOICES = [
        ("allow_only", "Allow Only Selected"),
        ("block_selected", "Block Selected"),
    ]

    event_id = models.AutoField(primary_key=True)
    slug = models.SlugField(max_length=80, unique=True, blank=True, db_index=True, null=True)
    competition_type = models.CharField(max_length=10, choices=COMPETITION_TYPE_CHOICES)
    participant_type = models.CharField(max_length=10, choices=PARTICIPANT_TYPE_CHOICES)
    event_type = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)
    max_teams_or_players = models.PositiveIntegerField()
    event_name = models.CharField(max_length=40)
    event_mode = models.CharField(max_length=20, choices=EVENT_MODE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    registration_open_date = models.DateField()
    registration_end_date = models.DateField()
    prizepool = models.CharField(max_length=40)
    prizepool_cash_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    prize_distribution = models.JSONField(default=dict)
    event_rules = models.CharField(max_length=200)
    event_status = models.CharField(max_length=20, choices=EVENT_STATUS_CHOICES)
    registration_link = models.URLField()
    tournament_tier = models.CharField(max_length=20, choices=TOURNAMENT_TIER_CHOICES, default="tier_3")
    # rankings §4/§7.2 — prize money conversion locked at award date
    prize_currency = models.CharField(max_length=3, default="NGN")  # USD | NGN
    usd_to_ngn_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    prizepool_ngn_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    event_banner = models.ImageField(upload_to='event_banner/', null=True)
    number_of_stages = models.PositiveIntegerField()
    uploaded_rules = models.FileField(upload_to='event_rules/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_events', null=True, blank=True)
    # organizers: owning organization (null = native AFC event). SET_NULL so soft-deleting an
    # org re-homes its events to AFC instead of destroying tournaments/registrations/results.
    organization = models.ForeignKey("afc_organizers.Organization", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="events")
    # organizers integrity gate: an org-owned event's results only count toward the official
    # afc_rankings scores once an AFC admin verifies it. Native AFC events (organization=None)
    # are unaffected — aggregation only excludes org events where this is still False.
    rankings_verified = models.BooleanField(default=False)
    # partner API gate: only events an AFC admin has explicitly published are reachable
    # through the read-only partner API (afc_partner_api). Defaults off; AFC flips it.
    partner_published = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    is_draft = models.BooleanField(default=True)
    registration_restriction = models.CharField(
        max_length=20,
        choices=REG_RESTRICTION_CHOICES,
        default="none"
    )

    restriction_mode = models.CharField(
        max_length=20,
        choices=RESTRICTION_MODE_CHOICES,
        null=True, blank=True
    )

    # store what frontend picked
    # restricted_regions = models.JSONField(default=list, blank=True)   # ["West Africa", "Europe", ...]
    restricted_countries = models.JSONField(default=list, blank=True) # ["Nigeria", "Ghana", ...]

    is_public = models.BooleanField(default=True)
    is_sponsored = models.BooleanField(default=False)
    sponsor_name = models.CharField(max_length=100, null=True, blank=True)
    sponsor_requirement_description = models.CharField(max_length=200, null=True, blank=True)
    sponsor_field_label = models.CharField(max_length=100, null=True, blank=True)

    is_waitlist_enabled = models.BooleanField(default=False)
    waitlist_capacity = models.PositiveIntegerField(null=True, blank=True)
    waitlist_discord_role_id = models.CharField(max_length=100, null=True, blank=True)

    event_start_time = models.TimeField(null=True, blank=True)
    event_end_time = models.TimeField(null=True, blank=True)
    registration_start_time = models.TimeField(null=True, blank=True)
    registration_end_time = models.TimeField(null=True, blank=True)



    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.event_name)[:70] or "event"
            slug = base
            i = 2
            while Event.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)


class EventInviteToken(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invite_tokens")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_used = models.BooleanField(default=False)
    used_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="used_invite_tokens")
    used_at = models.DateTimeField(null=True, blank=True)
    # ── shared (reusable) invite link ──
    # A SHARED token (is_shared=True) is ONE reusable link that many people register
    # through. It is NEVER consumed: the register_for_event invite gate accepts it
    # regardless of is_used, and the post-registration "mark used" step skips it so it
    # stays open. FCFS is still enforced by the EXISTING capacity check
    # (active_count >= event.max_teams_or_players -> "Registration limit reached" /
    # waitlist): the first max_teams_or_players registrations through the shared link
    # take the slots, then the event is full and the link can no longer register anyone.
    # A NON-shared token (is_shared=False, the default) keeps today's single-use behavior:
    # it is consumed by the first successful registration (is_used=True) and rejected
    # afterwards.
    is_shared = models.BooleanField(default=False)


class SponsorEvent(models.Model):
    sponsor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

# ---------------- Stream Channels ----------------
class StreamChannel(models.Model):
    channel_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="stream_channels")
    channel_url = models.URLField()

# ---------------- Stages ----------------
class Stages(models.Model):
    STAGE_FORMAT_CHOICES = [
        ("br - normal", "Battle Royale - Normal"),
        ("br - roundrobin", "Battle Royale - Knockout"),
        # NOTE: "br - point rush" / "br - champion rush" used to be scoring *formats* here.
        # They are now per-stage TOGGLES (champion_point_enabled / point_rush_enabled below),
        # combinable with any bracket format, so they are no longer format choices.
        ("cs - normal", "Clash Squad - Normal"),
        ("cs - league", "Clash Squad - League"),
        ("cs - knockout", "Clash Squad - Knockout"),
        ("cs - double elimination", "Clash Squad - Double Elimination"),
        ("cs - round robin", "Clash Squad - Round Robin"),
        # BR Round-Robin (sub-project B): base groups A/B/C merge into game-day lobbies.
        # Distinct from the dead "br - roundrobin" (mislabelled "Knockout") entry above —
        # that one is left untouched for backward compatibility.
        ("br - round robin", "Battle Royale - Round Robin")
    ]

    STAGE_STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("ongoing", "Ongoing"),
        ("completed", "Completed")
    ]


    stage_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="stages")
    stage_name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    number_of_groups = models.PositiveIntegerField()
    stage_format = models.CharField(max_length=100, choices=STAGE_FORMAT_CHOICES)
    teams_qualifying_from_stage = models.PositiveIntegerField()
    stage_discord_role_id = models.CharField(max_length=100, null=True, blank=True)
    stage_status = models.CharField(max_length=20, choices=STAGE_STATUS_CHOICES, default="upcoming")
    prizepool = models.CharField(max_length=40, null=True, blank=True)
    prizepool_cash_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    prize_distribution = models.JSONField(default=dict,null=True, blank=True) # {"1": "50%", "2": "30%", "3": "20%"}
    is_finals_stage = models.BooleanField(default=False)  # rankings §4.5/§6.1 — admin marks the finals stage

    # ── Scoring-mode config (scoring-modes sub-project A). Both features are independent
    # and combinable per stage. They are computed ON READ in the standings builder
    # (nothing here persists derived points), matching how standings already work, so an
    # admin edit auto-corrects the leaderboard. See WEBSITE/tasks/scoring-modes-design.md. ──
    # Champion-Point: a stage is decided by a match-point WIN rule (first competitor to
    # Booyah while already at/over the threshold) rather than by summed points.
    champion_point_enabled = models.BooleanField(default=False)
    champion_point_threshold = models.PositiveIntegerField(null=True, blank=True)  # required when enabled
    # Point-Rush: this stage's per-lobby standings hand out a placement→bonus reward that
    # carries over into a LATER stage (point_rush_target_stage). on_delete=SET_NULL so
    # deleting the target stage just nulls the link, it does not cascade to the source.
    point_rush_enabled = models.BooleanField(default=False)
    point_rush_reward = models.JSONField(default=dict, blank=True)  # {"1":10,"2":7,...} placement→bonus
    point_rush_target_stage = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="point_rush_sources",  # target.point_rush_sources -> stages that feed it
    )

class StageGroups(models.Model):
    group_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="groups")
    group_name = models.CharField(max_length=50)
    playing_date = models.DateField()
    playing_time = models.TimeField()
    teams_qualifying = models.PositiveIntegerField()
    group_discord_role_id = models.CharField(max_length=100, null=True, blank=True)
    match_count = models.PositiveIntegerField()
    match_maps = models.JSONField(default=list)  # List of maps for the matches
    prizepool = models.CharField(max_length=40, null=True, blank=True)
    prizepool_cash_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    prize_distribution = models.JSONField(default=dict, null=True, blank=True) # {"1": "50%", "2": "30%", "3": "20%"}

    # ── BR Round-Robin (sub-project B): a StageGroups row doubles as a game-day LOBBY ──
    # For a round-robin stage, each game day is a lobby formed by MERGING base groups
    # (RoundRobinGroup). game_day numbers the day within the stage; source_groups records
    # which base groups were merged to fill this lobby. Both stay null/empty for every
    # other stage format, so nothing else changes. RoundRobinGroup is referenced by string
    # because it is declared after this class (forward reference).
    game_day = models.PositiveIntegerField(null=True, blank=True)
    source_groups = models.ManyToManyField("RoundRobinGroup", blank=True, related_name="lobbies")


# ---------------- Round-Robin Base Group ----------------
class RoundRobinGroup(models.Model):
    """Base group (A/B/C…) in a Round-Robin stage. Teams keep this identity; game-day
    lobbies are formed by merging base groups (see StageGroups.source_groups)."""
    group_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="round_robin_groups")
    label = models.CharField(max_length=20)
    order = models.PositiveIntegerField(default=0)
    teams = models.ManyToManyField("TournamentTeam", blank=True, related_name="round_robin_groups")

    class Meta:
        # Self-enforce A/B/C order everywhere groups are read (schedule generation,
        # standings, UI) so later tasks never have to re-sort by `order` by hand.
        ordering = ["order"]


# ---------------- Registered Competitors ----------------
class RegisteredCompetitors(models.Model):

    STATUS_CHOICES = [
    ("registered", "Registered"),
    ("disqualified", "Disqualified"),
    ("withdrawn", "Withdrawn"),
    ("left", "Left"),
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("rejected", "Rejected")
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    registration_date = models.DateTimeField(auto_now_add=True)
    user_id_from_sponsor = models.CharField(max_length=100, null=True, blank=True)
    is_waitlisted = models.BooleanField(default=False)


# ---------------- Leaderboard ----------------
class Leaderboard(models.Model):
    LEADERBOARD_METHOD_CHOICES = [
        ("manual", "Manual"),
        ("room_file_upload", "Room File Upload"),
        ("image_upload", "Image Upload")
    ]

    FILE_TYPE_CHOICES = [
        ("math_result_file", "Match Result File"),
        ("debugger_file", "Debugger File")
    ]

    leaderboard_id = models.AutoField(primary_key=True)
    leaderboard_name = models.CharField(max_length=120)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="leaderboards")
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="leaderboards")
    group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, null=True, blank=True, related_name="leaderboards")
    creation_date = models.DateField(auto_now=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    placement_points = models.JSONField(default=dict, blank=True)  
    # example: {"1": 12, "2": 9, "3": 8, ..., "10": 1}
    kill_point = models.FloatField(default=1.0)
    leaderboard_method = models.CharField(max_length=30, choices=LEADERBOARD_METHOD_CHOICES)
    file_type = models.CharField(max_length=30, choices=FILE_TYPE_CHOICES, null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("event", "stage", "group")

# ---------------- Matches & Stats ----------------
class Match(models.Model):
    match_id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE, related_name="matches", null=True, blank=True)
    group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, related_name="matches", null=True, blank=True)
    mvp = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="mvp_matches")
    match_date = models.DateTimeField(auto_now_add=True)
    # afc_rankings buckets stats by played_on (actual play date), NOT match_date
    # (auto_now_add). Backfill played_on for historical matches or they bucket into the
    # wrong month/quarter.
    played_on = models.DateField(null=True, blank=True)  # rankings: actual play date for month/quarter bucketing (match_date is entry date)
    match_number = models.PositiveIntegerField()
    room_id = models.CharField(max_length=50, null=True, blank=True)
    room_password = models.CharField(max_length=50, null=True, blank=True)
    room_name = models.CharField(max_length=100, null=True, blank=True)
    result_inputted = models.BooleanField(default=False)
    upload_method = models.CharField(max_length=30, null=True, blank=True)
    scoring_settings = models.JSONField(default=dict, blank=True)
    match_map = models.CharField(
        max_length=50,
        choices=[
            ('bermuda', 'Bermuda'),
            ('purgatory', 'Purgatory'),
            ('kalahari', 'Kalahari'),
            ('alpine', 'Alpine'),
            ('nexterra', 'Nexterra'),
            ('solara', 'Solara'),
        ]
    )

class TournamentTeam(models.Model):
    """
    Links a Team to a Tournament Event.
    """
    TEAM_STATUS = [
        ("active", "Active"),
        ("disqualified", "Disqualified"),
        ("withdrawn", "Withdrawn"),
        ("left", "Left"),
    ]
    tournament_team_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tournament_teams")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="tournament_entries")
    status = models.CharField(max_length=20, choices=TEAM_STATUS, default="active")
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    registration_date = models.DateTimeField(auto_now_add=True)
    country = models.CharField(max_length=100, null=True, blank=True) # Store country at time of registration for historical accuracy
    is_waitlisted = models.BooleanField(default=False)
    # rankings result markers — set by admin at result entry via afc_rankings.admin_results
    # (spec §4.4/§4.5/§5.1); consumed by afc_rankings.aggregation to award win/finals points.
    # result_finalized gates whether aggregation counts this event at all.
    is_tournament_winner = models.BooleanField(default=False)
    reached_finals = models.BooleanField(default=False)
    finals_appearances = models.PositiveIntegerField(default=0)
    result_finalized = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.team.team_name} in {self.event.event_name}"
    

class TournamentTeamMember(models.Model):
    """
    Members of the team for this tournament
    """
    TEAM_MEMBER_STATUS = [
        ("pending", "Pending"),
        ("active", "Active"),
        ("rejected", "Rejected"),
        ("approved", "Approved"),
    ]
    tournament_team = models.ForeignKey(TournamentTeam, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=TEAM_MEMBER_STATUS, default="active")
    user_id_from_sponsor = models.CharField(max_length=100, null=True, blank=True) # For sponsored events, to link user to sponsor's system
    reason = models.CharField(max_length=2000, null=True, blank=True)
        

    class Meta:
        unique_together = ("tournament_team", "user")

    def __str__(self):
        return f"{self.user.username} in {self.tournament_team.team.team_name}"

class TournamentTeamMatchStats(models.Model):
    """
    Stores stats per team in a match
    """
    team_stats_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="team_stats")
    tournament_team = models.ForeignKey(TournamentTeam, on_delete=models.CASCADE, related_name="match_stats")
    placement = models.PositiveIntegerField()
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)
    placement_points = models.PositiveIntegerField(default=0)
    kill_points = models.PositiveIntegerField(default=0)
    total_points = models.PositiveIntegerField(default=0)
    played = models.BooleanField(default=True)
    penalty_points = models.IntegerField(default=0) # ✅ -
    bonus_points = models.IntegerField(default=0)   # ✅ +

class TournamentPlayerMatchStats(models.Model):
    """
    Stores stats per player in a match (solo/duo/squad)
    """
    player_stats_id = models.AutoField(primary_key=True)
    team_stats = models.ForeignKey(TournamentTeamMatchStats, on_delete=models.CASCADE, related_name="player_stats")
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)
    played = models.BooleanField(default=True)



class EventPageView(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pageviews")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)  # if available
    ip_address = models.CharField(max_length=45, null=True, blank=True)
    viewed_at = models.DateTimeField(auto_now_add=True)

class SocialShare(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="social_shares")
    platform = models.CharField(max_length=50, null=True, blank=True) # facebook/twitter/whatsapp...
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class StageCompetitor(models.Model):
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="competitors")
    tournament_team = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.CASCADE)
    player = models.ForeignKey(RegisteredCompetitors, null=True, blank=True, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=20,
        choices=[("active", "Active"), ("disqualified", "Disqualified"), ("withdrawn", "Withdrawn")],
        default="active"
    )

    class Meta:
        unique_together = ("stage", "tournament_team", "player")


class StageGroupCompetitor(models.Model):
    stage_group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, related_name="competitors")
    tournament_team = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.CASCADE)
    player = models.ForeignKey(RegisteredCompetitors, null=True, blank=True, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=20,
        choices=[("active", "Active"), ("disqualified", "Disqualified"), ("withdrawn", "Withdrawn")],
        default="active"
    )

    class Meta:
        unique_together = ("stage_group", "tournament_team", "player")


# class PlacementPointSystem(models.Model):
#     leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE, related_name="point_system")
#     placement = models.PositiveIntegerField()  # 1,2,3...
#     points = models.PositiveIntegerField()

#     class Meta:
#         unique_together = ("leaderboard", "placement")


class SoloPlayerMatchStats(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="solo_stats")
    competitor = models.ForeignKey(RegisteredCompetitors, on_delete=models.CASCADE)
    placement = models.PositiveIntegerField()
    kills = models.PositiveIntegerField(default=0)

    placement_points = models.PositiveIntegerField(default=0)
    kill_points = models.PositiveIntegerField(default=0)

    bonus_points = models.IntegerField(default=0)   # ✅ +
    penalty_points = models.IntegerField(default=0) # ✅ -
    total_points = models.IntegerField(default=0)
    played = models.BooleanField(default=True)

    class Meta:
        unique_together = ("match", "competitor")

# # TournamentTeamMatchStats
# played = models.BooleanField(default=True)

# # TournamentPlayerMatchStats
# played = models.BooleanField(default=True)

# # SoloPlayerMatchStats
# played = models.BooleanField(default=True)


class MatchResultImage(models.Model):
    image_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="result_images")
    image = models.ImageField(upload_to='match_result_images/')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, null=True, blank=True)

    def __str__(self):
        return f"Result image for match {self.match_id}"


class EventPrizePayout(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="payouts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    tournament_team = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event", "user"]),
            models.Index(fields=["event", "tournament_team"]),
        ]

