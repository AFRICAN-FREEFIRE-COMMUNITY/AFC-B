"""
PUBLIC sponsors of an event: logos and links every visitor sees (owner 2026-08-05, backlog item 26).

WHAT THIS IS: three endpoints and one serializer, over EventPublicSponsor. An organizer or admin
adds a sponsor's name, an optional logo, and an optional link; every visitor to the event page
sees them, logged in or not, and is asked for nothing in return.

WHAT THIS IS DELIBERATELY NOT: the registration sponsor. AFC has two of those already, and both
are gates. The legacy one lives on Event itself (is_sponsored / sponsor_name /
sponsor_field_label / sponsor_requirement_description) and makes a registrant type a value. The
current one is afc_sponsors.EventSponsorship, whose whole purpose is requires_approval +
engagements: follow this account, join that group, and a registration that does not complete
until the sponsor approves it. Nothing in this module reads or writes either of them, and
nothing in afc_sponsors reads this table, which is the point: a display-only sponsor can never
change who is allowed to register, and a registration sponsor never appears in the public strip
unless somebody deliberately adds it here too. The full reasoning for the separate table is in
the EventPublicSponsor docstring in models.py.

WHO CALLS WHAT (mounted under events/ by urls.py)
    POST   events/public-sponsors/add/                 organizer / admin -> add one (multipart)
    POST   events/public-sponsors/<id>/update/         organizer / admin -> edit one (multipart)
    DELETE events/public-sponsors/<id>/delete/         organizer / admin -> remove one
    (there is no list endpoint on purpose: the event's public_sponsors ride in the two
     get_event_details payloads the pages already load, and every write below returns the whole
     updated list, so the edit form never has to re-fetch.)

FRONTEND SURFACES THAT CONSUME THEM
    * frontend/lib/eventPublicSponsors.ts, called from the shared Sponsor tab of the admin and
      organizer event-edit wizards (app/(a)/a/events/[slug]/edit/_components/SponsorTab.tsx);
    * the public tournament page, app/(user)/tournaments/[slug]/_components/EventDetailsWrapper.tsx,
      which reads event_details.public_sponsors and renders the strip.

UNTRUSTED INPUT. The link is typed by a human and rendered as an anchor on a page anyone can
reach, and the logo is an uploaded binary served from AFC's own origin. Neither is validated
here from scratch: both go through the cleaners afc_sso/provisioning.py already wrote for the
partner logo and the partner URLs, imported the same way afc_partner_apply/views_public.py
imports them. One copy of those rules, not two.

CONVENTION NOTE: function-based @api_view views with the inline Authorization header +
validate_token preamble, the house idiom in views.py and views_team_submissions.py.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.views import validate_token
# The SAME cleaners the partner-application form uses, imported the same way
# afc_partner_apply/views_public.py imports them. _clean_url refuses anything that is not an
# absolute https URL (loopback excepted, for local development); _clean_logo_upload decodes the
# bytes with Pillow, refuses anything that is not a real PNG / JPG / WEBP, guards against
# decompression bombs, re-encodes through afc_auth.image_utils.normalize_image_upload and rebuilds
# the stored filename so no attacker-chosen extension reaches the media directory.
from afc_sso.provisioning import _clean_logo_upload, _clean_url

from .models import Event, EventPublicSponsor


# A public event page is not a link farm. The cap is generous for a real tournament (a big AFC
# event runs three or four sponsors) and small enough that the strip stays readable on a phone,
# which is where most AFC visitors are.
MAX_PUBLIC_SPONSORS = 12

# Matches EventPublicSponsor.name. Enforced here rather than left to the database, because MySQL
# would silently truncate on some configurations and a half-cut sponsor name is worse than a 400.
MAX_NAME_LENGTH = 100


# ── serialization ─────────────────────────────────────────────────────────────────────────────
def serialize_public_sponsors(event, request):
    """The `public_sponsors` list for an event, in display order (creation order).

    Called from THREE places and deliberately shared by all of them: the two public detail
    builders in views.py (get_event_details and get_event_details_not_logged_in) and every write
    endpoint below, which returns the full updated list so the edit form can replace its state
    without a second round trip.

    `request` is needed only to absolutize the logo URL, exactly as the event banner and the org
    logo are absolutized in the same payloads.
    """
    return [
        {
            "id": sponsor.id,
            "name": sponsor.name,
            "link": sponsor.link or None,
            "logo_url": request.build_absolute_uri(sponsor.logo.url) if sponsor.logo else None,
        }
        for sponsor in event.public_sponsors.all()
    ]


# ── auth ──────────────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    """Resolve the Bearer caller. Returns (user, error_response); exactly one is not None."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."},
                              status=http.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."},
                              status=http.HTTP_401_UNAUTHORIZED)
    return user, None


def _can_edit(user, event):
    """AFC event admins always; otherwise an organizer holding can_edit_events on the event's
    owning org. The SAME gate edit_event and set_results_visibility apply, because deciding which
    logos appear on the event page is editing the event. Imported lazily so this module can be
    imported from views.py without a circular import."""
    from .views import _is_event_admin, org_can_event

    return _is_event_admin(user) or org_can_event(user, "can_edit_events", event)


# ── shared field parsing ──────────────────────────────────────────────────────────────────────
def _clean_name(raw):
    """Validate the sponsor's display name. Returns (name, error_message)."""
    name = (raw or "").strip()
    if not name:
        return None, "Sponsor name is required."
    if len(name) > MAX_NAME_LENGTH:
        return None, f"Sponsor name must be {MAX_NAME_LENGTH} characters or fewer."
    return name, None


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def add_event_public_sponsor(request):
    """Add one publicly displayed sponsor to an event.

    Request (multipart, because of the logo):
        event_id  required, the event to attach to
        name      required, <= 100 chars, shown under/next to the logo and used as its alt text
        link      optional, must be an absolute https URL when present
        logo      optional image file (PNG / JPG / WEBP)
    Response 201: {message, public_sponsors: [{id, name, link, logo_url}, ...]}  (the FULL list)
    Auth: Bearer. AFC event admin, or an organizer with can_edit_events on the owning org.
    Consumed by: frontend lib/eventPublicSponsors.ts add(), from the shared SponsorTab.
    """
    user, err = _auth_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=http.HTTP_400_BAD_REQUEST)
    event = get_object_or_404(Event, event_id=event_id)

    if not _can_edit(user, event):
        return Response({"message": "You do not have permission to perform this action."},
                        status=http.HTTP_403_FORBIDDEN)

    if event.public_sponsors.count() >= MAX_PUBLIC_SPONSORS:
        return Response(
            {"message": f"An event can show at most {MAX_PUBLIC_SPONSORS} public sponsors."},
            status=http.HTTP_400_BAD_REQUEST)

    name, name_err = _clean_name(request.data.get("name"))
    if name_err:
        return Response({"message": name_err}, status=http.HTTP_400_BAD_REQUEST)

    link, link_err = _clean_url(request.data.get("link"), "Sponsor link")
    if link_err:
        return Response({"message": link_err}, status=http.HTTP_400_BAD_REQUEST)

    logo = None
    if request.FILES.get("logo"):
        logo, logo_err = _clean_logo_upload(request.FILES["logo"])
        if logo_err:
            return Response({"message": logo_err}, status=http.HTTP_400_BAD_REQUEST)
        # _clean_logo_upload was written for the SSO partner logo and names the file
        # "partner-logo.<ext>". Only the EXTENSION carries any risk and it has already been forced
        # into the safe set, so renaming the stem here is safe and keeps media/event_public_sponsors
        # readable to whoever opens that folder next.
        logo.name = f"sponsor-logo.{logo.name.rsplit('.', 1)[-1]}"

    EventPublicSponsor.objects.create(event=event, name=name, link=link or "", logo=logo)

    return Response(
        {"message": "Public sponsor added.",
         "public_sponsors": serialize_public_sponsors(event, request)},
        status=http.HTTP_201_CREATED)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def update_event_public_sponsor(request, sponsor_id):
    """Edit one publicly displayed sponsor: its name, its link, or its logo.

    Every field is OPTIONAL and only what is sent is written, so fixing a typo in the link never
    requires re-uploading the logo. Sending link="" clears the link (a sponsor may want the credit
    without the outbound click); the logo can only be replaced, not cleared, because a sponsor
    row with neither logo nor link is just a name and can be deleted instead.

    Request (multipart): name?, link?, logo?     Path: the EventPublicSponsor id.
    Response 200: {message, public_sponsors: [...]}  (the FULL list for the owning event)
    Auth: Bearer. Same gate as add_event_public_sponsor, resolved against the OWNING event.
    Consumed by: frontend lib/eventPublicSponsors.ts update(), from the shared SponsorTab.
    """
    user, err = _auth_user(request)
    if err:
        return err

    sponsor = get_object_or_404(EventPublicSponsor.objects.select_related("event"), id=sponsor_id)
    event = sponsor.event

    if not _can_edit(user, event):
        return Response({"message": "You do not have permission to perform this action."},
                        status=http.HTTP_403_FORBIDDEN)

    if "name" in request.data:
        name, name_err = _clean_name(request.data.get("name"))
        if name_err:
            return Response({"message": name_err}, status=http.HTTP_400_BAD_REQUEST)
        sponsor.name = name

    if "link" in request.data:
        link, link_err = _clean_url(request.data.get("link"), "Sponsor link")
        if link_err:
            return Response({"message": link_err}, status=http.HTTP_400_BAD_REQUEST)
        sponsor.link = link or ""

    if request.FILES.get("logo"):
        logo, logo_err = _clean_logo_upload(request.FILES["logo"])
        if logo_err:
            return Response({"message": logo_err}, status=http.HTTP_400_BAD_REQUEST)
        logo.name = f"sponsor-logo.{logo.name.rsplit('.', 1)[-1]}"
        sponsor.logo = logo

    sponsor.save()

    return Response(
        {"message": "Public sponsor updated.",
         "public_sponsors": serialize_public_sponsors(event, request)},
        status=http.HTTP_200_OK)


@api_view(["DELETE"])
def delete_event_public_sponsor(request, sponsor_id):
    """Remove one publicly displayed sponsor from its event.

    Path: the EventPublicSponsor id.
    Response 200: {message, public_sponsors: [...]}  (the FULL remaining list)
    Auth: Bearer. Same gate as add_event_public_sponsor, resolved against the OWNING event.
    Consumed by: frontend lib/eventPublicSponsors.ts remove(), from the shared SponsorTab.
    """
    user, err = _auth_user(request)
    if err:
        return err

    sponsor = get_object_or_404(EventPublicSponsor.objects.select_related("event"), id=sponsor_id)
    event = sponsor.event

    if not _can_edit(user, event):
        return Response({"message": "You do not have permission to perform this action."},
                        status=http.HTTP_403_FORBIDDEN)

    sponsor.delete()

    return Response(
        {"message": "Public sponsor removed.",
         "public_sponsors": serialize_public_sponsors(event, request)},
        status=http.HTTP_200_OK)
