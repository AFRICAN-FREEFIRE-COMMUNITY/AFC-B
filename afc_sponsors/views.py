"""
afc_sponsors.views — REST endpoints for the sponsor-system redesign P1.

PURPOSE
    Admin CRUD for Sponsor entities + member assignment, and the MEMBER-SCOPED sponsor portal
    reads (my sponsors -> a sponsor's events -> one event's submissions, with CSV export).
    Spec: WEBSITE/tasks/sponsors-redesign-design.md v2 (owner-approved mockup
    frontend/public/_sponsor_system_preview.html).

HOUSE IDIOMS (mirrors afc_leaderboard.views / afc_shop vendor endpoints)
    - Function-based @api_view, Bearer SessionToken via afc_auth.views.validate_token
      (wrapped in _auth_user below).
    - Errors: Response({"message": ...}, status=4xx).
    - Pagination envelope {results, has_more, next_offset, total_count} (limit<=100 default 25).

PRIVACY (owner decision 2026-06-12)
    Sponsors see USERNAMES + SUBMITTED VALUES ONLY. The legacy sponsor endpoint leaked player
    emails; nothing here ever serializes a player's email or account phone.

HOW IT CONNECTS
    - Models: afc_sponsors.models (Sponsor / SponsorMember / EventSponsorship).
    - Legacy data: the P1 submissions read pulls the SAME per-competitor sponsor ids the old
      dashboard shows (RegisteredCompetitors.user_id_from_sponsor for solo,
      TournamentTeamMember.user_id_from_sponsor for squads) — scoped per event + per sponsor.
    - Notifications: adding a member writes an afc_auth.Notifications row ("sponsor_access"),
      which also powers the FE one-time dashboard coachmark trigger.
    - Consumed by frontend lib/sponsors.ts -> app/(a)/a/sponsors (admin) and the sponsor portal
      (app/(sponsor)/sponsor/...).

ENDPOINTS (mounted at sponsors/ via afc/urls.py)
    POST   sponsors/create/                          create_sponsor          (sponsor-admin)
    GET    sponsors/                                 list_sponsors           (sponsor-admin, paginated)
    GET    sponsors/<id>/                            sponsor_detail          (sponsor-admin or member)
    PATCH  sponsors/<id>/edit/                       edit_sponsor            (sponsor-admin)
    POST   sponsors/<id>/members/add/                add_member              (sponsor-admin)
    DELETE sponsors/<id>/members/<member_id>/        remove_member           (sponsor-admin)
    POST   sponsors/<id>/events/attach/              attach_event            (sponsor-admin)
    DELETE sponsors/<id>/events/<event_id>/          detach_event            (sponsor-admin)
    GET    sponsors/mine/                            my_sponsors             (any member)
    GET    sponsors/<id>/events/                     sponsor_events          (member or sponsor-admin)
    GET    sponsors/<id>/events/<event_id>/submissions/   event_submissions  (member or sponsor-admin;
                                                          ?csv=1 streams text/csv)
"""
import csv

from django.db.models import Q
from django.http import HttpResponse
from django.utils.text import slugify

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_auth.models import User, Roles, Notifications
from afc_tournament_and_scrims.models import (
    Event, RegisteredCompetitors, TournamentTeam, TournamentTeamMember,
)

from .models import Sponsor, SponsorMember, EventSponsorship


DEFAULT_LIMIT = 25
MAX_LIMIT = 100


# ── auth helpers (mirror afc_leaderboard.views._auth_user) ───────────────────────────────────
def _auth_user(request):
    """Resolve the Bearer caller. Returns (user, error_response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _is_sponsor_admin(user):
    """Can the caller MANAGE sponsor entities? Base role admin, or the granular
    head_admin / super_admin / sponsor_admin roles (the same ladder the rest of the admin
    panel uses for its sections)."""
    if user.role == "admin":
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin", "sponsor_admin"),
    ).exists()


def _can_create_sponsor(user):
    """Can the caller CREATE and LIST sponsor entities for the event-create picker?

    Authorizes (owner 2026-06-30, "organizers should be able to create sponsors also; admins
    should not have to create sponsors before organizers can use them"):
      - any sponsor-admin (the existing admin path — see _is_sponsor_admin), OR
      - any ACTIVE organizer who can create events: an AFC platform org-admin
        (is_platform_org_admin), an org OWNER (implicitly), or a sub_organizer whose
        OrganizationMember row grants can_create_events. This mirrors exactly how event-create
        authorizes organizers (afc_organizers.permissions.org_can with "can_create_events"), so
        anyone allowed to run an event can self-serve the sponsors that event attaches to.

    CONSUMED BY create_sponsor + list_sponsors below, which back the SHARED event-create sponsor
    picker (frontend components/sponsorship-builder.tsx, used by both the admin and organizer
    create wizards). Organizers need BOTH gates: list (to pick existing sponsors) and create (the
    inline "Create sponsor" modal). Lazy import keeps afc_sponsors free of an import-time
    dependency on afc_organizers (whose models pull in tournament + team)."""
    if _is_sponsor_admin(user):
        return True
    from afc_organizers.models import OrganizationMember
    from afc_organizers.permissions import is_platform_org_admin
    if is_platform_org_admin(user):
        return True
    # Any active membership that can create events: owners implicitly, sub_organizers by grant.
    return OrganizationMember.objects.filter(
        user=user, status="active",
    ).filter(
        Q(role="owner") | Q(can_create_events=True),
    ).exists()


def _membership(user, sponsor):
    """The caller's ACTIVE membership row for `sponsor`, or None."""
    return SponsorMember.objects.filter(sponsor=sponsor, user=user, status="active").first()


def _can_view_sponsor(user, sponsor):
    """Members see their own sponsor; sponsor-admins see all (the ydpay-only scoping rule)."""
    return _is_sponsor_admin(user) or _membership(user, sponsor) is not None


def _serialize_sponsor(s, with_members=False):
    out = {
        "id": s.id,
        "name": s.name,
        "slug": s.slug,
        "logo": s.logo.url if s.logo else None,
        "description": s.description,
        "website": s.website,
        "socials": s.socials or [],
        "status": s.status,
        "events_count": s.sponsorships.count(),
        "members_count": s.members.filter(status="active").count(),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
    if with_members:
        out["members"] = [
            {
                "member_id": m.id,
                "user_id": m.user_id,
                "username": m.user.username,
                "role": m.role,
            }
            for m in s.members.filter(status="active").select_related("user")
        ]
    return out


def _page_params(request):
    """Parse + sanitize ?limit / ?offset (house pagination idiom)."""
    try:
        limit = min(int(request.GET.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT, 0
    return (limit if limit >= 1 else DEFAULT_LIMIT, offset if offset >= 0 else 0)


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN — sponsor CRUD + members + event attachment (/a/sponsors)
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def create_sponsor(request):
    """POST sponsors/create/  body: {name, description?, website?, socials?}

    Create a sponsor entity. Slug derives from the name (unique-suffixed on collision).
    Auth: sponsor-admin OR an organizer who can create events (_can_create_sponsor) — so an
    organizer can self-serve a sponsor inline from the event-create picker without an admin
    pre-creating it. created_by + the model's default active status apply to BOTH paths (same
    code), so an organizer-created sponsor is identical to an admin-created one. Response 201:
    {sponsor}. Consumed by /a/sponsors "Create sponsor" AND the shared sponsorship-builder modal."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _can_create_sponsor(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"message": "name is required."}, status=400)
    if Sponsor.objects.filter(name__iexact=name).exists():
        return Response({"message": "A sponsor with that name already exists."}, status=400)

    base = slugify(name) or "sponsor"
    slug = base
    n = 2
    while Sponsor.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    sponsor = Sponsor.objects.create(
        name=name,
        slug=slug,
        description=(request.data.get("description") or "").strip(),
        website=(request.data.get("website") or "").strip(),
        socials=request.data.get("socials") or [],
        created_by=user,
    )
    return Response({"message": "Sponsor created.", "sponsor": _serialize_sponsor(sponsor)}, status=201)


@api_view(["GET"])
def list_sponsors(request):
    """GET sponsors/?q=&limit=&offset=  — paginated sponsor list for the admin table AND the
    event-create sponsor typeahead. Auth: sponsor-admin OR an organizer who can create events
    (_can_create_sponsor) — organizers must be able to LIST to pick existing sponsors in the
    builder. Response: the house envelope of _serialize_sponsor rows."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _can_create_sponsor(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)

    qs = Sponsor.objects.all()
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(name__icontains=q)
    limit, offset = _page_params(request)
    total = qs.count()
    rows = [_serialize_sponsor(s) for s in qs[offset:offset + limit]]
    return Response({
        "results": rows,
        "has_more": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "total_count": total,
    })


@api_view(["GET"])
def sponsor_detail(request, sponsor_id):
    """GET sponsors/<id>/  — one sponsor incl. its active members.
    Auth: sponsor-admin OR an active member of THIS sponsor."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)
    if not _can_view_sponsor(user, sponsor):
        return Response({"message": "You do not have access to this sponsor."}, status=403)
    return Response({"sponsor": _serialize_sponsor(sponsor, with_members=True)})


@api_view(["PATCH"])
def edit_sponsor(request, sponsor_id):
    """PATCH sponsors/<id>/edit/  body: any of {name, description, website, socials, status}.
    Auth: sponsor-admin. Renames keep the slug (links stay stable)."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _is_sponsor_admin(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)

    fields = []
    if "name" in request.data:
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"message": "name cannot be empty."}, status=400)
        if Sponsor.objects.filter(name__iexact=name).exclude(id=sponsor.id).exists():
            return Response({"message": "A sponsor with that name already exists."}, status=400)
        sponsor.name = name
        fields.append("name")
    for key in ("description", "website"):
        if key in request.data:
            setattr(sponsor, key, (request.data.get(key) or "").strip())
            fields.append(key)
    if "socials" in request.data:
        sponsor.socials = request.data.get("socials") or []
        fields.append("socials")
    if "status" in request.data:
        status_val = request.data.get("status")
        if status_val not in ("active", "suspended"):
            return Response({"message": "status must be active or suspended."}, status=400)
        sponsor.status = status_val
        fields.append("status")
    if fields:
        sponsor.save(update_fields=fields)
    return Response({"message": "Sponsor updated.", "sponsor": _serialize_sponsor(sponsor)})


@api_view(["POST"])
def add_member(request, sponsor_id):
    """POST sponsors/<id>/members/add/  body: {user_id, role?: owner|member}

    Assign a user to the sponsor. Re-adding a removed member reactivates the same row.
    Writes a "sponsor_access" Notifications row for the user (also the FE coachmark trigger:
    next login shows the one-time "You now have the Sponsor Dashboard" pointer).
    Auth: sponsor-admin. Consumed by /a/sponsors "Manage members" (UserSearchSelect)."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _is_sponsor_admin(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)

    target_id = request.data.get("user_id")
    role = request.data.get("role") or "member"
    if role not in ("owner", "member"):
        return Response({"message": "role must be owner or member."}, status=400)
    try:
        target = User.objects.get(user_id=target_id)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=404)

    member, created = SponsorMember.objects.get_or_create(
        sponsor=sponsor, user=target, defaults={"role": role, "status": "active"},
    )
    if not created:
        if member.status == "active":
            return Response({"message": f"{target.username} is already a member."}, status=400)
        member.status = "active"
        member.role = role
        member.save(update_fields=["status", "role"])

    # In-app notification (the player-facing bell + the dashboard coachmark's access signal).
    Notifications.objects.create(
        user=target,
        notification_type="sponsor_access",
        title="Sponsor dashboard access",
        message=f"You now have access to the {sponsor.name} sponsor dashboard.",
    )
    return Response({
        "message": f"{target.username} added to {sponsor.name}.",
        "member": {"member_id": member.id, "user_id": target.user_id,
                   "username": target.username, "role": member.role},
    }, status=201)


@api_view(["DELETE"])
def remove_member(request, sponsor_id, member_id):
    """DELETE sponsors/<id>/members/<member_id>/  — soft-remove (status=removed, row kept for
    audit; re-adding reactivates). Auth: sponsor-admin."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _is_sponsor_admin(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)
    try:
        member = SponsorMember.objects.get(id=member_id, sponsor_id=sponsor_id)
    except SponsorMember.DoesNotExist:
        return Response({"message": "Member not found."}, status=404)
    member.status = "removed"
    member.save(update_fields=["status"])
    return Response({"message": "Member removed."})


@api_view(["POST"])
def attach_event(request, sponsor_id):
    """POST sponsors/<id>/events/attach/  body: {event_id}

    Attach an event to this sponsor (multiple sponsors per event are allowed — unique only per
    (event, sponsor) pair). P1 scoping only; the engagement builder configures the sponsorship
    in P3. Auth: sponsor-admin."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _is_sponsor_admin(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)
    try:
        event = Event.objects.get(event_id=request.data.get("event_id"))
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)

    _, created = EventSponsorship.objects.get_or_create(event=event, sponsor=sponsor)
    if not created:
        return Response({"message": "That event is already attached to this sponsor."}, status=400)
    return Response({"message": f"{event.event_name} attached to {sponsor.name}."}, status=201)


@api_view(["DELETE"])
def detach_event(request, sponsor_id, event_id):
    """DELETE sponsors/<id>/events/<event_id>/  — detach. Submissions data is untouched (it
    lives on the registrations); only the dashboard scoping link is removed."""
    user, err = _auth_user(request)
    if err:
        return err
    if not _is_sponsor_admin(user):
        return Response({"message": "You do not have permission to manage sponsors."}, status=403)
    deleted, _ = EventSponsorship.objects.filter(sponsor_id=sponsor_id, event_id=event_id).delete()
    if not deleted:
        return Response({"message": "That event is not attached to this sponsor."}, status=404)
    return Response({"message": "Event detached."})


# ═════════════════════════════════════════════════════════════════════════════
# PORTAL — the member-scoped sponsor dashboard reads
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def my_sponsors(request):
    """GET sponsors/mine/  — the ACTIVE sponsors the caller belongs to (drives the portal's
    sponsor switcher; empty list = no sponsor dashboard access). Any authenticated user."""
    user, err = _auth_user(request)
    if err:
        return err
    rows = [
        _serialize_sponsor(m.sponsor)
        for m in SponsorMember.objects.filter(user=user, status="active")
        .select_related("sponsor")
        .order_by("sponsor__name")
        if m.sponsor.status == "active"
    ]
    return Response({"results": rows, "total_count": len(rows)})


@api_view(["GET"])
def sponsor_events(request, sponsor_id):
    """GET sponsors/<id>/events/  — the events attached to this sponsor, newest first, with the
    registrant count the dashboard list shows. Auth: member of THIS sponsor or sponsor-admin."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)
    if not _can_view_sponsor(user, sponsor):
        return Response({"message": "You do not have access to this sponsor."}, status=403)

    out = []
    for sp in sponsor.sponsorships.select_related("event").order_by("-created_at"):
        e = sp.event
        if e.participant_type == "solo":
            registrants = RegisteredCompetitors.objects.filter(event=e, user__isnull=False).count()
        else:
            registrants = TournamentTeamMember.objects.filter(tournament_team__event=e).count()
        out.append({
            "event_id": e.event_id,
            "event_name": e.event_name,
            "slug": e.slug,
            "event_status": e.event_status,
            "participant_type": e.participant_type,
            "registrants": registrants,
            "requires_approval": sp.requires_approval,
        })
    return Response({"results": out, "total_count": len(out)})


def _event_submission_rows(event):
    """The LEGACY per-competitor sponsor values for one event, privacy-stripped (usernames +
    submitted value + status; NEVER emails). Solo events read RegisteredCompetitors, squads
    read TournamentTeamMember — the exact fields the old dashboard table renders, scoped to
    one event. P3 swaps this source for SponsorEngagementSubmission without changing the FE."""
    rows = []
    if event.participant_type == "solo":
        comps = RegisteredCompetitors.objects.filter(
            event=event, user__isnull=False,
        ).select_related("user")
        for c in comps:
            rows.append({
                "id": c.id,
                "username": c.user.username,
                "team_name": None,
                "value": c.user_id_from_sponsor,
                "status": c.status,
            })
    else:
        members = TournamentTeamMember.objects.filter(
            tournament_team__event=event,
        ).select_related("user", "tournament_team__team")
        for m in members:
            rows.append({
                "id": m.id,
                "username": m.user.username,
                "team_name": m.tournament_team.team.team_name if m.tournament_team.team_id else None,
                "value": m.user_id_from_sponsor,
                "status": m.status,
            })
    return rows


@api_view(["GET"])
def event_submissions(request, sponsor_id, event_id):
    """GET sponsors/<id>/events/<event_id>/submissions/[?csv=1]

    The submissions table for ONE attached event: username, team, the value the registrant
    submitted for this sponsor, status. ?csv=1 streams the same rows as text/csv (owner: CSV
    export ships at P1). Auth: member of THIS sponsor or sponsor-admin; the event must be
    attached to the sponsor (404 otherwise, so members cannot probe other events)."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)
    if not _can_view_sponsor(user, sponsor):
        return Response({"message": "You do not have access to this sponsor."}, status=403)
    sp = EventSponsorship.objects.filter(sponsor=sponsor, event_id=event_id).select_related("event").first()
    if not sp:
        return Response({"message": "That event is not attached to this sponsor."}, status=404)

    rows = _event_submission_rows(sp.event)

    if request.GET.get("csv"):
        # CSV: same privacy-stripped columns, named for the sponsor + event.
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = (
            f'attachment; filename="{sponsor.slug}-{sp.event.slug or sp.event.event_id}-submissions.csv"'
        )
        w = csv.writer(resp)
        w.writerow(["username", "team", "value", "status"])
        for r in rows:
            w.writerow([r["username"], r["team_name"] or "", r["value"] or "", r["status"]])
        return resp

    return Response({
        "event": {"event_id": sp.event.event_id, "event_name": sp.event.event_name},
        "results": rows,
        "total_count": len(rows),
    })
