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

# Count/aggregate helpers used by the directory endpoint to derive per-org stats in
# the database (no per-row Python loops / N+1 fetches of each org's events).
#   Count → events per org / verified events per org
#   Min   → best (lowest-numbered) tournament_tier code across an org's events
#   Q     → scope each aggregate to published (non-draft) events only
from django.db.models import Count, Min, Q

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


# ── tier helper ───────────────────────────────────────────────────────────────
# Map the Event.tournament_tier code (tier_1 / tier_2 / tier_3) to the badge label
# the frontend directory card shows ("Tier 1" …). The "best" (lowest-numbered) tier
# an org has ever run is the strongest signal of its standing, so the directory
# surfaces that. Returns None when the org has run no tiered events yet.
#
# We sort by the raw code: "tier_1" < "tier_2" < "tier_3" lexicographically, so the
# DB Min() over tournament_tier already gives us the best tier without a CASE.
def _tier_label(tier_code):
    return {
        "tier_1": "Tier 1",
        "tier_2": "Tier 2",
        "tier_3": "Tier 3",
    }.get(tier_code)


# ── public organizer DIRECTORY: GET /organizers/get-organizations-public/ ─────
# Unauthenticated. Powers the new "Organizers" tab on the frontend /tournaments
# page (app/(user)/tournaments/page.tsx → <OrganizerDirectory/>). Returns every
# ACTIVE organization that has at least one published (non-draft) event, each with
# the branding + derived stats the directory card needs in ONE round-trip:
#
#   logo, name, slug, description  → real branding columns on Organization
#   event_count                    → number of that org's published events
#   verified                       → True when the org has >=1 event whose results
#                                     AFC has marked rankings_verified (a real
#                                     integrity signal — NOT a separate "verified"
#                                     flag, which the schema does not have)
#   tier                           → "Tier 1/2/3" derived from the BEST tournament_tier
#                                     across the org's events (null if none) — also
#                                     real data off the events, not invented
#
# Request:  GET, no auth, no body. No params (the directory is small; the frontend
#           filters/sorts client-side, mirroring how /tournaments already filters).
# Response: 200 {"organizations": [ {slug, name, logo, description, event_count,
#           verified, tier}, … ] }, sorted by event_count desc then name.
#
# Why an endpoint and not "derive entirely from the events list": the events feed
# (events/get-all-events/) carries organization_id/name/slug but NOT the org LOGO or
# description. The directory card needs the logo, so we expose it here. AFC-official
# events (organization=None) are surfaced by the frontend itself from the events it
# already holds — they are not a real Organization row, so they don't belong in this
# list. Suspended/deleted orgs are excluded (status="active" only), same visibility
# rule as get_organization_public above.
@api_view(["GET"])
def get_organizations_directory(request):
    # No auth header — public surface, identical posture to get_organization_public.

    # Annotate every active org with its published-event stats in the DB:
    #   - pub_event_count: count of non-draft events under the org
    #   - verified_count : how many of those have rankings_verified=True
    #   - best_tier      : the strongest (lowest-numbered) tier code across them
    # The filter= on each aggregate scopes it to published events only, so a draft
    # event never inflates a count or flips the verified flag.
    published = Q(events__is_draft=False)
    orgs = (
        Organization.objects.filter(status="active")
        .annotate(
            pub_event_count=Count("events", filter=published),
            verified_count=Count(
                "events", filter=published & Q(events__rankings_verified=True)
            ),
            best_tier=Min("events__tournament_tier", filter=published),
        )
        # Only orgs that actually have something to show in the directory.
        .filter(pub_event_count__gt=0)
        # Most-active orgs first; name breaks ties so the order is stable.
        .order_by("-pub_event_count", "name")
    )

    organizations = []
    for org in orgs:
        organizations.append({
            "slug": org.slug,
            "name": org.name,
            "logo": org.logo.url if org.logo else None,
            "description": org.description,
            "event_count": org.pub_event_count,
            # verified = the org has at least one AFC-verified event result.
            "verified": org.verified_count > 0,
            # best tier label, or null when none of its events carry a tier.
            "tier": _tier_label(org.best_tier),
        })

    return Response({"organizations": organizations}, status=status.HTTP_200_OK)
