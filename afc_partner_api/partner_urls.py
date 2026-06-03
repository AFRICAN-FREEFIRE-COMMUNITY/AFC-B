# afc_partner_api/partner_urls.py
# ──────────────────────────────────────────────────────────────────────────────
# URL map for the public partner read API, mounted in afc/urls.py at
#   path("api/v1/partner/", include("afc_partner_api.partner_urls"))
# so every route below lives under /api/v1/partner/ — the version is baked into the
# mount point (spec §9), giving us room to ship a /api/v2/partner/ later without
# breaking existing partner integrations.
#
# Seven GET endpoints, each a function-based @api_view (DRF returns 405 for the wrong
# verb, so one path() per view is enough). Events are addressed by their human-readable
# slug (<slug:event_slug>) — never the raw event_id — because the serializer firewall
# never exposes PKs, so the slug is the only handle a partner ever has.
# Full spec: WEBSITE/tasks/partner-api-design.md (§9 endpoints).
# ──────────────────────────────────────────────────────────────────────────────
from django.urls import path

from . import views_partner

urlpatterns = [
    # Event list + detail (both gated by can_read_events).
    path("events/", views_partner.list_events, name="partner_list_events"),
    path("events/<slug:event_slug>/", views_partner.event_detail, name="partner_event_detail"),

    # Per-event nested resources, each gated by its own can_read_* toggle and each
    # resolving the event (scope-checked, 404 if out of scope) before reading children.
    path("events/<slug:event_slug>/stages/", views_partner.event_stages, name="partner_event_stages"),
    path("events/<slug:event_slug>/matches/", views_partner.event_matches, name="partner_event_matches"),
    path("events/<slug:event_slug>/standings/", views_partner.event_standings, name="partner_event_standings"),
    path("events/<slug:event_slug>/teams/", views_partner.event_teams, name="partner_event_teams"),
    path("events/<slug:event_slug>/players/", views_partner.event_players, name="partner_event_players"),
]
