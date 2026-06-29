from django.urls import path, include
from .views import *
# Paid-event registration payments (feature "paid-events", Phase 1): Stripe Checkout init/verify,
# the webhook backstop, and the admin escrow (list/release/refund). Kept in its own module so the
# money-handling code is isolated from the big views.py.
from .event_payments import (
    init_registration_payment,
    verify_registration_payment,
    stripe_webhook,
    admin_list_event_payments,
    admin_release_payment,
    admin_refund_payment,
)
# Event linking / qualification chains (feature "event-linking" P1, owner-approved 2026-06-12):
# per-stage top-N qualification into other events. Own module, same isolation rationale as
# event_payments. Spec: WEBSITE/tasks/event-linking-design.md.
from .event_links import (
    create_link,
    list_links,
    link_chain,
    public_inbound_links,
    public_structure_links,
    import_competitors,
    cancel_link,
    fire_link_view,
    decide,
)
# Clash-Squad head-to-head brackets (bracket sub-project C): generate / read / report-result
# for knockout, double-elimination and league CS stages. Engine in head_to_head.py; the
# completed-bracket placements feed the existing leaderboard + rankings pipelines via the
# sub-project D bridge (head_to_head.write_placement_stats).
from .head_to_head_views import (
    generate_h2h_bracket,
    get_h2h_bracket,
    report_h2h_match_result,
)
# Roster Discord verification (owner 2026-06-13): per-player connected/in-server
# check consumed by the registration SPONSOR step's Discord join_group panel
# (frontend SponsorEngagementForm). Own module, same isolation rationale as
# event_payments / event_links.
from .roster_discord import roster_discord_status
# Seeding undo/redo + delete-and-reseed (owner 2026-06-15): admins AND organizers reorganise
# competitor placement after the initial seed. Own module, same isolation rationale as
# event_payments / event_links. Spec: WEBSITE/tasks/seeding-undo-redo-delete-reseed-plan.md.
from .seeding_management import (
    undo_seeding,
    reseed_into_groups,
    delete_group_managed,
    delete_stage_managed,
    move_team_between_groups,
    sync_entry_stage_seeding,
)
# Branching advancement routing (feature #9, owner plan WEBSITE/tasks/advancement-routing-plan.md):
# run a stage's StageAdvancementRule rows to seed each rule's [from..to] finishers into a later
# stage. Own module (same isolation rationale as seeding_management / event_links). Additive — the
# legacy advance endpoints below are untouched and still serve rule-less stages.
from .advancement_routing import advance_stage_by_rules
from .views_event_graphic import event_stage_graphic
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    # path('admin-login/', admin_login, name='admin_login'),
    # Render an event STAGE's standings onto a leaderboard design -> PNG (owner 2026-06-14).
    # Mounted under events/ -> GET events/<event_id>/stages/<stage_id>/graphic/.
    path('<int:event_id>/stages/<int:stage_id>/graphic/', event_stage_graphic,
         name='event_stage_graphic'),
    path('create-event/', create_event, name='create_event'),
    path('edit-event/', edit_event, name='edit_event'),
    # Event duplication (feature "event-duplicate", 2026-06-10): clone an event's config +
    # stage/group structure into a fresh draft (NO results/registrations/teams/matches).
    # event_id in the path = the SOURCE event. Auth = AFC event admin OR org can_create_events
    # on the event's org. Consumed by the organizer + admin events lists' "Duplicate" action
    # (lib events.duplicateEvent). Full URL: events/<event_id>/duplicate-event/.
    path('<int:event_id>/duplicate-event/', duplicate_event, name='duplicate_event'),
    # Manual tournament-tier override / reset (head/super admin only); owner 2026-06-30.
    # Full URL: events/<event_id>/set-tier/. Consumed by the admin event detail page tier control.
    path('<int:event_id>/set-tier/', set_event_tier, name='set_event_tier'),

    # ── Event linking / qualification chains ── (events/<id>/links/... + events/links/<id>/...)
    path('<int:event_id>/links/create/', create_link, name='create_event_link'),      # POST
    path('<int:event_id>/links/', list_links, name='list_event_links'),               # GET
    path('<int:event_id>/links/chain/', link_chain, name='event_link_chain'),         # GET (P3 chain map)
    path('<int:event_id>/links/public/', public_inbound_links, name='public_inbound_links'),  # GET (public provenance)
    # Public, no-auth structure read (owner 2026-06-15): BOTH directions of an event's
    # qualification links (inbound feeders + outbound destinations, with slugs) for the public
    # tournament page's "Qualification Links" chips. See event_links.public_structure_links.
    path('<int:event_id>/links/structure/', public_structure_links, name='public_structure_links'),  # GET (public structure)
    # Event MERGE (owner 2026-06-12): bulk-enter every confirmed competitor of N same-type
    # source events into this one (e.g. the Dynasty Cup country events into one finals).
    path('<int:event_id>/import-competitors/', import_competitors, name='import_event_competitors'),  # POST
    path('links/<int:link_id>/fire/', fire_link_view, name='fire_event_link'),        # POST
    path('links/<int:link_id>/decide/', decide, name='decide_event_link'),            # POST
    path('links/<int:link_id>/', cancel_link, name='cancel_event_link'),              # DELETE

    # ── Clash-Squad head-to-head brackets (sub-project C) ──
    # Generate (admin/organizer, seed-ordered team_ids), public bracket read, and per-match
    # result entry. Full URLs: events/stages/<id>/bracket/... + events/h2h-matches/<id>/result/.
    path('stages/<int:stage_id>/bracket/generate/', generate_h2h_bracket, name='generate_h2h_bracket'),  # POST
    path('stages/<int:stage_id>/bracket/', get_h2h_bracket, name='get_h2h_bracket'),                     # GET (public)
    path('h2h-matches/<int:match_id>/result/', report_h2h_match_result, name='report_h2h_match_result'), # POST

    # ── Paid-event registration payments (Stripe) ──
    path('init-registration-payment/', init_registration_payment, name='init_registration_payment'),
    path('verify-registration-payment/', verify_registration_payment, name='verify_registration_payment'),
    path('stripe-webhook/', stripe_webhook, name='stripe_event_webhook'),
    path('admin/event-payments/', admin_list_event_payments, name='admin_list_event_payments'),
    path('admin/event-payments/release/', admin_release_payment, name='admin_release_payment'),
    path('admin/event-payments/refund/', admin_refund_payment, name='admin_refund_payment'),
    # DEPRECATED / HIDDEN: like create-leaderboard-manually, this manual create path is
    # no longer used - leaderboards are created AUTOMATICALLY for every group at event
    # setup (create_event + edit_event sync). Its only FE caller, UpdatedConfigurePointSystem,
    # is dead code (imported by nothing). Route commented out so it can't be reached; the
    # view function is kept (marked deprecated) but unused.
    # path('create-leaderboard/', create_leaderboard, name='create_leaderboard'),
    path('get-all-events/', get_all_events, name='get_all_events'),
    path('get-event-details/', get_event_details, name='get_event_details'),
    path('get-all-events-paginated/', get_all_events_paginated, name='get_all_events_paginated'),
    path('get-all-tournaments-and-scrims/', get_all_tournaments_and_scrims, name='get_all_tournaments_and_scrims'),
    path('get-all-tournaments-and-scrims-paginated/', get_all_tournaments_and_scrims_paginated, name='get_all_tournaments_and_scrims_paginated'),
    path('get-all-tournaments-and-scrims-separated/', get_all_tournaments_and_scrims_separated, name='get_all_tournaments_and_scrims_separated'),
    path('get-all-tournaments-and-scrims-separated-paginated/', get_all_tournaments_and_scrims_separated_paginated, name='get_all_tournaments_and_scrims_separated_paginated'),
    path('get-total-events-count/', get_total_events_count, name='get_total_events_count'),
    path('get-total-tournaments-count/', get_total_tournaments_count, name='get_total_tournaments_count'),
    path('get-total-scrims-count/', get_total_scrims_count, name='get_total_scrims_count'),
    path('get-upcoming-events-count/', get_upcoming_events_count, name='get_upcoming_events_count'),
    path('get-ongoing-events-count/', get_ongoing_events_count, name='get_ongoing_events_count'),
    path('get-completed-events-count/', get_completed_events_count, name='get_completed_events_count'),
    path('get-average-participants-per-event/', get_average_participants_per_event, name='get_average_participants_per_event'),
    path('get-most-popular-event-format/', get_most_popular_event_format, name='get_most_popular_event_format'),
    path('register-for-event/', register_for_event, name='register_for_event'),
    path('get-event-details-for-admin/', get_event_details_for_admin, name='get_event_details_for_admin'),
    path('seed-solo-players-to-stage/', seed_solo_players_to_stage, name='seed_solo_players_to_stage'),
    path('seed-stage-competitors-to-groups/', seed_stage_competitors_to_groups, name='seed_stage_competitors_to_groups'),
    path('disqualify-registered-competitor/', disqualify_registered_competitor, name='disqualify_registered_competitor'),
    path('reactivate-registered-competitor/', reactivate_registered_competitor, name='reactivate_registered_competitor'),
    path('delete-event/', delete_event, name='delete_event'),
    path('remove-all-stage-competitors-from-groups-with-their-discord-roles/', remove_all_stage_competitors_from_groups_and_their_discord_roles, name='remove_all_stage_competitors_from_groups'),
    path('send-match-room-details-notification-to-competitor/', send_match_room_details_notification_to_competitor, name='send_match_room_details_notification_to_competitor'),
    path('delete-stage/', delete_stage, name='delete_stage'),
    path('delete-group/', delete_group, name='delete_group'),
    path('discord-role-progress/', discord_role_progress, name='discord_role_progress'),
    path('retry-failed-discord-roles/', retry_failed_discord_roles, name='retry_failed_discord_roles'),
    path('get-stage-role-assignment-progress/', get_stage_role_assignment_progress, name='get_stage_role_assignment_progress'),
    path('get-all-role-progress/', get_all_role_progress, name='get_all_role_progress'),
    path('get-all-user-id-in-stage/', get_all_user_id_in_stage, name='get_all_user_id_in_stage'),
    path('get-all-user-id-in-group/', get_all_user_id_in_group, name='get_all_user_id_in_group'),
    path('delete-notifications-from-users-in-a-group/', delete_notifications_from_users_in_a_group, name='delete_notification_from_users_in_a_group'),
    path('check-if-user-registered-in-event/', check_if_user_registered_in_event, name='check_if_user_registered_in_event'),
    path('upload-solo-match-result/', upload_solo_match_result, name='upload_solo_match_result'),
    path('get-all-leaderboards/', get_all_leaderboards, name='get_all_leaderboards'),
    path('sync-group-discord-roles/', sync_group_discord_roles, name='sync_group_discord_roles'),
    path('reconcile-group-roles/', reconcile_group_roles, name='reconcile_group_roles'),
    path('get-all-leaderboard-details-for-event/', get_all_leaderboard_details_for_event, name='get_all_leaderboard_details_for_event'),
    # Event group-roster view: stage -> group -> teams -> per-event roster (or solo players).
    # Read-only seeding check used during a LIVE event. Consumed by the organizer Groups page
    # (app/(organizer)/organizer/events/[slug]/groups/page.tsx, POSTs {slug}) and the admin
    # "Group Rosters" tab (app/(a)/a/events/[slug]/page.tsx, POSTs {event_id}). Full URL:
    # events/get-event-group-rosters/. Auth = AFC event admin OR org can_manage_registrations.
    path('get-event-group-rosters/', get_event_group_rosters, name='get_event_group_rosters'),
    # BR Round-Robin (sub-project B): per-day + cumulative standings for a round-robin stage.
    path('get-round-robin-standings/', get_round_robin_standings, name='get_round_robin_standings'),
    path('advance-group-competitors-to-next-stage/', advance_group_competitors_to_next_stage, name='advance_group_competitors_to_next_stage'),
    # BR Round-Robin (sub-project B): advance top-N from the CUMULATIVE table (or top-K per base group).
    path('advance-round-robin/', advance_round_robin, name='advance_round_robin'),
    # Branching advancement (feature #9): run a stage's StageAdvancementRule rows (split a stage's
    # finishers into different later stages). {event_id, stage_id, dry_run?}. Admins + organizers
    # (advancement_routing._advance_gate). dry_run = the "who routes where" preview. Fires only when
    # the stage has rules; rule-less stages keep using the two legacy advance endpoints above.
    path('advance-stage-by-rules/', advance_stage_by_rules, name='advance_stage_by_rules'),
    path('edit-solo-match-result/', edit_solo_match_result, name='edit_solo_match_result'),
    path('edit-leaderboard/', edit_leaderboard, name='edit_leaderboard'),
    path('remove-non-nigeria-registered-competitors/', remove_non_nigeria_registered_competitors, name='remove_non_nigeria_registered_competitors'),
    path('delete-match/', delete_match, name='delete_match'),
    # Redo map (owner 2026-06-15): wipe one map's results without deleting the Match.
    # FE: app/(a)/a/leaderboards/[id]/edit/page.tsx "Redo this map" button.
    path('clear-match-result/', clear_match_result, name='clear_match_result'),
    # Seeding management (owner 2026-06-15): undo/redo group seeding + delete group/stage with a
    # disposition choice (auto-redistribute / manual / delete-all). Admin + organizer (org-aware).
    # FE: ActionsTab "Seeding management" section + organizer event groups page.
    path('seeding/undo/', undo_seeding, name='seeding_undo'),
    path('seeding/reseed/', reseed_into_groups, name='seeding_reseed'),
    path('seeding/delete-group/', delete_group_managed, name='seeding_delete_group'),
    path('seeding/delete-stage/', delete_stage_managed, name='seeding_delete_stage'),
    # F2 (owner 2026-06-19): move a team/player between groups in a stage (drag-and-drop).
    path('seeding/move-team/', move_team_between_groups, name='seeding_move_team'),
    # Stats-page safety-net (owner 2026-06-21): idempotently auto-seed every registration into the
    # entry stage's groups. Fired by the admin + organizer leaderboard pages on open. Gated admin+org.
    path('seeding/sync-entry-stage/', sync_entry_stage_seeding, name='seeding_sync_entry_stage'),
    # Reorder stages / groups (manual drag-to-arrange, owner 2026-06-15). Default order=0 means
    # "auto-arrange by date/time"; these endpoints write 1-based orders that override the date sort
    # (and return a `warning` when the manual order diverges from the schedule). Views
    # reorder_stages / reorder_groups live in views.py (auto-imported via `from .views import *`).
    # FE: app/(a)/a/events/[slug]/edit/_components/StagesGroupsTab.tsx drag handles.
    path('reorder-stages/', reorder_stages, name='reorder_stages'),
    path('reorder-groups/', reorder_groups, name='reorder_groups'),
    path('edit-match-details/', edit_match_details, name='edit_match_details'),
    # DEPRECATED / HIDDEN: manual leaderboard creation is no longer used. Leaderboards
    # are created AUTOMATICALLY for every group when an event's stages/groups/maps are
    # set up (create_event + edit_event sync). The route is commented out so the dead
    # endpoint can't be reached; the view function is kept (marked deprecated) but
    # unused. The FE "Create Leaderboard" entry points are hidden to match.
    # path('create-leaderboard-manually/', create_leaderboard_manually, name='create_leaderboard_manually'),
    path('enter-solo-match-result-manual/', enter_solo_match_result_manual, name='enter_solo_match_result_manual'),
    path('enter-team-match-result-manual/', enter_team_match_result_manual, name='enter_team_match_result_manual'),
    path('edit-match-result/', edit_match_result, name='edit_match_result'),
    path('disqualify-player/', disqualify_player, name='disqualify_player'),
    path('disqualify-team/', disqualify_team, name='disqualify_team'),
    # Team management (owner 2026-06-22): remove a team from an event ENTIRELY (frees the slot;
    # distinct from disqualify) + reactivate a disqualified team. Gated admin/organizer.
    path('remove-team-from-event/', remove_team_from_event, name='remove_team_from_event'),
    path('reactivate-team/', reactivate_team, name='reactivate_team'),
    path('sync-event-registrations-with-discord-roles/', sync_event_registrations_with_discord_roles, name='sync_event_registrations_with_discord_roles'),
    path('get-event-details-not-logged-in/', get_event_details_not_logged_in, name='get_event_details_not_logged_in'),
    path('validate-team-roster-discord/', validate_team_roster_discord, name='validate_team_roster_discord'),
    path('get-drafted-events/', get_drafted_events, name='get_drafted_events'),
    path('get-my-drafted-events/', get_my_drafted_events, name='get_my_drafted_events'),
    path("get-total-kills/", get_total_kills, name="get_total_kills"),
    path("generate-single-use-invite-link-for-private-event/", generate_single_use_invite_link_for_private_event, name="generate_single_use_invite_link_for_private_event"),
    path("generate-multiple-single-use-invite-links-for-private-event/", generate_multiple_single_use_invite_links_for_private_event, name="generate_multiple_single_use_invite_links_for_private_event"),
    path("get-all-invite-links-for-private-event/", get_all_invite_links_for_private_event, name="get_all_invite_links_for_private_event"),
    path("check-invite-token-status/", check_invite_token_status, name="check_invite_token_status"),
    path("leave-event/", leave_event, name="leave_event"),
    path("seed-event-competitors-to-stage/", seed_event_competitors_to_stage, name="seed_event_competitors_to_stage"),
    path("seed-stage-competitors-to-groups-team/", seed_stage_competitors_to_groups_team, name="seed_stage_competitors_to_groups_team"),
    path("upload-team-match-result/", upload_team_match_result, name="upload_team_match_result"),
    path("add-teams-to-event/", add_teams_to_event, name="add_teams_to_event"),
    path("add-teams-to-stage/", add_teams_to_stage, name="add_teams_to_stage"),
    path("add-teams-to-group/", add_teams_to_group, name="add_teams_to_group"),
    path("get-all-tournament-player-match-stats/", get_all_tournament_player_match_stats, name="get_all_tournament_player_match_stats"),
    path("confirm-player/", confirm_player, name="confirm_player"),
    path("reject-player/", reject_player, name="reject_player"),
    path("get-all-competitors-and-their-sponsor-id/", get_all_competitors_and_their_sponsor_id, name="get_all_competitors_and_their_sponsor_id"),
    path("create-sponsor-account/", create_sponsor_account, name="create_sponsor_account"),
    path("assign-sponsor-to-event/", assign_sponsor_to_event, name="assign_sponsor_to_event"),
    path("get-all-sponsors/", get_all_sponsors, name="get_all_sponsors"),
    path("get-list-of-players-in-sponsor-event/", get_list_of_players_in_sponsor_event, name="get_list_of_players_in_sponsor_event"),
    path("edit-match-scoring-config/", edit_match_scoring_config, name="edit_match_scoring_config"),
    path("get-sponsor-details/", get_sponsor_details, name="get_sponsor_details"),
    path("edit-sponsor-details/", edit_sponsor_details, name="edit_sponsor_details"),
    path("edit-roster/", edit_roster, name="edit_roster"),
    # Roster-edit window toggle (owner 2026-06-15): organizer/admin opens/closes a time-boxed window
    # that lets team captains edit their event roster past registration close (auto-closes, capped at
    # event end). View: set_roster_edit_window. Consumed by the admin + organizer event-manage toggle.
    path("roster-edit-window/", set_roster_edit_window, name="set_roster_edit_window"),
    # PER-TEAM roster-edit allowance (owner 2026-06-24): same as the event-wide window but for ONE team.
    # View: set_team_roster_edit_window. Consumed by the admin + organizer per-team roster control.
    path("team-roster-edit-window/", set_team_roster_edit_window, name="set_team_roster_edit_window"),
    # ── Letter avatars (A-Z) per-event team assignment (feature #7, owner 2026-06-29) ──
    # assign-team-letter: set/clear the UNIQUE-per-event letter for one registered team (admin/org),
    #   notifying its members (+ optional email broadcast). event-team-letters: paginated list of every
    #   registered team's live available_letters + assigned_letter + member_count for the assign UI.
    #   Both gated _is_event_admin OR org can_manage_registrations. Consumed by RegisteredTeamsTab.
    path("assign-team-letter/", assign_team_letter, name="assign_team_letter"),
    path("event-team-letters/", get_event_team_letters, name="get_event_team_letters"),
    # Admin / organizer single-player roster add (roster-rules, 2026-06-15). Additive sibling of
    # edit-roster/: appends ONE TournamentTeamMember. View: add_player_to_event_roster.
    path("add-player-to-event-roster/", add_player_to_event_roster, name="add_player_to_event_roster"),
    path("get-roster-details/", get_roster_details, name="get_roster_details"),
    path("total-members-this-month/", total_members_this_month, name="total_members_this_month"),
    path("total-team-this-month/", total_teams_this_month, name="total_teams_this_month"),
    path("total-active-tournaments/", total_active_tournaments, name="total_active_tournaments"),
    path("total-published-news/", total_published_news, name="total_published_news"),
    path("upload-match-result-image/", upload_match_result_image, name="upload_match_result_image"),
    path("get-match-result-images/", get_match_result_images, name="get_match_result_images"),
    path("delete-match-result-image/", delete_match_result_image, name="delete_match_result_image"),
    path("cancel-event/", cancel_event, name="cancel_event"),
    path("complete-event/", complete_event, name="complete_event"),
    # Reopen a completed event (owner 2026-06-25): admins OR organizers (can_edit_events) flip a
    # completed event back to active to fix/add results. Consumed by the shared ActionsTab "Reopen".
    path("reopen-event/", reopen_event, name="reopen_event"),
    # Per-event results visibility (owner 2026-06-29): admins OR organizers (can_edit_events) publish
    # or hide an event's PUBLIC standings (social-reveal timing). Consumed by the shared ActionsTab
    # "Results visibility" toggle; the public detail endpoints withhold standings when hidden.
    path("set-results-visibility/", set_results_visibility, name="set_results_visibility"),
    # Flagged-kill controls (owner 2026-06-16): list ringers + the event default, flip the event
    # default, override one flagged player. Each mutation recomputes the event's team totals.
    # Consumed by the FlaggedKillsPanel on the event leaderboard editor (admin + organizer).
    path("flagged-kills/", get_event_flagged_kills, name="get_event_flagged_kills"),
    path("flagged-kills/set/", set_event_flagged_kills, name="set_event_flagged_kills"),
    path("flagged-kills/flag/", set_match_kill_flag, name="set_match_kill_flag"),
    path("broadcast-announcement/", broadcast_announcement, name="broadcast_announcement"),
    # per-group broadcast composer (AFC official + organizer). See broadcast_to_group.
    path("broadcast-to-group/", broadcast_to_group, name="broadcast_to_group"),
    # per-stage broadcast (all groups in a stage) + per-event broadcast history + event typeahead. owner 2026-06-17.
    path("broadcast-to-stage/", broadcast_to_stage, name="broadcast_to_stage"),
    # per-MAP (single match) room-details broadcast. owner 2026-06-18. See broadcast_match_room_details.
    path("broadcast-match-room-details/", broadcast_match_room_details, name="broadcast_match_room_details"),
    path("broadcast-history/", get_broadcast_history, name="get_broadcast_history"),
    # Live organizer-broadcast rate-limit snapshot for the composer counter (owner 2026-06-27).
    path("broadcast-rate-status/", broadcast_rate_status, name="broadcast_rate_status"),
    path("search/", search_events, name="search_events"),
    # Waitlist no-show + promotion (owner 2026-06-17). See mark_no_show / promote_from_waitlist / promote_next_waitlist.
    path("mark-no-show/", mark_no_show, name="mark_no_show"),
    # F1 no-show reputation (owner 2026-06-19): bulk warning badges + suggest-only detection.
    path("no-show-warnings/", get_no_show_warnings, name="get_no_show_warnings"),
    path("detect-no-shows/", detect_no_shows, name="detect_no_shows"),
    path("promote-from-waitlist/", promote_from_waitlist, name="promote_from_waitlist"),
    path("promote-next-waitlist/", promote_next_waitlist, name="promote_next_waitlist"),
    # Pause / resume a started stage (Actions tab Start -> Pause/Resume). See set_stage_status.
    path("set-stage-status/", set_stage_status, name="set_stage_status"),
    path("export-participants/", export_participants, name="export_participants"),
    # ZIP of team logos + player esport images (sets of teams/players, or everything registered
    # for an event). Admins + organizers. Consumed by the "Download media" buttons.
    path("download-esport-media/", download_esport_media, name="download_esport_media"),
    # organizers: toggle Event.rankings_verified (platform org admins only).
    path("verify-event/", verify_event, name="verify_event"),
    # Per-player Discord readiness for the sponsor join_group(discord) engagement:
    # POST {user_ids:[...]} -> [{user_id, discord_connected, in_server, ...}].
    # Any authenticated user. Full URL: events/roster-discord-status/.
    path("roster-discord-status/", roster_discord_status, name="roster_discord_status"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)