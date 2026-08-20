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
    # team captains edit their EVENT roster (typically AFTER registration closes - e.g. a fix-up
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
    # ── What the tournament IS, in the organizer's own words (owner 2026-08-05, backlog item 26) ──
    # Deliberately NOT the same thing as event_rules above, which is why it is a separate column
    # rather than a longer event_rules. Rules answer "what will get you disqualified" and are
    # already served two ways (this 200-char field, or an uploaded PDF in uploaded_rules). Nothing
    # on the event answered "what is this tournament, who is it for, what is the story", so an
    # organizer had nowhere to write it and players read a page of dates and numbers.
    #
    # TextField, not CharField, because event_rules' 200-char ceiling is exactly the limitation
    # being fixed: a paragraph or two is the point. Blank by default, so every existing event
    # keeps rendering as it does today and the About block simply does not appear.
    #
    # Written by create_event / edit_event (Basic Info tab + create wizard step 1), echoed by BOTH
    # public detail builders (get_event_details and get_event_details_not_logged_in) and run
    # through the translate-on-read layer beside event_name / event_rules, so a French or
    # Portuguese visitor reads it in their own language. Rendered by the public tournament page
    # (EventDetailsWrapper, the "About this tournament" block).
    event_description = models.TextField(blank=True, default="")
    event_status = models.CharField(max_length=20, choices=EVENT_STATUS_CHOICES)
    registration_link = models.URLField()
    tournament_tier = models.CharField(max_length=20, choices=TOURNAMENT_TIER_CHOICES, default="tier_3")
    # tier_overridden (owner 2026-06-30): True when a HEAD or SUPER admin manually set the tier,
    # which pins it so the automatic classifier (afc_rankings EventTierRule, run on create/edit via
    # afc_tournament_and_scrims.views.apply_event_tier) never overwrites the manual decision. False =
    # the tier is auto-classified from the event's prize/teams/format. Mirrors the rankings
    # TeamQuarterlyScore.tier_overridden pattern (a manual lock the recalc respects).
    tier_overridden = models.BooleanField(default=False)
    # rankings §4/§7.2 - prize money conversion locked at award date
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
    # are unaffected - aggregation only excludes org events where this is still False.
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
    # (the organizer's own stream shows their standings even before the public reveal) - but a
    # suspended-org event still 404s (_org_hidden). Generated with _gen_overlay_token (256-bit,
    # url-safe). CONNECTS TO: the overlay_feed endpoint (reader) + the FE OBS Browser Source URL.
    overlay_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    # ── Live overlay BROADCAST selection (owner 2026-07-01) ───────────────────────────────────────
    # Lets an organizer choose, ON THE WEBSITE, WHICH standings the live overlay shows, and COMBINE
    # groups/stages into a cumulative - WITHOUT touching OBS. A "follow broadcast" overlay link omits
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
    #    AFTER effective_total when ranking teams - like maps, they apply to ALL, or per stage, or
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

    # ── Fully-automatic events (owner 2026-07-04) ──────────────────────────────────────────────
    # When auto_seed_on_start is on, the daily status sweep AUTO-SEEDS the event's AVAILABLE teams
    # (registered + not waitlisted; and, if check-in is on, only checked-in-eligible squads) into the
    # entry stage's groups the moment the event's start instant passes - so the organizer only has to
    # enter each group's room ID + PASS. auto_seeded_at stamps when that ran so it never re-seeds (and
    # it is skipped if the stage was already seeded manually). See views_autoseed.run_auto_seed.
    auto_seed_on_start = models.BooleanField(default=False)
    auto_seeded_at = models.DateTimeField(null=True, blank=True)

    # WHAT SETS IT OFF (owner 2026-08-05). The switch above used to imply "when the event starts",
    # which is one organizer's answer, not everybody's. A qualifier that wants its groups drawn the
    # moment registration shuts should not have to wait for the start whistle, and an event running
    # check-in wants the draw AFTER the no-shows have been swept out, or it seeds teams that never
    # turned up.
    #
    #   event_start        when the event's start instant passes. The default, and what every
    #                      existing event already does, so nothing changes underneath anybody.
    #   registration_close when registration closes. Earlier, and it gives the organizer time to
    #                      look at the draw before the event begins.
    #   checkin_close      when the check-in window closes. Only meaningful when check-in is ON;
    #                      falls back to the event start when it is not, because a trigger that
    #                      never fires would silently mean "never seed".
    AUTO_SEED_TRIGGER_CHOICES = [
        ("event_start", "When the event starts"),
        ("registration_close", "When registration closes"),
        ("checkin_close", "When check-in closes"),
    ]
    auto_seed_trigger = models.CharField(
        max_length=24, choices=AUTO_SEED_TRIGGER_CHOICES, default="event_start")

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
    # ── WhatsApp-number registration requirement (owner 2026-08-03) ─────────────────────────
    #   require_whatsapp -> every registering player (solo registrant, or each roster member of a
    #                       team registration) must have a WhatsApp number saved on their profile
    #                       (afc_auth.UserProfile.whatsapp_number non-empty).
    # WHY: AFC pushes room ID / password over WhatsApp, but only ~90 of ~6,790 players have a usable
    # number on file, so those messages reach almost nobody. Instead of nagging every player at
    # registration, an event that actually RELIES on WhatsApp room details can demand a number up
    # front. Read the profile through afc_auth.canonical_profile semantics (lowest profile_id):
    # UserProfile.user is a plain FK and duplicate rows exist in prod.
    # Same lifecycle as the require_* flags above: set in create_event / edit_event, toggles on both
    # wizards, carried by clone_event, enforced in register_for_event (+ the event_links
    # qualification gate) via the shared _missing_registration_assets() helper, and surfaced to
    # players on the public event page (EventRequirementsCard) plus as a per-player red badge in the
    # registration roster-requirements panel (EventDetailsWrapper.memberMissingRequirements).
    require_whatsapp = models.BooleanField(default=False)

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

    # ── Teams submit their own per-map results (owner 2026-08-04, backlog item 6) ──────────
    # OFF by default, and that default is the whole point: on a normal event the organizer
    # enters results and nothing changes for them. On a large event with many groups, typing
    # every map is the bottleneck, and the organizer is usually transcribing screenshots the
    # teams sent them anyway. Turning this on lets each team submit ITS OWN row for a map,
    # which the organizer then approves before it counts for anything.
    #
    # Nothing a team submits reaches the standings on its own: a submission is a proposal
    # (TeamMapResultSubmission) until an organizer approves it, and approval writes through
    # the same code an organizer's own entry uses (result_writes.write_team_result_row).
    # Read by views_team_submissions on every submit, so switching it off stops new
    # submissions immediately while leaving already-approved results alone.
    allow_team_result_submissions = models.BooleanField(default=False)


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


# ── TEAM INVITATIONS TO AN EVENT (owner backlog item 34, 2026-08-06) ─────────────────────────────
class EventTeamInvitation(models.Model):
    """One ASK: "we would like your team in this event", which the team must ACCEPT or DECLINE.

    THE ITEM IN THE OWNER'S WORDS
        "Invite teams to an event as a distinct invitation type they must accept or decline."

    WHY THIS IS A NEW TABLE AND NOT A FLAG ON SOMETHING THAT EXISTS
        AFC already had two things that look adjacent and are not this:
          * add_teams_to_event (POST events/add-teams-to-event/) FORCE-registers a team. Nobody on
            the team is asked, nobody can say no, and the team is in the bracket the same second.
          * EventInviteToken (above) is a LINK for a private event. It carries no addressee, so it
            cannot be listed as "who did we invite", it cannot be declined, and it cannot tell an
            organizer why a team said no.
        An invitation is a conversation with a named team that has a state, so it needs a row of
        its own: who asked, which team, what they said, and why.

    HOW ACCEPTING WORKS (the important part)
        Accepting does NOT write registration rows here. It replays the captain's answer through
        the ORDINARY registration endpoint (views.register_for_event) - see
        event_invites._register_through_the_normal_path - so an invited team passes exactly the
        same gates a self-registering team passes (roster size, staff exclusion, bans, per-player
        profile requirements, Discord, letter avatars, country restriction, organizer blacklist,
        capacity/waitlist, closed window, already-registered) and gets exactly the same error text
        when one of them refuses. An invitation is a shortcut to the FRONT of the queue, never a
        way around the door.

    HOW IT CONNECTS
        - Written + read by afc_tournament_and_scrims/event_invites.py (all six endpoints).
        - Accept path -> views.register_for_event -> RegisteredCompetitors + TournamentTeam +
          TournamentTeamMember (the same rows a normal registration creates).
        - Notifies through afc_auth.Notifications with target_type/target_id set, so the captain's
          "Take me there" opens their team page and the inviter's opens the event.
        - Frontend: the organizer/admin side is EventTeamInvitesCard.tsx (inside the shared
          RegisteredTeamsTab); the team side is EventInvitationsCard.tsx on the team page.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),        # sent, waiting on the team
        ("accepted", "Accepted"),      # the team registered through register_for_event
        ("declined", "Declined"),      # the team said no (decline_reason may say why)
        ("cancelled", "Cancelled"),    # the inviter took it back before it was answered
        ("expired", "Expired"),        # expires_at passed with nobody answering
    ]

    id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="team_invitations")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="event_invitations")
    # SET_NULL, not CASCADE: an organizer's account being deleted must not silently erase the
    # invitations they sent, because the team may already have accepted one and be in the bracket.
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="event_team_invitations_sent",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    # Optional note from the inviter ("we saved you a slot in the Lagos qualifier"). Shown to the
    # team verbatim, so it is length-capped rather than a TextField.
    message = models.CharField(max_length=280, blank=True, default="")
    # Optional reason the team gave when declining. The whole point of a decline over silence is
    # that the organizer learns WHY, so it is surfaced on the organizer's invitation list.
    decline_reason = models.CharField(max_length=280, blank=True, default="")
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="event_team_invitations_answered",
    )
    # PRIVATE events only. register_for_event demands an invite_token when Event.is_public is
    # False, so an invitation to a closed event would be impossible to accept without one. Rather
    # than teaching the registration path a second way in (which is exactly the bypass this
    # feature must not create), creating the invitation MINTS a single-use EventInviteToken and
    # the accept replays it. Public events leave this NULL. Cancelling deletes the token so a
    # withdrawn invitation cannot still let the team in.
    invite_token = models.ForeignKey(
        EventInviteToken, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="team_invitation",
    )
    # The send this row belongs to (owner 2026-08-08). NULL on every row written before campaigns
    # existed, and on nothing since: the create endpoint always makes a campaign now. Readers treat
    # a NULL campaign as "per_team", which is exactly what those older rows are, so no backfill is
    # needed. Declared as a string because EventInvitationCampaign is defined below this class.
    # SET_NULL rather than CASCADE: deleting a campaign must not delete the record of which teams
    # answered it, since some of them are in the bracket by then.
    campaign = models.ForeignKey(
        "EventInvitationCampaign", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    # Optional deadline. NULL (the default) means the invitation stands until the event's own
    # registration window closes, which register_for_event enforces anyway on accept.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["event", "status"]),   # organizer list: this event's invitations
            models.Index(fields=["team", "status"]),    # captain list: my team's invitations
        ]
        # NO UniqueConstraint for "one PENDING invitation per (event, team)". Expressing that needs
        # a CONDITIONAL unique index (condition=Q(status="pending")), and MySQL - the database this
        # project runs on - has no partial indexes, so Django would silently skip creating it and
        # the guarantee would be a comment pretending to be a constraint. The rule is enforced in
        # event_invites.create_team_invitations instead, inside the same transaction that creates
        # the rows. (Same reasoning the Invite.accepted_user_ids comment in afc_team/models.py
        # records for its own MySQL workaround.)

    def is_expired(self):
        """True when a deadline was set and it has passed. Callers flip such rows to 'expired' on
        read (a lazy sweep in the two list endpoints) rather than needing a scheduled job."""
        return bool(self.expires_at and timezone.now() > self.expires_at)

    def __str__(self):
        return f"invite {self.team_id} -> event {self.event_id} ({self.status})"


# ── INVITATION CAMPAIGNS: what KIND of invitation, and where it is delivered ──────────────────────
# (owner 2026-08-08, the follow-up to backlog item 34)
class EventInvitationCampaign(models.Model):
    """ONE invitation as the organizer AUTHORED it: the kind of offer, the note, and the channels.

    THE ITEM IN THE OWNER'S WORDS
        "The admins can pick where they receive the invitations, the normal places, can also decide
        what kind: if it is fcfs, or single per team that's automatically generated and attributed
        to each team and sent, or it's a single general bulk invite."

    WHY A CAMPAIGN EXISTS AT ALL, RATHER THAN THREE FLAGS ON EventTeamInvitation
        Item 34 shipped exactly one kind of invitation: one addressed row per team. The owner asked
        for three, and the three differ in HOW MANY ROWS THEY PRODUCE, which is precisely the thing
        a per-row flag cannot express:

          per_team  N teams -> N addressed rows. Every one may be accepted (the event's own
                    capacity is the only ceiling). This is item 34's behaviour, unchanged.
          fcfs      N teams -> N addressed rows, but only `slots` of them may ever be accepted.
                    More teams are asked than there is room for, and the quick ones get in.
          bulk      N teams -> ZERO addressed rows. One general offer, held open, that any team in
                    `audience_team_ids` may take up. A row is written only when somebody ANSWERS,
                    so the row records the answer rather than the ask.

        Putting `kind` on the invitation row would therefore be a lie for `bulk`, where at creation
        time there is no row to put it on. The campaign is the thing that always exists, so the kind
        (and the note, the deadline, the channels, the slot count, the audience) lives there and the
        invitation rows stay what they always were: one team's answer.

    THE ONE RULE THAT DID NOT CHANGE
        None of this touches how ACCEPTING works. Every kind still ends up in
        event_invites._register_through_the_normal_path, which replays the answer through
        views.register_for_event, so an invited team passes exactly the gates a self-registering
        team passes. A campaign decides WHO IS ASKED and HOW MANY MAY SAY YES. It never decides who
        gets in: register_for_event does, on the same terms as everybody else.

    HOW THE FCFS RACE IS MADE SAFE (the part worth reading twice)
        Two captains pressing Accept on the last slot at the same instant must not both get in.
        There are two independent ceilings and they are guarded in two different places, on purpose:

          1. THE EVENT'S CAPACITY is already race-safe and is NOT re-implemented here.
             views.register_for_event does `Event.objects.select_for_update()` inside its atomic
             block and counts active TournamentTeam rows behind that lock, so two concurrent
             registrations serialize and the loser gets "Registration limit reached." That existing
             lock is the guarantee; this feature leans on it rather than counting anything itself.
          2. THIS CAMPAIGN'S OWN `slots`, when the organizer set one, is claimed by a single
             guarded UPDATE (see claim_slot below) rather than a read-then-write. One SQL statement
             with a WHERE clause cannot interleave, so exactly one of two simultaneous callers sees
             a rowcount of 1.

        Deliberately NOT done: wrapping register_for_event in an outer transaction of ours to reuse
        its event lock for the campaign count too. That would work (a nested atomic rolls back only
        to its savepoint) but it silently moves that endpoint's commit boundary and holds its row
        lock for the whole of our request, and item 34's promise is that the registration path is
        called, not modified. A one-statement claim needs none of that.

    HOW IT CONNECTS
        - Written + read by afc_tournament_and_scrims/event_invites.py (the create endpoint builds
          one of these per send; accept/decline read it back through EventTeamInvitation.campaign).
        - Delivered by afc_tournament_and_scrims/event_invite_delivery.py, which fans the ask out
          over the channels named in `delivery` (in-app notification, email, WhatsApp).
        - `delivery` speaks the EXISTING channel vocabulary in afc_auth.audience (parse_delivery /
          delivery_token: "push", "email", "both", "whatsapp", comma-joined). Reused rather than
          invented so the invitation composer and the broadcast composer mean the same words.
        - Frontend: the kind + channel picker in EventTeamInvitesCard.tsx (organizer/admin) and the
          offer card in EventInvitationsCard.tsx (team page).
    """
    KIND_CHOICES = [
        ("per_team", "One invitation per team"),        # item 34's original behaviour
        ("fcfs", "First come, first served"),           # more teams asked than there are slots
        ("bulk", "One general invitation"),             # a single open offer, no addressed rows
    ]
    STATUS_CHOICES = [
        ("open", "Open"),              # still accepting answers
        ("closed", "Closed"),          # fcfs slots all taken, or the organizer closed it
        ("cancelled", "Cancelled"),    # withdrawn before it was answered
    ]

    id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invitation_campaigns")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="per_team")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")
    # The organizer's note, shown to every team this campaign reaches. Length-capped rather than a
    # TextField for the same reason EventTeamInvitation.message is: it is displayed verbatim.
    message = models.CharField(max_length=280, blank=True, default="")
    # WHERE the invitation is delivered, in afc_auth.audience's vocabulary. "both" (in-app + email)
    # is the default because those are the two channels every recipient actually has: all 813 people
    # who can answer an invitation have an email address, where WhatsApp reaches 32 of them.
    delivery = models.CharField(max_length=40, default="both")
    # FCFS ONLY. How many of THIS campaign's invitations may be accepted. NULL means "no ceiling of
    # our own", i.e. the event's own capacity is the only limit, which is the common case: invite 20
    # teams to an event with 16 free places and the registration endpoint sorts it out. Set it when
    # the organizer wants to reserve fewer places than the event actually has free.
    slots = models.PositiveIntegerField(null=True, blank=True)
    # FCFS ONLY, and a CLAIM counter rather than a tally: claim_slot() bumps it BEFORE the
    # registration is attempted and release_slot() puts it back if that registration is refused.
    # It is deliberately not "the number of accepted invitations" (which is derivable by counting
    # rows) because the thing that has to be race-safe is the RESERVATION, not the reporting.
    seats_claimed = models.PositiveIntegerField(default=0)
    # BULK ONLY. Bulk writes no addressed rows, so the teams told about the offer have nowhere else
    # to live. Queried with `audience_team_ids__contains=<team_id>` (a JSON containment lookup MySQL
    # supports) to answer "is this team allowed to see this offer".
    audience_team_ids = models.JSONField(default=list, blank=True)
    # PRIVATE events only, and SHARED (is_shared=True) rather than single-use, because a bulk or
    # fcfs campaign is by definition redeemed by more than one team. EventInviteToken already models
    # exactly this ("ONE reusable link that many people register through", see its comment above),
    # so a campaign reuses it instead of teaching register_for_event a second way in. Public events
    # leave this NULL.
    invite_token = models.ForeignKey(
        EventInviteToken, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invitation_campaigns",
    )
    # SET_NULL for the same reason EventTeamInvitation.invited_by is: deleting an organizer's
    # account must not erase campaigns teams have already accepted and are playing in.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="event_invitation_campaigns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Optional deadline, copied onto each addressed row it creates so the existing per-row expiry
    # sweep (event_invites._expire_stale) keeps working untouched.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["event", "status"]),   # organizer list: this event's campaigns
            models.Index(fields=["status", "kind"]),    # team list: open bulk offers
        ]

    def is_expired(self):
        """True when a deadline was set and it has passed. Same lazy-sweep contract as
        EventTeamInvitation.is_expired: readers flip stale rows rather than a cron doing it."""
        return bool(self.expires_at and timezone.now() > self.expires_at)

    def slots_remaining(self):
        """Places left in THIS campaign, or None when it sets no ceiling of its own (in which case
        the event's capacity is the only limit and the team card says so instead of a number).
        Read-only: the authoritative claim happens in claim_slot()."""
        if self.kind != "fcfs" or self.slots is None:
            return None
        return max(0, self.slots - self.seats_claimed)

    def claim_slot(self):
        """Reserve one FCFS place, race-safely. True when this caller got one.

        The whole guard is the WHERE clause: `UPDATE ... SET seats_claimed = seats_claimed + 1
        WHERE id = %s AND seats_claimed < slots` is ONE statement, so the database serializes two
        simultaneous callers and the second one matches zero rows once the last place is gone. A
        read-then-write here (`if remaining > 0: save()`) is the classic lost-update bug and would
        let two captains both take the final slot.

        Campaigns with no ceiling of their own (slots is NULL, or a kind that is not fcfs) always
        return True: there is nothing of ours to run out of, and register_for_event's own capacity
        check is what stops the event overfilling.
        """
        from django.db.models import F

        if self.kind != "fcfs" or self.slots is None:
            return True
        claimed = EventInvitationCampaign.objects.filter(
            pk=self.pk, seats_claimed__lt=F("slots"),
        ).update(seats_claimed=F("seats_claimed") + 1)
        if claimed:
            self.refresh_from_db(fields=["seats_claimed"])
        return bool(claimed)

    def release_slot(self):
        """Hand a claimed FCFS place back after the registration it was claimed for was REFUSED.

        Without this, a captain whose accept bounced off register_for_event (an incomplete roster,
        say) would burn a place nobody occupies. Guarded by `seats_claimed__gt=0` so a double
        release can never drive the counter negative."""
        from django.db.models import F

        if self.kind != "fcfs" or self.slots is None:
            return
        EventInvitationCampaign.objects.filter(pk=self.pk, seats_claimed__gt=0).update(
            seats_claimed=F("seats_claimed") - 1,
        )
        self.refresh_from_db(fields=["seats_claimed"])

    def __str__(self):
        return f"{self.kind} campaign -> event {self.event_id} ({self.status})"


class SponsorEvent(models.Model):
    sponsor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)


# ---------------- Public (display-only) sponsors ----------------
class EventPublicSponsor(models.Model):
    """A logo and a link an event shows to EVERY visitor, with nothing asked of them in return
    (owner 2026-08-05, backlog item 26: "sponsor logos and sponsor links visible to everyone,
    distinct from the registration sponsor").

    WHY THIS IS A NEW TABLE AND NOT A FLAG ON afc_sponsors.EventSponsorship
        AFC already has TWO sponsor concepts, and both of them are GATES on registering:
          * the legacy free-text one on Event itself (is_sponsored / sponsor_name /
            sponsor_field_label / sponsor_requirement_description), which makes a registrant type
            a value for the sponsor;
          * afc_sponsors.EventSponsorship, whose entire reason to exist is requires_approval +
            engagements - follow this account, join that group, and a registration that does not
            complete until the sponsor approves it (see SponsorEngagementSubmission).
        What is wanted here is the opposite of a gate: a strip of logos on the public page that
        asks the visitor for nothing. Bolting a "show this one publicly" flag onto EventSponsorship
        would mean every query in afc_sponsors/engagements.py that reasons about "the sponsors of
        this event" would first have to work out whether a given row is a real gate or a piece of
        decoration, and getting that wrong in either direction is a registration bug.

        The second reason is permissions, and it is the decisive one. A Sponsor entity can only be
        created by a sponsor-admin (afc_sponsors.views._is_sponsor_admin); an ORGANIZER cannot make
        one. The owner's ask starts with "organizers and admins can add", so a model that needs an
        AFC sponsor-admin to mint a row first cannot satisfy it. This table is owned by the event,
        so whoever may edit the event may edit its public sponsors, and nothing else changes.

    HOW IT CONNECTS
        - Written by afc_tournament_and_scrims.views_public_sponsors (add / update / delete),
          gated by the SAME permission as edit_event: _is_event_admin OR org_can_event.
        - Read by BOTH public detail builders, get_event_details and
          get_event_details_not_logged_in, as the `public_sponsors` key, via
          views_public_sponsors.serialize_public_sponsors - so a logged-out visitor sees them.
        - Edited on the shared Sponsor tab of the admin + organizer event-edit wizards
          (frontend app/(a)/a/events/[slug]/edit/_components/SponsorTab.tsx, "Public sponsors"
          card) and rendered on the public tournament page (EventDetailsWrapper).
        - NOT copied by clone_event, matching the existing policy there for SponsorEvent /
          StreamChannel (a clone copies Event config columns, not attached rows).

    The `link` is attacker-supplied and ends up in an anchor on a public page, so it is validated
    with afc_sso.provisioning._clean_url (https only, real URL) on the way in, and rendered with
    rel="noopener noreferrer". The `logo` goes through afc_sso.provisioning._clean_logo_upload,
    the same Pillow-decode + re-encode + rename guard the partner logo upload uses.
    """
    id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="public_sponsors")
    # Always shown, and the alt text of the logo, so a sponsor with no usable image still reads.
    name = models.CharField(max_length=100)
    # Optional: a sponsor may want to be credited without sending anyone anywhere. Stored only
    # after passing _clean_url, so anything in here is already an absolute https URL or "".
    link = models.URLField(max_length=300, blank=True, default="")
    logo = models.ImageField(upload_to="event_public_sponsors/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Creation order IS the display order. No separate rank column, because the owner asked
        # for logos on a page, not a ranking UI, and "the order I added them" is what an organizer
        # expects when there is no control to say otherwise.
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} (public sponsor of event {self.event_id})"


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
        # Distinct from the dead "br - roundrobin" (mislabelled "Knockout") entry above -
        # that one is left untouched for backward compatibility.
        ("br - round robin", "Battle Royale - Round Robin"),
        # ── The two-question picker (owner backlog item 21, built 2026-08-13) ─────────────────
        # A stage now answers ONE question here - which game is this? - and the specific mode
        # (knockout / double elimination / league / round robin) is chosen PER GROUP, on
        # StageGroups.bracket_format. That is what lets a single Clash Squad stage run
        # "Group A - Knockout" beside "Group B - League".
        #
        # Every value above still works and still means what it always meant: this is a new,
        # shorter way to say the same thing, not a rename. Anything reading a format should ask
        # eventformats.is_clash_squad() rather than testing for a literal.
        ("cs", "Clash Squad"),
        ("br", "Battle Royale"),
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
    is_finals_stage = models.BooleanField(default=False)  # rankings §4.5/§6.1 - admin marks the finals stage

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

    # WHICH STAGES the auto-seed applies to (owner 2026-08-05: "build it that admins/organizers
    # can select what they want, if it should apply to specific stages or groups").
    #
    # DEFAULT FALSE, AND THAT IS NOT THE SAME AS "OFF". When an event has auto-seed on and NO stage
    # marked, run_auto_seed falls back to the entry stage, which is exactly what it did before this
    # field existed. So every event already in the database keeps behaving the way its organizer
    # set it up, and marking a stage is an explicit widening rather than a migration that changes
    # what people's events do overnight.
    #
    # The owner scoped execution to the first stage FOR NOW. The field is per-stage anyway, because
    # the constraint they asked for is that the CHOICE exists; carrying qualified teams into a
    # later stage is a separate piece of work about when a stage is finished, not about where the
    # switch lives.
    auto_seed = models.BooleanField(default=False)
    # Per-stage stamp, so seeding one stage never blocks another. Event.auto_seeded_at remains the
    # event-level "the automatic pass has run at least once" marker the status sweep reads.
    auto_seeded_at = models.DateTimeField(null=True, blank=True)

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
    # Does the automatic draw put teams in THIS group (owner 2026-08-05)? Default TRUE, because a
    # group exists to be played in, and a stage where somebody had excluded every group by accident
    # would seed nobody and look broken. Unticking one is how an organizer reserves a group, for
    # example a bracket-only or invitational group they intend to fill by hand.
    auto_seed_include = models.BooleanField(default=True)

    # ── Clash Squad: this group IS a bracket (owner backlog item 21, built 2026-08-13) ─────────
    # A Battle Royale group is a LOBBY: teams drop in together and are scored on placement + kills.
    # A Clash Squad group is a BRACKET: its teams play each other head to head in the mode named
    # here, and it has its own winner. One stage can hold several, on different modes -
    # "Group A - Knockout" beside "Group B - League" - which is the whole point of the change.
    #
    # BLANK means an ordinary Battle Royale lobby, which is every group that existed before today,
    # so nothing has to be backfilled for BR. Set means the group's matches are HeadToHeadMatch
    # rows carrying group_id, and head_to_head.py treats "a bracket" as the matches of ONE group.
    #
    # The mode lives HERE and not on the stage on purpose: putting it on the stage is exactly what
    # forced one bracket per stage and made the format list eight near-identical lines.
    BRACKET_FORMAT_CHOICES = [
        ("single_elim", "Knockout"),
        ("double_elim", "Double elimination"),
        ("league", "League"),
        ("round_robin_h2h", "Round robin"),
    ]
    bracket_format = models.CharField(
        max_length=24, choices=BRACKET_FORMAT_CHOICES, blank=True, default="")
    # The optional bronze match, per group: this group decides its own 3rd and 4th rather than
    # sharing 3rd between two beaten semi-finalists. Single elimination only, like the stage-level
    # flag it replaces.
    bracket_third_place = models.BooleanField(default=False)

    # Is this group a BOOKKEEPING row rather than a lobby anyone plays in (owner 2026-08-12)?
    # head_to_head.write_placement_stats has to hang its synthetic result Match off a StageGroups,
    # because that is what the leaderboard and ranking pipelines read, so a Clash Squad bracket
    # stage - which has no groups by design - gets a "Bracket Results" group created for it. That
    # row then showed up on the admin Stages tab as a normal group card, complete with "Add Teams
    # to Group" and "View Results", which is nonsense for a bracket. Marked here so every group
    # surface can filter it out; nothing about how it stores results changes.
    is_synthetic = models.BooleanField(default=False)

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
# standard Battle-Royale ladder - 1 point/kill, no assist/damage bonus, placement 12/9/8/7/6/5/4/3/2/1 - 
# so admins/organizers no longer fill it in per map. It is still fully editable per map (the Scoring
# Config tab -> POST /events/edit-match-scoring-config/ overwrites Match.scoring_settings), and "Apply
# to..." copies one map's config to others. Persisted as Match.scoring_settings; read by every scoring
# compute path as `match.scoring_settings or {}`. DEFAULT_PLACEMENT_POINTS is the single source of truth
# (mirror it in the frontend leaderboard editor's default form state).
DEFAULT_PLACEMENT_POINTS = {
    "1": 12, "2": 9, "3": 8, "4": 7, "5": 6, "6": 5, "7": 4, "8": 3, "9": 2, "10": 1,
}


def default_scoring_settings():
    """Fresh default Match.scoring_settings dict (a NEW object each call - required for a mutable
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
    # Is this map's room a 3D CUSTOM ROOM (owner 2026-08-04)? Off by default, set per map beside the
    # room id and password. When on, the joining steps are shown to players underneath the room
    # credentials, because a 3D room is not joined the way an ordinary custom room is: the squad has
    # to be a complete group first, and the leader goes in through Customs and League.
    #
    # NAMED room_is_3d, NOT is_3d_room, ON PURPOSE. "3D" already means something else in this
    # codebase: the Free Fire 3D observer client whose debugger-*.log files feed the rich per-player
    # stats (see debugger_ingest.py and views_mvp.py). That is a RESULTS pipeline; this is a
    # property of the ROOM players join. Leading with `room_` keeps the two from being read as the
    # same switch by somebody grepping for "3d".
    room_is_3d = models.BooleanField(default=False)
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
    # ── WHO is competing: a real AFC team, or a GHOST (owner 2026-08-20, external results import) ──
    # EXACTLY ONE of these two is set, enforced by the tt_team_xor_ghost CheckConstraint below.
    #
    # WHY team IS NULLABLE NOW. AFC carries tournaments it did not run (FFWS Africa is the driving
    # case). Most competitors in those events have no AFC account at all, so there is no Team row to
    # point at. afc_rankings.GhostTeam is the identity AFC already uses for exactly this on
    # standalone leaderboards, complete with a claim lifecycle, so it is reused here rather than
    # inventing a second "team that is not really a team" concept.
    #
    # THE PATTERN IS COPIED, NOT INVENTED: afc_leaderboard.LeaderboardParticipant has carried
    # team XOR ghost_team with a DB CheckConstraint since the standalone leaderboards were built.
    #
    # READERS MUST NOT REACH THROUGH .team. Use display_name / competitor / is_ghost below. A
    # `tournament_team.team.team_name` on a ghost row is an AttributeError on None in production.
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="tournament_entries",
        null=True, blank=True,
    )
    ghost_team = models.ForeignKey(
        "afc_rankings.GhostTeam", on_delete=models.CASCADE,
        related_name="tournament_entries", null=True, blank=True,
    )
    status = models.CharField(max_length=20, choices=TEAM_STATUS, default="active")
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    registration_date = models.DateTimeField(auto_now_add=True)
    country = models.CharField(max_length=100, null=True, blank=True) # Store country at time of registration for historical accuracy
    is_waitlisted = models.BooleanField(default=False)
    # No-show (owner 2026-06-17 waitlist): team-side mirror of RegisteredCompetitors.is_no_show - the
    # organizer marked this active team absent, freeing a slot for a waitlisted team. See mark_no_show.
    is_no_show = models.BooleanField(default=False)
    # rankings result markers - set by admin at result entry via afc_rankings.admin_results
    # (spec §4.4/§4.5/§5.1); consumed by afc_rankings.aggregation to award win/finals points.
    # result_finalized gates whether aggregation counts this event at all.
    is_tournament_winner = models.BooleanField(default=False)
    reached_finals = models.BooleanField(default=False)
    finals_appearances = models.PositiveIntegerField(default=0)
    result_finalized = models.BooleanField(default=False)

    # PER-TEAM roster-edit allowance (owner 2026-06-24). The event-wide Event.roster_edit_until opens
    # roster editing for ALL teams; this opens it for THIS ONE team only - an admin/organizer can let a
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
            #   • MySQL - the PRODUCTION database - IGNORES the partial-index `condition` on a
            #     UniqueConstraint, so the previous conditional form (condition=assigned_letter is not
            #     null) gave ZERO DB enforcement there: two teams in one event could be saved with the
            #     same letter straight through the ORM. It only ever worked on Postgres.
            #   • A plain unique index DOES enforce on MySQL. And because BOTH MySQL and Postgres allow
            #     MULTIPLE NULLs in a unique index, every unassigned team (assigned_letter = NULL) still
            #     coexists without colliding - only two NON-NULL teams sharing a letter in the same event
            #     are rejected at the DB level. So dropping the condition loses nothing and gains real
            #     MySQL enforcement.
            # The app-level 409 in assign_team_letter stays as the friendly first line of defence; this
            # constraint is the DB backstop that enforces Open Q (g) even on a direct/bulk write.
            models.UniqueConstraint(
                fields=["event", "assigned_letter"],
                name="uniq_assigned_letter_per_event",
            ),
            # ── Bug C (duplicate registration + un-removable dupe) ────────────────────────────────
            # One registration per (event, team). Before this, a race in register_for_event /
            # add_teams_to_event could create TWO TournamentTeam rows for the same team in the same
            # event (e.g. #15 and #16), which then made the team un-removable
            # (get_object_or_404(event, team) -> MultipleObjectsReturned -> 500) and double-counted
            # it in the bracket. A plain UniqueConstraint enforces on BOTH MySQL (production) and
            # Postgres. The 0050 migration DEDUPES existing rows before adding this, so the
            # AddConstraint cannot fail on legacy dupes. App-level select_for_update + IntegrityError
            # guards (register_for_event, add_teams_to_event) are the friendly first line of defence.
            models.UniqueConstraint(
                fields=["event", "team"],
                name="uniq_event_team_registration",
            ),
            # ── Exactly one competitor kind (owner 2026-08-20) ────────────────────────────────
            # Mirrors the XOR on afc_leaderboard.LeaderboardParticipant. Both null would be a row
            # that competes as nobody; both set would be a row whose identity depends on which
            # reader you ask. MySQL 8.0.16+ enforces CHECK constraints, and this deployment already
            # relies on that for the participant XOR, so this is not a new dependency.
            models.CheckConstraint(
                name="tt_team_xor_ghost",
                check=(
                    models.Q(team__isnull=False, ghost_team__isnull=True)
                    | models.Q(team__isnull=True, ghost_team__isnull=False)
                ),
            ),
            # The ghost twin of uniq_event_team_registration. A PLAIN unique constraint, for the
            # reason spelled out on uniq_assigned_letter_per_event above: MySQL IGNORES the partial
            # index `condition` on a UniqueConstraint, so a conditional form would give zero
            # enforcement in production. Both databases allow multiple NULLs in a unique index, so
            # every real-team row (ghost_team NULL) coexists without colliding.
            models.UniqueConstraint(
                fields=["event", "ghost_team"],
                name="uniq_event_ghost_registration",
            ),
        ]

    @property
    def roster_edit_open(self) -> bool:
        """True while THIS team's per-team roster-edit allowance is open (roster_edit_until set AND now
        is at/before it). Mirrors Event.roster_edit_open but scoped to one team. Auto-closes by time."""
        from django.utils import timezone as _tz
        return bool(self.roster_edit_until) and _tz.now() <= self.roster_edit_until

    # ── The competitor accessors (owner 2026-08-20, external results import) ──────────────────
    # ONE definition each, because there are ~172 places in this codebase that used to reach
    # through .team and every one of them is an AttributeError on a ghost row. Anything that needs
    # to name, fetch or test the competitor uses these and never the FKs directly.
    #
    # Read by: the event page and bracket serializers, the standings builders, the overlay
    # renderers, the CSV/xlsx exports, notifications, the partner API, and afc_player_market.

    @property
    def is_ghost(self) -> bool:
        """True when this registration represents an unclaimed external competitor."""
        return self.ghost_team_id is not None

    @property
    def competitor(self):
        """The underlying afc_team.Team or afc_rankings.GhostTeam. Never None: the
        tt_team_xor_ghost constraint guarantees exactly one of the two is set."""
        return self.ghost_team if self.is_ghost else self.team

    @property
    def display_name(self) -> str:
        """What to show a human for this competitor. Both models spell it team_name, so this
        stays a single attribute read rather than a branch on type.

        Deliberately the SAME NAME as afc_leaderboard.LeaderboardParticipant.display_name, which
        answers the identical question for the standalone-leaderboard half of the system. One name
        for one idea across both."""
        return self.competitor.team_name

    def __str__(self):
        return f"{self.display_name} in {self.event.event_name}"


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

    # ── FROZEN in-game role for THIS event (owner 2026-08-04: "role history is not stored") ──────
    # A copy of afc_team.TeamMembers.in_game_role taken at the moment the player was put on THIS
    # event's roster, and never touched again afterwards. It exists because the club roster row is a
    # LIVE value: a player who was a sniper in July and is a rusher today reads back as a rusher, so
    # July's sniper ladder listed them under the wrong role. This row is already the thing AFC
    # freezes per event (the roster snapshot), so the role belongs on it.
    #
    # Why the frozen copy rather than reading TeamMembers at scoring time: the match-result upload
    # DELETES and re-creates every TournamentPlayerMatchStats row for a match on each (idempotent)
    # re-upload. If the role were re-read live, re-uploading a July match in September would stamp
    # the September role onto July, which is exactly the bug being fixed. Reading a frozen per-event
    # value makes a re-upload reproduce the same historical role every time.
    #
    # NULL means "no role recorded", which is the honest answer for three real cases and must NOT be
    # guessed at: staff roles (coach / manager / analyst have no in_game_role), players registered
    # before this field existed, and roster rows copied from a source event that itself has none.
    #
    # Written by: register_for_event, add_teams_to_event, edit_roster, add_player_to_event_roster
    # (views.py) and event_links._promote / import_competitors (roster carried from the source event).
    # Read by: roster_roles.frozen_roles_for_event, which stamps TournamentPlayerMatchStats
    # .role_at_match when a match result is recorded. Backfilled (upcoming events only) by the
    # afc_rankings backfill_player_roles management command.
    in_game_role = models.CharField(
        max_length=20, choices=TeamMembers.IN_GAME_ROLE_CHOICES, null=True, blank=True,
    )

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
    # SIGNED (owner 2026-07-06): total = placement + kill + assist + damage + bonus - penalty, which
    # goes NEGATIVE when a team's penalty exceeds its earned points. As a PositiveIntegerField (MySQL
    # UNSIGNED) that crashed the save with DataError 1264 under STRICT mode on any heavily-penalized
    # team (upload / manual entry / edit / the scoring-config recompute all persist this value).
    total_points = models.IntegerField(default=0)
    played = models.BooleanField(default=True)
    penalty_points = models.IntegerField(default=0) # ✅ -
    bonus_points = models.IntegerField(default=0)   # ✅ +

    class Meta:
        # One stats row per (match, team). Without this a second row for the same team in a match
        # (pre-2026-06-29 foreign-log residue: a ringer block credited to a site team it already had
        # a row for) double-counts in the standings Sum(total_points)/Count(match_id), inflating that
        # team's total and match count (bug found 2026-07-06: event 134 Alpha Wolves showed 84 vs the
        # correct 70). The upload path already clears a match's rows before re-inserting, so this only
        # guards against the residual dupes + any future accidental double-insert. Stale duplicates
        # must be collapsed first (management command dedupe_team_match_stats) or the migration adding
        # this constraint will fail.
        constraints = [
            models.UniqueConstraint(
                fields=["match", "tournament_team"],
                name="uniq_team_stats_per_match",
            ),
        ]

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
    # Filled ONLY by the debugger-log backfill (debugger_ingest.py) or a future live-capture write - 
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

    # ── the in-game role this player held WHEN THIS MATCH WAS PLAYED (owner 2026-08-04) ──────────
    # The precise anchor for role history: the per-match stats row is written at the moment a result
    # is recorded, so it is the finest grain at which "the role the points were earned under" can be
    # attached to anything. The per-role ladders aggregate these stamps per period, which is how a
    # player who was a sniper in July stays a sniper in July's table after switching to rusher.
    #
    # SOURCE, and the one rule that keeps this honest: it is copied from the FROZEN per-event roster
    # row (TournamentTeamMember.in_game_role), never from the live afc_team.TeamMembers row. Every
    # write path here deletes and re-inserts a match's rows on re-upload, so reading a live value
    # would let a September re-upload rewrite July's role. Reading the frozen per-event value makes
    # the stamp reproducible: re-uploading an old match reproduces the old role.
    #
    # NULL = no role recorded for this match, and it is left NULL rather than guessed. That covers
    # staff (no in_game_role), players whose event roster row predates the frozen field, and rows
    # written for matches whose roster row could not be resolved.
    #
    # Written by: every match-result write path (upload_team_match_result, the manual entry and edit
    # endpoints in views.py, and afc_ocr.services.commit) through
    # afc_tournament_and_scrims.roster_roles.frozen_roles_for_event.
    # Read by: afc_rankings.aggregation._collect_player, which turns the period's stamps into
    # PlayerMonthlyScore.role / role_breakdown (and the quarterly equivalents) for the role ladders.
    role_at_match = models.CharField(
        max_length=20, choices=TeamMembers.IN_GAME_ROLE_CHOICES, null=True, blank=True,
    )


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
        # NAME-MATCH reasons: created by upload_team_match_result when a file player did NOT UID-match
        # but their in-game NAME (ascii-folded, clan-tag-stripped) matches a registered roster member.
        # name_matched_uid_changed = matches a member of THIS team (the team's own player under a new
        # UID) -> created count_kills=None so it FOLLOWS the event count_flagged_kills toggle, exactly
        # like not_on_roster (owner 2026-07-06: forcing it PENDING silently dropped a returning player's
        # kills even with the toggle ON). name_matched_other_team = matches a member registered on a
        # DIFFERENT team -> created count_kills=False (explicit PENDING), needs admin/organizer approval
        # (set_match_kill_flag -> True) before those kills join the team total, since a cross-team name
        # match is a genuine borrowed-ringer concern.
        ("name_matched_uid_changed", "Name matches a roster member of this team but the UID differs"),
        ("name_matched_other_team", "Name matches a roster member registered on another team"),
        # UNLISTED (owner 2026-07-07): the file's team KillScore exceeds the sum of the KILL lines it
        # listed against players - the Free Fire client dropped a player's row, so those kills belong to
        # the team but have no player to attach to. Recorded as ONE synthetic flag (uid="unlisted",
        # registered_user=None) with count_kills=None so it FOLLOWS count_flagged_kills (counts by
        # default, toggleable) and the team total honors the official KillScore.
        ("unlisted_in_file", "Kills in the team score that the match file did not list against any player"),
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
      • WRITE-scoped - it ONLY authorizes upload_team_match_result for THIS event (see that view's
        alternative-auth branch), never any other endpoint or event.
      • Revocable + auditable - created_by records who granted it; `revoked` retires a leaked key
        without deleting the row (a rotate REVOKES the old + issues a new one), so the audit trail
        of who-issued-what survives.
    A request presenting the token acts AS created_by (the granting user's upload permission), so the
    event admin / organizer who minted it is accountable for what the capture PC posts.

    CONNECTS TO: minted/rotated by ensure_upload_token (events/<id>/upload/token/, gated like the
    overlay token - event admin OR org_can_event can_edit_events); consumed by
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


class PendingCaptureUpload(models.Model):
    """A captured result the desktop AFC Capture client could NOT auto-attribute, parked for a human to
    resolve later on the website (owner 2026-07-05, complaint D - "decide later" bucket).

    WHY THIS EXISTS
    ---------------
    The capture client posts each round's MatchResult file to upload_team_match_result with a stage +
    group but NO match_id; the backend fills the next unscored map slot. When EVERY configured map slot
    for that group is already scored and an EXTRA game lands, the old code SILENTLY created a new slot - 
    the complaint-D bug (an accidental re-run / a wrong-event capture became a phantom "map"). The new
    behaviour is: the backend returns a structured 409 asking the operator to decide, and the desktop
    prompt offers three choices - attribute as a NEW map, REPLACE an existing map, or "decide later".
    "Decide later" reliably parks the raw upload HERE (never dropped) so an admin/organizer resolves it
    from the website later. A pending row therefore ALWAYS carries enough to re-score it verbatim.

    WHAT IS STORED
    --------------
      • raw_payload   : {file_text, file_name, file_type, stage_id, group_id} - the exact bytes + the
                        client's set stage/group, so resolve re-runs the IDENTICAL scoring path.
      • parsed_summary: a small human-readable digest ({teams:[{team_name, placement, players, kills}],
                        team_count, player_count}) built at intake so the resolve UI can show what the
                        upload contains WITHOUT re-parsing.
      • status        : pending -> resolved (scored into a match) | discarded (operator dropped it).

    CONNECTS TO
    -----------
    Created by upload_team_match_result's attribution="pending" branch (via
    views_capture_pending._create_pending_capture). Listed / resolved / discarded by
    views_capture_pending (events/<id>/pending-captures/...), gated exactly like the other event result
    endpoints (AFC event admin OR an organizer with can_upload_results). Resolving runs the SAME
    _score_team_match_result core the live upload uses, into a new or replacement Match slot.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),      # awaiting an operator decision
        ("resolved", "Resolved"),    # scored into a match (new or replacement)
        ("discarded", "Discarded"),  # operator dropped it (a genuine mis-capture)
    ]
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pending_captures")
    # Who/what sent it. uploaded_by = the token's granting user (may be None if the user was deleted);
    # upload_token = the exact capture key, kept for the audit trail (SET_NULL so a rotate keeps history).
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="pending_captures_uploaded")
    upload_token = models.ForeignKey(EventUploadToken, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="pending_captures")
    # The client's SET stage/group at capture time (a hint for the resolver's default target). SET_NULL
    # so deleting a stage/group never destroys the parked result.
    stage = models.ForeignKey(Stages, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="pending_captures")
    group = models.ForeignKey(StageGroups, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="pending_captures")
    file_name = models.CharField(max_length=255, blank=True)
    raw_payload = models.JSONField(default=dict)       # {file_text, file_name, file_type, stage_id, group_id}
    parsed_summary = models.JSONField(default=dict)    # {teams:[...], team_count, player_count}
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending", db_index=True)
    # Bookkeeping filled when an operator resolves/discards it.
    resolution = models.CharField(max_length=40, blank=True)   # "new" | "replace:<match_id>" | "discarded"
    resolved_match = models.ForeignKey(Match, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="resolved_pending_captures")
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="pending_captures_resolved")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"PendingCaptureUpload(event={self.event_id}, {self.status})"


class CaptureRelease(models.Model):
    """A published desktop AFC Capture INSTALLER release (owner 2026-07-05, full auto-update).

    WHY THIS EXISTS
    ---------------
    Copies of the AFC Capture tray client are installed on tournament observer PCs. Without an update
    mechanism, shipping a fix meant asking every operator to hunt down + re-download the installer. This
    row is the server-side "what is the latest version" record the installed client polls on startup so
    it can update ITSELF. The owner publishes a new version by uploading the new Inno Setup installer
    somewhere it can be downloaded (any static host: the frontend /public/downloads, S3, a release asset,
    etc.) and creating one of these rows with that URL. NO code deploy is needed to bump the version.

    WHAT IS STORED
    --------------
      • version              : the semver of this release ("1.3.0"). The client compares it to its own
                               afc_capture.__version__ (proper numeric semver compare, not string compare).
      • installer_url        : absolute URL the client downloads the new installer .exe from, then runs
                               SILENTLY to replace itself (a running .exe cannot overwrite its own file on
                               Windows, so the installer does the swap + relaunch).
      • notes                : optional human-readable changelog shown/logged with the update.
      • min_supported_version: optional floor; when set and the client is BELOW it, the update is treated
                               as required (the client may nag harder). Purely advisory server-side.
      • is_latest            : exactly one row is the "current" release the version endpoint serves.
                               Publishing a new release sets this True and clears it on every other row.

    CONNECTS TO
    -----------
    Written by views_capture_update.capture_releases (POST events/capture/releases/, gated to a super
    admin / head_admin via views._is_head_or_super_admin). Read by views_capture_update.capture_version
    (GET events/capture/version/, PUBLIC - no token, exposes only a version + a public download URL).
    Consumed by the desktop client afc-capture/afc_capture/updater.py, which is invoked on startup by
    app.CaptureController and from the tray "Check for updates" item.
    """
    version = models.CharField(max_length=32, db_index=True)          # semver, e.g. "1.3.0"
    installer_url = models.URLField(max_length=1000)                  # where the installer .exe is hosted
    notes = models.TextField(blank=True)                             # optional changelog / release notes
    # Optional force-update floor: clients older than this SHOULD update (advisory; the server never blocks).
    min_supported_version = models.CharField(max_length=32, blank=True, default="")
    # Exactly one row is the served "latest". Publishing sets this True + clears every other row (see
    # capture_releases). Indexed because the version endpoint filters on it on every poll.
    is_latest = models.BooleanField(default=False, db_index=True)
    # Who published it (audit); SET_NULL so removing the user keeps the release history.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="capture_releases")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"CaptureRelease(v{self.version}{' [latest]' if self.is_latest else ''})"


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


class MatchResultLog(models.Model):
    """The original .log FILE an admin/organizer uploaded to score a match (owner 2026-07-07:
    "store the match files so it can be checked later if needed"). The .log-file upload path
    (views.upload_team_match_result) parses the text then discards it; this keeps the exact bytes
    as a per-match AUDIT TRAIL so a disputed result can be re-checked against the raw game export.

    Parallel to MatchResultImage (which already retains OCR SCREENSHOT uploads). Each upload of a
    match appends a NEW row (not replace) so the full history of what was uploaded is preserved.

    Written by: views.upload_team_match_result (on a real, non-dry-run .log upload).
    Read by: views.get_match_result_logs (lists + download URLs for the results editor's evidence
    view, the sibling of get_match_result_images). Deleted by: views.delete_match_result_log."""
    log_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="result_logs")
    file = models.FileField(upload_to='match_result_logs/')
    file_name = models.CharField(max_length=255, blank=True)   # original filename for display
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-log_id"]   # newest upload first

    def __str__(self):
        return f"Result log for match {self.match_id} ({self.file_name})"


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
        # The optional bronze match in a single-elimination bracket (owner 2026-08-12: an event
        # that pays 3rd differently from 4th needs them separated, and sharing a placement between
        # the two semifinal losers cannot do that). Fed by the loser links of the two semifinals;
        # its winner is 3rd and its loser 4th. Deliberately NOT "winners", so the
        # "the final is the winners match with no next_match" rule that decides when a bracket is
        # complete keeps pointing at the real final. Value kept short ("third", not "third_place")
        # so the existing max_length=10 column needs no schema change.
        ("third", "Third-place match"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("live", "Live"),
        ("completed", "Completed"),
    ]
    SLOT_CHOICES = [("a", "Slot A"), ("b", "Slot B")]

    h2h_match_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="h2h_matches")
    # ── which BRACKET this match belongs to (owner backlog item 21, built 2026-08-13) ──────────
    # A Clash Squad stage can now hold several independent brackets, one per StageGroups row with
    # a bracket_format. "A bracket" therefore means the matches of one GROUP, not of one stage:
    # generation, standings, completion and the placement bridge all filter on this column.
    #
    # NULL means the legacy shape - a single bracket owned by the whole stage - which is what every
    # row created before today looks like until the data migration moves it. Keeping `stage`
    # alongside is deliberate denormalisation: "everything in this stage" stays a one-column filter
    # and the hottest read (the public bracket page) needs no join.
    group = models.ForeignKey(
        StageGroups, null=True, blank=True, on_delete=models.CASCADE,
        related_name="h2h_matches")
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
    # Written by head_to_head_views.update_h2h_match (owner 2026-08-12: the columns existed but
    # nothing ever set or showed them, so a CS match had no kick-off time anywhere on the site).
    # Stored as the organizer's chosen wall-clock date/time for the event, and rendered in the
    # VIEWER's timezone by the FE LocalTime component, like every other time on AFC.
    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_time = models.TimeField(null=True, blank=True)

    # ── how the result came about (owner 2026-08-12) ──
    # A set that nobody turned up for is not the same as a 7-0 thrashing, and paying prizes or
    # judging a no-show record needs the difference recorded rather than inferred from a suspicious
    # scoreline. "normal" is every ordinary played set, so existing rows keep their meaning.
    RESULT_TYPE_CHOICES = [
        ("normal", "Played"),
        ("forfeit", "Forfeit"),        # a team gave the set up (late, short-handed, withdrew)
        ("walkover", "Walkover"),      # the opponent never showed at all
        ("dq", "Disqualification"),    # an admin removed a team for breaking a rule
    ]
    result_type = models.CharField(max_length=10, choices=RESULT_TYPE_CHOICES, default="normal")
    # The one-line reason shown beside a non-normal result ("opponent did not join by 20:15").
    result_note = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Stable tree order for any reader; the views additionally group by bracket side.
        ordering = ["round_number", "position", "h2h_match_id"]
        indexes = [
            models.Index(fields=["stage", "bracket", "round_number"]),
            # Reading ONE group's bracket is now the common case, so it gets its own index
            # rather than filtering a stage-wide scan (owner 2026-08-13).
            models.Index(fields=["group", "bracket", "round_number"], name="idx_h2h_group_bracket"),
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
    like the countdown timer), NAME/RENAME, DUPLICATE, DELETE - and whose public link NEVER changes.
    The link (/overlay/view/<Event.overlay_token>/<id>) polls the public config feed, so editing the
    overlay's design/stage/group/animations from the studio updates what the SAME link renders live - 
    the operator never re-copies a URL into OBS.

    kind:   "leaderboard" (design + live TEAM standings) | "timer" (countdown scene) |
            "booyah" (winner banner) | "h2h" (head-to-head) | "mvp" (design + ranked PLAYERS by per-map
            MVP count) | "top_killers" (design + ranked PLAYERS by summed kills). The mvp + top_killers
            kinds (owner 2026-07-05, complaints G+H) are PLAYER-driven: they render player rows (rank /
            photo / IGN / kills / damage / assists) through ANY design and can COMBINE selected whole
            stages + individual groups. See views_mvp.py (the CONTRACT block) + views_overlays.py
            (_mvp_payload / _top_killers_payload).
    config: freeform per kind - 
      leaderboard:       {design_id, follow (bool), scope, stage_id, group_id, group_ids, stage_ids,
                          anim, reveal, interval, size, live}
      timer:             {end_at (ISO), label}
      mvp / top_killers: {design_id, scope, group_ids, stage_ids, group_id, stage_id} - the SAME combine
                          shape complaint C added for leaderboards (whole stages expand to their groups;
                          absent => whole event).
    active: scenes (timer) toggle visibility with it; leaderboard / mvp / top_killers overlays ignore it
            (always render).

    CONNECTS TO: views_overlays.py (CRUD via the broadcast gate + the public config feed) <-
    FE studio app/(a)/a/overlays/[eventId] (cards) + renderer app/overlay/view/[token]/[overlayId].
    """
    KINDS = (("leaderboard", "Leaderboard"), ("timer", "Timer"), ("booyah", "Booyah banner"),
             ("h2h", "Head to head"), ("mvp", "MVP"), ("top_killers", "Top killers"))

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


class TeamMapResultSubmission(models.Model):
    """A team's own proposal for its row on one map, waiting for an organizer to approve it.

    WHY IT EXISTS (owner 2026-08-04, backlog item 6): only an organizer or admin can enter
    results today. On a large event that is a bottleneck, and the organizer is usually
    transcribing screenshots the teams already sent them. This moves the typing to the people
    who hold the data while leaving the organizer in control of what is true.

    A SUBMISSION IS NOT A RESULT. Nothing here is read by the standings. Approving one is what
    writes TournamentTeamMatchStats and TournamentPlayerMatchStats, through the same function
    an organizer's own entry uses (result_writes.write_team_result_row), so an approved
    submission produces byte-for-byte the rows the organizer would have produced by hand.

    WHY A TEAM SUBMITS ONLY ITS OWN ROW, never the whole lobby:
      * a team has first-hand knowledge of its own placement and its own players' kills, and
        second-hand knowledge of everyone else's;
      * a lobby-wide submission would invite a team to report its rivals down a place, and
        would make the organizer arbitrate between two full and differing lobbies;
      * scoping it to the submitter's own team makes the permission question total and
        simple: are you on this team, and is this team in this match.
    The organizer assembles a map from N one-team submissions, approving each as it lands,
    which is exactly the transcription work being removed.

    STATE MACHINE
        pending ──approve──> approved      the row is written to the stats tables
                └─reject───> rejected      with a reason the team can read
        approved ──(a later approval for the same team and match)──> superseded

    WHAT STAYS AUDITABLE: who submitted and when, who reviewed and when, the reason for a
    rejection, the payload as SUBMITTED and, separately, the payload as APPROVED. Keeping both
    payloads is what lets anyone see that the organizer corrected a placement before approving
    rather than having to trust that they did not.

    Written and read by afc_tournament_and_scrims/views_team_submissions.py. Consumed by the
    team-side submit form and the organizer's review queue on the event results page.
    """

    STATUS_CHOICES = [
        ("pending", "Pending review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("superseded", "Superseded by a later approval"),
    ]

    submission_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(
        Match, on_delete=models.CASCADE, related_name="team_result_submissions")
    tournament_team = models.ForeignKey(
        TournamentTeam, on_delete=models.CASCADE, related_name="result_submissions")

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="team_result_submissions")
    submitted_at = models.DateTimeField(auto_now_add=True)

    # The team's proposal, in the same per-team shape the manual entry form posts:
    # {placement, played, bonus_points, penalty_points, players: [{user_id, kills, damage,
    # assists, played}]}. Stored as sent, and never edited afterwards, so the record of what
    # the team actually claimed survives whatever the organizer does next.
    submitted_payload = models.JSONField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="team_result_reviews")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # What was ACTUALLY written, which differs from submitted_payload whenever the organizer
    # corrected something before approving. Null until approved. The pair is the audit: same
    # means approved as sent, different shows exactly what the organizer changed.
    approved_payload = models.JSONField(null=True, blank=True)

    # Required on a rejection and shown to the team. A team told only "rejected" resubmits the
    # same numbers, which wastes the organizer's time twice.
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            # The organizer's queue is "everything still pending on this match", and the team's
            # own view is "my submissions for this match", so both filter on match first.
            models.Index(fields=["match", "status"], name="idx_tmrs_match_status"),
            models.Index(fields=["tournament_team", "status"], name="idx_tmrs_team_status"),
        ]
        # NO DATABASE CONSTRAINT for "one pending submission per team per map", and that is a
        # deliberate choice rather than an oversight. The natural expression of it is a partial
        # unique index (unique on (match, tournament_team) WHERE status='pending'), and MySQL
        # does not support conditional constraints: Django raises models.W036 and silently
        # creates nothing. A constraint that exists in the model and not in the database is
        # worse than none, because it reads as a guarantee nobody is enforcing.
        #
        # The invariant is therefore held where it is actually true: views_team_submissions.
        # submit_team_map_result deletes the team's existing pending row and creates the new one
        # inside one transaction, so a team correcting itself REPLACES its pending answer rather
        # than queueing a second one, and the organizer always sees one current answer per team.

    def __str__(self):
        return (f"Submission {self.submission_id} | match {self.match_id} "
                f"| team {self.tournament_team_id} | {self.status}")


# ════════════════════════════════════════════════════════════════════════════════════════════
# CLASH-SQUAD PER-PLAYER STATS
#
# WHY THIS EXISTS (owner 2026-08-12: "when entering results you should be able to enter for each
# player also ... then there will be stats for players too like the BR section"):
# a HeadToHeadMatch only records the SET score (round wins per team). Battle Royale results carry
# a per-player row each (kills/damage/assists via TournamentPlayerMatchStats), which is what feeds
# player profiles, the kill leaderboards and the player ranking ladders. Clash Squad had nothing
# equivalent, so a CS player's kills were always zero no matter how they played.
#
# WHY NOT REUSE TournamentPlayerMatchStats DIRECTLY: that model hangs off TournamentTeamMatchStats,
# which hangs off a BR Match. A CS stage has ONE synthetic Match for the whole bracket (see
# head_to_head.write_placement_stats), so it cannot express "per set". This model is the per-set
# grain; head_to_head.write_placement_stats then SUMS these rows per player into that single
# synthetic TournamentPlayerMatchStats row, which is how the numbers reach the existing player
# profile / kill-table / afc_rankings pipelines with no changes on their side.
#
# HOW IT CONNECTS
#   - written by afc_tournament_and_scrims/head_to_head.report_result (the optional player_stats
#     part of the body), served back by head_to_head_views._match_payload;
#   - the FE surface is the "Enter result" dialog on components/h2h-bracket.tsx, which lists both
#     rosters under the two score boxes;
#   - rosters come from TournamentTeamMember, the same frozen per-event roster BR entry uses, so
#     the in-game role stamped on the aggregated row stays consistent with BR.
# ════════════════════════════════════════════════════════════════════════════════════════════
class H2HPlayerStat(models.Model):
    """One player's line in one Clash Squad set.

    Deliberately narrow: kills, damage and assists are what an organizer can read off the CS
    end-of-set screen. The richer fields on TournamentPlayerMatchStats (deaths, headshots,
    survival) come from debugger-log ingest, which does not exist for Clash Squad, so they are
    not mirrored here rather than being stored as misleading zeroes.
    """
    h2h_player_stat_id = models.AutoField(primary_key=True)
    h2h_match = models.ForeignKey(
        HeadToHeadMatch, on_delete=models.CASCADE, related_name="player_stats")
    # Which side of the set the player was on. Kept explicitly (rather than inferred from the
    # roster) so a correction that swaps the teams cannot silently reattribute the line.
    tournament_team = models.ForeignKey(
        TournamentTeam, on_delete=models.CASCADE, related_name="h2h_player_stats")
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="h2h_player_stats")

    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)
    # False marks a rostered player who did not play this set (a substitute). Their row still
    # exists so the dialog can show the whole roster, but participation-based credit skips them.
    played = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One line per player per set. A re-report overwrites rather than appends.
        unique_together = ("h2h_match", "player")
        indexes = [
            models.Index(fields=["h2h_match"], name="idx_h2hps_match"),
            models.Index(fields=["tournament_team"], name="idx_h2hps_team"),
        ]

    def __str__(self):
        return (f"H2H stat m{self.h2h_match_id} player {self.player_id} "
                f"| {self.kills}k {self.damage}dmg {self.assists}a")


class H2HResultSubmission(models.Model):
    """A team's own proposal for a Clash Squad set result, waiting for an organizer to approve it.

    WHY IT EXISTS (owner 2026-08-12): TeamMapResultSubmission gave Battle Royale teams a way to
    send in their own results, and Clash Squad had no equivalent - it is keyed to a BR `Match` with
    a placement and kills, which a head-to-head set does not have. So on a CS event the organizer
    was still the only person who could enter anything, and players had no way to see their own
    result land.

    A SUBMISSION IS NOT A RESULT. Nothing here is read by the bracket, the standings or the
    leaderboard. APPROVING one is what calls head_to_head.report_result, the same function the
    organizer's own "Enter result" uses, so an approved submission advances the bracket byte for
    byte the way a manually typed one would.

    WHY BOTH SIDES MAY SUBMIT: unlike a BR lobby, a set has exactly two teams and one scoreline,
    and each of them knows it first-hand. Two submissions that AGREE are the strongest evidence an
    organizer can get, so the queue shows agreement explicitly and a disagreement is visible
    rather than being silently resolved by whoever typed first. A team still submits only its OWN
    players' stat lines.

    STATE MACHINE (mirrors TeamMapResultSubmission on purpose - one mental model for both)
        pending ──approve──> approved       the result is written and the bracket advances
                └─reject───> rejected       with a reason the team can read
        pending ──(the same team submits again)──> superseded

    Written and read by afc_tournament_and_scrims/h2h_submissions.py. Consumed by the player-side
    "Submit our result" control on the bracket card, and by the organizer's review queue on the
    same card.
    """
    STATUS_CHOICES = [
        ("pending", "Pending review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("superseded", "Superseded by a later submission"),
    ]

    submission_id = models.AutoField(primary_key=True)
    h2h_match = models.ForeignKey(
        HeadToHeadMatch, on_delete=models.CASCADE, related_name="result_submissions")
    # The submitting side. Always one of the two teams in the match - checked at submit time, and
    # stored so a later roster change cannot make the submission ambiguous.
    tournament_team = models.ForeignKey(
        TournamentTeam, on_delete=models.CASCADE, related_name="h2h_result_submissions")

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="h2h_result_submissions")
    submitted_at = models.DateTimeField(auto_now_add=True)

    # {"score_a": int, "score_b": int, "players": [{"player_id", "kills", "damage", "assists"}]}
    # Scores are always in the match's OWN a/b order, never "us/them", so two submissions can be
    # compared without knowing which side sent which. Stored exactly as sent and never edited, so
    # the record of what the team actually claimed survives whatever the organizer does next.
    submitted_payload = models.JSONField()
    # Anything the team wants the organizer to know ("we have a screenshot in Discord").
    note = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="h2h_result_reviews")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Why it was rejected, or what the organizer changed before approving. Read by the team.
    review_note = models.CharField(max_length=255, blank=True, default="")
    # What was actually written on approval, when the organizer corrected the proposal first.
    # Keeping BOTH payloads is what lets anyone see that a correction happened rather than having
    # to trust that it did not.
    approved_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["h2h_match", "status"], name="idx_h2hsub_match_status"),
        ]

    def __str__(self):
        return (f"H2H submission {self.submission_id} | match {self.h2h_match_id} "
                f"| team {self.tournament_team_id} | {self.status}")


# ════════════════════════════════════════════════════════════════════════════════════════════
# CLASH-SQUAD ROOM SETTINGS  (owner 2026-08-12, spec: WEBSITE/tasks/cs-room-settings-spec.md)
#
# WHAT THIS IS: the in-game custom-room configuration for a Clash Squad match, built on AFC by
# the organizer instead of being agreed verbally in the lobby. Rounds, map, economy, the store,
# the long list of yes/no toggles, the per-round areas - everything the Free Fire room screen
# offers - plus the room ID and password players need to actually get in.
#
# WHY IT EXISTS: a CS stage carried NO room configuration at all. Teams learned the rules in the
# lobby or from a Discord message, and any dispute ("we agreed headshot off") had no record on
# the platform. The owner asked for the settings to live where the event lives and to be
# readable by players before they play.
#
# TWO MODELS
#   CSRoomConfig  - one configuration ATTACHED to exactly one scope (event / stage / group /
#                   match). Resolution for a given match is match -> group -> stage -> event,
#                   so "apply to every match in this stage" is a stage-scoped row and an
#                   exception on the grand final is a match-scoped one. No copy-per-match.
#   CSRoomPreset  - a saved, reusable configuration: AFC-global (the six Free Fire preset modes,
#                   seeded read-only) or owned by one organization (their house rules).
#                   Applying a preset COPIES its values into a config; it does NOT link, so
#                   editing a preset later cannot silently rewrite an event that already ran.
#
# STORAGE SHAPE: named columns for what we filter, sort or show prominently (rounds, map,
# economy ...) plus JSON for the long tail (the ~110-item store, the per-round economy and the
# per-round areas). Garena changes that tail every patch; a JSON document avoids a migration per
# gun while the named columns keep the common reads cheap and legible. The option lists
# themselves live in cs_room_catalogue.py, never here.
#
# HOW IT CONNECTS
#   - option lists + defaults: cs_room_catalogue.py
#   - resolver, validation, preset apply, player summary: cs_room.py
#   - endpoints: cs_room_views.py (mounted under events/, see urls.py)
#   - FE: lib/csRoom.ts -> components/cs-room-settings.tsx (the admin editor, reachable from the
#     bracket card and from a single match) and components/cs-room-card.tsx (what players read on
#     the public event page and under a bracket match).
# ════════════════════════════════════════════════════════════════════════════════════════════
class CSRoomSettingsBase(models.Model):
    """The settings themselves, shared by a scoped config and a reusable preset.

    Abstract on purpose: a preset IS a configuration, just one that is not attached to anything
    yet, so both surfaces edit the identical field set and one serializer covers both. The
    alternative (a preset holding an opaque JSON blob) would have drifted from the config shape
    the first time a column was added.
    """
    # ── core (named columns: shown in the summary line, filtered on, sorted by) ──
    rounds = models.PositiveSmallIntegerField(default=7)          # cs_room_catalogue.ROUND_CHOICES
    economy = models.CharField(max_length=20, default="500")      # ECONOMY_CHOICES value
    special_mode = models.CharField(max_length=32, default="no")  # SPECIAL_MODE_CHOICES value
    special_airdrop = models.CharField(max_length=32, default="no")
    hp = models.PositiveSmallIntegerField(default=200)
    ep = models.PositiveSmallIntegerField(default=0)
    movement_speed = models.PositiveSmallIntegerField(default=100)   # percent
    jump_height = models.PositiveSmallIntegerField(default=100)      # percent
    environment = models.CharField(max_length=8, default="day")      # day | night
    map_name = models.CharField(max_length=32, default="nexterra")   # MAP_CHOICES value

    # Which built-in / organization preset this was last built from, for the "Esports Mode" line
    # in the UI. Purely a label: the values above are already copied in, so clearing it changes
    # nothing about how the room plays.
    preset_key = models.CharField(max_length=40, blank=True, default="")

    # ── the long tail (JSON: the catalogue changes every patch) ──
    # {toggle_key: bool} for every key in cs_room_catalogue.TOGGLES.
    toggles = models.JSONField(default=dict, blank=True)
    # {item_code: {"enabled": bool, "price": int}} for every weapon/item in the store.
    store = models.JSONField(default=dict, blank=True)
    # {"1": 500, "2": 900, ...} starting cash per round. Keys are STRINGS: JSON has no integer
    # keys, and MySQL hands them back as strings anyway, so we store what we will read.
    round_economy = models.JSONField(default=dict, blank=True)
    # {event_key: amount} for the winning-round / elimination / losing-streak bonuses.
    economy_events = models.JSONField(default=dict, blank=True)
    # {"1": "deca_square", ...} which area of the map each round is played in.
    areas = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True


class CSRoomConfig(CSRoomSettingsBase):
    """One room configuration attached to exactly one scope.

    EXACTLY ONE of event / stage / group / h2h_match is set - enforced by the endpoint and by
    cs_room.save_config - and each scope is unique (OneToOne), so "the config for this stage" is
    always one row that gets updated, never a pile of history.

    Read it through cs_room.resolve_for_match / resolve_for_stage, never directly, when the
    question is "what settings apply here": a match usually has no row of its own and inherits.
    """
    cs_room_config_id = models.AutoField(primary_key=True)

    # ── scope (exactly one) ──
    event = models.OneToOneField(
        Event, null=True, blank=True, on_delete=models.CASCADE, related_name="cs_room_config")
    stage = models.OneToOneField(
        Stages, null=True, blank=True, on_delete=models.CASCADE, related_name="cs_room_config")
    # Group scope is for Battle Royale stages, which do have groups. A Clash Squad bracket has
    # none, so nothing writes this today - it exists because the owner's decision was "Clash Squad
    # first, widen to Battle Royale later" and adding the column now costs nothing.
    group = models.OneToOneField(
        StageGroups, null=True, blank=True, on_delete=models.CASCADE,
        related_name="cs_room_config")
    h2h_match = models.OneToOneField(
        HeadToHeadMatch, null=True, blank=True, on_delete=models.CASCADE,
        related_name="cs_room_config")

    # ── how to get in (the part players actually need at match time) ──
    # Free Fire room IDs are numeric but stored as text: they are an identifier, never arithmetic,
    # and any leading zero must survive.
    room_id = models.CharField(max_length=40, blank=True, default="")
    room_password = models.CharField(max_length=40, blank=True, default="")
    # Anything the organizer wants players to read that is not a setting ("join 10 minutes early").
    notes = models.TextField(blank=True, default="")
    # Off until the organizer is ready: an unpublished config is visible to managers only, so a
    # room ID is not sitting on a public page hours early for anyone to walk into.
    is_published = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["stage"], name="idx_csroom_stage"),
            models.Index(fields=["h2h_match"], name="idx_csroom_match"),
        ]

    @property
    def scope(self):
        """'match' | 'group' | 'stage' | 'event' - narrowest first, matching resolution order."""
        if self.h2h_match_id:
            return "match"
        if self.group_id:
            return "group"
        if self.stage_id:
            return "stage"
        return "event"

    @property
    def scope_object_id(self):
        return self.h2h_match_id or self.group_id or self.stage_id or self.event_id

    def __str__(self):
        return f"CS room config ({self.scope} #{self.scope_object_id}) {self.rounds} rounds"


class CSRoomPreset(CSRoomSettingsBase):
    """A saved room configuration an organizer can apply to any event.

    organization NULL = an AFC-global preset (the six Free Fire modes, seeded by
    `manage.py seed_cs_room_presets`); set = that organization's house rules, visible only to its
    members. is_builtin marks the seeded ones so the UI can stop anyone editing or deleting them.

    Applying COPIES the values into a CSRoomConfig (cs_room.apply_preset). No foreign key points
    from a config back to a preset, on purpose: an event that has already been played must not
    change because someone edited a preset afterwards.
    """
    cs_room_preset_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True, default="")
    organization = models.ForeignKey(
        "afc_organizers.Organization", null=True, blank=True, on_delete=models.CASCADE,
        related_name="cs_room_presets")
    is_builtin = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_builtin", "name"]
        # One name per owner. Two organizations may both call a preset "Finals"; one organization
        # may not have two. A NULL organization is the AFC-global set, which MySQL treats as
        # distinct per row, so the built-ins are additionally guarded by an idempotent seed.
        unique_together = ("organization", "name")

    def __str__(self):
        owner = self.organization.name if self.organization_id else "AFC"
        return f"CS preset '{self.name}' ({owner})"
