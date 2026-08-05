# ──────────────────────────────────────────────────────────────────────────────
# Room ID and password, delivered to players on WhatsApp.
#
# WHY THIS MATTERS MORE THAN OTHER MESSAGES: a player who does not receive the room
# password does not play. It is the most time critical message AFC sends, which is why
# every send is recorded (afc_whatsapp.WhatsAppMessage) instead of counted and forgotten.
#
# REPLACES afc_tournament_and_scrims/whatsapp_zernio.py. Zernio was a middleman with no
# direct-to-phone send: it upserted contacts under a throwaway tag, created a broadcast
# for that tag and fired it, and told us nothing about what arrived. AFC now holds its
# own Meta WhatsApp Business account, so we send directly and Meta reports back the
# delivery status per message (afc_whatsapp/webhooks.py).
#
# CALLED BY: afc_tournament_and_scrims.views.broadcast_match_room_details, which is the
# "Send to players" button on each match row in the admin and organizer match editor
# (frontend app/(a)/a/events/[slug]/edit/_components/EditMatchModal.tsx).
#
# TEMPLATE: Meta requires a pre-approved template for anything AFC initiates. Name and
# language come from settings so the owner can point them at whatever Meta approved,
# and `en` and `en_US` are DIFFERENT templates to Meta, so the language is explicit.
# Variables, in order:
#   {{1}} player name   {{2}} event name   {{3}} room id   {{4}} room password   {{5}} map
#
# THE 3D ROOM STEPS ARE A SECOND TEMPLATE, NOT A SIXTH VARIABLE (owner 2026-08-05). A
# template's wording is frozen when Meta approves it, so the joining steps that appear
# under the room details on the event page cannot simply be appended here. The owner
# chose a separate message, sent only when Match.room_is_3d, over widening this one:
# this message is scanned for a room id under time pressure, and burying that in a
# paragraph of instructions is how a player misses it. See WHATSAPP_ROOM_3D_TEMPLATE.
# ──────────────────────────────────────────────────────────────────────────────
import logging

from django.conf import settings

from afc_auth.models import canonical_profile
from afc_whatsapp.tasks import queue_template

logger = logging.getLogger(__name__)


def send_room_details(users, event, match):
    """Send this map's room details to every opted-in player who has a number.

    Returns (queued, skipped): how many messages were handed to the sender, and how many
    recipients were passed over because they have no WhatsApp number on file. The caller
    reports both, because "we messaged 12 of your 40 players" is the useful sentence for
    an organizer, and the old code threw this number away entirely.

    Never raises. A WhatsApp problem must never block the in-app notification and email
    that go out alongside it.
    """
    template = getattr(settings, "WHATSAPP_ROOM_TEMPLATE", "room_details")
    language = getattr(settings, "WHATSAPP_ROOM_TEMPLATE_LANG", "en_US")

    # The follow-up, sent ONLY for a 3D room and only to players who just received the
    # room details above. Blank until the owner has an approved template, and a blank
    # name means the follow-up is skipped entirely rather than failing every send with a
    # template-not-found: the room id is what matters, and it must go out either way.
    help_template = getattr(settings, "WHATSAPP_ROOM_3D_TEMPLATE", "")
    help_language = getattr(
        settings, "WHATSAPP_ROOM_3D_TEMPLATE_LANG", language)
    send_3d_help = bool(help_template) and bool(getattr(match, "room_is_3d", False))

    queued = 0
    skipped = 0

    for user in users or []:
        if user is None:
            continue
        try:
            # canonical_profile, not .get(): UserProfile.user is a plain FK and duplicate
            # rows exist in production, where .get() has raised MultipleObjectsReturned.
            profile = canonical_profile(user)
            number = (getattr(profile, "whatsapp_number", "") or "").strip()
            if not number:
                skipped += 1
                continue

            # queue_template does the rest: it skips an opted-out player, normalises a
            # locally written number using the player's country, writes the log row
            # BEFORE sending, and retries transient failures.
            message_id = queue_template(
                number,
                template,
                language,
                body_params=[
                    getattr(user, "username", "") or "",
                    event.event_name or "",
                    match.room_id or "",
                    match.room_password or "",
                    getattr(match, "match_map", "") or "",
                ],
                user=user,
                event=event,
                match=match,
                context="room_details",
            )
            # An opted-out player returns None and is not an error: they asked not to be
            # messaged, so they count as skipped rather than queued.
            if message_id is None:
                skipped += 1
            else:
                queued += 1
                # Only after the room details actually went out. A player who was opted
                # out or unreachable above must not receive joining instructions for a
                # room whose id they never got, and the counters deliberately do NOT
                # move for this: `queued` answers "how many players were told their room
                # details", and counting the same player twice would make that number a
                # lie on exactly the screen an organizer uses to chase people.
                if send_3d_help:
                    queue_template(
                        number,
                        help_template,
                        help_language,
                        body_params=[
                            getattr(user, "username", "") or "",
                            event.event_name or "",
                        ],
                        user=user,
                        event=event,
                        match=match,
                        context="room_3d_help",
                    )
        except Exception:
            # Best effort per recipient: one bad row must not cost the rest of the group
            # their room password.
            logger.exception("whatsapp room details: failed for user %s", getattr(user, "pk", "?"))
            skipped += 1

    return queued, skipped
