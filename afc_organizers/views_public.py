# afc_organizers/views_public.py
# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC (unauthenticated) organizer-page endpoint.
#
# This module holds the single read-only view that powers the public organization
# profile page (frontend route /organizations/<slug>). It is deliberately kept
# separate from the authenticated organizer/admin views so the route mounting and
# auth posture stay obvious: anything in here is world-readable.
#
# Why "public" still defines a Bearer-token auth helper:
#   The org page itself needs no login, BUT this file follows the same hand as
#   afc_tournament_and_scrims / afc_team — function-based @api_view views, manual
#   inline dict serialization (no serializers.py), and the validate_token() pattern
#   imported the same way afc_team/views.py imports it. Keeping the shape identical
#   means the original dev can read these views without friction, and any future
#   "members-only" sibling view dropped in here already has the auth helper to hand.
#
# Route mounting lives in afc_organizers/urls.py and is owned by the coordinator —
# this file ONLY defines the view function(s).
# ──────────────────────────────────────────────────────────────────────────────
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token is imported the SAME way afc_team/views.py does it (from afc_auth.views).
# It returns the User for a valid, non-expired session token, or None otherwise.
from afc_auth.views import validate_token

from afc_organizers.models import Organization
from afc_tournament_and_scrims.models import Event


# ── public org profile: GET /organizations/<slug> ─────────────────────────────
# Unauthenticated. Returns the org's public branding + its public events so the
# frontend can render the organizer profile page in one round-trip.
#
# Visibility rule: only "active" organizations are exposed. A suspended or
# soft-deleted org must look identical to a non-existent one (same 404 body) so we
# never leak that a slug exists but is frozen.
@api_view(["GET"])
def get_organization_public(request, slug):
    # No auth header needed here — this is a public surface. The Bearer helper above
    # exists so a future members-only sibling view can reuse the same pattern.

    # Fetch by slug. We do NOT use get_object_or_404 because we want the exact same
    # 404 body for "missing" and "not active" (see visibility rule above).
    org = Organization.objects.filter(slug=slug).first()
    if org is None or org.status != "active":
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── org's public events ──
    # Public = published (is_draft=False). Newest first by start_date, which is the
    # real date field on Event (confirmed against afc_tournament_and_scrims/models.py;
    # there is no separate "event_date" column — start_date is it).
    events = Event.objects.filter(
        organization=org, is_draft=False
    ).order_by("-start_date")

    # Small card dicts — only the fields the public profile page needs to list an event.
    # Image fields use (img.url if img else None) per the endpoint contract.
    events_data = []
    for event in events:
        events_data.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            # slug is the public handle the frontend uses to link to the event page.
            "slug": event.slug,
            "banner": event.event_banner.url if event.event_banner else None,
            "status": event.event_status,
            # start_date is a DateField; isoformat() gives a stable "YYYY-MM-DD" string.
            "start_date": event.start_date.isoformat() if event.start_date else None,
        })

    # ── inline serialization of the org itself ──
    # Manual dict (no serializers module), mirroring the rest of this codebase.
    # rating is null for now — the reviews/ratings aggregate is not part of this
    # endpoint yet, so we return an explicit null rather than omit the key, keeping
    # the response shape stable for the frontend.
    return Response(
        {
            "name": org.name,
            "slug": org.slug,
            "logo": org.logo.url if org.logo else None,
            "default_banner": org.default_banner.url if org.default_banner else None,
            "description": org.description,
            "socials": org.socials,
            "events": events_data,
            "rating": None,
        },
        status=status.HTTP_200_OK,
    )
