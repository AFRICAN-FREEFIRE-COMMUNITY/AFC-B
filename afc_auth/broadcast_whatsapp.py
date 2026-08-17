# afc_auth/broadcast_whatsapp.py
# ──────────────────────────────────────────────────────────────────────────────
# BROADCASTS, THIRD CHANNEL: WhatsApp (owner 2026-08-05)
#
# A broadcast could already go out as an in-app notification and as an email. This module is the
# WhatsApp leg of the same send, and nothing else: it turns a list of recipients into one approved
# template message each, and it says how many were reached and how many were passed over.
#
# WHY WHATSAPP IS NOT JUST "A THIRD CHECKBOX" (the reason most of this file is guard rails):
#   1. Every message COSTS MONEY. Email and in-app are free; a MARKETING template is billed per
#      conversation, so a careless "send to everyone" is a real invoice.
#   2. Meta THROTTLES a business that sends too much too fast. The account sits in a messaging
#      tier (a ceiling on how many people it may start a conversation with in 24 hours), and
#      blowing through it stops sends for the rest of the window.
#   3. AFC HAS ONE NUMBER, and it is the number that carries ROOM IDS. A marketing blast that
#      people mute, block or report drags that number's quality rating down, and a downgraded
#      number throttles the one message a player cannot play without. The blast is optional;
#      the room ID is not.
# That is what WHATSAPP_BROADCAST_MAX_RECIPIENTS defends, and why an audience over the cap is
# REFUSED with the number named rather than quietly truncated: a truncated send is worse than no
# send, because nobody can tell who got it.
#
# TEMPLATE: `broadcast`, approved by Meta on 2026-08-05, category MARKETING, language `en` (NOT
# `en_US`, which is what the room templates were approved under and is a DIFFERENT template as far
# as Meta is concerned). Variables, in the order the body reads:
#   {{1}} recipient name   {{2}} the message body
# Name and language come from settings so the owner can repoint them, and a BLANK name means "do
# not send" rather than "fail every send", the rule every WhatsApp template name follows.
#
# HOW THIS CONNECTS TO THE REST OF THE SYSTEM:
#   CALLED BY : afc_auth.views.deliver_broadcast, the single delivery chokepoint every broadcast
#               on the site funnels through, when the chosen channels include "whatsapp". It is
#               therefore reachable from every broadcast surface at once (the admin audience
#               builder, the event/stage/group announcements, the single player/team message).
#   REFUSED BY: afc_auth.views_broadcast_audience.broadcast_audience_send, which calls
#               whatsapp_volume_assessment() BEFORE delivering and 400s an over-cap audience, the
#               same shape the email path uses for afc_auth.audience.email_volume_assessment.
#   CALLS     : afc_whatsapp.tasks.queue_template (the one send entry point: it skips an opted-out
#               recipient, normalises a locally written number using the recipient's country,
#               writes the WhatsAppMessage log row BEFORE sending and retries transient failures).
#   READS     : afc_auth.UserProfile.whatsapp_number / whatsapp_opt_in (the number and the consent).
#   MIRRORS   : afc_tournament_and_scrims/whatsapp_room_details.py, which is the same shape for the
#               room-details message: best effort per recipient, returning (queued, skipped).
# ──────────────────────────────────────────────────────────────────────────────
import logging

from django.conf import settings

from afc_whatsapp.tasks import queue_template

from .models import UserProfile

logger = logging.getLogger(__name__)

# Fallback cap, used only when the setting is missing entirely. The live value is
# settings.WHATSAPP_BROADCAST_MAX_RECIPIENTS; see that block in afc/settings.py.
DEFAULT_MAX_RECIPIENTS = 500

# Longest message body we will put in variable {{2}}. Meta rejects a template whose rendered body
# runs past roughly 1,024 characters, and it rejects the WHOLE message rather than trimming it, so
# a long announcement would otherwise reach nobody on WhatsApp. The template's own fixed wording
# counts towards that limit too, hence the headroom. The full text is never lost: the in-app
# notification and the email carry it verbatim, and WhatsApp is the nudge that points at them.
MAX_BODY_CHARS = 900


# ──────────────────────────────────────────────────────────────────────────────
# Is the channel switched on at all?
# ──────────────────────────────────────────────────────────────────────────────
def whatsapp_broadcast_configured():
    """True when this deployment can actually send a WhatsApp broadcast.

    WHY THIS IS ASKED OUT LOUD (owner 2026-08-05: "add a disclaimer that the whatsapp is not
    available yet, but will be in due time soon"). WHATSAPP_BROADCAST_TEMPLATE defaults to EMPTY,
    deliberately, so that deploying the WhatsApp work could never start messaging real players
    before somebody chose to. Until it is set, send_broadcast_whatsapp skips every recipient and
    returns quietly - correct behaviour, but from the composer it looks identical to a channel
    that worked.

    So the composer asks, and shows a "not switched on yet" notice instead of a channel that does
    nothing. Deriving it from the setting rather than hardcoding a disclaimer means the notice
    disappears by itself the moment the env value is set on the server - there is no second commit
    to remember, and no chance of the UI claiming "coming soon" about something already live.
    """
    return bool(getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE", ""))


# ──────────────────────────────────────────────────────────────────────────────
# The cap
# ──────────────────────────────────────────────────────────────────────────────
def whatsapp_max_recipients():
    """How many WhatsApp messages ONE broadcast may send. Read from settings on every call, not
    frozen at import, so the owner can tune it with an env var and a restart (and so tests can
    override_settings it)."""
    try:
        return max(0, int(getattr(settings, "WHATSAPP_BROADCAST_MAX_RECIPIENTS",
                                  DEFAULT_MAX_RECIPIENTS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RECIPIENTS


def whatsapp_volume_assessment(whatsapp_recipient_count):
    """Judge whether WhatsApp-ing `whatsapp_recipient_count` people is allowed, and say so in plain
    words. The WhatsApp twin of afc_auth.audience.email_volume_assessment, same shape, same job.

    Returns a dict the API hands straight to the composer:
      {level, whatsapp_recipient_count, max_recipients, blocked, requires_confirmation, message}

    Levels:
      "ok"      - within the cap. Send it.
      "blocked" - above the cap: the send endpoint REFUSES the WhatsApp channel (400) and points
                  the admin at in-app notification instead. Refused, NOT truncated: a broadcast
                  that reached the first 500 of 3,000 people is impossible to reason about
                  afterwards, and the 2,500 who heard nothing look identical to the 500 who did.

    There is deliberately NO middle "confirm this large send" level, unlike email. The send
    endpoint already makes the admin confirm the exact recipient count for EVERY send
    (confirmed_count), so a second confirmation would only be a second click on a number they have
    already read."""
    count = max(0, int(whatsapp_recipient_count or 0))
    cap = whatsapp_max_recipients()

    if count > cap:
        level = "blocked"
        message = (
            f"{count} WhatsApp recipients is above the {cap}-per-broadcast limit AFC allows on its "
            f"WhatsApp number. Every WhatsApp message is paid for, and a blast this size risks the "
            f"number AFC also sends room IDs from. Send an in-app notification instead, or narrow "
            f"the audience."
        )
    else:
        level = "ok"
        message = (
            f"{count} WhatsApp recipients, within the limit of {cap} per broadcast."
            if count else "No recipients have a WhatsApp number."
        )

    return {
        "level": level,
        "whatsapp_recipient_count": count,
        "max_recipients": cap,
        "blocked": level == "blocked",
        # Always False. Present so the composer can read the two volume verdicts (email and
        # WhatsApp) with one piece of code instead of special-casing this one.
        "requires_confirmation": False,
        "message": message,
    }


def whatsapp_recipient_count(queryset):
    """How many users in this audience can actually be reached on WhatsApp: a number on file AND
    consent given. This is the number the cap is judged on, exactly as the email cap is judged on
    how many have an email address, not on the size of the audience. An audience of 3,000 of whom
    90 have WhatsApp costs 90 messages and must not be refused.

    Passed as a SUBQUERY (a .values() queryset), following the rule in afc_auth/audience.py: no id
    list is ever pulled into Python, so previewing "everyone" stays one SQL statement.

    Slight, deliberate imprecision: this counts a user with ANY profile row carrying a number,
    while the send resolves the CANONICAL profile (lowest profile_id, see canonical_profile). The
    two differ only for the handful of accounts with duplicate profile rows where the numbers
    disagree, and erring towards counting them keeps the preview from under-promising."""
    reachable = (
        UserProfile.objects.filter(whatsapp_opt_in=True)
        .exclude(whatsapp_number="")
        .values("user_id")
    )
    return queryset.filter(user_id__in=reachable).count()


# ──────────────────────────────────────────────────────────────────────────────
# The send
# ──────────────────────────────────────────────────────────────────────────────
def _param(value):
    """One template variable, never empty.

    Same guard as afc_tournament_and_scrims/whatsapp_room_details.py, for the same two reasons:
    Meta REJECTS a send whose parameter is an empty string (the whole message, not the one
    variable), and it refuses a parameter containing a newline, which a pasted multi-line broadcast
    body always has. A dash is the smallest thing that reads as "not set"."""
    text = " ".join(str(value or "").split())
    return text or "-"


def _body_text(title, message):
    """Variable {{2}}: what the recipient actually reads.

    The template has two variables, so the broadcast TITLE has nowhere of its own to go, and a
    title is usually the part that carries the point ("Registration closes tonight"). Dropping it
    would send the detail without the headline, so it is folded into the front of the body. The
    in-app notification and the email keep title and body separate, as they always have.

    Trimmed to MAX_BODY_CHARS because Meta rejects an over-long body outright; see that constant."""
    title = (title or "").strip()
    body = (message or "").strip()
    if title:
        # Colon, not a dash: this renders as one running line in a chat bubble once _param has
        # collapsed the newlines, and "Headline: detail" is the shape that reads correctly there.
        body = f"{title}: {body}" if body else title
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "..."
    return body


def _reachable(users):
    """[(user, number)] for the recipients we can actually message, resolved in ONE query.

    The number and the consent flag both live on UserProfile, and looking them up per recipient
    would be one query per person on a send that can span hundreds. Ordering by profile_id and
    keeping the FIRST row per user reproduces canonical_profile's rule (the lowest profile_id is
    THE row, because duplicate UserProfile rows exist in production and readers and writers must
    agree on which one they mean).

    Opted-out users are dropped here as well as inside queue_template. That is not belt-and-braces
    for its own sake: it keeps the skipped count honest without depending on a return value, and it
    saves a per-recipient profile query on a send that may cover the whole site."""
    users = [u for u in users if u is not None]
    if not users:
        return []

    profiles = {}
    for user_id, number, opt_in in (
        UserProfile.objects.filter(user_id__in=[u.pk for u in users])
        .order_by("profile_id")
        .values_list("user_id", "whatsapp_number", "whatsapp_opt_in")
    ):
        profiles.setdefault(user_id, ((number or "").strip(), opt_in))

    out = []
    for user in users:
        number, opt_in = profiles.get(user.pk, ("", True))
        if number and opt_in:
            out.append((user, number))
    return out


def send_broadcast_whatsapp(recipients, title, message):
    """Send the broadcast to every recipient who has a WhatsApp number and has not opted out.

    Returns (queued, skipped): how many messages were handed to the sender, and how many recipients
    were passed over (no number on file, opted out, or a bad row). The caller reports both, because
    "we messaged 1,200 of your 3,000 players" is the useful sentence, and it is the only way an
    admin can see that this channel reaches a fraction of the audience the other two do.

    Never raises, and never sends a partial broadcast:
      - a BLANK template name means the channel is not configured on this deployment, so nothing is
        sent and nothing is logged as an error;
      - an audience over the cap is REFUSED WHOLE (0 queued), because half a broadcast is worse
        than none. The send endpoint stops this first with a message naming the number; this is the
        backstop for every other caller of deliver_broadcast, which is most of the site;
      - past those two gates it is best effort PER RECIPIENT, so one bad row cannot cost the rest
        of the group their message.
    """
    template = getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE", "")
    language = getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE_LANG", "en")

    # Exposed so the composer can say so up front rather than letting somebody tick a channel that
    # will quietly do nothing. See whatsapp_broadcast_configured() below.

    recipients = [r for r in recipients if r is not None]
    if not template:
        # Not configured here. Silence is the correct outcome: the in-app notification and the
        # email have already gone out, and a template Meta never approved would fail per recipient.
        logger.info("whatsapp broadcast: no template configured, skipping %s recipient(s).",
                    len(recipients))
        return 0, len(recipients)

    targets = _reachable(recipients)
    skipped = len(recipients) - len(targets)

    cap = whatsapp_max_recipients()
    if len(targets) > cap:
        # Refuse the whole thing. See the module header: this protects the number that carries
        # room IDs, and a silently truncated blast is unauditable.
        logger.error(
            "whatsapp broadcast: REFUSED, %s reachable recipients is above the cap of %s.",
            len(targets), cap,
        )
        return 0, len(recipients)

    # Resolved per recipient, exactly as the push and email legs do, so {{time:...}} renders in
    # each person's own timezone and {{money:...}} in their own currency. Imported here rather than
    # at module scope to keep this module importable from afc_auth.views without widening an
    # already-heavy import graph.
    from .broadcast_tokens import resolve_broadcast_tokens

    queued = 0
    for user, number in targets:
        try:
            # The return value is deliberately NOT read. queue_template hands back a
            # WhatsAppMessage id only when the send ran INLINE (WHATSAPP_SYNC, which defaults to
            # DEBUG); in production it hands the send to the whatsapp worker and returns None for a
            # perfectly good message, so treating None as a failure would report every production
            # broadcast as nought delivered. The other None case, an opted-out recipient, is
            # already excluded by _reachable() above, which is why the count stays honest.
            queue_template(
                number,
                template,
                language,
                body_params=[
                    _param(getattr(user, "username", "")),
                    _param(_body_text(title, resolve_broadcast_tokens(message, user))),
                ],
                user=user,
                # queue_template resolves the country itself from the user (ip_country, then
                # country), which is what turns a locally written "0805..." into E.164.
                context="broadcast",
            )
            queued += 1
        except Exception:
            # Best effort per recipient: one bad row must not cost the rest of the audience their
            # message. The in-app notification has already gone out to everybody regardless.
            logger.exception("whatsapp broadcast: failed for user %s", getattr(user, "pk", "?"))
            skipped += 1

    return queued, skipped
