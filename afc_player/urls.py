from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("get-player-details/", get_player_details, name="get_player_details"),
    path("get-all-players/", get_all_users, name="get_all_users"),
    # PUBLIC player profile + stats (no auth), keyed by username/IGN — feature D.
    path("get-public-player-stats/", get_public_player_stats, name="get_public_player_stats"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)