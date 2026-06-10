"""
URL configuration for afc project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path("auth/", include('afc_auth.urls')),
    path("tournament-leaderboard/", include('afc_leaderboard_calc.urls')),
    path("events/", include('afc_tournament_and_scrims.urls')),
    path("team/", include('afc_team.urls')),
    path("awards/", include('afc_awards.urls')),
    path("shop/", include('afc_shop.urls')),
    path("player/", include('afc_player.urls')),
    path("player-market/", include('afc_player_market.urls')),
    path("events/", include('afc_ocr.urls')),
    path("rankings/", include('afc_rankings.urls')),
    path("organizers/", include('afc_organizers.urls')),
    # Standalone Leaderboards (afc_leaderboard, Phase 1). Event-less leaderboards an AFC admin or
    # organizer creates with real-or-ghost participants + per-map results. Routes live under
    # leaderboards/standalone/… (distinct from the event-tied tournament-leaderboard/ prefix above).
    path("leaderboards/", include('afc_leaderboard.urls')),
    # Versioned, read-only partner data API (afc_partner_api). Mounted under a /v1/
    # prefix so a future breaking version can ship as /api/v2/partner/ without
    # disrupting existing partner integrations.
    path("api/v1/partner/", include('afc_partner_api.partner_urls')),
    # AFC-staff partner-admin surface (provision partners, set scope/toggles, issue/
    # revoke keys, publish events). Mounted at partners/ so its routes are
    # partners/admin/… — the human Bearer-authenticated provisioning surface, kept
    # OFF the versioned partner-facing read tree above.
    path("partners/", include('afc_partner_api.admin_urls')),

]

# In development, the Django dev server must serve uploaded media itself
# (in production this is handled by S3/static hosting).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
