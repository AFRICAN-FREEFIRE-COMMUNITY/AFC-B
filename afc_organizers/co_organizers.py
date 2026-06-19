"""
afc_organizers/co_organizers.py — Multi-org event CO-OWNERSHIP (F6, owner 2026-06-19).

Only the CREATOR (primary) org's OWNER may invite another org to co-own an event; the invited org's
OWNER must accept (mutual consent). Accepted co-owners gain SCOPED access to the event via
permissions.org_can_event (which now checks accepted co-owners too). Co-owners CANNOT invite further
orgs. An empty EventCoOrganizer table = today's single-org behaviour (fully backward-compatible).

Self-contained module (same isolation pattern as seeding_management.py / event_links.py) so we don't
grow the 19k-line tournament views.py. Endpoints mounted under organizers/ via afc_organizers/urls.py:
    POST organizers/co-organizers/invite/    invite_co_organizer    {event_id, organization_id, permissions{}, payout_percent?}
    POST organizers/co-organizers/respond/   respond_co_organizer   {co_organizer_id, action: accept|decline}
    POST organizers/co-organizers/revoke/    revoke_co_organizer    {co_organizer_id}
    GET  organizers/co-organizers/?event_id= list_event_co_organizers
"""
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import validate_token

from .models import Organization, OrganizationMember, EventCoOrganizer, PERMISSION_FIELDS
from .permissions import is_platform_org_admin


# ── helpers ──────────────────────────────────────────────────────────────────────────────────
def _auth(request):
    """Bearer-token user, or (None, error Response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Authorization header is required"}, status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def _event_or_404(event_id):
    from afc_tournament_and_scrims.models import Event
    ev = Event.objects.filter(event_id=event_id).first()
    if not ev:
        return None, Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
    return ev, None


def _is_event_primary_owner_or_admin(user, event):
    """True if the user is the OWNER of the event's PRIMARY org, or an AFC platform admin."""
    if is_platform_org_admin(user):
        return True
    if not event.organization_id:
        return False
    return OrganizationMember.objects.filter(
        organization_id=event.organization_id, user=user, role="owner", status="active",
    ).exists()


def _co_payload(co, full=True):
    """Co-organizer row. `full` adds the COMMERCIAL detail (payout_percent + the scoped permission
    grant) — only privileged callers (the primary org owner / an involved org's member / AFC admin)
    get it. Non-privileged callers get the branding shape only, so the negotiated split + grants are
    not readable by every logged-in user (adversarial-review fix, owner 2026-06-19)."""
    base = {
        "id": co.id,
        "organization_id": co.organization_id,
        "name": co.organization.name,
        "slug": co.organization.slug,
        "status": co.status,
    }
    if full:
        base["payout_percent"] = float(co.payout_percent or 0)
        base["permissions"] = {f: getattr(co, f) for f in PERMISSION_FIELDS}
    return base


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def invite_co_organizer(request):
    """Invite another org to co-own an event. Gate: OWNER of the event's primary org (or AFC admin).
    Creates a PENDING EventCoOrganizer + notifies the invited org's owner(s) with a deep link."""
    user, err = _auth(request)
    if err:
        return err
    event, err = _event_or_404(request.data.get("event_id"))
    if err:
        return err
    if not _is_event_primary_owner_or_admin(user, event):
        return Response({"message": "Only the creating organization's owner can invite co-organizers."},
                        status=status.HTTP_403_FORBIDDEN)

    # Resolve the target by organization_id (preferred) OR slug (the FE picker sends a slug).
    target_qs = Organization.objects.filter(status="active")
    org_id = request.data.get("organization_id")
    org_slug = request.data.get("organization_slug")
    target = (target_qs.filter(organization_id=org_id).first() if org_id
              else target_qs.filter(slug=org_slug).first() if org_slug else None)
    if not target:
        return Response({"message": "Target organization not found or not active."}, status=status.HTTP_404_NOT_FOUND)
    if target.organization_id == event.organization_id:
        return Response({"message": "That organization already owns this event."}, status=status.HTTP_400_BAD_REQUEST)

    perms = request.data.get("permissions") or {}
    grant = {f: bool(perms.get(f, False)) for f in PERMISSION_FIELDS}
    # Clamp payout to a real percentage. payout_percent is DecimalField(max_digits=5) so a value
    # >= 1000 would raise on save (unhandled 500); negatives are nonsensical. The FE has max=100 but
    # that HTML attr isn't enforced on submit. (Adversarial-review fix, owner 2026-06-19.)
    try:
        payout = float(request.data.get("payout_percent") or 0)
    except (TypeError, ValueError):
        payout = 0
    if not (payout == payout) or payout in (float("inf"), float("-inf")):  # reject NaN/inf
        payout = 0
    payout = max(0.0, min(100.0, payout))

    co, created = EventCoOrganizer.objects.get_or_create(
        event=event, organization=target,
        defaults={"status": "pending", "invited_by": user, "payout_percent": payout, **grant},
    )
    if not created:
        # Update the grant on an existing row. If it was ALREADY ACCEPTED, PRESERVE the accepted
        # status (apply the grant/payout change live) — don't silently revoke a live co-ownership back
        # to pending and force a re-accept just because the owner tweaked a permission. Only a
        # previously declined/pending row goes (back) to pending for a fresh consent. (Adversarial-
        # review fix, owner 2026-06-19.)
        co.invited_by = user
        co.payout_percent = payout
        for f in PERMISSION_FIELDS:
            setattr(co, f, grant[f])
        if co.status != "accepted":
            co.status = "pending"
            co.responded_at = None
        co.save()

    # Best-effort notify the invited org's active owners (push + email) with a deep link.
    try:
        from afc_auth.views import deliver_broadcast
        owner_users = [
            m.user for m in OrganizationMember.objects.filter(
                organization=target, role="owner", status="active",
            ).select_related("user") if m.user
        ]
        if owner_users:
            primary_name = event.organization.name if event.organization_id else "AFC"
            deliver_broadcast(
                owner_users,
                f"Co-organizer invite: {event.event_name}",
                f"{primary_name} invited {target.name} to co-organize {event.event_name}. "
                f"Review it in your organizer portal to accept or decline.",
                delivery="both", notification_type="organizer", related_event=event,
                target_type="event", target_id=event.slug, scope="event", log=False,
            )
    except Exception:
        pass

    return Response({"message": "Co-organizer invited.", "co_organizer": _co_payload(co)},
                    status=status.HTTP_201_CREATED)


@api_view(["POST"])
def respond_co_organizer(request):
    """Accept / decline a co-ownership invite. Gate: OWNER of the INVITED org (mutual consent)."""
    user, err = _auth(request)
    if err:
        return err
    co = EventCoOrganizer.objects.filter(
        id=request.data.get("co_organizer_id"),
    ).select_related("organization").first()
    if not co:
        return Response({"message": "Co-organizer invite not found."}, status=status.HTTP_404_NOT_FOUND)
    is_owner = OrganizationMember.objects.filter(
        organization=co.organization, user=user, role="owner", status="active",
    ).exists()
    if not (is_owner or is_platform_org_admin(user)):
        return Response({"message": "Only the invited organization's owner can respond."},
                        status=status.HTTP_403_FORBIDDEN)
    action = (request.data.get("action") or "").lower()
    if action not in ("accept", "decline"):
        return Response({"message": "action must be 'accept' or 'decline'."}, status=status.HTTP_400_BAD_REQUEST)
    # State-machine guard: only a PENDING invite may be responded to. Without this the invited owner
    # could flip a previously declined invite straight to accepted (unilaterally re-activating
    # co-ownership the primary org thought was settled), or re-flip an already-decided one. A genuine
    # re-offer goes through invite_co_organizer, which resets a declined row to pending. (Adversarial-
    # review fix, owner 2026-06-19.)
    if co.status != "pending":
        return Response({"message": "This invite is no longer pending."}, status=status.HTTP_400_BAD_REQUEST)
    co.status = "accepted" if action == "accept" else "declined"
    co.responded_at = timezone.now()
    co.save(update_fields=["status", "responded_at"])
    return Response({"message": f"Invite {co.status}.", "co_organizer": _co_payload(co)},
                    status=status.HTTP_200_OK)


@api_view(["POST"])
def revoke_co_organizer(request):
    """Remove a co-ownership grant. Gate: OWNER of the event's primary org (or AFC admin)."""
    user, err = _auth(request)
    if err:
        return err
    co = EventCoOrganizer.objects.filter(
        id=request.data.get("co_organizer_id"),
    ).select_related("event").first()
    if not co:
        return Response({"message": "Co-organizer not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _is_event_primary_owner_or_admin(user, co.event):
        return Response({"message": "Only the creating organization's owner can revoke a co-organizer."},
                        status=status.HTTP_403_FORBIDDEN)
    co.delete()
    return Response({"message": "Co-organizer removed."}, status=status.HTTP_200_OK)


@api_view(["GET"])
def list_event_co_organizers(request):
    """List an event's co-organizers (+ status). Any logged-in user may read the BRANDING shape, but
    the commercial detail (payout_percent + permission grant) is returned ONLY to privileged callers:
    the event's primary-org owner / AFC admin, or an active member of any org involved in the event
    (primary or a co-org). This stops any logged-in user enumerating the negotiated split + grants of
    two other organizations (adversarial-review fix, owner 2026-06-19)."""
    user, err = _auth(request)
    if err:
        return err
    event, err = _event_or_404(request.GET.get("event_id"))
    if err:
        return err
    rows = list(EventCoOrganizer.objects.filter(event=event).select_related("organization"))
    # Privileged = primary-org owner / AFC admin, OR an active member of any involved org.
    privileged = _is_event_primary_owner_or_admin(user, event)
    if not privileged:
        involved_org_ids = [c.organization_id for c in rows]
        if event.organization_id:
            involved_org_ids.append(event.organization_id)
        privileged = OrganizationMember.objects.filter(
            organization_id__in=involved_org_ids, user=user, status="active",
        ).exists()
    return Response(
        {"co_organizers": [_co_payload(c, full=privileged) for c in rows]},
        status=status.HTTP_200_OK,
    )
