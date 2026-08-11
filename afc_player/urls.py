from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("get-player-details/", get_player_details, name="get_player_details"),
    path("get-all-players/", get_all_users, name="get_all_users"),
    # ADMIN players list: the same rows PLUS uid + email, behind a token. Feeds the search-by-UID
    # box on the Players tab (frontend PlayersAdminContent.tsx). Deliberately NOT the same
    # endpoint as get-all-players/, which is public - see admin_list_players' docstring.
    path("admin/list-players/", admin_list_players, name="admin_list_players"),
    # PUBLIC player profile + stats (no auth), keyed by username/IGN - feature D.
    path("get-public-player-stats/", get_public_player_stats, name="get_public_player_stats"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)