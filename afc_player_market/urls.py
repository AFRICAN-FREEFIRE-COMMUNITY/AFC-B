from django.urls import path, include
from .views import *
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
    path("view-applications/", view_applications, name="view_applications"),
    path("view-my-applications/", view_my_applications, name="view_my_applications"),
    path("application-details/", view_application_details, name="view_application_details"),
    path("trial-chat/messages/", get_trial_chat_messages, name="get_trial_chat_messages"),
    path("trial-chat/send/", send_trial_chat_message, name="send_trial_chat_message"),
    path("trial-chats/", get_my_trial_chats, name="get_my_trial_chats"),
    path("invite-player-to-trial/", invite_player_to_trial, name="invite_player_to_trial"),
    path("my-trial-invites/", view_my_trial_invites, name="view_my_trial_invites"),
    path("respond-to-trial-invite/", respond_to_direct_trial_invite, name="respond_to_direct_trial_invite"),
    path("admin/all-trials-and-applications/", view_all_trials_and_applications, name="view_all_trials_and_applications"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)