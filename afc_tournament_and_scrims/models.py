import secrets
import uuid
from django.db import models
from afc_team.models import Team, TeamMembers
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify


# ── live overlay / capture-client token generator (owner 2026-07-01) ──────────────────────────────
# Single source of truth for minting the opaque, URL-safe keys used by the OBS live-leaderboard
# overlay (Event.overlay_token, a READ-only public key) AND the desktop capture client
# (EventUploadToken.token, a revocable WRITE key). secrets.token_urlsafe(32) yields ~43 URL-safe
# chars (well under the 64-char columns) with 256 bits of entropy, so a token can't be guessed.
# Used as the DEFAULT for EventUploadToken.token, and called explicitly by the overlay/upload token
# endpoints (afc_tournament_and_scrims.views) that ensure/rotate Event.overlay_token.
def _gen_overlay_token():
    return secrets.token_urlsafe(32)


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
    # Roster-edit window (owner 2026-06-15): organizers/admins can OPEN a time-boxed window that lets
    # team captains edit their EVENT roster (typically AFTER registration closes — e.g. a fix-up
    # period before the event). NULL or a PAST datetime = closed (normal registration-window rules
    # apply). A FUTURE datetime = open until then, after which it AUTO-CLOSES (a pure time comparison,
    # no cron). Capped server-side so it can never extend past end_date. Written by
    # set_roster_edit_window (POST events/<id>/roster-edit-window/); read as an extra allow-path in
    # edit_roster and surfaced in event-detail payloads as roster_edit_until + roster_edit_open for
    # the organizer/admin toggle and the team-facing roster UI.
    roster_edit_until = models.DateTimeField(null=True, blank=True)
    prizepool = models.CharField(max_length=40)
    prizepool_cash_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    prize_distribution = models.JSONField(default=dict)
    event_rules = models.CharField(max_length=200)
    event_status = models.CharField(max_length=20, choices=EVENT_STATUS_CHOICES)
    registration_link = models.URLField()
    tournament_tier = models.CharField(max_length=20, choices=TOURNAMENT_TIER_CHOICES, default="tier_3")
    # tier_overridden (owner 2026-06-30): True when a HEAD or SUPER admin manually set the tier,
    # which pins it so the automatic classifier (afc_rankings EventTierRule, run on create/edit via
    # afc_tournament_and_scrims.views.apply_event_tier) never overwrites the manual decision. False =
    # the tier is auto-classified from the event's prize/teams/format. Mirrors the rankings
    # TeamQuarterlyScore.tier_overridden pattern (a manual lock the recalc respects).
    tier_overridden = models.BooleanField(default=False)
    # rankings §4/§7.2 — prize money conversion locked at award date
    prize_currency = models.CharField(max_length=3, default="USD")  # USD | NGN (owner 2026-07-01: AFC enters prizes in USD, the platform base currency)
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
    # Manual-reopen guard (owner 2026-06-25): set True when an admin/organizer REOPENS a completed
    # event (reopen_event). It ONLY excludes the event from the DATE-based daily auto-complete sweep
    # (update_event_and_stage_statuses, views.py) so a reopened PAST-end event isn't re-completed
    # overnight. It does NOT block results-based auto-complete (maybe_autocomplete_event) or the manual
    # complete_event, so a reopened event still closes normally once its final results are (re)entered
    # or an admin marks it complete. Read nowhere on the user side.
    auto_complete_suppressed = models.BooleanField(default=False)
    # Per-event results visibility (owner 2026-06-29): organizers/admins can HIDE the public
    # standings until they're ready to reveal them (social-reveal timing). Defaults True so every
    # EXISTING event stays visible. When False, the two public detail endpoints
    # (get_event_details / get_event_details_not_logged_in) withhold each group's
    # overall_leaderboard (returned as []) and echo results_published=false; the admin/organizer
    # result surfaces (get_event_details_for_admin, get-group-leaderboard) are NOT gated, so staff
    # can still enter/manage results. Flipped via the set_results_visibility endpoint, surfaced by
    # the shared Event Actions tab (ActionsTab) on both the admin + organizer event-edit pages.
    results_published = models.BooleanField(default=True)
    # ── Live OBS overlay READ key (owner 2026-07-01) ──────────────────────────────────────────────
    # A public, read-only, rotatable key that authorizes the live-leaderboard OVERLAY feed
    # (events/overlay/feed/?token=...). Null until an organizer/admin first mints it via
    # events/<id>/overlay/token/ (see ensure_overlay_token). Because the token itself proves the
    # organizer chose to broadcast this event, the feed intentionally bypasses results_published
    # (the organizer's own stream shows their standings even before the public reveal) — but a
    # suspended-org event still 404s (_org_hidden). Generated with _gen_overlay_token (256-bit,
    # url-safe). CONNECTS TO: the overlay_feed endpoint (reader) + the FE OBS Browser Source URL.
    overlay_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    # ── Live overlay BROADCAST selection (owner 2026-07-01) ───────────────────────────────────────
    # Lets an organizer choose, ON THE WEBSITE, WHICH standings the live overlay shows, and COMBINE
    # groups/stages into a cumulative — WITHOUT touching OBS. A "follow broadcast" overlay link omits
    # ?stage=/?group=, so overlay_feed reads this selection each poll: switch it here and the overlay
    # updates within one poll. broadcast_scope drives which standings the feed builds:
    #   "group" -> broadcast_group_id's group standings (default, single lobby)
    #   "stage" -> CUMULATIVE across every group of broadcast_stage_id
    #   "event" -> CUMULATIVE across every group of every stage
    #   "custom"-> CUMULATIVE across the broadcast_group_ids list (arbitrary combination)
    # Explicit ?stage=/?group= in the overlay URL still OVERRIDE this (per-link pinning). Set via
    # events/<id>/broadcast/set/, read by events/<id>/broadcast/ (FE BroadcastControl) + overlay_feed.
    broadcast_scope = models.CharField(max_length=10, default="group")  # group|stage|event|custom
    broadcast_stage_id = models.PositiveIntegerField(null=True, blank=True)
    broadcast_group_id = models.PositiveIntegerField(null=True, blank=True)
    broadcast_group_ids = models.JSONField(default=list, blank=True)  # for scope="custom"
    # ── Event MVP config (owner 2026-07-02): {"criteria": ["kills","damage",...], "scope":
    #    "overall"|"winning_team"}. The ORDERED criteria act like tie-breakers (compare players on
    #    the 1st, ties fall to the 2nd, ...); scope picks the candidate pool (everyone vs only the
    #    event-winning team). Saved from the leaderboard "MVPs" tab; computed by views_mvp.event_mvp. ──
    mvp_config = models.JSONField(default=dict, blank=True)
    # ── Leaderboard TIE-BREAKERS (owner 2026-07-02): {"default": ["booyahs","kills",...],
    #    "stages": {"<stage_id>": [...]}, "groups": {"<group_id>": [...]}}. Ordered criteria applied
    #    AFTER effective_total when ranking teams — like maps, they apply to ALL, or per stage, or
    #    per group (group overrides stage overrides default; empty = the legacy hardcoded chain
    #    booyahs -> kills). Criteria keys: booyahs, kills, placement_points, kill_points, bonus,
    #    fewest_penalties, matches_played, mvp_count. Resolved by round_robin.apply_tie_breakers. ──
    tie_breakers = models.JSONField(default=dict, blank=True)
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
    # ── Per-event DISCORD requirement (owner 2026-06-22) ──────────────────────────────────────────
    # When require_discord is True, EVERY participant (the solo registrant, or ALL roster members of a
    # team) must have a connected Discord account AND be a member of discord_server_id before they can
    # register; register_for_event blocks otherwise with code "discord_required" (naming who fails).
    # discord_server_id blank => fall back to the global AFC guild (settings.DISCORD_GUILD_ID). NOTE:
    # the AFC bot must be a member of discord_server_id for the membership check to resolve. Set in the
    # create/edit event modals (admin + organizer), echoed by get_event_details +
    # get-event-details-for-admin, enforced in register_for_event. Independent of the discord ROLE ids
    # below (which only auto-assign a role to whoever already has Discord connected).
    require_discord = models.BooleanField(default=False)
    discord_server_id = models.CharField(max_length=100, null=True, blank=True)
    # The Discord INVITE LINK players must use to join the event's server (owner 2026-06-22). Required
    # in the create/edit modal when require_discord is turned on, and shown to EVERY user on the event
    # page so they can join before registering. The toggle is gated in the UI behind a "the AFC bot is
    # a member of discord_server_id" check (afc_auth.verify_bot_in_guild) so membership can be verified.
    discord_invite_link = models.CharField(max_length=255, null=True, blank=True)
    is_sponsored = models.BooleanField(default=False)
    sponsor_name = models.CharField(max_length=100, null=True, blank=True)
    sponsor_requirement_description = models.CharField(max_length=200, null=True, blank=True)
    sponsor_field_label = models.CharField(max_length=100, null=True, blank=True)

    is_waitlist_enabled = models.BooleanField(default=False)
    waitlist_capacity = models.PositiveIntegerField(null=True, blank=True)
    waitlist_discord_role_id = models.CharField(max_length=100, null=True, blank=True)
    # ── Waitlist slot-assignment MODE (owner 2026-06-17) ──────────────────────────────────────────
    # When a registered team/player no-shows, a waitlisted one takes the slot. This picks HOW the
    # organizer decides who:
    #   first_registered -> the earliest-registered waitlist entry is promoted (admin clicks "Promote next").
    #   fcfs_room        -> all waitlist teams get the room ID/PASS (released on the user event page for
    #                        fcfs_room events); they race into the in-game room, admin promotes whoever got in.
    #   manual_admin     -> admin/organizer hand-picks which waitlist entry is promoted.
    # AFC can't auto-detect attendance, so freeing a slot is always an admin/organizer action
    # (mark-no-show) and promotion is admin-triggered. Shown on the user event page so waitlisted
    # competitors know how slots are assigned. Default first_registered for backward compat.
    WAITLIST_MODE_CHOICES = [
        ("first_registered", "Earliest registered gets the slot"),
        ("fcfs_room", "First to join the room gets the slot"),
        ("manual_admin", "Organizer picks who gets the slot"),
    ]
    waitlist_mode = models.CharField(max_length=20, choices=WAITLIST_MODE_CHOICES, default="first_registered")

    event_start_time = models.TimeField(null=True, blank=True)
    event_end_time = models.TimeField(null=True, blank=True)
    registration_start_time = models.TimeField(null=True, blank=True)
    registration_end_time = models.TimeField(null=True, blank=True)

    # ── Check-in (owner 2026-07-04) ────────────────────────────────────────────────────────────
    # When enabled, every registered competitor must LOG IN and tap "check in" inside the window to
    # stay eligible; a squad is eligible only when ALL its registered players check in. Competitors
    # (or squads with any missing player) who do not check in by checkin_end are RELEGATED to the
    # waitlist (is_waitlisted=True) - see relegate_unchecked_competitors. The window must open AFTER
    # registration ends and close BEFORE the event starts (validated in set_event_checkin). Consumed
    # by: player_checkin (user taps), get_event_checkin_status (status), the admin/organizer event
    # edit Check-in settings, and the user event page's Check-in button. Records live in EventCheckIn.
    checkin_enabled = models.BooleanField(default=False)
    checkin_start = models.DateTimeField(null=True, blank=True)
    checkin_end = models.DateTimeField(null=True, blank=True)

    # IANA timezone of the person who created/last set the event's times (e.g.
    # "Africa/Lagos"), captured from the browser on create/edit (owner 2026-06-21).
    # The date/time fields above are stored as the HOST's wall-clock; pairing them
    # with this tz lets the frontend show BOTH the viewer's local time AND the host's
    # time with a label ("17:00 your time • 18:00 WAT"). Nullable for events created
    # before this field existed (the UI falls back to showing the raw time, no label).
    # Read by: get_event_details / get-event-details-for-admin -> EventDetailsWrapper
    # (lib/i18n/time.ts formatEventWindow). Written by: create_event / edit_event.
    timezone = models.CharField(max_length=64, null=True, blank=True)

    # ── Paid registration (feature "paid-events", 2026-06-08) ──────────────────────────────
    # registration_type: "free" keeps the current instant-register flow; "paid" means a
    # registration is only created AFTER the entry fee is paid. registration_fee is the entry
    # amount in registration_fee_currency (USD base; admin picks per event). The CHARGE + ESCROW
    # (funds held by the payment processor, e.g. Stripe Connect, and released to the organizer
    # by an AFC admin only after the event runs) is the separate payment phase. These three
    # fields are the create/edit + display layer: set in create_event / edit_event, shown on the
    # admin + organizer event forms, and read by the public event page to decide free-vs-paid
    # registration. For an organizer-owned event, the organizer must have accepted the paid-event
    # terms (afc_organizers.Organization.paid_terms_accepted_at) before a paid event is created.
    REGISTRATION_TYPE_CHOICES = [("free", "Free"), ("paid", "Paid")]
    registration_type = models.CharField(max_length=10, choices=REGISTRATION_TYPE_CHOICES, default="free")
    registration_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    registration_fee_currency = models.CharField(max_length=3, default="USD")
    # PER-COUNTRY payment rules for a PAID event (owner 2026-06-24). registration_fee above is the BASE
    # fee; this lets the creator set, per country, whether teams/players from that country pay or join
    # FREE, and optionally OVERRIDE the amount + currency for that country. A squad's country is its
    # derived Team.country; a solo registrant's is their User.country. Shape:
    #   { "default_pays": true|false,                  # unlisted countries: pay the base fee, or free?
    #     "countries": { "Nigeria": {"pays": true, "amount": "50.00", "currency": "NGN"},  # amount/currency optional -> base
    #                    "Ghana":   {"pays": false} } }
    # NULL / empty on a paid event == everyone pays the base fee (back-compatible with pre-2026-06-24
    # paid events). The single resolver is resolve_registration_fee() in views.py; never trust a
    # client-sent amount. Independent of the private-event invite-link gate (both apply).
    country_payment_rules = models.JSONField(null=True, blank=True)

    # ── Media registration criteria (owner 2026-06-12) ─────────────────────────────────────
    # Event creators (admins or organizers) can REQUIRE media before registration:
    #   require_team_logo     -> a TEAM registration is blocked until Team.team_logo is uploaded.
    #   require_esport_images -> every registering player (solo user, or each roster member of a
    #                            team registration) must have their ESPORT IMAGE uploaded
    #                            (afc_auth.UserProfile.esports_pic, replace-only - see
    #                            afc_auth.views.upload_esport_image).
    # Set in create_event / edit_event, shown as toggles on both event wizards, enforced in
    # register_for_event, and surfaced on the public event page so players know before trying.
    require_team_logo = models.BooleanField(default=False)
    require_esport_images = models.BooleanField(default=False)
    # ── Extra registration requirements (F3, owner 2026-06-19) ──────────────────────────────
    #   require_player_uid           -> every registering player (solo user, or each roster member
    #                                   of a team registration) must have their Free Fire UID set
    #                                   (afc_auth.User.uid non-empty). When ON, registration HARD-
    #                                   BLOCKS until every roster UID is filled (the inline UID
    #                                   prompt still lets them set it). When OFF, behaves as before.
    #   require_player_profile_image -> every registering player must have a PROFILE image uploaded
    #                                   (afc_auth.UserProfile.profile_pic) - distinct from the
    #                                   esports image gated by require_esport_images above.
    # Same lifecycle as require_team_logo/require_esport_images: set in create_event/edit_event,
    # toggles on both wizards, enforced in register_for_event (+ event_links qualification gate)
    # via the shared missing_registration_assets() helper, surfaced on the public event page.
    require_player_uid = models.BooleanField(default=False)
    require_player_profile_image = models.BooleanField(default=False)

    # ── Letter avatars (A-Z) registration requirement (feature #7, owner 2026-06-29) ──────────────
    # 0 = off (the default; every existing event is unaffected). When > 0, a team/player may only
    # register once the LETTERS available to them cover at least this many: for a team that is the
    # LIVE union of every roster member's afc_auth.User.letter_avatars PLUS the team's
    # afc_team.Team.manual_letter_avatars (never stored - mirrors Team.total_earnings); for a solo
    # registrant it is their own User.letter_avatars count. Enforced in register_for_event (which
    # returns a 403 {code:"letter_avatars_required", required, available_count, available_letters}
    # that the public tournament page surfaces with a deep link to the player/team letter editor).
    # Set in create_event / edit_event (parsed + clamped 0-26), echoed by get_event_details so the
    # admin/organizer Step1EventDetails toggle + the public event page can read it. The per-team
    # letter actually ASSIGNED for in-game use lives on TournamentTeam.assigned_letter below.
    min_letter_avatars = models.PositiveIntegerField(default=0)

    # ── Flagged-kill counting (owner 2026-06-16) ───────────────────────────────────────────
    # The match-log FILE upload (upload_team_match_result) credits a team's TOTAL kills from the
    # file, which includes any UID that played for the team but is NOT on its site roster (a
    # "ringer": reason not_on_roster / belongs_to_other_team). Each such player is recorded as a
    # MatchKillFlag. count_flagged_kills is the EVENT-WIDE default for whether those flagged kills
    # count toward the team's score: True (default) keeps today's behavior (count everything);
    # False drops every flagged player's kills from the team total. A per-flag override
    # (MatchKillFlag.count_kills) can force a specific flagged player in/out regardless of this
    # default. Set by admins + organizers (org_can_event); honored by _effective_team_kills, which
    # recomputes the stored team totals on upload AND whenever the toggle/override changes.
    count_flagged_kills = models.BooleanField(default=True)


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

    @property
    def roster_edit_open(self) -> bool:
        """True while the organizer/admin's roster-edit window is currently open: a roster_edit_until
        is set AND now is at/before it. Auto-closes once now passes it (no cron needed). Consumed by
        edit_roster (extra allow-path past registration close) and the event-detail payloads
        (the FE organizer/admin toggle + the team-facing roster UI)."""
        from django.utils import timezone as _tz
        return bool(self.roster_edit_until) and _tz.now() <= self.roster_edit_until


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
        # "paused" (owner 2026-06-13): a started stage an admin/organizer has paused. Set via
        # set_stage_status from the event Actions tab; toggles back to "ongoing" on resume.
        ("paused", "Paused"),
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

    # Explicit display order (owner 2026-06-15 reorder feature). Default 0 = "auto by date":
    # equal orders fall back to start_date then stage_id, so stages auto-sort chronologically
    # until an admin/organizer manually reorders them (which sets distinct orders that win).
    # Mirrors RoundRobinGroup.order. Set by create_event/edit_event (submit sequence) and by
    # the reorder-stages endpoint. Consumed by get_event_details + the standings builder.
    stage_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["stage_order", "start_date", "stage_id"]

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

    # Explicit display order (owner 2026-06-15 reorder feature). Default 0 = "auto by date/time":
    # equal orders fall back to playing_date, playing_time, then group_id. A manual reorder sets
    # distinct orders that override the chronological sort. Set by create_event/edit_event and the
    # reorder-groups endpoint; consumed by get_event_details + the standings builder.
    group_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["group_order", "playing_date", "playing_time", "group_id"]


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
    # No-show (owner 2026-06-17 waitlist): an active competitor the organizer marked absent, freeing a
    # slot a waitlisted competitor can take. Set via mark_no_show; excluded from active counts so the
    # waitlist promotion has room. Cleared if the team turns up after all (undo).
    is_no_show = models.BooleanField(default=False)


class EventCheckIn(models.Model):
    """One "I'm here" record: a registered user tapped Check-in for an event inside its check-in
    window (owner 2026-07-04). Presence of a row = that user is checked in. Written by
    views.player_checkin; read by get_event_checkin_status + relegate_unchecked_competitors (a squad
    counts as checked-in only when EVERY registered roster member has a row). One row per (event,
    user); the unique constraint also makes a double-tap idempotent."""
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="checkins")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_checkins")
    # The squad this user checked in FOR (null for a solo event), so the status view can group by team
    # without re-deriving the roster.
    tournament_team = models.ForeignKey(
        "TournamentTeam", on_delete=models.CASCADE, null=True, blank=True, related_name="checkins")
    checked_in_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "user")
        indexes = [models.Index(fields=["event", "user"])]


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
# Default per-map SCORING CONFIG (owner 2026-06-21): every new map (Match) starts pre-filled with the
# standard Battle-Royale ladder — 1 point/kill, no assist/damage bonus, placement 12/9/8/7/6/5/4/3/2/1 —
# so admins/organizers no longer fill it in per map. It is still fully editable per map (the Scoring
# Config tab -> POST /events/edit-match-scoring-config/ overwrites Match.scoring_settings), and "Apply
# to..." copies one map's config to others. Persisted as Match.scoring_settings; read by every scoring
# compute path as `match.scoring_settings or {}`. DEFAULT_PLACEMENT_POINTS is the single source of truth
# (mirror it in the frontend leaderboard editor's default form state).
DEFAULT_PLACEMENT_POINTS = {
    "1": 12, "2": 9, "3": 8, "4": 7, "5": 6, "6": 5, "7": 4, "8": 3, "9": 2, "10": 1,
}


def default_scoring_settings():
    """Fresh default Match.scoring_settings dict (a NEW object each call — required for a mutable
    JSONField default). The standard BR ladder above + 1 kill point, 0 assist, 0 damage."""
    return {
        "kill_point": 1,
        "points_per_assist": 0,
        "points_per_1000_damage": 0,
        "placement_points": dict(DEFAULT_PLACEMENT_POINTS),
    }


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
    # When room details were RELEASED to players (owner 2026-06-17). NULL = the admin/organizer has
    # entered room id/name/password but not yet posted them; a timestamp = they were broadcast to the
    # group (broadcast_to_group / broadcast_to_stage mode=room_details). get_event_details only shows
    # room creds to the group's registered competitors AFTER this is set, so the room appears on the
    # user-facing event page exactly when (and only when) the organizer posts it.
    room_details_released_at = models.DateTimeField(null=True, blank=True)
    result_inputted = models.BooleanField(default=False)
    upload_method = models.CharField(max_length=30, null=True, blank=True)
    scoring_settings = models.JSONField(default=default_scoring_settings, blank=True)
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
    # No-show (owner 2026-06-17 waitlist): team-side mirror of RegisteredCompetitors.is_no_show — the
    # organizer marked this active team absent, freeing a slot for a waitlisted team. See mark_no_show.
    is_no_show = models.BooleanField(default=False)
    # rankings result markers — set by admin at result entry via afc_rankings.admin_results
    # (spec §4.4/§4.5/§5.1); consumed by afc_rankings.aggregation to award win/finals points.
    # result_finalized gates whether aggregation counts this event at all.
    is_tournament_winner = models.BooleanField(default=False)
    reached_finals = models.BooleanField(default=False)
    finals_appearances = models.PositiveIntegerField(default=0)
    result_finalized = models.BooleanField(default=False)

    # PER-TEAM roster-edit allowance (owner 2026-06-24). The event-wide Event.roster_edit_until opens
    # roster editing for ALL teams; this opens it for THIS ONE team only — an admin/organizer can let a
    # specific team fix its roster (and its members fix IGN/UID) even when the event-wide window is
    # closed. Set via set_team_roster_edit_window; honoured by edit_roster (an allow-path that also
    # overrides the match-start results freeze while open) and by afc_auth._has_active_event_registration
    # (releases the identity lock for that team's members while open). Auto-closes by time, no cron.
    roster_edit_until = models.DateTimeField(null=True, blank=True)

    # ── Letter avatar assigned for THIS event (feature #7, owner 2026-06-29) ──────────────────────
    # The single A-Z letter an admin/organizer assigned to this registered team for in-game use in
    # this event (e.g. so every team flies a distinct letter banner). NULL = not yet assigned. It is
    # written by the assign_team_letter endpoint (POST events/assign-team-letter/) and echoed per team
    # in get_event_details.tournament_teams + the event-team-letters list, where the RegisteredTeamsTab
    # Assign-letter Select reads it and the SendNotificationModal "Letter assignments" broadcast
    # announces it. OWNER DECISION (Open Q g, 2026-06-29): a letter is UNIQUE per team per event - the
    # Meta UniqueConstraint below stops two teams in the same event holding the same letter (the
    # endpoint also guards it with a friendly 409). Reassigning a team to a new letter frees its old
    # one automatically (a single column update). Distinct from Event.min_letter_avatars, which is the
    # registration REQUIREMENT, not the per-team in-game assignment.
    assigned_letter = models.CharField(max_length=1, null=True, blank=True)

    class Meta:
        constraints = [
            # One letter per event: no two TournamentTeam rows in the same event may share the SAME
            # non-null assigned_letter. This is a PLAIN UniqueConstraint (no `condition=`) ON PURPOSE.
            #   • MySQL — the PRODUCTION database — IGNORES the partial-index `condition` on a
            #     UniqueConstraint, so the previous conditional form (condition=assigned_letter is not
            #     null) gave ZERO DB enforcement there: two teams in one event could be saved with the
            #     same letter straight through the ORM. It only ever worked on Postgres.
            #   • A plain unique index DOES enforce on MySQL. And because BOTH MySQL and Postgres allow
            #     MULTIPLE NULLs in a unique index, every unassigned team (assigned_letter = NULL) still
            #     coexists without colliding — only two NON-NULL teams sharing a letter in the same event
            #     are rejected at the DB level. So dropping the condition loses nothing and gains real
            #     MySQL enforcement.
            # The app-level 409 in assign_team_letter stays as the friendly first line of defence; this
            # constraint is the DB backstop that enforces Open Q (g) even on a direct/bulk write.
            models.UniqueConstraint(
                fields=["event", "assigned_letter"],
                name="uniq_assigned_letter_per_event",
            ),
        ]

    @property
    def roster_edit_open(self) -> bool:
        """True while THIS team's per-team roster-edit allowance is open (roster_edit_until set AND now
        is at/before it). Mirrors Event.roster_edit_open but scoped to one team. Auto-closes by time."""
        from django.utils import timezone as _tz
        return bool(self.roster_edit_until) and _tz.now() <= self.roster_edit_until

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
    # ── 3D-room rich stats (owner 2026-07-02, debugger-log ingest). ─────────────────────────────
    # Filled ONLY by the debugger-log backfill (debugger_ingest.py) or a future live-capture write —
    # the normal MatchResult upload has no such data, so these stay 0 for upload-only matches.
    # rich_stats_filled marks a row whose values REALLY came from a debugger log, so consumers (MVP
    # criteria, design columns, KDR) can tell "0 deaths" apart from "no data". Feeds the MVP
    # deaths/survival_time/headshots/kdr criteria + the design columns of the same names.
    deaths = models.PositiveIntegerField(default=0)
    knockdowns = models.PositiveIntegerField(default=0)
    headshots = models.PositiveIntegerField(default=0)
    revives_received = models.PositiveIntegerField(default=0)
    survival_seconds = models.PositiveIntegerField(default=0)
    rich_stats_filled = models.BooleanField(default=False)


class MatchKillFlag(models.Model):
    """A "ringer" found in a match-log FILE upload (owner 2026-06-16): a UID that played for a
    team but is NOT on that team's site roster, so its kills are flagged for admin/organizer
    review before they count toward the team's score.

    Created by upload_team_match_result for every flagged player (reason `not_on_roster` =
    UID on no roster for this event, or `belongs_to_other_team` = UID registered on a DIFFERENT
    team). Re-derived on every (idempotent) re-upload of the match (old rows for the match are
    cleared first). The team's stored TournamentTeamMatchStats.kills is computed as
    rostered-player kills PLUS the kills of flagged players that currently count, where "counts"
    = `count_kills` if set, else the event default `Event.count_flagged_kills`. Changing the
    event toggle or a per-flag `count_kills` recomputes the affected team totals via
    views._recompute_team_kills_for_event.

    Consumed by: views.upload_team_match_result (create), the flagged-players admin/organizer
    panel (list + per-player toggle), and the standings team-total recompute.
    """
    REASON_CHOICES = [
        ("not_on_roster", "Played for this team but is on no roster for this event"),
        ("belongs_to_other_team", "Played for this team but is registered on another team"),
        # NAME-MATCH reasons (owner 2026-06-29): created by upload_team_match_result when a file
        # player did NOT UID-match but their in-game NAME (ascii-folded, clan-tag-stripped) matches a
        # registered roster member. Both are created count_kills=False (explicit PENDING) so they need
        # an admin/organizer approval (set_match_kill_flag -> True) before their kills join the team
        # total. name_matched_uid_changed = matches a member of THIS team (UID just changed);
        # name_matched_other_team = matches a member registered on a DIFFERENT team.
        ("name_matched_uid_changed", "Name matches a roster member of this team but the UID differs"),
        ("name_matched_other_team", "Name matches a roster member registered on another team"),
    ]
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="kill_flags")
    # The team the ringer's kills were credited TO in the file (the block they appeared in).
    tournament_team = models.ForeignKey(TournamentTeam, on_delete=models.CASCADE,
                                        related_name="kill_flags")
    uid = models.CharField(max_length=64)          # Free Fire UID from the file
    name = models.CharField(max_length=120, blank=True)   # in-game name from the file
    kills = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=32, choices=REASON_CHOICES)
    # If the UID belongs to a registered user on ANOTHER team (belongs_to_other_team), link them
    # so the panel can show who they really are. Null for not_on_roster (no site user at all).
    registered_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL)
    # Per-flag override: True = always count this player's kills, False = never, NULL = follow the
    # event default (Event.count_flagged_kills). Admin/organizer sets it from the panel.
    count_kills = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One flag row per (match, team, uid): a re-upload clears the match's rows first, and a
        # ringer appears once per block, so this also guards against accidental duplicates.
        unique_together = ("match", "tournament_team", "uid")

    @property
    def effective_count(self) -> bool:
        """Whether this flagged player's kills currently count toward the team total: the per-flag
        override if set, else the owning event's count_flagged_kills default."""
        if self.count_kills is not None:
            return self.count_kills
        ev = self.tournament_team.event if self.tournament_team_id else None
        return bool(ev.count_flagged_kills) if ev else True


class UnmatchedTeamBlock(models.Model):
    """A team block from a match-log FILE upload whose in-game name matched NO registered team
    (owner 2026-06-30). Instead of silently dropping it, the upload PERSISTS it so an admin/organizer
    can attribute its result to a registered team (or leave it uncounted) from the SAME persistent
    panel that resolves ringer players - one place for every upload-attribution decision, no re-upload.

    Stores the block's placement + total kills (its KillScore) so attribution re-scores WITHOUT the
    file. When `attributed_team` is set, that team's TournamentTeamMatchStats for the match is created
    (with this block's placement if it had no row) and this `kills` is added to its total by
    _recompute_team_kills_for_event. NULL `attributed_team` = unresolved / "don't count" (nothing
    scored). Re-derived on every idempotent re-upload of the match (old rows cleared first); the
    admin's prior attribution is restored across a re-upload, mirroring MatchKillFlag's approval restore.

    Consumed by: views.upload_team_match_result (create + restore), get_event_flagged_kills (list),
    attribute_unmatched_team (set), _recompute_team_kills_for_event (scoring).
    """
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="unmatched_team_blocks")
    team_name = models.CharField(max_length=120)        # in-game team name from the file
    placement = models.PositiveIntegerField(default=0)  # the block's Rank in the file
    kills = models.PositiveIntegerField(default=0)      # the block's KillScore (team kill total)
    # The registered team an admin attributed this block to. NULL = unresolved (its points are NOT
    # counted). SET_NULL so removing a team from the event doesn't delete the upload record.
    attributed_team = models.ForeignKey(TournamentTeam, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="attributed_blocks")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One row per (match, in-game name): a re-upload clears the match's rows first; a team name
        # appears once per block.
        unique_together = ("match", "team_name")

    def __str__(self):
        return f"UnmatchedTeamBlock({self.team_name!r} m={self.match_id} -> {self.attributed_team_id})"


class EventUploadToken(models.Model):
    """A revocable, event-scoped WRITE key for the desktop capture client (owner 2026-07-01, live
    leaderboard spec §4). The capture app runs on the tournament observer PC and can't do an
    interactive Bearer login, so it authenticates result uploads with one of these tokens instead.

    Unlike the read-only Event.overlay_token (public, single, rotate-in-place), an upload token is:
      • WRITE-scoped — it ONLY authorizes upload_team_match_result for THIS event (see that view's
        alternative-auth branch), never any other endpoint or event.
      • Revocable + auditable — created_by records who granted it; `revoked` retires a leaked key
        without deleting the row (a rotate REVOKES the old + issues a new one), so the audit trail
        of who-issued-what survives.
    A request presenting the token acts AS created_by (the granting user's upload permission), so the
    event admin / organizer who minted it is accountable for what the capture PC posts.

    CONNECTS TO: minted/rotated by ensure_upload_token (events/<id>/upload/token/, gated like the
    overlay token — event admin OR org_can_event can_edit_events); consumed by
    upload_team_match_result (afc_tournament_and_scrims.views) which resolves ?token= / X-Upload-Token
    to a non-revoked row and authorizes as created_by.
    """
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="upload_tokens")
    token = models.CharField(max_length=64, unique=True, db_index=True, default=_gen_overlay_token)
    # Who granted this key (for audit); SET_NULL so removing the user keeps the token's history.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="event_upload_tokens")
    label = models.CharField(max_length=120, blank=True)   # optional human note ("Observer PC 1")
    revoked = models.BooleanField(default=False)           # retire a leaked/rotated key in place
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        state = "revoked" if self.revoked else "active"
        return f"EventUploadToken(event={self.event_id}, {state})"


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


# ════════════════════════════════════════════════════════════════════════════════════════════
# BRANCHING ADVANCEMENT ROUTING (feature #9, owner plan WEBSITE/tasks/advancement-routing-plan.md)
#
# WHAT THIS ADDS
#   Until now advancement was MANUAL + hardcoded-linear: advance_group_competitors_to_next_stage
#   (views.py) takes the top `StageGroups.teams_qualifying` of ONE group into the single stage that
#   follows it in display order, and advance_round_robin does the same off a stage's cumulative
#   table. There was no way to SPLIT a stage's finishers into DIFFERENT later stages (e.g. "top 1-8
#   of the Group Stage go to the Finals, 9-16 go to the Play-In"), or to skip a stage.
#
#   StageAdvancementRule is the additive primitive that makes that possible: each row says
#   "positions [position_from .. position_to] of <source_stage> (optionally restricted to one
#   <source_group>) advance into <target_stage>". A stage with one or more rows is in "branching
#   mode"; the PRESENCE of rules is the only mode signal (no boolean flag), so a legacy event with
#   ZERO rows behaves byte-identically (the old endpoints still serve it).
#
# HOW IT CONNECTS (trace end-to-end)
#   - Authored in the create/edit event wizards (StageModal / StageConfigModal "Advancement
#     routing" section) as a per-stage array of {position_from, position_to, source_group_index|
#     null, target_stage_index}. The FE sends INDICES (mirroring point_rush_target_index); the
#     backend resolves them to the FK rows in a SECOND PASS after every stage+group exists
#     (create_event / edit_event, views.py), exactly how Point-Rush targets are wired. Validated
#     pre-transaction by views._validate_advancement_rules (no cycles, no overlap, clamp).
#   - Echoed back (resolved to ids + display names) under each stage in get_event_details
#     (views.py) as `advancement_rules`, consumed by the public TournamentStructure branch chips
#     and by the edit form to rehydrate the rows.
#   - EXECUTED by afc_tournament_and_scrims.advancement_routing.route_stage_advancement(stage):
#     it builds the source standings per scope (group / stage-wide, reusing the canonical
#     round_robin._aggregate_team_standings for teams), slices [from-1:to], and seeds the winners
#     into each target_stage via StageCompetitor.get_or_create + the same Discord-role queue the
#     legacy advance uses. Fired by the events/advance-stage-by-rules/ endpoint (admins+orgs) from
#     the shared ActionsTab.
#   - CASCADE on both stage ends + the group end, so deleting any referenced stage/group drops the
#     rule (no dangling routing). teams_qualifying_from_stage / StageGroups.teams_qualifying are
#     KEPT untouched as the legacy default + the "Top N" display; rules OVERRIDE only when present.
# ════════════════════════════════════════════════════════════════════════════════════════════
class StageAdvancementRule(models.Model):
    """One branching-advancement edge: positions [position_from..position_to] of `source_stage`
    (optionally scoped to `source_group`) advance into `target_stage`.

    Ranges are 1-based and INCLUSIVE (position_from=1, position_to=8 -> the top 8). When
    `source_group` is null the ranking is the WHOLE stage's standings (stage-wide); when set it is
    that single group's standings. `target_stage` must be strictly LATER than `source_stage` in
    display order (no cycles) - enforced by views._validate_advancement_rules at author time.
    `order` keeps the author's row order for display + a stable apply sequence. See the module
    header above for the full data-flow."""
    id = models.AutoField(primary_key=True)
    # Both stage ends CASCADE: a rule is meaningless once either stage is gone (mirrors how
    # EventLink/EventQualification hang off their stages). The reverse accessors are named so a
    # stage can ask for BOTH the rules it feeds out of and the rules that feed into it.
    source_stage = models.ForeignKey(
        Stages, on_delete=models.CASCADE, related_name="advancement_rules")
    # null = stage-wide scope (rank across the whole source stage). When set, the rule ranks only
    # this group's standings. CASCADE so deleting the group drops its per-group rules.
    source_group = models.ForeignKey(
        StageGroups, null=True, blank=True, on_delete=models.CASCADE,
        related_name="advancement_rules_as_source")
    target_stage = models.ForeignKey(
        Stages, on_delete=models.CASCADE, related_name="advancement_rules_as_target")
    position_from = models.PositiveIntegerField()   # 1-based, inclusive
    position_to = models.PositiveIntegerField()     # inclusive (>= position_from)
    # Author row order (display + apply sequence). Mirrors RoundRobinGroup.order / Stages.stage_order.
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Stable order for the engine + the get_event_details echo + the public chips.
        ordering = ["source_stage_id", "order", "id"]
        indexes = [
            models.Index(fields=["source_stage"]),
            models.Index(fields=["target_stage"]),
        ]

    def __str__(self):
        scope = (f"group {self.source_group_id}" if self.source_group_id
                 else f"stage {self.source_stage_id}")
        return (f"{scope} #{self.position_from}-{self.position_to} -> "
                f"stage {self.target_stage_id}")


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


# ════════════════════════════════════════════════════════════════════════════════════════════
# EVENT LINKING / QUALIFICATION CHAINS (owner-approved design 2026-06-12,
# spec: WEBSITE/tasks/event-linking-design.md v2 + feedback round 1)
#
# An EventLink declares "the top N of SOURCE STAGE qualify into TARGET EVENT" - per STAGE, not
# per event, so one event can feed different targets from different stages (top 6 of Semis ->
# event A, top 2 of Finals -> event B). When the stage's standings settle, EventQualification
# rows are created (and auto-promoted into the target via the same rows register_for_event
# writes) - see afc_tournament_and_scrims.event_links for the endpoints + the promote logic.
# Everything (allow/reject/decline/replace) is UNDOable, and standings edited after a link
# fires surface as a diff + an in-app notification to the link's creator.
# ════════════════════════════════════════════════════════════════════════════════════════════
class EventLink(models.Model):
    """One per-stage qualification rule: top `qualify_count` of `source_stage` flow into
    `target_event`. Admins link any events; organizers only events of orgs they manage
    (both ends) - enforced in event_links.py, not here."""
    ROSTER_MODE_CHOICES = [
        ("copy", "Copy finishing roster"),
        ("captain_repick", "Captain re-picks"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),        # waiting on the stage's standings
        ("fired", "Fired"),          # qualifications created
        ("cancelled", "Cancelled"),
    ]

    id = models.AutoField(primary_key=True)
    source_event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="outbound_links")
    source_stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="qualification_links")
    target_event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="inbound_links")
    qualify_count = models.PositiveIntegerField(default=2)
    # False = qualifications land "pending" and an admin presses Promote per row.
    auto_promote = models.BooleanField(default=True)
    # Owner decision: the admin chooses per link whether the finishing roster is copied as-is
    # or the captain must confirm/edit it via the existing Edit Registration flow.
    roster_mode = models.CharField(max_length=20, choices=ROSTER_MODE_CHOICES, default="copy")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="active")
    # Snapshot of the stage's top-N at fire time ([{placement, team_id|user_id, name}]) so a
    # LATER standings edit can be diffed against what the link acted on (the "standings
    # edited" banner + the creator notification).
    fired_snapshot = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="event_links_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_stage", "target_event"], name="uniq_stage_target_link",
            ),
        ]

    def __str__(self):
        return f"top {self.qualify_count} of stage {self.source_stage_id} -> event {self.target_event_id}"


class EventQualification(models.Model):
    """One competitor's flow-through record on a fired link. `placement` is their finishing
    spot in the source stage; status walks pending -> promoted (registered in the target) or
    declined -> replaced. Every decision is UNDOable: prev_status/prev_note hold the state to
    restore, and undoing a promotion withdraws the registration it created."""
    STATUS_CHOICES = [
        ("pending", "Pending"),      # awaiting promote/allow (auto_promote off, window closed, or gate failed)
        ("promoted", "Promoted"),    # registered in the target
        ("declined", "Declined"),    # captain/admin declined; awaiting replacement choice
        ("replaced", "Replaced"),    # a replacement team was promoted in their place
        ("rejected", "Rejected"),    # admin rejected a window-bypassed pending row
    ]

    id = models.AutoField(primary_key=True)
    link = models.ForeignKey(EventLink, on_delete=models.CASCADE, related_name="qualifications")
    placement = models.PositiveIntegerField()
    # Squad links carry team; solo links carry user. The replacement flow may swap `team` for
    # the admin-picked replacement (the original is named in `note`).
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    note = models.CharField(max_length=255, blank=True)
    # What the promotion created in the target (withdrawn again on undo). Squad: the
    # TournamentTeam; solo: the RegisteredCompetitors row.
    promoted_tournament_team = models.ForeignKey(
        TournamentTeam, on_delete=models.SET_NULL, null=True, blank=True, related_name="qualification_source",
    )
    promoted_competitor = models.ForeignKey(
        RegisteredCompetitors, on_delete=models.SET_NULL, null=True, blank=True, related_name="qualification_source",
    )
    # One-step undo: the state before the last decision.
    prev_status = models.CharField(max_length=12, blank=True)
    prev_note = models.CharField(max_length=255, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="qualification_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["link", "placement"], name="uniq_link_placement"),
        ]

    def __str__(self):
        return f"#{self.placement} of link {self.link_id}: {self.status}"

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
    # AUTO-SYNCED payouts (owner 2026-07-02): derived from the event's prize_distribution + final
    # standings when the event completes (sync_event_prize_payouts), so the season's Prize Money
    # page + evaluation fill themselves. Manual rows (Add prize / an edited row) keep this False
    # and are NEVER touched by a re-sync; editing an auto row flips it manual.
    auto_synced = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event", "user"]),
            models.Index(fields=["event", "tournament_team"]),
        ]


class PlayerWinning(models.Model):
    """Individual player's share of an event prize payout (owner 2026-06-15).

    When an admin/organizer records a team prize (EventPrizePayout) for a winning TournamentTeam,
    that payout is split among the team's ACTIVE members and one PlayerWinning row is written per
    member, so the prize shows up in each player's OWN history/stats, not only the team's
    total_earnings. Distribution happens in afc_rankings.admin_prize.prize_create (the single place
    EventPrizePayout rows are created) and is re-derived (delete-then-recreate keyed on `payout`) if
    the payout changes, so re-saving a prize never double-counts a player.

    Connects to: EventPrizePayout (source, via `payout`), TournamentTeam (the winning team),
    Event, and User (the player). Surfaced on the player profile through afc_player stats
    (tournament_winnings) and consumed by the frontend players/[username] profile.
    """
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="player_winnings")
    tournament_team = models.ForeignKey(
        TournamentTeam, null=True, blank=True, on_delete=models.CASCADE, related_name="player_winnings")
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tournament_winnings")
    # Source payout this share was derived from. Delete-then-recreate by payout keeps it idempotent.
    payout = models.ForeignKey(
        EventPrizePayout, null=True, blank=True, on_delete=models.CASCADE, related_name="player_winnings")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # this player's share, NGN
    share_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event", "player"]),
            models.Index(fields=["player", "created_at"]),
        ]


class EventRegistrationPayment(models.Model):
    """Pay-to-register ESCROW record for a PAID event (feature "paid-events", Phase 1).

    The entry fee is charged via Stripe Checkout and HELD in AFC's Stripe balance (Stripe is the
    custodian, not the organizer, not AFC's bank). A registration is only allowed for a paid event
    once a row here is status="paid" (see register_for_event's paid guard), so a user who pays can
    always finish registering, even if they close the tab (their paid record persists).
    release_status tracks the escrow: "held" until an AFC admin RELEASES it (after the event runs)
    or REFUNDS it. The actual organizer transfer (Stripe Connect) is a later phase; release here
    records the decision. Mirrors afc_shop.Order's Stripe fields.

    Consumed by afc_tournament_and_scrims/event_payments.py (init / verify / webhook / admin
    list+release+refund) and the register_for_event paid guard. The FE registration modal inits a
    payment, redirects to Stripe Checkout, then completes registration on return.
    """
    STATUS_CHOICES = [("pending", "Pending"), ("paid", "Paid"), ("failed", "Failed"), ("refunded", "Refunded")]
    RELEASE_CHOICES = [("held", "Held"), ("released", "Released"), ("refunded", "Refunded")]

    payment_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registration_payments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_registration_payments")
    team = models.ForeignKey("afc_team.Team", on_delete=models.SET_NULL, null=True, blank=True)  # duo/squad payer's team
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    provider = models.CharField(max_length=20, default="stripe")            # stripe | paystack (future)
    # Stripe handles (test or live depending on env). session = the Checkout Session we redirect to;
    # payment_intent = the underlying charge (used for refunds).
    stripe_session_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    stripe_payment_intent = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    release_status = models.CharField(max_length=12, choices=RELEASE_CHOICES, default="held")
    paid_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="released_event_payments")
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event", "user"]),
            models.Index(fields=["status"]),
            models.Index(fields=["release_status"]),
        ]

    def __str__(self):
        return f"EventRegistrationPayment({self.event_id} {self.user_id} {self.amount}{self.currency} {self.status})"


# ════════════════════════════════════════════════════════════════════════════════════════════
# CLASH-SQUAD HEAD-TO-HEAD BRACKET (bracket sub-project C; D bridge lives in head_to_head.py)
#
# Until now every "cs - ..." stage_format was DECORATIVE: all results flowed through the
# BR-shaped TournamentTeamMatchStats (placement + kills) and no head-to-head model existed.
# HeadToHeadMatch is the first real H2H primitive: ONE row = one Clash Squad set between two
# TournamentTeam rows, with explicit advancement wiring (next_match / loser_next_match), so a
# knockout / double-elimination / league bracket is just a linked set of these rows.
#
# HOW IT CONNECTS
#   - Generated + advanced by afc_tournament_and_scrims/head_to_head.py
#     (generate_bracket / report_result / standings / write_placement_stats).
#   - Served by afc_tournament_and_scrims/head_to_head_views.py:
#       POST events/stages/<stage_id>/bracket/generate/   (admin/organizer)
#       GET  events/stages/<stage_id>/bracket/            (public bracket tree + standings)
#       POST events/h2h-matches/<match_id>/result/        (admin/organizer)
#   - Feeds the EXISTING leaderboard + afc_rankings pipelines indirectly: when a bracket
#     completes, head_to_head.write_placement_stats() writes one synthetic
#     TournamentTeamMatchStats row per team (placement only, 0 kills) into a synthetic Match
#     (match_number=0) so nothing downstream has to learn about this model.
#   - Hangs off the same Stages row the rest of the engine uses; a stage either runs BR
#     lobbies (StageGroups/Match) or an H2H bracket (these rows). The two coexist only via
#     the synthetic results match above.
# ════════════════════════════════════════════════════════════════════════════════════════════
class HeadToHeadMatch(models.Model):
    """One head-to-head Clash Squad match inside a bracket stage.

    score_a / score_b are ROUND WINS within the CS set (e.g. 4-2), not kills. winner is
    denormalized for cheap reads. Advancement is explicit: when this match completes, the
    winner is copied into next_match's slot (next_match_slot) and, in double elimination,
    the loser is copied into loser_next_match's slot. A match with one team and a slot that
    can never fill (no feeder left) is a BYE: auto-completed at generation/report time with
    winner = the present team and score 0-0 (see head_to_head._resolve_byes)."""

    BRACKET_CHOICES = [
        ("winners", "Winners bracket"),   # single-elim rounds, double-elim upper bracket,
                                          # AND the grand final (round = winners rounds + 1)
        ("losers", "Losers bracket"),     # double elimination lower bracket
        ("league", "League / round robin"),  # every-pair-once formats; no advancement links
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("live", "Live"),
        ("completed", "Completed"),
    ]
    SLOT_CHOICES = [("a", "Slot A"), ("b", "Slot B")]

    h2h_match_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="h2h_matches")
    # 1 = first round of its bracket side. In double elimination the grand final lives in
    # bracket="winners" at round (winners rounds + 1) - convention documented in head_to_head.py.
    round_number = models.PositiveIntegerField(default=1)
    bracket = models.CharField(max_length=10, choices=BRACKET_CHOICES, default="winners")
    # Slot index of this match WITHIN its (bracket, round): 0, 1, 2... drives the pairing
    # math (match p of round r feeds match p//2 of round r+1) and the FE's vertical order.
    position = models.PositiveIntegerField(default=0)

    # The two competitors. Null = slot not yet filled (waiting on a feeder match) or a bye.
    # SET_NULL so withdrawing/deleting a TournamentTeam vacates the slot instead of tearing
    # the bracket tree down.
    team_a = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="h2h_matches_as_a")
    team_b = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="h2h_matches_as_b")
    score_a = models.PositiveIntegerField(default=0)  # round wins for team_a in the CS set
    score_b = models.PositiveIntegerField(default=0)  # round wins for team_b
    winner = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="h2h_match_wins")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    # ── advancement wiring (set once at generation, then read-only) ──
    # Winner advances into next_match at next_match_slot ("a" -> team_a, "b" -> team_b).
    next_match = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="feeder_matches")
    next_match_slot = models.CharField(max_length=1, choices=SLOT_CHOICES, null=True, blank=True)
    # Double elimination only: the loser drops into the losers bracket here.
    loser_next_match = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="loser_feeder_matches")
    loser_next_match_slot = models.CharField(max_length=1, choices=SLOT_CHOICES, null=True, blank=True)

    # Optional schedule the admin can fill in later (parallels StageGroups.playing_date/time).
    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_time = models.TimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Stable tree order for any reader; the views additionally group by bracket side.
        ordering = ["round_number", "position", "h2h_match_id"]
        indexes = [
            models.Index(fields=["stage", "bracket", "round_number"]),
        ]

    def __str__(self):
        a = self.team_a.team.team_name if self.team_a else "?"
        b = self.team_b.team.team_name if self.team_b else "?"
        return f"H2H {self.bracket} R{self.round_number}.{self.position}: {a} vs {b} ({self.status})"



# ── No-show reputation (F1, owner 2026-06-19) ──────────────────────────────────────────────────
class NoShowRecord(models.Model):
    """One NO-SHOW occurrence (a team OR a solo player) in one event.

    Powers the repeat-no-show WARNING: a team/player is "flagged" when it has >= 2 records that are
    still standing (cleared_at IS NULL) with occurred_at within a trailing 7 days, counted across
    ALL events (platform-wide, so any organizer/admin sees the warning). Created when an organizer/
    admin marks a no-show (afc_tournament_and_scrims.views.mark_no_show) or confirms a
    detect-no-shows suggestion; SOFT-CLEARED (cleared_at set) when the no-show is undone, so the
    warning reflects only currently-standing no-shows (history is retained for audit).

    team xor user is populated per the event's participant type (team events -> team; solo -> user).
    Read by: get_no_show_warnings (bulk badge endpoint consumed by the FE NoShowWarningBadge +
    useNoShowWarnings hook on RegisteredTeamsTab and the admin Teams list)."""
    SOURCE_CHOICES = [("manual", "manual"), ("auto", "auto")]
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="no_show_records")
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.CASCADE, related_name="no_show_records"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="no_show_records",
    )
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="manual")
    occurred_at = models.DateTimeField(default=timezone.now)
    # Soft-clear on undo (null = still standing). Keeps the audit trail while dropping the count.
    cleared_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["team", "cleared_at", "occurred_at"]),
            models.Index(fields=["user", "cleared_at", "occurred_at"]),
        ]

    def __str__(self):
        who = (self.team.team_name if self.team_id else
               (self.user.username if self.user_id else "?"))
        return f"NoShow {who} @ {self.event_id} ({'cleared' if self.cleared_at else 'standing'})"


class EventOverlay(models.Model):
    """One SAVED, NAMED broadcast overlay of an event (owner 2026-07-02, overlay studio v2).

    The owner's model: an "overlay" is a persistent entity you CREATE from a design (or as a scene
    like the countdown timer), NAME/RENAME, DUPLICATE, DELETE — and whose public link NEVER changes.
    The link (/overlay/view/<Event.overlay_token>/<id>) polls the public config feed, so editing the
    overlay's design/stage/group/animations from the studio updates what the SAME link renders live —
    the operator never re-copies a URL into OBS.

    kind:   "leaderboard" (renders a design + live standings) | "timer" (countdown scene).
    config: freeform per kind —
      leaderboard: {design_id, follow (bool), stage_id, group_id, anim, reveal, interval, size, live}
      timer:       {end_at (ISO), label}
    active: scenes (timer) toggle visibility with it; leaderboard overlays ignore it (always render).

    CONNECTS TO: views_overlays.py (CRUD via the broadcast gate + the public config feed) <-
    FE studio app/(a)/a/overlays/[eventId] (cards) + renderer app/overlay/view/[token]/[overlayId].
    """
    KINDS = (("leaderboard", "Leaderboard"), ("timer", "Timer"), ("booyah", "Booyah banner"), ("h2h", "Head to head"))

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="overlays")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=KINDS, default="leaderboard")
    config = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.event_id}:{self.name} ({self.kind})"


class EventMediaOptOut(models.Model):
    """Per-EVENT broadcast-media suppression (owner 2026-07-02): a team can remove its LOGO, or a
    player their ESPORT IMAGE, from one event's overlays/graphics without deleting the upload.
    One row = one suppression. CONNECTS TO: views_media_audit.py (created/removed there; the audit
    lists them) -> _overlay_rows_from_standings + future versus/H2H feeds skip suppressed media."""
    KINDS = (("team_logo", "Team logo"), ("esports_image", "Player esport image"))

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="media_opt_outs")
    kind = models.CharField(max_length=20, choices=KINDS)
    team = models.ForeignKey("afc_team.Team", null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "kind", "team", "user")


class MediaFlag(models.Model):
    """A 'bad media' flag (owner 2026-07-02): an admin/organizer tags a team logo or a player's
    esport image as needing replacement; the owner is notified (afc_auth.Notifications) and the flag
    stays open until resolved. CONNECTS TO: views_media_audit.py (create/list/resolve) -> the
    media-audit card on the overlay studio; notification deep-links via target_type/target_id."""
    KINDS = (("team_logo", "Team logo"), ("esports_image", "Player esport image"))

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="media_flags")
    kind = models.CharField(max_length=20, choices=KINDS)
    team = models.ForeignKey("afc_team.Team", null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.CASCADE, related_name="media_flags_received")
    reason = models.CharField(max_length=200, blank=True, default="")
    flagged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, related_name="media_flags_raised")
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
