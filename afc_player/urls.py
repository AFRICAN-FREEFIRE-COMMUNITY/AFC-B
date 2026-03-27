from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("get-player-details/", get_player_details, name="get_player_details"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)