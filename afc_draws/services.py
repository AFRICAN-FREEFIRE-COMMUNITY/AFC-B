"""
afc_draws/services.py - everything the group draw DOES, kept out of the views so the rules are
testable without a request and reusable by Phase 2 (AFC Seeds: rerolls and peeks act on the same
cards through the same helpers).

    deal(stage, user)                 create the draw: one sealed card per active competitor,
                                      the stage's group size respected (group_capacity.py)
    open_draw(draw, closes_at, auto)  start accepting picks, notify every eligible captain/player
                                      in-app and by email
    update_window(draw, ...)          change the close time or the straggler choice while open
    pick(draw, user, number, team_id) turn one card over for the competitor the user acts for
    close_draw(draw, place_rest)      publish the salt; deal the stragglers into what is left
                                      (the organizer's choice, auto_place_at_close by default)
    maybe_lazy_close(draw)            a draw past its close time closes on the next read
    remind(draw, user)                "you have not picked yet", in-app + email, rate limited
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
from afc_tournament_and_scrims.group_capacity import GroupCapacityError, plan_placements
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

    # Even deal, then shuffle the order the cards are numbered in. The split comes from the stage's
    # structure (group_capacity.plan_placements): round robin on an empty stage, never more cards
    # for a group than competitors_per_group, refused with the numbers when the pool does not fit.
    try:
        slots = plan_placements(stage, groups, len(competitors))
    except GroupCapacityError as e:
        raise DrawError(str(e))
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


def open_draw(draw, closes_at, auto_place_at_close=True):
    """Start the window. closes_at must be in the future. At that moment (lazily, on the next read
    after it passes) or when the organizer closes early, the stragglers are dealt the remaining
    cards if auto_place_at_close, otherwise left for the organizer to place by hand."""
    if draw.status != StageDraw.STATUS_DRAFT:
        raise DrawError("Only a draft draw can be opened.", 409)
    if closes_at is None or closes_at <= timezone.now():
        raise DrawError("The close time must be in the future.")
    draw.status = StageDraw.STATUS_OPEN
    draw.opens_at = timezone.now()
    draw.closes_at = closes_at
    draw.auto_place_at_close = bool(auto_place_at_close)
    draw.save(update_fields=["status", "opens_at", "closes_at", "auto_place_at_close", "updated_at"])
    _notify_open(draw)
    return draw


def update_window(draw, closes_at=None, auto_place_at_close=None):
    """Change the close time and/or the straggler choice of an OPEN draw (owner 2026-09-12: "a time
    frame that can be set before it closes"). A new close time must be in the future; the board
    everyone is watching picks it up on its next poll."""
    if draw.status != StageDraw.STATUS_OPEN:
        raise DrawError("Only an open draw can be changed.", 409)
    fields = ["updated_at"]
    if closes_at is not None:
        if closes_at <= timezone.now():
            raise DrawError("The close time must be in the future.")
        draw.closes_at = closes_at
        fields.append("closes_at")
    if auto_place_at_close is not None:
        draw.auto_place_at_close = bool(auto_place_at_close)
        fields.append("auto_place_at_close")
    if len(fields) == 1:
        raise DrawError("Nothing to change.")
    draw.save(update_fields=fields)
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


def _stragglers(draw, cards):
    """Active competitors of the stage with no card and no hand-placed group row."""
    taken_keys = {
        ("team", c.tournament_team_id) if c.tournament_team_id else ("player", c.player_id)
        for c in cards if c.is_taken
    }
    # Someone hand-placed in a group after the draw was dealt is not a straggler either. One query
    # for the whole stage: the board calls this on every poll.
    grouped_keys = {
        ("team", t) if t else ("player", p)
        for t, p in StageGroupCompetitor.objects.filter(stage_group__stage=draw.stage)
        .values_list("tournament_team_id", "player_id")
    }
    return [
        c for c in active_competitors(draw.stage)
        if _competitor_key(c) not in taken_keys and _competitor_key(c) not in grouped_keys
    ]


def close_draw(draw, place_rest=None):
    """Publish the salt. Whoever has not picked is dealt the remaining cards at random when
    place_rest (default: the draw's auto_place_at_close, the organizer's choice at open time) is
    true; otherwise they stay unplaced in the stage pool for the organizer to seed or move by hand,
    and the board lists them. Either way each of them is told."""
    with transaction.atomic():
        draw = StageDraw.objects.select_for_update().get(pk=draw.pk)
        if draw.status == StageDraw.STATUS_CLOSED:
            return draw
        if draw.status != StageDraw.STATUS_OPEN:
            raise DrawError("Only an open draw can be closed.", 409)
        if place_rest is None:
            place_rest = draw.auto_place_at_close

        cards = list(draw.cards.select_related("stage_group").all())
        stragglers = _stragglers(draw, cards)
        free = [c for c in cards if not c.is_taken]
        random.SystemRandom().shuffle(free)

        placed_auto = []
        left_unplaced = []
        for comp in stragglers:
            if not place_rest:
                left_unplaced.append(comp)
                continue
            if free:
                card = free.pop()
            else:
                # A competitor added to the stage after the deal: give them a card in the group
                # holding the fewest cards so the deal stays as even as it can be. A stage whose
                # groups are full (competitors_per_group) has nowhere to put them: left unplaced.
                card = _extra_card(draw, cards)
                if card is None:
                    left_unplaced.append(comp)
                    continue
                cards.append(card)
            _take(card, comp, None, DrawCard.VIA_AUTO)
            placed_auto.append((comp, card))

        draw.status = StageDraw.STATUS_CLOSED
        draw.closed_at = timezone.now()
        draw.auto_place_at_close = bool(place_rest)
        draw.save(update_fields=["status", "closed_at", "auto_place_at_close", "updated_at"])
    _notify_auto_placed(draw, placed_auto)
    _notify_unplaced(draw, left_unplaced)
    return draw


def maybe_lazy_close(draw):
    """A draw whose close time has passed closes on the next read, so no beat task is needed."""
    if draw.status == StageDraw.STATUS_OPEN and draw.closes_at and draw.closes_at <= timezone.now():
        return close_draw(draw)
    return draw


REMIND_EVERY_MINUTES = 10


def remind(draw, user):
    """Tell everyone who has not picked yet, in-app and by email (owner 2026-09-12: "notify
    everyone through notifications or mail"). One reminder per draw per REMIND_EVERY_MINUTES.
    Returns the number of competitors reminded."""
    if draw.status != StageDraw.STATUS_OPEN:
        raise DrawError("Only an open draw has anyone left to remind.", 409)
    now = timezone.now()
    if draw.last_reminder_at and (now - draw.last_reminder_at).total_seconds() < REMIND_EVERY_MINUTES * 60:
        wait = REMIND_EVERY_MINUTES - int((now - draw.last_reminder_at).total_seconds() // 60)
        raise DrawError(f"A reminder went out less than {REMIND_EVERY_MINUTES} minutes ago. Try again in {wait} min.", 429)
    cards = list(draw.cards.all())
    stragglers = _stragglers(draw, cards)
    if not stragglers:
        raise DrawError("Everyone has picked already.")
    event = draw.stage.event
    when = _when(draw.closes_at)
    users = []
    for comp in stragglers:
        users.extend(_people_who_pick_for(comp))
    _notify(
        users, event,
        f"Reminder: pick your group for {event.event_name}",
        f"The group draw for {draw.stage.stage_name} closes {when} and you have not turned a card "
        f"over yet. Open the event page and pick before it closes.",
        "group_draw_reminder",
    )
    _email(
        users, event,
        f"Reminder: pick your group for {event.event_name}",
        f"The group draw for <strong>{draw.stage.stage_name}</strong> closes <strong>{when}</strong> "
        f"and you have not turned a card over yet.",
        f"{'Anyone who has not picked by then is placed automatically.' if draw.auto_place_at_close else 'Anyone who has not picked by then will be placed by the organizer.'}",
    )
    draw.last_reminder_at = now
    draw.save(update_fields=["last_reminder_at", "updated_at"])
    return len(stragglers)


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
    """One more card, in the group holding the fewest cards that still has room under the stage's
    competitors_per_group (group_capacity.plan_placements). None when every group is full."""
    counts = {}
    for c in cards:
        counts[c.stage_group_id] = counts.get(c.stage_group_id, 0) + 1
    groups = list(StageGroups.objects.filter(stage=draw.stage).order_by("group_id"))
    for g in groups:
        counts.setdefault(g.group_id, 0)
    target = plan_placements(draw.stage, groups, 1, counts=counts, strict=False)
    if not target:
        return None
    number = max((c.number for c in cards), default=0) + 1
    return DrawCard.objects.create(draw=draw, number=number, stage_group=target[0])


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


def _when(dt):
    """A close time as people read it, with the zone named, since email has no viewer clock."""
    if not dt:
        return ""
    local = timezone.localtime(dt)
    return local.strftime("%d %b %Y, %H:%M ") + (local.tzname() or "UTC")


def _event_url(event):
    from django.conf import settings
    return f"{settings.FRONTEND_URL}/tournaments/{event.slug or event.event_id}"


def _email(users, event, subject, lead_html, tail_text):
    """The email twin of _notify: one branded message per distinct user with an address, in that
    user's language, sent by afc_draws.tasks.send_draw_emails off the request thread (one SMTP
    session per recipient would hold a 48-team open for a minute). Returns the recipient count."""
    from .tasks import send_draw_emails

    ids = []
    seen = set()
    for u in users:
        if getattr(u, "email", None) and u.user_id not in seen:
            seen.add(u.user_id)
            ids.append(u.user_id)
    if ids:
        send_draw_emails.delay(ids, subject, lead_html, tail_text, _event_url(event))
    return len(ids)


def _notify_open(draw):
    stage = draw.stage
    event = stage.event
    when = _when(draw.closes_at)
    users = []
    for comp in active_competitors(stage):
        users.extend(_people_who_pick_for(comp))
    tail = (
        "Anyone who has not picked by then is placed automatically."
        if draw.auto_place_at_close else
        "Anyone who has not picked by then will be placed by the organizer."
    )
    n = _notify(
        users, event,
        f"Group draw open: {event.event_name}",
        f"Pick your group for {stage.stage_name}. Turn over a card on the event page before {when}; {tail[0].lower() + tail[1:]}",
        "group_draw_open",
    )
    _email(
        users, event,
        f"Group draw open: {event.event_name}",
        f"Pick your group for <strong>{stage.stage_name}</strong>. Turn over a card on the event page "
        f"before <strong>{when}</strong>.",
        tail,
    )
    return n


def _notify_unplaced(draw, comps):
    event = draw.stage.event
    for comp in comps:
        _notify(
            _people_who_pick_for(comp), event,
            f"Group draw closed: {event.event_name}",
            f"The group draw for {draw.stage.stage_name} closed before you picked. The organizer will "
            f"place your team in a group.",
            "group_draw_unplaced",
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

def _straggler_name(comp):
    if comp.tournament_team_id:
        return comp.tournament_team.display_name
    if comp.player_id and comp.player.user:
        return comp.player.user.username
    return "Player"


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
        # The organizer's choice about stragglers, and how the structure sizes the groups.
        "auto_place_at_close": draw.auto_place_at_close,
        "per_group": stage.competitors_per_group,
        "last_reminder_at": draw.last_reminder_at.isoformat() if draw.last_reminder_at else None,
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
        # Who still has no card (open) or was left for the organizer (closed without placing).
        "unpicked": [_straggler_name(c) for c in _stragglers(draw, cards)],
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
