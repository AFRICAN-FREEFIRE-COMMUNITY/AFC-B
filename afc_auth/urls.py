from django.urls import path, include
from .views import *
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
    path('get-news-detail/', get_news_detail, name='get_news_detail'),
    path('get-all-news/', get_all_news, name='get_all_news'),
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
    # Admin Settings broadcast history (general + direct sends). owner 2026-06-17.
    path("broadcast-history/", get_general_broadcast_history, name="get_general_broadcast_history"),
    path("view-notification/", view_notification, name="view_notification"),
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
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)