from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    path('create-team/', create_team, name='create_team'),
    path('edit-team/', edit_team, name='edit_team'),
    path('invite-member/', invite_member, name='invite_member'),
    path('review-invitation/', review_invitation, name='review_invitation'),
    path('rank-teams-into-tiers/', rank_teams_into_tiers, name='rank_team_into_tiers'),
    path('disband-team/', disband_team, name='disband_team'),
    path('transfer-ownership/', transfer_ownership, name='transfer_ownership'),
    path('send-join-request/', send_join_request, name='send_join_request'),
    path('review-join-request/', review_join_request, name='review_join_request'),
    path('view-join-requests/', view_join_requests, name='view_join_requests'),
    path('view-join-requests-for-a-team/', view_join_requests_for_a_team, name='view_join_requests_for_a_team'),
    path('edit-team', edit_team, name='edit_team'),
    path('get-all-teams/', get_all_teams, name='get_all_teams'),
    path('get-team-details/', get_team_details, name='get_team_details'),
    path('get-user-current-team/', get_user_current_team, name='get_user_current_team'),
    path('get-player-details/', get_player_details, name='get_player_details'),
    path('generate-invite-link/', generate_invite_link, name='generate_invite_link'),
    path('respond-invite/<int:invite_id>/', respond_invite, name='respond_invite'),
    path('get-team-details-based-on-invite/<int:invite_id>/', get_team_details_based_on_invite, name='get_team_details_based_on_invite'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)