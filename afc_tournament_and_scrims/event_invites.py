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

THE THREE KINDS (owner 2026-08-08, the follow-up ask)
    "The admins can pick where they receive the invitations, the normal places, can also decide what
    kind: if it is fcfs, or single per team that's automatically generated and attributed to each
    team and sent, or it's a single general bulk invite."

    Every send now creates an EventInvitationCampaign (models.py) carrying the KIND, the note, the
    deadline, the delivery channels and, for fcfs, the slot count. The kinds differ in exactly one
    thing, HOW MANY ADDRESSED ROWS THEY WRITE, which is why the kind lives on the campaign and not
    on the invitation:

      per_team  N teams -> N EventTeamInvitation rows. All may be accepted. Item 34, unchanged.
      fcfs      N teams -> N rows, but only `slots` of them may ever be accepted (and the event's
                own capacity still applies on top). More teams are asked than there is room for.
      bulk      N teams -> ZERO rows. One general offer any audience team may take up; a row is
                written when somebody ANSWERS, so it records the answer rather than the ask.

    The accept path is the SAME for all three and still ends in register_for_event. A kind decides
    who is asked and how many may say yes; it never decides who gets in.

DELIVERY (the other half of the same ask)
    Which channels an invitation goes out on is the admin's choice, carried on the campaign as
    `delivery` in afc_auth.audience's existing vocabulary and fanned out by
    event_invite_delivery.deliver_invitation: in-app notification, email (hand-authored copy, in the
    recipient's own language), and WhatsApp. Every channel reaches EVERYONE who may answer, which is
    the same set _user_can_register_team accepts, not just the captain.

ENDPOINTS (mounted under events/ by afc_tournament_and_scrims/urls.py)
    POST events/team-invitations/create/            create_team_invitations   organizer/admin
    GET  events/team-invitations/                   list_event_invitations    organizer/admin
    POST events/team-invitations/<id>/cancel/       cancel_team_invitation    organizer/admin
    POST events/invitation-campaigns/<id>/close/    close_invitation_campaign organizer/admin
    GET  events/team-invitations/mine/              list_my_team_invitations  team side
    POST events/team-invitations/<id>/accept/       accept_team_invitation    team side
    POST events/team-invitations/<id>/decline/      decline_team_invitation   team side
    POST events/invitation-campaigns/<id>/accept/   accept_bulk_campaign      team side (bulk only)
    POST events/invitation-campaigns/<id>/decline/  decline_bulk_campaign     team side (bulk only)
"""
import json

from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

# BannedPlayer and User are needed for the SOLO invitation path (owner 2026-08-26): a player's
# ban lives on its own table rather than as a flag, unlike Team.is_banned.
from afc_auth.models import BannedPlayer, Notifications, User
from afc_auth.views import validate_token
from afc_team.models import Team, TeamMembers

from .event_invite_delivery import deliver_invitation, reach_for_teams
from .models import (
    INVITE_MESSAGE_MAX_LENGTH,
    Event, EventInvitationCampaign, EventInviteToken, EventTeamInvitation, RegisteredCompetitors,
    TournamentTeam,
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
        # SOLO invitations (owner 2026-08-26) address a PLAYER instead of a team. Both shapes are
        # emitted from the same serializer so the cards can render one list containing either.
        "user_id": inv.user_id,
        "username": inv.user.username if inv.user_id else None,
        "user_country": inv.user.country if inv.user_id else None,
        "is_solo": inv.user_id is not None,
        "invited_by": inv.invited_by.username if inv.invited_by_id else None,
        "responded_by": inv.responded_by.username if inv.responded_by_id else None,
        # WHICH KIND of offer this row came from. A row written before campaigns existed has no
        # campaign, and "per_team" is exactly what those rows are, so the fallback is the truth
        # rather than a guess. Both cards branch on this to say the right thing to the team.
        "kind": inv.campaign.kind if inv.campaign_id else "per_team",
        "campaign_id": inv.campaign_id,
    }
    if for_team:
        event = inv.event
        campaign = inv.campaign
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
            # FCFS only, and NULL for every other kind: how many of this campaign's places are
            # left. The team card turns this into "3 places left", which is the one number that
            # makes a first come, first served invitation behave like one.
            "slots_remaining": campaign.slots_remaining() if campaign else None,
            # Places the EVENT itself has left, whatever the campaign says. Shown for fcfs and bulk
            # because for those two the event filling up is the thing that ends the offer, and a
            # team deciding needs to see it coming.
            "event_places_left": _event_places_left(event),
        })
    return data


def _event_places_left(event):
    """How many active places the EVENT still has, or None when it is uncapped.

    Read-only and advisory: the authoritative capacity check is the one inside register_for_event,
    behind its select_for_update lock. This number is what the team card shows so a captain can see
    a first come, first served race closing; it is never what decides a registration."""
    if not event.max_teams_or_players:
        return None
    taken = TournamentTeam.objects.filter(event=event, is_waitlisted=False).count()
    return max(0, event.max_teams_or_players - taken)


def _serialize_campaign(campaign, *, for_team=False, team=None):
    """One campaign as the two frontend cards read it.

    The organizer's table lists campaigns so a bulk send appears as the ONE thing it is rather than
    as nothing at all (bulk writes no addressed rows, so without this it would be invisible).

    `for_team=True` renders an OPEN BULK campaign as an offer on the team page. It deliberately
    borrows the shape of a serialized invitation (same keys: status, message, event_name, accept
    affordances) so EventInvitationsCard can render offers and addressed invitations in one list
    without a second component. `id` is negative for exactly this reason: the team card keys rows by
    id, and a campaign id and an invitation id would otherwise collide in that list.
    """
    data = {
        "campaign_id": campaign.id,
        "kind": campaign.kind,
        "status": campaign.status,
        "message": campaign.message,
        "delivery": campaign.delivery,
        "slots": campaign.slots,
        "slots_remaining": campaign.slots_remaining(),
        "audience_size": len(campaign.audience_team_ids or []),
        "created_at": campaign.created_at,
        "expires_at": campaign.expires_at,
        "created_by": campaign.created_by.username if campaign.created_by_id else None,
        # How many teams have actually taken this campaign up. For bulk this is the only count that
        # exists, since there are no pending rows to tally.
        "accepted_count": campaign.invitations.filter(status="accepted").count(),
    }
    if for_team:
        event = campaign.event
        data.update({
            "id": -campaign.id,          # see the docstring: keeps the team list's keys unique
            "is_offer": True,            # the card branches on this to hit the campaign endpoints
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_slug": event.slug,
            "participant_type": event.participant_type,
            "start_date": event.start_date,
            "registration_open": registration_is_open(event),
            "event_status": effective_event_status(event),
            "invited_by": campaign.created_by.username if campaign.created_by_id else None,
            "decline_reason": "",
            "responded_by": None,
            "team_registered": bool(team) and TournamentTeam.objects.filter(
                event=event, team_id=team.team_id,
            ).exists(),
            "event_places_left": _event_places_left(event),
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
    # A SOLO invitation carries no team: register_for_event takes the solo branch when team_id is
    # absent, and the acting user IS the invitee (checked in _load_for_response), so the same
    # replay-through-the-real-endpoint property holds for both shapes.
    if invitation.team_id:
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
def _whatsapp_reachable(users):
    """How many of `users` could actually receive a WhatsApp message: a saved number AND the opt-in
    left on. Resolved through afc_auth.canonical_profile because duplicate UserProfile rows exist in
    production and canonical_profile (lowest profile_id) is the row every other reader agrees on,
    which is the same rule event_invite_delivery.reach_for_teams applies."""
    try:
        from afc_auth.models import canonical_profile
    except Exception:
        return 0
    count = 0
    for user in users:
        try:
            profile = canonical_profile(user)
        except Exception:
            continue
        if profile is None:
            continue
        number = (getattr(profile, "whatsapp_number", "") or "").strip()
        if number and getattr(profile, "whatsapp_opt_in", True):
            count += 1
    return count


def _team_decision_makers(team):
    """The people on `team` who may answer an invitation, i.e. exactly those _user_can_register_team
    accepts: the owner plus captain / vice-captain / manager / coach.

    Kept as a thin alias over the delivery module's own resolver so there is ONE definition of "who
    can answer" rather than two that can drift. Used here only to count WhatsApp reach before a
    send; the actual delivery calls it itself."""
    from .event_invite_delivery import _decision_makers

    return _decision_makers(team)


def _deliver(invitation_or_team, event, campaign, organizer_name):
    """Send one team's invitation out over the campaign's chosen channels.

    Replaces the old notification-only helper. Everything about WHO is told and HOW lives in
    event_invite_delivery.deliver_invitation; this wrapper exists so the two call sites (an
    addressed invitation, and one audience team of a bulk campaign) read the same.

    Returns the per-channel counts so create_team_invitations can tell the organizer what actually
    went out ("18 notified, 18 emailed, 2 on WhatsApp") instead of just "invited".
    """
    # A SOLO invitation addresses a player, so there is no team to resolve decision-makers from:
    # the invitee IS the recipient. Passed through as `player` and handled by the same delivery
    # function, so both shapes speak one channel vocabulary and produce one set of counts.
    player = getattr(invitation_or_team, "user", None)
    if player is not None:
        return deliver_invitation(
            player=player,
            event=event,
            delivery=campaign.delivery,
            organizer_name=organizer_name,
            note=campaign.message,
            kind=campaign.kind,
        )
    team = getattr(invitation_or_team, "team", invitation_or_team)
    return deliver_invitation(
        team=team,
        event=event,
        delivery=campaign.delivery,
        organizer_name=organizer_name,
        note=campaign.message,
        kind=campaign.kind,
    )


def _notify_inviter(invitation, accepted):
    """Tell whoever sent the invitation what the team decided. Their link opens the EVENT, which is
    where they act on the answer (seed the team, or invite somebody else in its place)."""
    if not invitation.invited_by_id:
        return
    event = invitation.event
    # Either shape of invitee, named the way the organizer would name them.
    who = invitation.team.team_name if invitation.team_id else invitation.user.username
    if accepted:
        message = f"{who} accepted your invitation to {event.event_name}."
    else:
        reason = invitation.decline_reason.strip()
        message = f"{who} declined your invitation to {event.event_name}."
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
# Kinds the create endpoint accepts, and the delivery channels it recognises. Both are validated
# against these rather than trusted, because both end up steering how many rows get written and how
# many people get emailed.
# per_player is the SOLO analogue of per_team (owner 2026-08-26): one addressed invitation per
# invitee. fcfs and bulk mean the same thing for either shape.
VALID_KINDS = {"per_team", "per_player", "fcfs", "bulk"}


def _clean_delivery(raw):
    """Normalise the caller's channel choice to a canonical token, or ("", error) when it names no
    channel we know.

    Delegates the vocabulary entirely to afc_auth.audience, the SAME parser the broadcast composer
    uses, so "both", "push,email" and ["push","email"] all mean the one thing and a typo cannot
    silently send nothing. An invitation that reaches nobody is worse than a refused request, which
    is why an unrecognised value is a 400 here rather than a quiet no-op."""
    from afc_auth.audience import delivery_token, parse_delivery

    channels = parse_delivery(raw if raw not in (None, "") else "both")
    token = delivery_token(channels)
    if not token:
        return "", "delivery must name at least one of: push, email, both, whatsapp."
    return token, None


@api_view(["POST"])
def create_team_invitations(request):
    """POST events/team-invitations/create/ - invite teams to an event, in one of three kinds.

    REQUEST   {event_id: int, team_ids: [int], kind?: "per_team"|"fcfs"|"bulk",
               delivery?: "push"|"email"|"both"|"whatsapp" (comma-joined combinations allowed),
               slots?: int (fcfs only), message?: str, expires_at?: ISO-8601}
    RESPONSE  201 {campaign: <campaign>, invited: [<invitation>],
                   skipped: [{team_id, team_name, reason}], delivered: {recipients, pushed,
                   emailed, whatsapp}, message: str}
              Partial success is normal and is reported rather than refused: picking eight teams of
              which one is already registered must invite the other seven, not fail the batch.
    AUTH      Bearer session token; AFC event admin, or organizer with can_manage_registrations on
              the event's owning org (_can_invite).
    CONSUMED BY  EventTeamInvitesCard.tsx (the "Invite teams" dialog inside the shared
              RegisteredTeamsTab, so both the admin and organizer event-edit pages get it).

    THE THREE KINDS, and what each actually writes
      per_team  one EventTeamInvitation per selected team. All may be accepted; the event's own
                capacity is the only ceiling. This is item 34's behaviour and is the default, so an
                older client that sends no `kind` keeps working unchanged.
      fcfs      one EventTeamInvitation per selected team, plus a campaign `slots` ceiling. More
                teams are asked than there is room for and the quick ones get in. The race is safe
                because the slot is CLAIMED by a single guarded UPDATE (campaign.claim_slot) and the
                event's capacity is guarded by register_for_event's own select_for_update.
      bulk      NO invitation rows at all. One open offer, delivered to the selected teams and
                acceptable by any of them, which is what "a single general bulk invite" means. The
                selected ids are kept on the campaign as its audience, and a row appears only when a
                team answers.

    A team is SKIPPED (never an error) when it is already registered, already holds a pending
    invitation, is banned, or does not exist. Each skip carries a reason the dialog shows. Bulk
    skips only the impossible cases (missing, banned, already registered): "already invited" cannot
    apply to an offer that addresses nobody.
    """
    user, err = _auth_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    team_ids = request.data.get("team_ids") or []
    # SOLO events invite PLAYERS (owner 2026-08-26). Which list is required is decided by the
    # event's participant_type below, once the event is loaded.
    user_ids = request.data.get("user_ids") or []
    # Trimmed rather than refused, and to the SAME number the column holds (models
    # .INVITE_MESSAGE_MAX_LENGTH): a note one character over the line must not lose an
    # organizer the whole batch of invitations they just composed.
    message = (request.data.get("message") or "").strip()[:INVITE_MESSAGE_MAX_LENGTH]
    expires_at = request.data.get("expires_at") or None
    kind = (request.data.get("kind") or "per_team").strip().lower()
    delivery, delivery_error = _clean_delivery(request.data.get("delivery"))

    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    if not isinstance(team_ids, list):
        return Response({"message": "team_ids must be a list of team ids."}, status=400)
    if not isinstance(user_ids, list):
        return Response({"message": "user_ids must be a list of player ids."}, status=400)
    if not team_ids and not user_ids:
        return Response(
            {"message": "Pick at least one team or player to invite."}, status=400,
        )
    if kind not in VALID_KINDS:
        return Response(
            {"message": "kind must be one of: per_team, per_player, fcfs, bulk."}, status=400,
        )
    if delivery_error:
        return Response({"message": delivery_error}, status=400)

    # `slots` is meaningful for fcfs alone. Accepting it for the other kinds would create a ceiling
    # nothing enforces, so it is refused rather than ignored: a silently dropped limit is how an
    # organizer ends up with more teams than they meant to invite.
    slots = request.data.get("slots")
    if slots in ("", None):
        slots = None
    else:
        try:
            slots = int(slots)
        except (TypeError, ValueError):
            return Response({"message": "slots must be a whole number."}, status=400)
        if slots < 1:
            return Response({"message": "slots must be at least 1."}, status=400)
        if kind != "fcfs":
            return Response(
                {"message": "slots only applies to a first come, first served invitation."},
                status=400,
            )

    event = get_object_or_404(Event, event_id=event_id)
    if not _can_invite(user, event):
        return Response({"message": "Unauthorized."}, status=403)
    # WHICH SHAPE THIS EVENT TAKES. A solo event has no teams to address and a duo/squad event has
    # no individual entrants, so sending the wrong list is a mistake worth naming rather than
    # silently ignoring half the request.
    is_solo_event = event.participant_type == "solo"
    if is_solo_event:
        if team_ids:
            return Response(
                {"message": "This is a solo event. Invite players, not teams."}, status=400,
            )
        if not user_ids:
            return Response(
                {"message": "user_ids must be a non-empty list of player ids."}, status=400,
            )
        if kind == "per_team":
            # An older client that sends no kind defaults to per_team; on a solo event that plainly
            # means "one invitation per invitee", so it is translated rather than refused.
            kind = "per_player"
        if kind == "per_player":
            pass
        elif kind not in ("fcfs", "bulk"):
            return Response(
                {"message": "kind must be one of: per_player, fcfs, bulk."}, status=400,
            )
    else:
        if user_ids:
            return Response(
                {"message": "This event is played in teams. Invite teams, not players."},
                status=400,
            )
        if not team_ids:
            return Response(
                {"message": "team_ids must be a non-empty list of team ids."}, status=400,
            )
        if kind == "per_player":
            return Response(
                {"message": "per_player invitations are for solo events."}, status=400,
            )
    if effective_event_status(event) in ("cancelled", "completed"):
        return Response({"message": "This event is no longer open for invitations."}, status=400)

    # Coerce ids defensively: the dialog sends ints, but a hand-made call must not 500 the batch.
    wanted = []
    for raw_id in (user_ids if is_solo_event else team_ids):
        try:
            wanted.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return Response(
            {"message": "user_ids must be a non-empty list of player ids."}
            if is_solo_event
            else {"message": "team_ids must be a non-empty list of team ids."},
            status=400,
        )

    # ONE lookup table keyed by invitee id, whichever shape this event takes, so the loop below is
    # written once. `label` is what the organizer sees in a skip row.
    if is_solo_event:
        invitees = {u.user_id: u for u in User.objects.filter(user_id__in=wanted)}
        label_of = lambda obj: obj.username                                    # noqa: E731
    else:
        invitees = {t.team_id: t for t in Team.objects.filter(team_id__in=wanted)}
        label_of = lambda obj: obj.team_name                                   # noqa: E731

    invited, skipped = [], []
    # Per-channel totals across the whole send, so the organizer is told what actually went out
    # rather than just how many rows were written.
    delivered = {"recipients": 0, "pushed": 0, "emailed": 0, "whatsapp": 0}
    organizer_name = getattr(user, "username", "") or ""

    with transaction.atomic():
        # One pending invitation per (event, invitee) is enforced HERE rather than by a DB
        # constraint: MySQL has no partial unique index, so a conditional UniqueConstraint would be
        # skipped silently (see the Meta comment on the model). Both reads happen inside the
        # transaction.
        if is_solo_event:
            # A solo entrant is a RegisteredCompetitors row carrying a user, with no TournamentTeam
            # to check: the team tables have nothing to say about a solo event.
            already_registered = set(
                RegisteredCompetitors.objects.filter(event=event, user_id__in=wanted)
                .values_list("user_id", flat=True)
            )
            already_pending = set(
                EventTeamInvitation.objects.filter(
                    event=event, user_id__in=wanted, status="pending",
                ).values_list("user_id", flat=True)
            )
            banned_ids = set(
                BannedPlayer.objects.filter(
                    banned_player_id__in=wanted, is_active=True,
                    ban_end_date__gt=timezone.now(),
                ).values_list("banned_player_id", flat=True)
            )
        else:
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
            banned_ids = set()

        # PRIVATE event: mint the token the accept will replay, so the invitation is actually
        # acceptable without teaching register_for_event a second way in. per_team / per_player keep
        # one single-use token PER INVITATION; fcfs and bulk need ONE SHARED token for the campaign,
        # because by definition more than one invitee redeems them (EventInviteToken.is_shared
        # already models exactly that, and register_for_event already honours it).
        campaign_token = None
        if not event.is_public and kind in ("fcfs", "bulk"):
            campaign_token = EventInviteToken.objects.create(
                event=event, created_by=user, is_shared=True,
            )

        campaign = EventInvitationCampaign.objects.create(
            event=event, kind=kind, message=message, delivery=delivery, slots=slots,
            expires_at=expires_at or None, created_by=user, invite_token=campaign_token,
            audience_team_ids=[], audience_user_ids=[],
        )

        audience = []
        for invitee_id in wanted:
            invitee = invitees.get(invitee_id)
            id_key = "user_id" if is_solo_event else "team_id"
            name_key = "username" if is_solo_event else "team_name"

            if not invitee:
                skipped.append({id_key: invitee_id, name_key: None, "reason": "not_found"})
                continue
            banned = invitee_id in banned_ids if is_solo_event else invitee.is_banned
            if banned:
                skipped.append(
                    {id_key: invitee_id, name_key: label_of(invitee), "reason": "banned"}
                )
                continue
            if invitee_id in already_registered:
                skipped.append({
                    id_key: invitee_id, name_key: label_of(invitee),
                    "reason": "already_registered",
                })
                continue
            # Only an ADDRESSED kind can collide with an existing addressed invitation. A bulk offer
            # addresses nobody, so an invitee holding a pending addressed invitation can still be
            # told about it.
            if kind != "bulk" and invitee_id in already_pending:
                skipped.append({
                    id_key: invitee_id, name_key: label_of(invitee), "reason": "already_invited",
                })
                continue

            audience.append(invitee_id)

            if kind == "bulk":
                # No row: the offer IS the campaign. The invitee is delivered to below, and a row
                # gets written only if they answer (accept_bulk_campaign / decline_bulk_campaign).
                counts = (
                    deliver_invitation(
                        player=invitee, event=event, delivery=delivery,
                        organizer_name=organizer_name, note=message, kind=kind,
                    )
                    if is_solo_event
                    else deliver_invitation(
                        team=invitee, event=event, delivery=delivery,
                        organizer_name=organizer_name, note=message, kind=kind,
                    )
                )
            else:
                token = None
                if not event.is_public:
                    # An addressed invitation keeps its own single-use token; fcfs shares the
                    # campaign's.
                    token = campaign_token or EventInviteToken.objects.create(
                        event=event, created_by=user,
                    )
                invitation = EventTeamInvitation.objects.create(
                    event=event,
                    team=None if is_solo_event else invitee,
                    user=invitee if is_solo_event else None,
                    invited_by=user, message=message,
                    expires_at=expires_at or None, invite_token=token, campaign=campaign,
                )
                counts = _deliver(invitation, event, campaign, organizer_name)
                invited.append(_serialize(invitation))

            for key in delivered:
                delivered[key] += counts.get(key, 0)

        if is_solo_event:
            campaign.audience_user_ids = audience
            campaign.save(update_fields=["audience_user_ids"])
        else:
            campaign.audience_team_ids = audience
            campaign.save(update_fields=["audience_team_ids"])

    noun = "player" if is_solo_event else "team"
    if kind == "bulk":
        summary = f"Open invitation sent to {len(audience)} {noun}(s), {len(skipped)} skipped."
    else:
        summary = f"{len(invited)} {noun}(s) invited, {len(skipped)} skipped."

    return Response({
        "message": summary,
        "campaign": _serialize_campaign(campaign),
        "invited": invited,
        "skipped": skipped,
        "delivered": delivered,
    }, status=201)


@api_view(["GET"])
def invitation_reach(request):
    """GET events/team-invitations/reach/?event_id=<id>&team_ids=1,2,3 - who a send would reach.

    REQUEST   query string; team_ids is a comma-separated list (the composer sends its current
              selection, so this is re-asked as teams are ticked).
    RESPONSE  200 {recipients: int, email: int, whatsapp: int, teams: int}
              `recipients` counts PEOPLE, deduplicated across the selected teams, because one
              person can run two of them and would still only be told once.
    AUTH      Bearer session token; the same _can_invite gate as creating an invitation. Reach is
              a read of who can be contacted, so it is not shown to anybody who could not send.
    CONSUMED BY  EventTeamInvitesCard.tsx - the line under the WhatsApp tick box in the invite
              dialog ("WhatsApp reaches 2 of these 14 people").

    WHY THIS ENDPOINT EXISTS AT ALL
        Because the channels became a choice, and the three are not equivalent. Everyone has an
        email address; WhatsApp only reaches somebody who saved a number AND left the opt-in on,
        which site-wide is about 4% of the people who can answer an invitation. Without this the
        admin ticks WhatsApp, sees "invitations sent", and believes the teams were told. The
        numbers are computed live rather than written into the copy so they cannot go stale.
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

    # Bounded like every other list here: a hand-made call must not be able to ask us to walk the
    # whole team table. MAX_LIMIT is the same ceiling the invitation lists use.
    # SOLO events send user_ids instead (owner 2026-08-26). Whichever list arrives, the answer has
    # the same shape, so the composer's "reaches N of these M people" line reads the same.
    is_solo_reach = bool((request.GET.get("user_ids") or "").strip())
    raw_ids = (
        request.GET.get("user_ids") if is_solo_reach else request.GET.get("team_ids")
    ) or ""

    wanted = []
    for raw_id in raw_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            wanted.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    wanted = wanted[:MAX_LIMIT]
    if not wanted:
        return Response(
            {"recipients": 0, "email": 0, "whatsapp": 0, "teams": 0}, status=200,
        )

    if is_solo_reach:
        # A solo invitation reaches exactly the invitees: there is no roster of decision-makers to
        # expand, so the count is the players themselves, filtered the same way reach_for_teams
        # filters (an address must exist to be counted email-reachable).
        players = list(User.objects.filter(user_id__in=wanted))
        data = {
            "recipients": len(players),
            "email": sum(1 for u in players if (getattr(u, "email", "") or "").strip()),
            "whatsapp": _whatsapp_reachable(players),
        }
        data["teams"] = 0
        return Response(data, status=200)

    teams = list(Team.objects.filter(team_id__in=wanted))
    data = reach_for_teams(teams)
    data["teams"] = len(teams)
    return Response(data, status=200)


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

    qs = base.select_related("team", "invited_by", "responded_by", "campaign").order_by("-created_at")
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    rows, meta = _paginate(qs, request)
    # Campaigns are listed ALONGSIDE the addressed rows, not instead of them, because a bulk send
    # writes no addressed rows and would otherwise be invisible on the organizer's own screen: they
    # would press Send, see nothing appear, and send again. Bounded by the same MAX_LIMIT as the
    # rows; an event has a handful of campaigns, not a page of them.
    campaigns = (
        EventInvitationCampaign.objects.filter(event=event)
        .select_related("created_by")
        .order_by("-created_at")[:MAX_LIMIT]
    )
    return Response({
        "invitations": [_serialize(inv) for inv in rows],
        "campaigns": [_serialize_campaign(c) for c in campaigns],
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
        # A per_team invitation owns its token outright, so cancelling destroys it. An fcfs
        # invitation SHARES its campaign's token with every other team in the same send, and
        # deleting that would lock all of them out of a private event because one was withdrawn.
        # So the token is only destroyed when it belongs to this invitation alone.
        token_is_shared = bool(
            token and invitation.campaign_id and invitation.campaign.invite_token_id == token.id
        )
        invitation.status = "cancelled"
        invitation.responded_by = user
        invitation.responded_at = timezone.now()
        invitation.invite_token = None
        invitation.save(update_fields=["status", "responded_by", "responded_at", "invite_token"])
        if token and not token_is_shared:
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

    qs = base.select_related(
        "event", "team", "invited_by", "responded_by", "campaign",
    ).order_by("-created_at")
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    rows, meta = _paginate(qs, request)
    offers = _open_offers_for(team)
    return Response({
        # Offers first: an open bulk invitation is the only row here with a live deadline attached
        # to somebody else's speed, so it belongs above the addressed ones a team can answer at
        # leisure. Both shapes carry the same keys (see _serialize_campaign), so the card renders
        # one list.
        "invitations": [_serialize_campaign(c, for_team=True, team=team) for c in offers]
                       + [_serialize(inv, for_team=True) for inv in rows],
        "can_respond": _user_can_register_team(user, team),
        "team_id": team.team_id,
        "pending_count": base.filter(status="pending").count() + len(offers),
        **meta,
    }, status=200)


def _open_offers_for(team):
    """The open BULK campaigns this team may still take up.

    Three things have to be true: the campaign is open, the team is on its guest list, and the team
    has not already answered it. The last one is why an accepted or declined offer stops appearing:
    answering writes an EventTeamInvitation row, and that row then shows in the ordinary list with
    its real status, so the offer does not need to linger.

    `audience_team_ids__contains` is a JSON containment lookup, which MySQL supports, so the guest
    list is filtered in the database rather than by loading every open campaign and scanning it.
    Capped at MAX_LIMIT for the same reason every list here is: a team's page must not be able to
    grow unbounded because somebody ran a lot of campaigns.
    """
    answered = set(
        EventTeamInvitation.objects.filter(team=team, campaign__isnull=False)
        .values_list("campaign_id", flat=True)
    )
    open_campaigns = (
        EventInvitationCampaign.objects.filter(
            kind="bulk", status="open", audience_team_ids__contains=team.team_id,
        )
        .select_related("event", "created_by")
        .order_by("-created_at")[:MAX_LIMIT]
    )
    offers = []
    for campaign in open_campaigns:
        if campaign.id in answered:
            continue
        # Lazy deadline sweep, matching _expire_stale's contract for addressed rows: a campaign
        # nobody looks at does not need a cron job to go stale.
        if campaign.is_expired():
            EventInvitationCampaign.objects.filter(pk=campaign.pk, status="open").update(
                status="closed",
            )
            continue
        offers.append(campaign)
    return offers


def _load_for_response(user, invitation_id):
    """Shared front half of accept and decline: fetch the invitation, check the caller may answer
    for that team, and check the invitation is still answerable. Returns (invitation, None) or
    (None, error Response)."""
    invitation = get_object_or_404(
        # `campaign` is joined here because both callers read it straight afterwards (accept for the
        # fcfs slot claim, decline for the serialized kind), so it costs one join instead of one
        # extra query per answer.
        EventTeamInvitation.objects.select_related(
            "event", "team", "user", "invited_by", "campaign"
        ),
        id=invitation_id,
    )
    # Answering IS registering, so the permission must be the SAME one register_for_event applies
    # to a self-registration (owner, captain, vice-captain, manager, coach). If these two ever
    # disagreed, a person could accept an invitation and then be refused by the endpoint the accept
    # itself calls.
    # A SOLO invitation is addressed to ONE person and only that person may answer it. There is no
    # roster of decision-makers to consult, and letting anybody else answer would register a player
    # for an event they never agreed to.
    if invitation.user_id:
        if user.user_id != invitation.user_id:
            return None, Response(
                {"message": "Only the invited player can answer this invitation."},
                status=403,
            )
    elif not _user_can_register_team(user, invitation.team):
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
              409 "This invitation is closed" is the ONE refusal that is ours rather than
              register_for_event's: a first come, first served campaign whose places are gone. It
              has to come before the registration attempt, because by the time that endpoint spoke
              the team would already be in.
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

    campaign = invitation.campaign

    # ── FCFS: claim one of the campaign's places BEFORE registering ──────────────────────────
    # Only bites when the organizer set a `slots` ceiling; otherwise claim_slot is a no-op returning
    # True and the event's own capacity is the only limit. The claim is a single guarded UPDATE, so
    # two captains pressing Accept on the last place at the same instant cannot both win it: one
    # matches a row, the other matches zero. See EventInvitationCampaign.claim_slot.
    claimed = False
    if campaign is not None:
        if campaign.status != "open":
            return Response(
                {"message": "This invitation is closed. All of its places have been taken."},
                status=409,
            )
        if not campaign.claim_slot():
            # Somebody else took the last place between the card rendering and this press. Close the
            # campaign so the remaining invitations stop offering something that is gone.
            _close_if_full(campaign)
            return Response(
                {"message": "This invitation is closed. All of its places have been taken."},
                status=409,
            )
        claimed = campaign.slots is not None and campaign.kind == "fcfs"

    response = _register_through_the_normal_path(request, invitation)

    # Anything that is not a success leaves the invitation exactly as it was: the team has not
    # registered, so they have not accepted. Handing the refusal back untouched is the whole point.
    # The claimed place goes back, or a team whose roster was one player short would have burned a
    # slot nobody is standing in.
    if response.status_code >= 400:
        if claimed:
            campaign.release_slot()
        return response

    invitation.status = "accepted"
    invitation.responded_by = user
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_by", "responded_at"])
    _notify_inviter(invitation, accepted=True)
    if campaign is not None:
        _close_if_full(campaign)

    if isinstance(response.data, dict):
        response.data["invitation"] = _serialize(invitation, for_team=True)
    return response


def _close_if_full(campaign):
    """Flip an fcfs campaign to 'closed' once its last place is claimed.

    Cosmetic but load-bearing for the team side: without it, the teams who were invited and lost the
    race keep seeing a live Accept button for an offer that will now always refuse them, and the
    organizer's list keeps showing pending rows for a race that is over. Only fcfs campaigns with an
    explicit ceiling can be 'full' in this sense; the other kinds end when the EVENT fills, which
    register_for_event reports on its own."""
    if campaign.kind != "fcfs" or campaign.slots is None:
        return
    if campaign.status == "open" and campaign.slots_remaining() == 0:
        EventInvitationCampaign.objects.filter(pk=campaign.pk, status="open").update(
            status="closed",
        )
        campaign.status = "closed"


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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# BULK CAMPAIGNS: the team side of an offer that addresses nobody
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _load_bulk_for_response(user, campaign_id, team_id):
    """Shared front half of the two bulk endpoints: resolve the campaign and the team answering it,
    check this caller may answer for that team, and check the offer is still takeable.

    Returns (campaign, team, None) or (None, None, error Response).

    The permission is the SAME one an addressed invitation uses (_user_can_register_team), for the
    same reason: answering IS registering, so anybody allowed to answer must be somebody
    register_for_event would accept. A bulk offer is open, but it is not open to a random member.
    """
    campaign = get_object_or_404(
        EventInvitationCampaign.objects.select_related("event", "created_by"), id=campaign_id,
    )
    if campaign.kind != "bulk":
        return None, None, Response(
            {"message": "This is not an open invitation."}, status=400,
        )

    if not team_id:
        return None, None, Response({"message": "team_id is required."}, status=400)
    team = get_object_or_404(Team, team_id=team_id)

    if not _user_can_register_team(user, team):
        return None, None, Response(
            {"message": "Only the team owner, captain, vice-captain, manager, or coach can "
                        "answer an event invitation."},
            status=403,
        )
    # The audience is the guest list. Without this check any team that learned the campaign id could
    # walk into an event they were never offered, which would turn a "general" invitation into a
    # public one.
    if team.team_id not in (campaign.audience_team_ids or []):
        return None, None, Response(
            {"message": "This invitation was not sent to your team."}, status=403,
        )
    if campaign.is_expired() and campaign.status == "open":
        EventInvitationCampaign.objects.filter(pk=campaign.pk).update(status="closed")
        campaign.status = "closed"
    if campaign.status != "open":
        return None, None, Response(
            {"message": "This invitation is closed."}, status=400,
        )
    # One answer per team. The row a previous answer wrote is what makes this idempotent, so a
    # double-tap on a phone cannot register the same team twice or leave two rows behind.
    existing = EventTeamInvitation.objects.filter(campaign=campaign, team=team).first()
    if existing:
        return None, None, Response(
            {"message": f"Your team already {existing.status} this invitation."}, status=400,
        )
    return campaign, team, None


@api_view(["POST"])
def accept_bulk_campaign(request, campaign_id):
    """POST events/invitation-campaigns/<campaign_id>/accept/ - take up an open (bulk) invitation.

    REQUEST   {team_id: int, roster_member_ids: [int], sponsor_ids?: {}, sponsorships?: [],
               invite_token?: str}
    RESPONSE  201 the EXACT body register_for_event returns, plus {invitation: <invitation>}.
              On refusal: the EXACT status and body register_for_event returned, unchanged, so a
              team taking up an open invitation reads the same wording as anybody else.
    AUTH      Bearer session token; whoever may register the team (views._user_can_register_team),
              and the team must be in the campaign's audience.
    CONSUMED BY  EventInvitationsCard.tsx, which renders an open offer beside addressed invitations
              and posts here instead of the per-invitation accept when the row is an offer.

    A bulk offer writes no row until it is answered, so accepting MATERIALIZES the
    EventTeamInvitation (already 'accepted') rather than updating one. That row is what gives the
    organizer a record of who took the offer up, and what stops the same team answering twice.

    Registration itself still goes through views.register_for_event, exactly as every other kind
    does; see _register_through_the_normal_path.
    """
    user, err = _auth_user(request)
    if err:
        return err

    campaign, team, err = _load_bulk_for_response(
        user, campaign_id, request.data.get("team_id"),
    )
    if err:
        return err

    # A throwaway, UNSAVED invitation carrying the campaign's event/team/token, purely so the accept
    # can reuse _register_through_the_normal_path unchanged. It is saved only if the registration
    # succeeds, which is what keeps a refused attempt from leaving a row behind.
    draft = EventTeamInvitation(
        event=campaign.event, team=team, invited_by=campaign.created_by,
        message=campaign.message, campaign=campaign, invite_token=campaign.invite_token,
    )
    response = _register_through_the_normal_path(request, draft)
    if response.status_code >= 400:
        return response

    draft.status = "accepted"
    draft.responded_by = user
    draft.responded_at = timezone.now()
    draft.save()
    _notify_inviter(draft, accepted=True)

    if isinstance(response.data, dict):
        response.data["invitation"] = _serialize(draft, for_team=True)
    return response


@api_view(["POST"])
def decline_bulk_campaign(request, campaign_id):
    """POST events/invitation-campaigns/<campaign_id>/decline/ - dismiss an open invitation.

    REQUEST   {team_id: int, reason?: str}
    RESPONSE  200 {message, invitation: <invitation>}
    AUTH      Bearer session token; whoever may register the team, and the team must be in the
              campaign's audience.
    CONSUMED BY  EventInvitationsCard.tsx - the Decline button on an offer row.

    Declining a general offer is not required (ignoring it is a perfectly good answer), but without
    it the card would have no way to be dismissed and would sit on the team page until the event
    ended. Writing the row also tells the organizer the team looked and said no, which is the whole
    reason a decline beats silence.
    """
    user, err = _auth_user(request)
    if err:
        return err

    campaign, team, err = _load_bulk_for_response(
        user, campaign_id, request.data.get("team_id"),
    )
    if err:
        return err

    invitation = EventTeamInvitation.objects.create(
        event=campaign.event, team=team, invited_by=campaign.created_by,
        message=campaign.message, campaign=campaign, status="declined",
        decline_reason=(request.data.get("reason") or "").strip()[:280],
        responded_by=user, responded_at=timezone.now(),
    )
    _notify_inviter(invitation, accepted=False)

    return Response(
        {"message": "Invitation declined.", "invitation": _serialize(invitation, for_team=True)},
        status=200,
    )


@api_view(["POST"])
def close_invitation_campaign(request, campaign_id):
    """POST events/invitation-campaigns/<campaign_id>/close/ - stop an offer taking new answers.

    REQUEST   (no body)
    RESPONSE  200 {message, campaign: <campaign>}
    AUTH      Bearer session token; same _can_invite gate as creating.
    CONSUMED BY  EventTeamInvitesCard.tsx - the Close button on an open campaign row.

    This is the bulk and fcfs equivalent of cancelling a single invitation. Answers already given
    stand (teams are in the bracket by then); it only stops NEW ones. The shared invite token is
    destroyed so a closed offer cannot still let a team through a private event's door, which is the
    same thing cancel_team_invitation does for a single invitation's token.
    """
    user, err = _auth_user(request)
    if err:
        return err

    campaign = get_object_or_404(
        EventInvitationCampaign.objects.select_related("event"), id=campaign_id,
    )
    if not _can_invite(user, campaign.event):
        return Response({"message": "Unauthorized."}, status=403)
    if campaign.status != "open":
        return Response({"message": f"This invitation is already {campaign.status}."}, status=400)

    with transaction.atomic():
        token = campaign.invite_token
        campaign.status = "cancelled"
        campaign.invite_token = None
        campaign.save(update_fields=["status", "invite_token"])
        # Pending ADDRESSED rows under this campaign die with it: leaving them 'pending' would keep
        # offering an Accept button that now always refuses.
        EventTeamInvitation.objects.filter(campaign=campaign, status="pending").update(
            status="cancelled", responded_by=user, responded_at=timezone.now(),
        )
        if token:
            token.delete()

    return Response(
        {"message": "Invitation closed.", "campaign": _serialize_campaign(campaign)}, status=200,
    )
