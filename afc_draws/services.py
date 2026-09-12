"""
afc_draws/services.py - everything the group draw DOES, kept out of the views so the rules are
testable without a request and reusable by Phase 2 (AFC Seeds: rerolls and peeks act on the same
cards through the same helpers).

    deal(stage, user)                 create the draw: one sealed card per active competitor
    open_draw(draw, closes_at)        start accepting picks, notify every eligible captain/player
    pick(draw, user, number, team_id) turn one card over for the competitor the user acts for
    close_draw(draw)                  deal the stragglers into what is left, publish the salt
    maybe_lazy_close(draw)            a draw past its close time closes on the next read
    reset(draw)                       throw the draw away (only while the stage has no result)
    draw_is_open(stage)               the guard the ordinary seeders ask before dealing
    serialize_board(draw, viewer)     the board the event page and the organizer card render

Every write goes through select_for_update on the draw row, so two captains tapping the same card
at the same instant leave exactly one holder (afc_draws/tests.py proves it with a threaded pick).

HOW IT CONNECTS
    Models: afc_draws.models (StageDraw, DrawCard). Writes StageGroupCompetitor rows in
    afc_tournament_and_scrims so every existing reader of group membership (standings, rooms,
    broadcasts, Discord roles, the advancement engine) sees a drawn group exactly like a seeded one.
    Permission helpers are borrowed from afc_tournament_and_scrims.views (_is_event_admin,
    _user_can_register_team, org_can_event) so a captain who may register the team may pick for it,
    and whoever may seed a stage may run its draw. Notifications: afc_auth.Notifications, deep
    linked to the event page ("Take me there"), the same shape h2h_notifications uses.
"""
import hashlib
import json
import random
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from afc_auth.models import Notifications
from afc_tournament_and_scrims.models import Match, StageCompetitor, StageGroupCompetitor, StageGroups

from .models import DrawCard, StageDraw


class DrawError(Exception):
    """A rule refused the action. `status` is the HTTP code the view answers with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── the seal ────────────────────────────────────────────────────────────────────────────────────

def mapping_of(cards):
    """The canonical mapping the seal covers: [[number, group_id], ...] sorted by number."""
    return sorted([[c.number, c.stage_group_id] for c in cards], key=lambda pair: pair[0])


def commitment_for(salt, mapping):
    payload = json.dumps(mapping, separators=(",", ":"))
    return hashlib.sha256(f"{salt}:{payload}".encode("utf-8")).hexdigest()


# ── who is in the stage, and who may act for them ─────────────────────────────────────────────

def active_competitors(stage):
    return list(
        StageCompetitor.objects.filter(stage=stage, status="active")
        .select_related("tournament_team__team", "player__user")
        .order_by("id")
    )


def _competitor_key(comp):
    """('team', id) or ('player', id): what a card is keyed on."""
    if comp.tournament_team_id:
        return ("team", comp.tournament_team_id)
    return ("player", comp.player_id)


def competitors_user_acts_for(stage, user):
    """The active competitors of `stage` this user may pick for.

    A team: anyone the club allows to register it (owner, captain, vice-captain, manager, coach by
    default, or whatever the club's own role matrix says). A solo entry: the player themself.
    """
    from afc_tournament_and_scrims.views import _user_can_register_team

    mine = []
    for comp in active_competitors(stage):
        if comp.tournament_team_id:
            team = comp.tournament_team.team
            if team is not None and _user_can_register_team(user, team):
                mine.append(comp)
        elif comp.player_id and comp.player.user_id == user.user_id:
            mine.append(comp)
    return mine


def user_may_run_draw(user, stage):
    """Whoever may seed the stage may run its draw: AFC event admins, or an organizer with
    can_manage_registrations on the owning org (the same gate the seeders use)."""
    from afc_organizers.permissions import org_can_event
    from afc_tournament_and_scrims.views import _is_event_admin

    return _is_event_admin(user) or org_can_event(user, "can_manage_registrations", stage.event)


def draw_is_open(stage):
    return StageDraw.objects.filter(stage=stage, status=StageDraw.STATUS_OPEN).exists()


# ── lifecycle ───────────────────────────────────────────────────────────────────────────────────

def deal(stage, user):
    """Create the draw for a stage: one card per active competitor, groups dealt evenly, order
    shuffled, mapping sealed. Refuses if the stage already has a draw, has no groups, has no active
    competitors, or its groups already hold rows (a half-seeded stage would double-place teams)."""
    # A query, not hasattr(stage, "draw"): the reverse one-to-one is cached on the instance, so a
    # stage that had its draw reset a moment ago would still look like it has one.
    if StageDraw.objects.filter(stage=stage).exists():
        raise DrawError("This stage already has a draw. Reset it first.", 409)
    groups = list(StageGroups.objects.filter(stage=stage).order_by("group_id"))
    if not groups:
        raise DrawError("This stage has no groups yet.")
    competitors = active_competitors(stage)
    if not competitors:
        raise DrawError("No active competitors in this stage yet. Seed the stage first.")
    if StageGroupCompetitor.objects.filter(stage_group__stage=stage).exists():
        raise DrawError("The groups of this stage already hold teams. Clear them before opening a draw.", 409)

    # Even deal, then shuffle the order the cards are numbered in. Card i hides groups[i % g] BEFORE
    # the shuffle, so every group gets its fair share whatever the numbers end up being.
    slots = [groups[i % len(groups)] for i in range(len(competitors))]
    random.SystemRandom().shuffle(slots)

    salt = secrets.token_hex(16)
    with transaction.atomic():
        draw = StageDraw.objects.create(stage=stage, salt=salt, commitment="", created_by=user)
        cards = [
            DrawCard(draw=draw, number=i + 1, stage_group=group) for i, group in enumerate(slots)
        ]
        DrawCard.objects.bulk_create(cards)
        cards = list(draw.cards.all())
        draw.commitment = commitment_for(salt, mapping_of(cards))
        draw.save(update_fields=["commitment"])
    return draw


def open_draw(draw, closes_at):
    """Start the window. closes_at must be in the future; stragglers are dealt at that moment
    (lazily, on the next read after it passes) or when the organizer closes early."""
    if draw.status != StageDraw.STATUS_DRAFT:
        raise DrawError("Only a draft draw can be opened.", 409)
    if closes_at is None or closes_at <= timezone.now():
        raise DrawError("The close time must be in the future.")
    draw.status = StageDraw.STATUS_OPEN
    draw.opens_at = timezone.now()
    draw.closes_at = closes_at
    draw.save(update_fields=["status", "opens_at", "closes_at", "updated_at"])
    _notify_open(draw)
    return draw


def pick(draw, user, number, tournament_team_id=None):
    """Turn card `number` over for the competitor `user` acts for. Returns the card.

    Rules, in the order they are checked:
      - the draw is open (a draw past its close time closes first and then refuses)
      - the user may act for at least one active competitor of the stage; if for several (a
        manager of two clubs in one lobby), `tournament_team_id` says which
      - that competitor holds no card yet, and is not already placed in a group by hand
      - the card exists and is still face down (checked under a row lock, so a race has one winner)
    """
    maybe_lazy_close(draw)
    if draw.status != StageDraw.STATUS_OPEN:
        raise DrawError("This draw is not open.", 409)

    mine = competitors_user_acts_for(draw.stage, user)
    if not mine:
        raise DrawError("You cannot pick for any team in this stage.", 403)
    if tournament_team_id is not None:
        mine = [c for c in mine if c.tournament_team_id == int(tournament_team_id)]
        if not mine:
            raise DrawError("You cannot pick for that team.", 403)
    if len(mine) > 1:
        raise DrawError("You can act for more than one team here. Say which one.", 400)
    comp = mine[0]

    try:
        number = int(number)
    except (TypeError, ValueError):
        raise DrawError("card number must be an integer.")

    with transaction.atomic():
        # Lock the draw row: every pick, close and reset serialises on it.
        StageDraw.objects.select_for_update().get(pk=draw.pk)
        if _card_of(draw, comp) is not None:
            raise DrawError("You already have a card in this draw.", 409)
        if _already_grouped(draw.stage, comp):
            raise DrawError("This competitor is already placed in a group.", 409)
        card = DrawCard.objects.select_for_update().filter(draw=draw, number=number).first()
        if card is None:
            raise DrawError("No such card.", 404)
        if card.is_taken:
            raise DrawError("That card has already been taken. Pick another.", 409)
        _take(card, comp, user, DrawCard.VIA_PICK)
    return card


def close_draw(draw):
    """Deal every unpicked competitor into the cards that are left, at random, then publish."""
    with transaction.atomic():
        draw = StageDraw.objects.select_for_update().get(pk=draw.pk)
        if draw.status == StageDraw.STATUS_CLOSED:
            return draw
        if draw.status != StageDraw.STATUS_OPEN:
            raise DrawError("Only an open draw can be closed.", 409)

        cards = list(draw.cards.select_related("stage_group").all())
        taken_keys = {
            ("team", c.tournament_team_id) if c.tournament_team_id else ("player", c.player_id)
            for c in cards if c.is_taken
        }
        stragglers = [c for c in active_competitors(draw.stage) if _competitor_key(c) not in taken_keys]
        # Someone hand-placed in a group after the draw was dealt is not a straggler.
        stragglers = [c for c in stragglers if not _already_grouped(draw.stage, c)]
        free = [c for c in cards if not c.is_taken]
        random.SystemRandom().shuffle(free)

        placed_auto = []
        for comp in stragglers:
            if free:
                card = free.pop()
            else:
                # A competitor added to the stage after the deal: give them a card in the group
                # holding the fewest cards so the deal stays as even as it can be.
                card = _extra_card(draw, cards)
                cards.append(card)
            _take(card, comp, None, DrawCard.VIA_AUTO)
            placed_auto.append((comp, card))

        draw.status = StageDraw.STATUS_CLOSED
        draw.closed_at = timezone.now()
        draw.save(update_fields=["status", "closed_at", "updated_at"])
    _notify_auto_placed(draw, placed_auto)
    return draw


def maybe_lazy_close(draw):
    """A draw whose close time has passed closes on the next read, so no beat task is needed."""
    if draw.status == StageDraw.STATUS_OPEN and draw.closes_at and draw.closes_at <= timezone.now():
        return close_draw(draw)
    return draw


def reset(draw):
    """Throw the draw away, and the group rows it wrote. Refused once any result exists in the
    stage, because a group with a played map is no longer a thing anybody may redraw."""
    stage = draw.stage
    if Match.objects.filter(group__stage=stage, result_inputted=True).exists():
        raise DrawError("Results have been entered for this stage; the draw cannot be reset.", 409)
    with transaction.atomic():
        StageDraw.objects.select_for_update().get(pk=draw.pk)
        for card in draw.cards.all():
            if card.is_taken:
                StageGroupCompetitor.objects.filter(
                    stage_group=card.stage_group,
                    tournament_team_id=card.tournament_team_id,
                    player_id=card.player_id,
                ).delete()
        draw.delete()


# ── internals ───────────────────────────────────────────────────────────────────────────────────

def _card_of(draw, comp):
    if comp.tournament_team_id:
        return draw.cards.filter(tournament_team_id=comp.tournament_team_id).first()
    return draw.cards.filter(player_id=comp.player_id).first()


def _already_grouped(stage, comp):
    qs = StageGroupCompetitor.objects.filter(stage_group__stage=stage)
    if comp.tournament_team_id:
        return qs.filter(tournament_team_id=comp.tournament_team_id).exists()
    return qs.filter(player_id=comp.player_id).exists()


def _take(card, comp, user, via):
    """Mark the card taken and write the ordinary group row in one go."""
    card.tournament_team_id = comp.tournament_team_id
    card.player_id = comp.player_id
    card.picked_by = user
    card.picked_at = timezone.now()
    card.via = via
    try:
        card.save(update_fields=["tournament_team", "player", "picked_by", "picked_at", "via"])
    except IntegrityError:
        raise DrawError("You already have a card in this draw.", 409)
    StageGroupCompetitor.objects.get_or_create(
        stage_group=card.stage_group,
        tournament_team_id=comp.tournament_team_id,
        player_id=comp.player_id,
    )


def _extra_card(draw, cards):
    counts = {}
    for c in cards:
        counts[c.stage_group_id] = counts.get(c.stage_group_id, 0) + 1
    groups = list(StageGroups.objects.filter(stage=draw.stage).order_by("group_id"))
    for g in groups:
        counts.setdefault(g.group_id, 0)
    smallest = min(groups, key=lambda g: (counts[g.group_id], g.group_id))
    number = max((c.number for c in cards), default=0) + 1
    return DrawCard.objects.create(draw=draw, number=number, stage_group=smallest)


# ── notifications ───────────────────────────────────────────────────────────────────────────────

def _people_who_pick_for(comp):
    """The users a notification about this competitor's draw should reach."""
    from afc_team.models import TeamMembers
    from afc_tournament_and_scrims.views import _user_can_register_team

    if comp.tournament_team_id:
        team = comp.tournament_team.team
        if team is None:
            return []
        return [
            m.member for m in TeamMembers.objects.filter(team=team).select_related("member")
            if _user_can_register_team(m.member, team)
        ]
    if comp.player_id and comp.player.user_id:
        return [comp.player.user]
    return []


def _notify(users, event, title, message, kind):
    seen = {}
    for u in users:
        seen.setdefault(u.user_id, u)
    if not seen:
        return 0
    Notifications.objects.bulk_create([
        Notifications(
            user=u, title=title, message=message, notification_type=kind, related_event=event,
            target_type="event", target_id=str(event.slug or event.event_id),
        )
        for u in seen.values()
    ])
    return len(seen)


def _notify_open(draw):
    stage = draw.stage
    event = stage.event
    when = timezone.localtime(draw.closes_at).strftime("%d %b %H:%M") if draw.closes_at else ""
    users = []
    for comp in active_competitors(stage):
        users.extend(_people_who_pick_for(comp))
    return _notify(
        users, event,
        f"Group draw open: {event.event_name}",
        f"Pick your group for {stage.stage_name}. Turn over a card on the event page before {when}; "
        f"anyone who has not picked by then is placed automatically.",
        "group_draw_open",
    )


def _notify_auto_placed(draw, placed):
    event = draw.stage.event
    for comp, card in placed:
        _notify(
            _people_who_pick_for(comp), event,
            f"You were placed in {card.stage_group.group_name}",
            f"The group draw for {draw.stage.stage_name} closed before you picked, so your team was "
            f"placed in {card.stage_group.group_name} (card {card.number}).",
            "group_draw_auto",
        )


# ── the board ───────────────────────────────────────────────────────────────────────────────────

def _competitor_name(card):
    if card.tournament_team_id:
        return card.tournament_team.display_name
    if card.player_id:
        u = card.player.user
        return u.username if u else "Player"
    return None


def serialize_board(draw, viewer=None):
    """What the event page and the organizer card render. The salt and the mapping are only
    included once the draw is closed; while it is open, only the commitment is shown."""
    draw = maybe_lazy_close(draw)
    stage = draw.stage
    cards = list(
        draw.cards.select_related("stage_group", "tournament_team__team", "tournament_team__ghost_team", "player__user")
        .order_by("number")
    )
    closed = draw.status == StageDraw.STATUS_CLOSED
    groups = list(StageGroups.objects.filter(stage=stage).order_by("group_id"))

    out = {
        "draw_id": draw.draw_id,
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "event_id": stage.event.event_id,
        "status": draw.status,
        "opens_at": draw.opens_at.isoformat() if draw.opens_at else None,
        "closes_at": draw.closes_at.isoformat() if draw.closes_at else None,
        "closed_at": draw.closed_at.isoformat() if draw.closed_at else None,
        "commitment": draw.commitment,
        "groups": [{"group_id": g.group_id, "group_name": g.group_name} for g in groups],
        "cards_total": len(cards),
        "cards_taken": sum(1 for c in cards if c.is_taken),
        "cards": [
            {
                "number": c.number,
                "taken": c.is_taken,
                # The group is only revealed on a taken card while the draw is open.
                "group_id": c.stage_group_id if (c.is_taken or closed) else None,
                "group_name": c.stage_group.group_name if (c.is_taken or closed) else None,
                "competitor": _competitor_name(c),
                "via": c.via,
                "picked_at": c.picked_at.isoformat() if c.picked_at else None,
            }
            for c in cards
        ],
        "salt": draw.salt if closed else None,
        "mapping": mapping_of(cards) if closed else None,
        "viewer": None,
    }
    if viewer is not None and getattr(viewer, "user_id", None):
        mine = competitors_user_acts_for(stage, viewer)
        my = []
        for comp in mine:
            card = _card_of(draw, comp)
            my.append({
                "tournament_team_id": comp.tournament_team_id,
                "player_id": comp.player_id,
                "name": comp.tournament_team.display_name if comp.tournament_team_id else (
                    comp.player.user.username if comp.player and comp.player.user else "You"),
                "card_number": card.number if card else None,
                "group_name": card.stage_group.group_name if card else None,
                # "pick" or "auto": the page words a card they turned over differently from one
                # the close dealt them.
                "via": card.via if card else None,
            })
        out["viewer"] = {
            "can_pick": draw.status == StageDraw.STATUS_OPEN and any(m["card_number"] is None for m in my),
            "competitors": my,
            "can_manage": user_may_run_draw(viewer, stage),
        }
    return out
