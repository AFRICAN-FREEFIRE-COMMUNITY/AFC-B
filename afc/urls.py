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
    path("events/", include('afc_tournament_and_scrims.urls')),
    path("team/", include('afc_team.urls')),
    path("awards/", include('afc_awards.urls')),
    path("shop/", include('afc_shop.urls')),
    path("player/", include('afc_player.urls')),
    path("player-market/", include('afc_player_market.urls')),
    path("events/", include('afc_ocr.urls')),
    path("rankings/", include('afc_rankings.urls')),
    path("organizers/", include('afc_organizers.urls')),
    # Sponsor-system redesign P1 (afc_sponsors): sponsor ENTITIES + admin-assigned members +
    # the member-scoped sponsor portal (a ydpay member sees only ydpay). Spec:
    # WEBSITE/tasks/sponsors-redesign-design.md; replaces the user-keyed SponsorEvent dashboard
    # over P2's cutover.
    path("sponsors/", include('afc_sponsors.urls')),
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
    # partners/admin/…, the human Bearer-authenticated provisioning surface, kept
    # OFF the versioned partner-facing read tree above.
    path("partners/", include('afc_partner_api.admin_urls')),
    # AFC acting as an OpenID Connect PROVIDER ("Sign in with AFC"). Everything under this
    # prefix is django-oauth-toolkit's standard OIDC surface (authorize, token, userinfo,
    # discovery, JWKS) plus, from later tasks, AFC's own consent screen. See afc_sso/urls.py
    # and WEBSITE/tasks/afc-sso-provider-design.md.
    path("sso/", include('afc_sso.urls')),
    # AFC's own WhatsApp Cloud API integration. The ONE public route here is
    # /whatsapp/webhook/, which Meta calls: a GET to verify the URL, then POSTs
    # carrying delivery receipts for messages we sent and messages players send us.
    # Every POST must be HMAC-signed with the Meta app secret. See afc_whatsapp/apps.py.
    path("whatsapp/", include('afc_whatsapp.urls')),
    # Always-on, REUSABLE site feedback (owner backlog item 29). The two PUBLIC routes here are
    # feedback/forms/<key>/ (the widget reads a form's schema) and feedback/forms/<key>/submit/,
    # which accepts an UNAUTHENTICATED write and is therefore rate limited per sender. The
    # feedback/admin/... routes are Bearer-gated to AFC admins. See afc_feedback/views.py.
    path("feedback/", include('afc_feedback.urls')),
    # Organisations applying to become AFC partners (owner 2026-08-04). The PUBLIC routes here
    # are partner-apply/applications/ (an UNAUTHENTICATED write that can carry a logo, so it is
    # rate limited per sender), the applicant's own status/edit route, and the single-use
    # credentials claim. The partner-apply/admin/... routes are Bearer-gated to head_admin /
    # partner_admin, the same staff who run both partner products. See
    # afc_partner_apply/views_public.py and views_admin.py.
    path("partner-apply/", include('afc_partner_apply.urls')),
    # ONE poll engine, with award ballots as a preset of it (WEBSITE/tasks/polls-spec.md).
    # PUBLIC: polls/ (the listing), polls/<slug>/ (one poll, its questions and the viewer's own
    # eligibility verdict) and polls/<slug>/responses/ (answering, auth required). The
    # polls/admin/... routes are Bearer-gated to whoever may manage the poll, which is the
    # EXISTING event-admin gate composed with the organizer gate, not a new permission. Replaces
    # the awards/ routes above, which stay mounted until the historical votes are migrated.
    # See afc_polls/views.py.
    path("polls/", include('afc_polls.urls')),
    path('bot/', include('afc_bot.urls')),

]

# In development, the Django dev server must serve uploaded media itself
# (in production this is handled by S3/static hosting).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
