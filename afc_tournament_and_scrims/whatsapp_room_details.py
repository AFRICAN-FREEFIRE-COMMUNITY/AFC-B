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
#   {{1}} player name  {{2}} event name  {{3}} map  {{4}} room name  {{5}} room id
#   {{6}} room key (the room password; the template calls it a key, see below)
#
# THE ORDER IS THE ORDER THE BODY READS, ascending, because Meta shows the variables in the
# template editor in numeric order and a reviewer comparing them to the body should not have to
# jump around. Renumbered on 2026-08-05 when room name was added: nothing was approved under the
# old five-variable order, so this cost nothing. If a template is EVER approved with a different
# order, this list is the thing that has to change with it, not the body of the message.
#
# THE TEMPLATE SAYS "ROOM KEY", NOT "PASSWORD", and that is not cosmetic. Meta's classifier read
# "Password:" next to a short value as a one-time login code and moved the whole template to the
# AUTHENTICATION category, which has a locked format that permits neither six variables nor a URL
# button. See docs/whatsapp-templates-to-submit.md.
#
# THE 3D ROOM STEPS DO NOT GO OUT ON WHATSAPP (owner 2026-08-17). They used to be sent as a
# SECOND template right after this one whenever Match.room_is_3d was on, which doubled the WhatsApp
# bill for every player in a 3D room: Meta charges per template message, so a 40-player group cost
# 80 sends instead of 40, every map.
#
# It bought nothing. The same joining steps already reach the player three other ways, all free:
#   * the EVENT PAGE renders them, translated, from frontend/messages/*/tournaments.json
#     (components/Room3dJoinHelp.tsx);
#   * the in-app NOTIFICATION and the EMAIL both carry them, appended by
#     afc_tournament_and_scrims.room_join_help.append_3d_help, which goes out in the same action
#     that sends this message.
# So WhatsApp carries the one thing it is uniquely good at - the room id and password, the message a
# player cannot play without - and the instructions travel on the channels that cost nothing.
# ──────────────────────────────────────────────────────────────────────────────
import logging

from django.conf import settings

from afc_auth.models import canonical_profile
from afc_whatsapp.tasks import queue_template

logger = logging.getLogger(__name__)


def _param(value):
    """One template variable, never empty.

    Meta REJECTS a template send whose parameter is an empty string, and it rejects the WHOLE
    message rather than the one variable. Room name is optional on a Match and is routinely blank,
    so without this a map with no room name would fail to send its room ID to anybody. A dash is
    the smallest thing that reads as "not set" in a chat message.

    It also collapses newlines: Meta refuses a parameter containing one, and an organizer pasting
    a room name out of the game client can easily bring one along.
    """
    text = " ".join(str(value or "").split())
    return text or "-"


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

    # EXACTLY ONE MESSAGE PER PLAYER, whether or not the map is a 3D room. See the note at the top
    # of this file: the 3D joining steps used to be a second billed send, and they now travel with
    # the notification, the email and the event page instead.

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
                    _param(getattr(user, "username", "")),
                    _param(event.event_name),
                    _param(getattr(match, "match_map", "")),
                    _param(match.room_name),
                    _param(match.room_id),
                    _param(match.room_password),
                ],
                # The "Visit website" button's dynamic tail. The approved template holds the
                # base URL, frozen at approval, and Meta appends only this: the event's slug
                # onto https://africanfreefirecommunity.com/tournaments/. Meta allows a variable
                # only at the END of a URL, which is exactly what stops an approved template
                # being repointed at another domain later.
                #
                # Harmless when the template has no URL button: Meta ignores a button component
                # the template does not declare, so this needs no second setting to guard it.
                url_button_suffix=getattr(event, "slug", "") or "",
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
        except Exception:
            # Best effort per recipient: one bad row must not cost the rest of the group
            # their room password.
            logger.exception("whatsapp room details: failed for user %s", getattr(user, "pk", "?"))
            skipped += 1

    return queued, skipped
