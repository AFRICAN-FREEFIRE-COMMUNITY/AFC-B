from django.urls import path, include
from .views import *
# Its own module rather than another function in the 3,000-line views.py: it is a
# self-contained two-endpoint feature with no overlap with account handling.
from .feature_interest import feature_interest
from django.conf import settings
from django.conf.urls.static import static
# Player-to-player reports (owner 2026-06-20) live in their own module, mirroring the
# afc_player_market moderation split. Imported explicitly so the route names are clear.
from .views_player_reports import (
    file_player_report,
    file_team_report,
    my_player_reports,
    admin_list_player_reports,
    admin_respond_player_report,
    complete_onboarding,
)
# Fan / Hater public sentiment (owner 2026-06-20). See views_sentiment.py.
from .views_sentiment import get_sentiment, set_sentiment
# Watchlist (owner 2026-06-21): shared advisory list of suspicious players/teams. See views_watchlist.py.
from .views_watchlist import (
    watchlist_collection,
    watchlist_item,
    watchlist_tags,
)
# Broadcast AUDIENCE builder (owner backlog item 15, 2026-08-03): pick WHO a broadcast goes to
# (explicit teams/players, or by tier/country/role/language, or the entire site), see the recipient
# COUNT before sending, and send through the existing deliver_broadcast chokepoint. See
# views_broadcast_audience.py + audience.py.
from .views_broadcast_audience import (
    broadcast_audience_options,
    broadcast_audience_preview,
    broadcast_audience_send,
)
# Two-factor authentication (owner 2026-08-06, authenticator apps added 2026-08-07). Opt-in codes
# as a second sign-in step, by email or from an authenticator app; see views_two_factor.py for the
# endpoint docs and two_factor.py for the rules they enforce.
from .views_two_factor import (
    two_factor_verify,
    two_factor_resend,
    two_factor_status,
    two_factor_send_code,
    two_factor_enable,
    two_factor_disable,
    two_factor_regenerate_backup_codes,
    totp_setup,
    totp_confirm,
)
# Admin identity repair (owner 2026-08-07): head_admin/super_admin fixing a user's Free Fire UID or
# their account email when the user cannot. See views_admin_identity.py for the gate, the audit
# fields, and why an admin-set email arrives verified.
from .views_admin_identity import (
    admin_user_identity,
    admin_set_user_uid,
    admin_set_user_email,
    admin_set_user_username,
    admin_set_user_country,
    admin_set_user_whatsapp,
)
# Account recovery by WhatsApp (owner 2026-08-08), for a user whose emailed reset token goes to an
# inbox they cannot read. ONE proof of the number saved on the account, then TWO possible endings:
# reset the password, or move the account onto an address they can actually read. See
# views_recovery.py for the steps, why nothing here leaks whether an account exists, why a reset
# proved this way is not a way around two-step sign-in, and why the email move REFUSES outright on
# any account that has two-step sign-in switched on.
from .views_recovery import (
    recovery_start,
    recovery_verify,
    recovery_reset_password,
    recovery_request_email_change,
    recovery_confirm_email_change,
)
# Devices and sessions (owner 2026-08-08): the "remember this device" control panel. A TRUSTED
# DEVICE skips the second sign-in step for 30 days but is not a sign-in; a SESSION is being signed
# in right now. Two different things, two different controls. See views_devices.py.
from .views_devices import (
    trusted_devices_list,
    trusted_device_revoke,
    sessions_list,
    sessions_sign_out_others,
)


urlpatterns = [
    # path("admin/", admin.site.urls),
    # path('admin-login/', admin_login, name='admin_login'),
    path('signup/', signup, name='signup'),
    # Email-verification link (GET, decodes uidb64 + checks token). Uses the renamed
    # view `verify_email_token` so it no longer collides with the password-reset
    # `verify_token` POST view below (the name clash silently bound this route to the
    # wrong view and caused a TypeError 500). See views.verify_email_token.
    path('verify/<uidb64>/<token>/', verify_email_token, name='verify_email_token'),
    path('login/', login, name='login'),
    # Google Sign-In (owner 2026-06-20): verifies a Google ID token and issues a
    # SessionToken (sign up + sign in in one). Consumed by the FE "Continue with
    # Google" button -> AuthContext.loginWithGoogle. See views.google_auth.
    path('google/', google_auth, name='google_auth'),
    # ── Two-factor authentication (owner 2026-08-06) ────────────────────────────
    # Step TWO of signing in, plus the self-service switches. verify/ + resend/ are
    # PUBLIC (they run before a session exists and are gated by the challenge token
    # login/ hands out); the rest are Bearer-gated. Only users who opted in ever see
    # any of this - login/ is unchanged for everyone else. See views_two_factor.py.
    path('two-factor/verify/', two_factor_verify, name='two_factor_verify'),
    path('two-factor/resend/', two_factor_resend, name='two_factor_resend'),
    path('two-factor/status/', two_factor_status, name='two_factor_status'),
    path('two-factor/send-code/', two_factor_send_code, name='two_factor_send_code'),
    path('two-factor/enable/', two_factor_enable, name='two_factor_enable'),
    path('two-factor/disable/', two_factor_disable, name='two_factor_disable'),
    path('two-factor/backup-codes/', two_factor_regenerate_backup_codes,
         name='two_factor_regenerate_backup_codes'),
    # Authenticator app (TOTP) enrolment, owner 2026-08-07. Only ENROLMENT needed new routes:
    # signing in, resending, disabling and regenerating recovery codes are method-blind and already
    # handle an authenticator user through the routes above. setup/ hands out the secret and
    # changes nothing; confirm/ proves it and only then switches the account over.
    path('two-factor/totp/setup/', totp_setup, name='totp_setup'),
    path('two-factor/totp/confirm/', totp_confirm, name='totp_confirm'),
    # ── Account recovery by WhatsApp (owner 2026-08-08) ─────────────────────────
    # ALL OF THESE ARE PUBLIC, by definition: the caller cannot sign in, which is
    # why they are here. Each step is gated by what the step before it handed out
    # (a recovery token, then a grant token), and start/ answers every input
    # identically so it cannot be used to find out whether an account exists.
    #
    # start/ + verify/ are the shared proof. Then ONE of two endings, and whichever
    # one completes consumes the grant, so a single code cannot do both:
    #   reset-password/         the priority case, a forgotten password.
    #   request-email-change/   for the person whose inbox is dead. A second code
    #   confirm-email-change/   goes to the NEW address and has to come back before
    #                           anything is written, so a typo cannot re-lock them.
    #
    # Neither ending signs anybody in. The reset leaves two-step sign-in untouched
    # and it is still demanded at the next login; the email move REFUSES outright
    # on any account that has two-step sign-in on, with no override, which is
    # stricter than the admin-assisted path. See views_recovery.py §4.
    path('recovery/whatsapp/start/', recovery_start, name='recovery_start'),
    path('recovery/whatsapp/verify/', recovery_verify, name='recovery_verify'),
    path('recovery/whatsapp/reset-password/', recovery_reset_password,
         name='recovery_reset_password'),
    path('recovery/whatsapp/request-email-change/', recovery_request_email_change,
         name='recovery_request_email_change'),
    path('recovery/whatsapp/confirm-email-change/', recovery_confirm_email_change,
         name='recovery_confirm_email_change'),
    # ── Devices and sessions (owner 2026-08-08) ─────────────────────────────────
    # All Bearer-gated and all scoped to the caller's OWN account. The trusted list
    # is what may skip the second factor (30 days, opted into per device on the code
    # screen); the sessions list is where the account is signed in at this moment.
    # Revoking trust and signing out are deliberately separate actions, because they
    # answer different questions. See views_devices.py.
    path('devices/trusted/', trusted_devices_list, name='trusted_devices_list'),
    path('devices/trusted/revoke/', trusted_device_revoke, name='trusted_device_revoke'),
    path('devices/sessions/', sessions_list, name='sessions_list'),
    path('devices/sessions/sign-out-others/', sessions_sign_out_others,
         name='sessions_sign_out_others'),
    # Discord sign-in/sign-up (SSO) - start -> Discord, callback exchanges the code +
    # issues a session, exchange swaps the one-time handoff for the token. See views.
    path('discord/sso/start/', discord_sso_start, name='discord_sso_start'),
    path('discord/sso/callback/', discord_sso_callback, name='discord_sso_callback'),
    path('discord/sso/exchange/', discord_sso_exchange, name='discord_sso_exchange'),
    # path('logout/', logout, name='logout'),
    # ── Player-to-player reports (owner 2026-06-20) ─────────────────────────────
    # A player reports another player (proof + notes); admins triage + answer; the
    # reporter reads the answer. See afc_auth/views_player_reports.py.
    path('report-player/', file_player_report, name='file_player_report'),
    path('report-team/', file_team_report, name='file_team_report'),
    path('my-player-reports/', my_player_reports, name='my_player_reports'),
    path('admin/player-reports/', admin_list_player_reports, name='admin_list_player_reports'),
    path('admin/player-reports/<int:report_id>/', admin_respond_player_report, name='admin_respond_player_report'),

    # ── Per-event Discord bot (owner 2026-06-22): invite the AFC bot to an organizer's server +
    # verify it is in before the require-Discord toggle can be enabled. Gated admin/organizer. ──
    path('discord-bot-invite-url/', discord_bot_invite_url, name='discord_bot_invite_url'),
    path('verify-bot-in-guild/', verify_bot_in_guild, name='verify_bot_in_guild'),

    # ── Watchlist (owner 2026-06-21): shared advisory list of suspicious players/teams. ──
    # /tags/ before the <int> item route so the int converter never swallows it. Gate (admin OR
    # organizer) is inside the views. See afc_auth/views_watchlist.py.
    path('watchlist/tags/', watchlist_tags, name='watchlist_tags'),
    path('watchlist/<int:watch_id>/', watchlist_item, name='watchlist_item'),
    path('watchlist/', watchlist_collection, name='watchlist_collection'),
    # First-login onboarding: mark the skippable requirements flow done/skipped.
    path('complete-onboarding/', complete_onboarding, name='complete_onboarding'),
    # Fan / Hater public sentiment on a player or team profile (owner 2026-06-20).
    path('sentiment/', get_sentiment, name='get_sentiment'),
    path('sentiment/set/', set_sentiment, name='set_sentiment'),
    path('send-verification-token/', send_verification_token, name='send_verification_token'),
    path('verify-token/', verify_token, name='verify_token'),
    path('resend-token/', resend_token, name='resend_token'),
    path('reset-password/', reset_password, name='reset_password'),
    # path('reset-password/<uidb64>/<token>/', reset_password, name='reset_password'),
    path('verify-code/', verify_code, name='verify_code'),
    path('resend-verification-code/', resend_verification_code, name='resend_verification_code'),
    path('change-password/', change_password, name='change_password'),
    # Change email (owner 2026-07-09, bug #1). Self-serve (re-auth: current password + old email,
    # then a code to the new address) + admin-assisted recovery for locked-out legacy users.
    # Consumed by frontend profile settings "Change email" dialog + admin player-detail "Edit email".
    path('request-email-change/', request_email_change, name='request_email_change'),
    path('confirm-email-change/', confirm_email_change, name='confirm_email_change'),
    # ── Admin identity repair (owner 2026-08-07), head_admin / super_admin ONLY ──────────────
    # Fixing what a user cannot fix themselves: a wrong Free Fire UID (unique column, and frozen
    # for the player's own edits while they are in a live event) and a wrong/dead account email
    # (the self-serve flow above needs them signed in AND able to read the new inbox). Both write
    # an AuditLog row carrying who, whom, before, after and a MANDATORY typed reason.
    # Consumed by the admin player-detail page (frontend app/(a)/a/players/[id]/page.tsx).
    # set-user-email keeps its original path so the existing dialog kept working across the move.
    path('admin/user-identity/<int:user_id>/', admin_user_identity, name='admin_user_identity'),
    path('admin/set-user-uid/', admin_set_user_uid, name='admin_set_user_uid'),
    path('admin/set-user-email/', admin_set_user_email, name='admin_set_user_email'),
    # Three more repairs on the same gate (owner 2026-08-11). The in-game name is the THIRD login
    # identifier and is frozen mid-event for the player; the country decides which broadcast
    # audience they land in; the WhatsApp number is what proves ownership in account recovery.
    path('admin/set-user-username/', admin_set_user_username, name='admin_set_user_username'),
    path('admin/set-user-country/', admin_set_user_country, name='admin_set_user_country'),
    path('admin/set-user-whatsapp/', admin_set_user_whatsapp, name='admin_set_user_whatsapp'),
    path('edit-profile/', edit_profile, name='edit_profile'),
    path('get-user-profile/', get_user_profile, name='get_user_profile'),
    # Flip the current user's first-time WELCOME tour flag to seen. Bearer-auth POST.
    # Consumed by frontend app/(user)/_components/WelcomeTour.tsx on finish/skip/close.
    path('mark-welcome-seen/', mark_welcome_seen, name='mark_welcome_seen'),
    # Flip ONE dashboard's one-time intro callout to seen ({"dashboard": "sponsor"|...}).
    # Bearer-auth POST. Consumed by app/(user)/_components/DashboardIntroCoachmark.tsx on dismiss.
    path('mark-dashboard-intro-seen/', mark_dashboard_intro_seen, name='mark_dashboard_intro_seen'),
    # Upload/REPLACE the current user's esport image (multipart `esport_image`; replace-only, no
    # delete). Consumed by the profile-edit "Esport Image" section.
    path('upload-esport-image/', upload_esport_image, name='upload_esport_image'),
    path('contact-us/', contact_us, name='contact_us'),
    path('create-news/', create_news, name='create_news'),
    path('edit-news/', edit_news, name='edit_news'),
    # News-overhaul media uploads. Persist ONE image/video into local MEDIA (news_images/ /
    # news_videos/) and return {"status":"ok","url": <absolute url>}. Bearer + news-admin gated (same
    # as create_news). Consumed by the Tiptap editor image/gallery/video "Upload" tabs
    # (frontend components/text-editor/Menubar.tsx); the returned url is embedded in the article node.
    path('upload-news-image/', upload_news_image, name='upload_news_image'),
    path('upload-news-video/', upload_news_video, name='upload_news_video'),
    path('get-news-detail/', get_news_detail, name='get_news_detail'),
    path('get-all-news/', get_all_news, name='get_all_news'),
    # Homepage notices (backlog item 22, owner 2026-08-08): the news posts currently pinned to the
    # homepage, newest first, capped at HOME_PINNED_NOTICES_LIMIT. Public, no auth. Consumed by
    # frontend app/(user)/_components/HomeNotices.tsx.
    path('get-pinned-news/', get_pinned_news, name='get_pinned_news'),
    # "I want this" on a feature that does not exist yet (owner 2026-08-16). GET is public so the
    # count renders for a signed-out reader; POST needs a login so the number counts PEOPLE rather
    # than clicks. Consumed by the Fantasy League coming-soon page, frontend app/(user)/fantasy.
    path('feature-interest/', feature_interest, name='feature_interest'),
    path('delete-news/', delete_news, name='delete_news'),
    path('add-role/', add_role, name='add_role'),
    path('delete-role/', delete_role, name='delete_role'),
    path('get-admin-info/', get_admin_info, name='get_admin_info'),
    path('get-all-roles/', get_all_roles, name='get_all_roles'),
    path('get-all-user-and-user-roles/', get_all_user_and_user_roles, name='get_all_user_and_user_roles'),
    # Typeahead user lookup for the <UserSearchSelect/> picker (admin bulk-notify, team invites, etc.).
    path('search-users/', search_users, name='search_users'),
    path('suspend-user/', suspend_user, name='suspend_user'),
    path('activate-user/', activate_user, name='activate_user'),
    path('assign-roles-to-user/', assign_roles_to_user, name='assign_roles_to_user'),
    path('edit-user-roles/', edit_user_roles, name='edit_user_roles'),
    path('get-admin-history/', get_admin_history, name='get_admin_history'),
    # Sitewide automatic admin audit log (rich, auto-captured by afc_auth.middleware.AuditLogMiddleware).
    # Paginated + filterable; consumed by the admin History page frontend app/(a)/a/history/page.tsx.
    path('get-audit-log/', get_audit_log, name='get_audit_log'),
    path('get-total-number-of-users/', get_total_number_of_users, name='get_total_number_of_users'),
    path('ban-team/', ban_team, name='ban_team'),
    path('unban-team/', unban_team, name='unban_team'),
    path('ban-player/', ban_player, name='ban_player'),
    path('unban-player/', unban_player, name='unban_player'),
    path("connect-discord/callback/", discord_callback, name="discord_callback"),
    path("connect-discord/", connect_discord, name="connect_discord"),
    path("connect-discord-account/", connect_discord_account, name="connect_discord_account"),
    path("is-discord-account-connected/", is_discord_account_connected, name="is_discord_account_connected"),
    path("get-all-login-history/", get_all_login_history, name="get_all_login_history"),
    path("get-user-login-history/", get_user_login_history, name="get_user_login_history"),
    # Account-overlap (multi-account / account-sharing review signal): IPs used by >1 account.
    path("get-account-overlap/", get_account_overlap, name="get_account_overlap"),
    path("get-notifications/", get_notifications, name="get_notifications"),
    path("send-notification/", send_notification, name="send_notification"),
    path("send-notification-to-multiple-users/", send_notification_to_multiple_users, name="send_notification_to_multiple_users"),
    path("admin-send-message/", admin_send_message, name="admin_send_message"),
    # Letter-avatar assignment broadcast (owner 2026-06-29, feature #7 / plan B7): notify every member
    # of each listed team of the letter assigned to them for an event. Gated to AFC event admins OR the
    # event's organizers; respects the shared broadcast rate limit. Consumed by the events
    # SendNotificationModal "Letter assignments" mode. See views.broadcast_letter_assignments.
    path("broadcast-letter-assignments/", broadcast_letter_assignments, name="broadcast_letter_assignments"),
    # ── Broadcast AUDIENCE builder (owner backlog item 15, 2026-08-03) ──────────────────────────
    # The RECIPIENT-SELECTION half of broadcasting (delivery already existed in deliver_broadcast).
    # options/ populates the composer's filter dropdowns from real data; preview/ turns a filter
    # spec into a recipient COUNT plus an email-volume verdict (there is no undo on a broadcast, so
    # the admin must see the number first); send/ requires that count back as confirmed_count and
    # refuses an email blast the mail provider cannot deliver. Consumed by the admin
    # Settings > Notifications tab (frontend app/(a)/a/settings/_components/AudienceBuilder.tsx).
    path("admin/broadcast-audience/options/", broadcast_audience_options,
         name="broadcast_audience_options"),
    path("admin/broadcast-audience/preview/", broadcast_audience_preview,
         name="broadcast_audience_preview"),
    path("admin/broadcast-audience/send/", broadcast_audience_send,
         name="broadcast_audience_send"),
    # Admin Settings broadcast history (general + direct sends). owner 2026-06-17.
    path("broadcast-history/", get_general_broadcast_history, name="get_general_broadcast_history"),
    # Admin GLOBAL broadcast audit (ALL scopes + senders, incl. organizer event broadcasts). owner 2026-06-27.
    path("all-broadcasts/", get_all_broadcasts, name="get_all_broadcasts"),
    path("view-notification/", view_notification, name="view_notification"),
    path("view-all-notifications/", view_all_notifications, name="view_all_notifications"),
    path('get-total-players-count/', get_total_players_count, name='get_total_players_count'),
    path('get-active-players-count/', get_active_players_count, name='get_active_players_count'),
    path('get-banned-players-count/', get_banned_players_count, name='get_banned_players_count'),
    path('get-new-players-count/', get_new_players_count, name='get_new_players_count'),
    path('get-average-total-kills-per-player/', get_average_total_kills_per_player, name='get_average_total_kills_per_player'),
    path('get-top-mvp-player/', get_top_mvp_player, name='get_top_mvp_player'),
    path('get-top-winner-player/', get_top_winner_player, name='get_top_winner_player'),
    path('get-admin-activities/', get_admin_activities, name='get_admin_activities'),
    path('like-news/', like_news, name='like_news'),
    path('dislike-news/', dislike_news, name='dislike_news'),
    path('unlike-news/', unlike_news, name='unlike_news'),
    path('undislike-news/', undislike_news, name='undislike_news'),
    path('get-news-likes-dislikes-count/', get_news_likes_dislikes_count, name='get_news_likes_dislikes_count'),
    path("check-discord-membership-v2/", check_discord_membership_v2, name="check_discord_membership_v2"),
    path("check-team-members-discord-membership/", check_team_members_discord_membership, name="check_team_members_discord_membership"),
    path("disconnect-discord-account/", disconnect_discord_account, name="disconnect_discord_account"),
    # Multi-currency: public FX rates + the viewer's resolved display currency (owner 2026-06-30).
    path("fx-rates/", fx_rates, name="fx_rates"),
    # Multi-currency: set ONLY the user's display currency (dedicated, won't wipe the profile).
    path("set-currency/", set_preferred_currency, name="set_preferred_currency"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)