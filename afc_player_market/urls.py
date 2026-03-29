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
   
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)