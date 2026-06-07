from django.urls import path, include
from .views import *
# Moderation views (reporting + bans) live in their own module, mirroring the
# afc_organizers split (views_reports.py). Imported explicitly so the names are clear.
from .views_moderation import (
    file_market_report,
    admin_list_market_reports,
    admin_update_market_report,
    admin_market_ban,
)
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("create-recruitment-post/", create_recruitment_post, name="create_recruitment_post"),
    path("get-recruitment-posts/", get_recruitment_posts, name="get_recruitment_posts"),
    path("view-team-recruitment-posts/", view_all_team_recruitment_post, name="view_all_team_recruitment_post"),
    path("view-player-availability-posts/", view_all_player_availability_post, name="view_all_player_availability_post"),
    path("apply-to-team/", apply_to_team, name="apply_to_team"),
    path("view-applications/", view_applications, name="view_applications"),
    path("update-application-status/", update_application_status, name="update_application_status"),
    path("get-player-contact/", get_player_contact, name="get_player_contact"),
    path("finalize-trial/", finalize_trial, name="finalize_trial"),
    # removed duplicate "view-applications/" route (already registered above on line 13)
    path("view-my-applications/", view_my_applications, name="view_my_applications"),
    path("application-details/", view_application_details, name="view_application_details"),
    path("trial-chat/messages/", get_trial_chat_messages, name="get_trial_chat_messages"),
    path("trial-chat/send/", send_trial_chat_message, name="send_trial_chat_message"),
    path("trial-chats/", get_my_trial_chats, name="get_my_trial_chats"),
    path("invite-player-to-trial/", invite_player_to_trial, name="invite_player_to_trial"),
    path("my-trial-invites/", view_my_trial_invites, name="view_my_trial_invites"),
    path("respond-to-trial-invite/", respond_to_direct_trial_invite, name="respond_to_direct_trial_invite"),
    path("admin/all-trials-and-applications/", view_all_trials_and_applications, name="view_all_trials_and_applications"),
    path("post-details/", get_post_details, name="get_post_details"),
    path("my-posts/", get_posts_related_to_me, name="get_posts_related_to_me"),
    path("edit-post/", edit_recruitment_post, name="edit_recruitment_post"),
    path("delete-post/", delete_recruitment_post, name="delete_recruitment_post"),

    # ── Moderation: reporting + bans (feature "J-market-reporting") ──────────────
    # User files a report against a post; moderators triage the queue + ban subjects.
    # See afc_player_market/views_moderation.py.
    path("report-post/", file_market_report, name="file_market_report"),
    path("admin/reports/", admin_list_market_reports, name="admin_list_market_reports"),
    path("admin/reports/<int:report_id>/", admin_update_market_report, name="admin_update_market_report"),
    path("admin/ban/", admin_market_ban, name="admin_market_ban"),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)