"""
afc_tournament_and_scrims.event_invites - INVITE A TEAM TO AN EVENT (owner backlog item 34).

THE ITEM IN THE OWNER'S WORDS
    "Invite teams to an event as a distinct invitation type they must accept or decline."

WHAT WAS THERE BEFORE
    An admin or organizer could only ADD a team (POST events/add-teams-to-event/). That
    force-registers: nobody on the team is asked, nobody can refuse, and a team can find itself in
    a bracket it never agreed to play. There was no way to say "we would like you in this event"
    and get an answer.

WHAT THIS ADDS
    A named, answerable invitation (models.EventTeamInvitation). AFC or the organizer invites one
    or several teams; whoever may register the team sees it and accepts or declines, optionally
    saying why. Both sides are notified, with a working "Take me there" deep link.

THE ONE RULE THAT SHAPES ALL OF THIS
    Accepting an invitation registers the team through the ORDINARY registration endpoint
    (views.register_for_event) - see _register_through_the_normal_path below. Not a copy of it, not
    a subset of its checks: the real one, called with the real captain's credentials, whose answer
    is handed back to the caller untouched. So an invited team hits the same roster-size rule, the
    same staff exclusion, the same ban / blacklist / country / Discord / letter-avatar / per-player
    profile gates, the same capacity-and-waitlist behaviour, the same closed-window refusal and the
    same already-registered 409 - with the same wording - as a team that registered itself. An
    invitation moves a team to the FRONT of the queue; it is never a way around the door.

    The one thing an invitation legitimately carries is entry to a PRIVATE event: register_for_event
    demands an EventInviteToken when Event.is_public is False, so creating an invitation to a
    private event mints a single-use token and the accept replays it. That is still the existing
    gate being satisfied, not bypassed.

HOW IT CONNECTS
    - Model: EventTeamInvitation (models.py, next to EventInviteToken, which it deliberately is
      not: that is an anonymous link, this is an addressed ask).
    - Accept -> views.register_for_event -> RegisteredCompetitors / TournamentTeam /
      TournamentTeamMember, plus everything that endpoint triggers (Discord role queue, watchlist
      warning, sponsor engagement submissions).
    - Who may invite: AFC event admins (views._is_event_admin) OR an organizer holding
      can_manage_registrations on the event's owning org (org_can_event) - the SAME gate
      add_teams_to_event uses, because inviting and adding are the same authority.
    - Who may answer: whoever may register that team (views._user_can_register_team - owner,
      captain, vice-captain, manager, coach). Accepting IS registering, so the two must not differ.
    - Notifications: afc_auth.Notifications with target_type/target_id, so the captain's link opens
      their team page and the organizer's opens the event.
    - Frontend: EventTeamInvitesCard.tsx (organizer/admin, inside the shared RegisteredTeamsTab)
      and EventInvitationsCard.tsx (the team page). i18n namespace messages/*/eventInvites.json.

ENDPOINTS (mounted under events/ by afc_tournament_and_scrims/urls.py)
    POST events/team-invitations/create/            create_team_invitations   organizer/admin
    GET  events/team-invitations/                   list_event_invitations    organizer/admin
    POST events/team-invitations/<id>/cancel/       cancel_team_invitation    organizer/admin
    GET  events/team-invitations/mine/              list_my_team_invitations  team side
    POST events/team-invitations/<id>/accept/       accept_team_invitation    team side
    POST events/team-invitations/<id>/decline/      decline_team_invitation   team side
"""
import json

from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import Notifications
from afc_auth.views import validate_token
from afc_team.models import Team, TeamMembers

from .models import (
    Event, EventInviteToken, EventTeamInvitation, RegisteredCompetitors, TournamentTeam,
)
# Top-level import of the big views module, exactly like views_checkin.py does: urls.py imports
# .views first, and views.py only ever reaches back into this app's satellite modules lazily, so
# there is no cycle to dodge here.
from .views import (
    _is_event_admin, _user_can_register_team, effective_event_status, org_can_event,
    register_for_event, registration_is_open,
)


# Default page size for both list endpoints. An event's invitation list and a team's own list are
# both naturally small, but neither may be unbounded (a season of invitations on a big event adds
# up), so every list takes ?limit= and reports has_more/total_count.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Body keys the accept endpoint forwards to register_for_event. Deliberately a WHITELIST rather
# than "everything the caller sent": event_id and team_id must come from the INVITATION, never from
# the request, or a captain could accept an invitation to event A and register for event B.
_REGISTRATION_PASSTHROUGH = ("roster_member_ids", "sponsor_ids", "sponsorships", "invite_token")


# ── auth helpers ─────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    """The Bearer-token user, or (None, error Response). Mirrors event_links._auth_user."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _can_invite(user, event):
    """Who may invite teams to `event`: AFC event admins for any event, or an organizer holding
    can_manage_registrations on the owning org. The SAME gate add_teams_to_event applies, because
    inviting a team and force-adding one are the same authority exercised more politely."""
    return _is_event_admin(user) or org_can_event(user, "can_manage_registrations", event)


def _paginate(qs, request):
    """(page, meta) for a list endpoint. `limit` is clamped to MAX_LIMIT so a caller cannot ask for
    the whole table, and the meta shape (total_count / has_more / next_offset) matches what the two
    frontend cards read."""
    try:
        limit = int(request.GET.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.GET.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    total = qs.count()
    rows = list(qs[offset:offset + limit])
    return rows, {
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
    }


def _expire_stale(qs):
    """Lazy deadline sweep: flip PENDING rows whose expires_at has passed to 'expired' as they are
    read. There is no cron for this on purpose - an invitation nobody looks at does not need a
    background job to become stale, and the accept path re-checks the deadline anyway."""
    stale = list(
        qs.filter(status="pending", expires_at__isnull=False, expires_at__lt=timezone.now())
        .values_list("id", flat=True)
    )
    if stale:
        EventTeamInvitation.objects.filter(id__in=stale).update(status="expired")
    return stale


# ── serialization ────────────────────────────────────────────────────────────────────────────
def _serialize(inv, for_team=False):
    """One invitation as the two frontend cards read it.

    `for_team=True` adds what a captain deciding needs and an organizer already knows: the event's
    name/slug/format and whether its registration window is open right now, so the team page can
    say "accepting is not possible, registration closed" before they press anything.
    """
    data = {
        "id": inv.id,
        "status": inv.status,
        "message": inv.message,
        "decline_reason": inv.decline_reason,
        "created_at": inv.created_at,
        "responded_at": inv.responded_at,
        "expires_at": inv.expires_at,
        "team_id": inv.team_id,
        "team_name": inv.team.team_name if inv.team_id else None,
        "team_tag": inv.team.team_tag if inv.team_id else None,
        "team_country": inv.team.country if inv.team_id else None,
        "invited_by": inv.invited_by.username if inv.invited_by_id else None,
        "responded_by": inv.responded_by.username if inv.responded_by_id else None,
    }
    if for_team:
        event = inv.event
        data.update({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_slug": event.slug,
            "participant_type": event.participant_type,
            "start_date": event.start_date,
            "registration_open": registration_is_open(event),
            "event_status": effective_event_status(event),
            # Already in the event (through this invitation or any other door)? The card shows
            # "registered" instead of Accept/Decline rather than letting them press into a 409.
            "team_registered": TournamentTeam.objects.filter(
                event=event, team_id=inv.team_id,
            ).exists(),
        })
    return data


# ── the accept path: replay the answer through the ordinary registration endpoint ────────────
def _register_through_the_normal_path(request, invitation):
    """Register the invited team by CALLING views.register_for_event, and return its Response.

    WHY IT IS DONE THIS WAY
        register_for_event is ~600 lines of gates (roster size and membership, staff exclusion,
        per-player profile requirements, bans, organizer blacklist, country restriction, Discord
        membership, letter avatars, paid-event payment, private-event token, capacity, waitlist
        overflow, sponsor engagements, the duplicate-registration lock). Re-implementing even a
        careful subset here would mean an invited team is judged by different rules than a
        self-registering one, and the two copies would drift the first time a gate is added. So the
        accept does not re-implement anything: it hands the real endpoint a request that says
        "register THIS team for THIS event" and passes its answer straight back, success or refusal.

    HOW THE INNER CALL IS BUILT
        register_for_event reads request.headers (the SAME Authorization header - the acting user is
        the captain, so every per-user check still judges the right person) and request.data. To
        change what request.data says, the underlying Django HttpRequest's cached body is replaced
        and marked as already-read; DRF's Request._load_stream then re-parses from that cached body
        (io.BytesIO(self.body)) instead of the consumed socket. The outer request.data was parsed
        before this point, so nothing the outer view still needs is disturbed.

        event_id and team_id come from the INVITATION, never from the caller's body, so accepting
        invitation #7 cannot be turned into a registration for some other event.
    """
    body = {}
    source = request.data if isinstance(request.data, dict) else {}
    for key in _REGISTRATION_PASSTHROUGH:
        if key in source:
            body[key] = source[key]
    body["event_id"] = invitation.event_id
    body["team_id"] = invitation.team_id

    # PRIVATE event: satisfy the existing invite-token gate with the token minted when the
    # invitation was created (see create_team_invitations). A caller who supplied their own token
    # keeps it - they may be holding a link the organizer sent separately.
    if not invitation.event.is_public and not body.get("invite_token") and invitation.invite_token_id:
        body["invite_token"] = str(invitation.invite_token.token)

    raw = json.dumps(body, default=str).encode("utf-8")
    http_request = request._request
    http_request._body = raw
    http_request._read_started = True
    http_request.META["CONTENT_TYPE"] = "application/json"
    http_request.META["CONTENT_LENGTH"] = str(len(raw))
    return register_for_event(http_request)


# ── notification helpers ─────────────────────────────────────────────────────────────────────
def _team_decision_makers(team):
    """The people on `team` who may answer an invitation, i.e. exactly those _user_can_register_team
    accepts: the owner plus captain / vice-captain / manager / coach. Used to decide who gets the
    "you have been invited" notification, so the ping lands on somebody who can actually act."""
    from .views import TEAM_EVENT_REGISTER_ROLES

    users = {
        m.member for m in TeamMembers.objects.filter(
            team=team, management_role__in=TEAM_EVENT_REGISTER_ROLES,
        ).select_related("member")
    }
    if team.team_owner_id:
        users.add(team.team_owner)
    return list(users)


def _notify_invited_team(invitation):
    """Tell the team they have been invited. The deep link points at the TEAM page, which is where
    the Accept / Decline card lives (target_type "team" -> /teams/<id>)."""
    event = invitation.event
    for user in _team_decision_makers(invitation.team):
        Notifications.objects.create(
            user=user,
            notification_type="event_team_invitation",
            title=f"Invitation to {event.event_name}",
            message=(
                f"{invitation.team.team_name} has been invited to {event.event_name}. "
                "Open your team page to accept or decline."
            ),
            related_event=event,
            target_type="team",
            target_id=str(invitation.team_id),
        )


def _notify_inviter(invitation, accepted):
    """Tell whoever sent the invitation what the team decided. Their link opens the EVENT, which is
    where they act on the answer (seed the team, or invite somebody else in its place)."""
    if not invitation.invited_by_id:
        return
    event = invitation.event
    if accepted:
        message = f"{invitation.team.team_name} accepted your invitation to {event.event_name}."
    else:
        reason = invitation.decline_reason.strip()
        message = f"{invitation.team.team_name} declined your invitation to {event.event_name}."
        if reason:
            message += f" Reason: {reason}"
    Notifications.objects.create(
        user=invitation.invited_by,
        notification_type="event_team_invitation_response",
        title=f"Invitation {'accepted' if accepted else 'declined'}",
        message=message,
        related_event=event,
        target_type="event",
        target_id=event.slug or str(event.event_id),
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ORGANIZER / ADMIN SIDE
# ══════════════════════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def create_team_invitations(request):
    """POST events/team-invitations/create/ - invite one or several teams to an event.

    REQUEST   {event_id: int, team_ids: [int], message?: str, expires_at?: ISO-8601}
    RESPONSE  201 {invited: [<invitation>], skipped: [{team_id, team_name, reason}],
                   message: str}
              Partial success is normal and is reported rather than refused: picking eight teams of
              which one is already registered must invite the other seven, not fail the batch.
    AUTH      Bearer session token; AFC event admin, or organizer with can_manage_registrations on
              the event's owning org (_can_invite).
    CONSUMED BY  EventTeamInvitesCard.tsx (the "Invite teams" dialog inside the shared
              RegisteredTeamsTab, so both the admin and organizer event-edit pages get it).

    A team is SKIPPED (never an error) when it is already registered, already holds a pending
    invitation, is banned, or does not exist. Each skip carries a reason the dialog shows.
    """
    user, err = _auth_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    team_ids = request.data.get("team_ids") or []
    message = (request.data.get("message") or "").strip()[:280]
    expires_at = request.data.get("expires_at") or None

    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    if not isinstance(team_ids, list) or not team_ids:
        return Response({"message": "team_ids must be a non-empty list of team ids."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    if not _can_invite(user, event):
        return Response({"message": "Unauthorized."}, status=403)
    if event.participant_type == "solo":
        return Response(
            {"message": "This is a solo event. Teams can only be invited to duo or squad events."},
            status=400,
        )
    if effective_event_status(event) in ("cancelled", "completed"):
        return Response({"message": "This event is no longer open for invitations."}, status=400)

    # Coerce ids defensively: the dialog sends ints, but a hand-made call must not 500 the batch.
    wanted = []
    for raw_id in team_ids:
        try:
            wanted.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return Response({"message": "team_ids must be a non-empty list of team ids."}, status=400)

    teams = {t.team_id: t for t in Team.objects.filter(team_id__in=wanted)}
    invited, skipped = [], []

    with transaction.atomic():
        # One pending invitation per (event, team) is enforced HERE rather than by a DB constraint:
        # MySQL has no partial unique index, so a conditional UniqueConstraint would be skipped
        # silently (see the Meta comment on the model). Both reads happen inside the transaction.
        already_registered = set(
            TournamentTeam.objects.filter(event=event, team_id__in=wanted)
            .values_list("team_id", flat=True)
        ) | set(
            RegisteredCompetitors.objects.filter(event=event, team_id__in=wanted)
            .values_list("team_id", flat=True)
        )
        already_pending = set(
            EventTeamInvitation.objects.filter(
                event=event, team_id__in=wanted, status="pending",
            ).values_list("team_id", flat=True)
        )

        for team_id in wanted:
            team = teams.get(team_id)
            if not team:
                skipped.append({"team_id": team_id, "team_name": None, "reason": "not_found"})
                continue
            if team.is_banned:
                skipped.append({"team_id": team_id, "team_name": team.team_name, "reason": "banned"})
                continue
            if team_id in already_registered:
                skipped.append({
                    "team_id": team_id, "team_name": team.team_name, "reason": "already_registered",
                })
                continue
            if team_id in already_pending:
                skipped.append({
                    "team_id": team_id, "team_name": team.team_name, "reason": "already_invited",
                })
                continue

            # PRIVATE event: mint the single-use token the accept will replay, so the invitation is
            # actually acceptable without teaching register_for_event a second way in.
            token = None
            if not event.is_public:
                token = EventInviteToken.objects.create(event=event, created_by=user)

            invitation = EventTeamInvitation.objects.create(
                event=event, team=team, invited_by=user, message=message,
                expires_at=expires_at or None, invite_token=token,
            )
            _notify_invited_team(invitation)
            invited.append(_serialize(invitation))

    return Response({
        "message": f"{len(invited)} team(s) invited, {len(skipped)} skipped.",
        "invited": invited,
        "skipped": skipped,
    }, status=201)


@api_view(["GET"])
def list_event_invitations(request):
    """GET events/team-invitations/?event_id=<id>[&status=&limit=&offset=] - an event's invitations.

    RESPONSE  200 {invitations: [<invitation>], counts: {pending, accepted, declined, cancelled,
                   expired}, total_count, limit, offset, has_more, next_offset}
    AUTH      Bearer session token; same _can_invite gate as creating (whoever may invite may see
              who was invited and what they said).
    CONSUMED BY  EventTeamInvitesCard.tsx - the status table under the Invite button.

    Rows whose deadline has passed are flipped to 'expired' as they are read (no cron).
    """
    user, err = _auth_user(request)
    if err:
        return err

    event_id = request.GET.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    if not _can_invite(user, event):
        return Response({"message": "Unauthorized."}, status=403)

    base = EventTeamInvitation.objects.filter(event=event)
    _expire_stale(base)

    # One grouped query for the status tally that heads the card (not len() over the page, which
    # would only count the rows this page happens to show).
    counts = {key: 0 for key, _ in EventTeamInvitation.STATUS_CHOICES}
    for row in base.values("status").annotate(n=Count("id")):
        counts[row["status"]] = row["n"]

    qs = base.select_related("team", "invited_by", "responded_by").order_by("-created_at")
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    rows, meta = _paginate(qs, request)
    return Response({
        "invitations": [_serialize(inv) for inv in rows],
        "counts": counts,
        **meta,
    }, status=200)


@api_view(["POST"])
def cancel_team_invitation(request, invitation_id):
    """POST events/team-invitations/<invitation_id>/cancel/ - take back a pending invitation.

    REQUEST   (no body)
    RESPONSE  200 {message, invitation: <invitation>}
    AUTH      Bearer session token; same _can_invite gate as creating.
    CONSUMED BY  EventTeamInvitesCard.tsx - the Cancel button on a pending row.

    Only a PENDING invitation can be cancelled: an answered one is a historical fact, and one the
    team already accepted must be undone by removing the team from the event, not by rewriting the
    invitation. Cancelling deletes the private-event token so a withdrawn invitation cannot still
    let the team through the door.
    """
    user, err = _auth_user(request)
    if err:
        return err

    invitation = get_object_or_404(
        EventTeamInvitation.objects.select_related("event", "team"), id=invitation_id,
    )
    if not _can_invite(user, invitation.event):
        return Response({"message": "Unauthorized."}, status=403)
    if invitation.status != "pending":
        return Response(
            {"message": f"This invitation was already {invitation.status}."}, status=400,
        )

    with transaction.atomic():
        token = invitation.invite_token
        invitation.status = "cancelled"
        invitation.responded_by = user
        invitation.responded_at = timezone.now()
        invitation.invite_token = None
        invitation.save(update_fields=["status", "responded_by", "responded_at", "invite_token"])
        if token:
            token.delete()

    return Response(
        {"message": "Invitation cancelled.", "invitation": _serialize(invitation)}, status=200,
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# TEAM SIDE
# ══════════════════════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def list_my_team_invitations(request):
    """GET events/team-invitations/mine/[?team_id=&status=&limit=&offset=] - my team's invitations.

    RESPONSE  200 {invitations: [<invitation with event fields>], can_respond: bool, team_id,
                   pending_count, total_count, limit, offset, has_more, next_offset}
    AUTH      Bearer session token. The team is resolved from the caller's own TeamMembers row (or
              from ?team_id= for a team owner who is not a member row), so nobody can read another
              team's invitations. `can_respond` says whether THIS viewer may accept or decline
              (views._user_can_register_team) - a plain player sees the invitation but no buttons.
    CONSUMED BY  EventInvitationsCard.tsx on the team page (and the notification deep link that
              lands there).

    Rows whose deadline has passed are flipped to 'expired' as they are read (no cron).
    """
    user, err = _auth_user(request)
    if err:
        return err

    # Resolve which team is being asked about: an explicit ?team_id= (the team page always knows
    # it), else the caller's own membership. Either way the caller must belong to that team or own
    # it, so this endpoint can never be used to read somebody else's invitations.
    team_id = request.GET.get("team_id")
    if team_id:
        team = get_object_or_404(Team, team_id=team_id)
        belongs = (
            team.team_owner_id == user.pk
            or TeamMembers.objects.filter(team=team, member=user).exists()
        )
        if not belongs:
            return Response({"message": "You are not a member of this team."}, status=403)
    else:
        membership = TeamMembers.objects.filter(member=user).select_related("team").first()
        if not membership:
            return Response({
                "invitations": [], "can_respond": False, "team_id": None, "pending_count": 0,
                "total_count": 0, "limit": DEFAULT_LIMIT, "offset": 0, "has_more": False,
                "next_offset": None,
            }, status=200)
        team = membership.team

    base = EventTeamInvitation.objects.filter(team=team)
    _expire_stale(base)

    qs = base.select_related("event", "team", "invited_by", "responded_by").order_by("-created_at")
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    rows, meta = _paginate(qs, request)
    return Response({
        "invitations": [_serialize(inv, for_team=True) for inv in rows],
        "can_respond": _user_can_register_team(user, team),
        "team_id": team.team_id,
        "pending_count": base.filter(status="pending").count(),
        **meta,
    }, status=200)


def _load_for_response(user, invitation_id):
    """Shared front half of accept and decline: fetch the invitation, check the caller may answer
    for that team, and check the invitation is still answerable. Returns (invitation, None) or
    (None, error Response)."""
    invitation = get_object_or_404(
        EventTeamInvitation.objects.select_related("event", "team", "invited_by"),
        id=invitation_id,
    )
    # Answering IS registering, so the permission must be the SAME one register_for_event applies
    # to a self-registration (owner, captain, vice-captain, manager, coach). If these two ever
    # disagreed, a person could accept an invitation and then be refused by the endpoint the accept
    # itself calls.
    if not _user_can_register_team(user, invitation.team):
        return None, Response(
            {"message": "Only the team owner, captain, vice-captain, manager, or coach can "
                        "answer an event invitation."},
            status=403,
        )
    if invitation.is_expired() and invitation.status == "pending":
        EventTeamInvitation.objects.filter(id=invitation.id).update(status="expired")
        invitation.status = "expired"
    if invitation.status != "pending":
        return None, Response(
            {"message": f"This invitation was already {invitation.status}."}, status=400,
        )
    return invitation, None


@api_view(["POST"])
def accept_team_invitation(request, invitation_id):
    """POST events/team-invitations/<invitation_id>/accept/ - say yes and register the team.

    REQUEST   {roster_member_ids: [int], sponsor_ids?: {}, sponsorships?: [], invite_token?: str}
              The body is whatever register_for_event needs; roster_member_ids is required for duo
              and squad events because that endpoint requires it.
    RESPONSE  201 the EXACT body register_for_event returns (registration_id, tournament_team_id,
              roster_size, or the waitlist variant) plus {invitation: <invitation>}.
              On refusal: the EXACT status and body register_for_event returned - "Registration is
              closed.", "Registration limit reached.", "Roster must contain 4 to 6 players.",
              "This team is already registered for this event.", the structured
              registration_requirements_unmet / discord_required / letter_avatars_required bodies,
              402 payment_required - unchanged, so the team sees the same wording anyone else does.
              The invitation stays PENDING on refusal, so it can be accepted again once fixed.
    AUTH      Bearer session token; whoever may register the team (views._user_can_register_team).
    CONSUMED BY  EventInvitationsCard.tsx - the Accept dialog's roster picker on the team page.

    The registration itself is performed by views.register_for_event; see
    _register_through_the_normal_path for why the real endpoint is called instead of its checks
    being copied.
    """
    user, err = _auth_user(request)
    if err:
        return err

    invitation, err = _load_for_response(user, invitation_id)
    if err:
        return err

    response = _register_through_the_normal_path(request, invitation)

    # Anything that is not a success leaves the invitation exactly as it was: the team has not
    # registered, so they have not accepted. Handing the refusal back untouched is the whole point.
    if response.status_code >= 400:
        return response

    invitation.status = "accepted"
    invitation.responded_by = user
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_by", "responded_at"])
    _notify_inviter(invitation, accepted=True)

    if isinstance(response.data, dict):
        response.data["invitation"] = _serialize(invitation, for_team=True)
    return response


@api_view(["POST"])
def decline_team_invitation(request, invitation_id):
    """POST events/team-invitations/<invitation_id>/decline/ - say no, optionally saying why.

    REQUEST   {reason?: str}   (free text, trimmed to 280 chars; optional by design - a team should
              not have to justify itself to decline)
    RESPONSE  200 {message, invitation: <invitation>}
    AUTH      Bearer session token; whoever may register the team (views._user_can_register_team) -
              the same people who could have accepted.
    CONSUMED BY  EventInvitationsCard.tsx - the Decline dialog on the team page.

    The reason is sent to the inviter in their notification, which is the point of a decline over
    silence: the organizer learns the slot is free AND why, so they can offer it to somebody else.
    """
    user, err = _auth_user(request)
    if err:
        return err

    invitation, err = _load_for_response(user, invitation_id)
    if err:
        return err

    invitation.status = "declined"
    invitation.decline_reason = (request.data.get("reason") or "").strip()[:280]
    invitation.responded_by = user
    invitation.responded_at = timezone.now()
    invitation.save(
        update_fields=["status", "decline_reason", "responded_by", "responded_at"],
    )
    _notify_inviter(invitation, accepted=False)

    return Response(
        {"message": "Invitation declined.", "invitation": _serialize(invitation, for_team=True)},
        status=200,
    )
