"""
afc_tournament_and_scrims.discord_reminders - Discord reminders an organizer sets on their event.

WHAT THE OWNER ASKED FOR (2026-09-14, inbox #22)
    "We can have discord automated reminders, organizers should be able to set it on the website,
    where they pick frequency of how the reminders send (this option is for discord only)."

WHAT A REMINDER IS
    A Discord DM from the AFC bot to every player who is on an accepted, non-waitlisted roster
    of the event (solo registrants included) and has Discord connected on the site
    (User.discord_id). It names the event and how long until it starts, in the recipient's own
    language, with the organizer's optional note. Discord only: no email, no push, by the owner's
    words. People with DMs closed to non-friends are counted as not delivered; that is Discord,
    not a fault.

THE FREQUENCY
    The organizer picks ONE cadence; each cadence is a list of "hours before the start" at which
    a DM goes out (FREQUENCIES). Every send is recorded as an EventDiscordReminder row keyed on
    (event, offset), so a sweep can never send the same reminder twice, and the organizer sees
    the history. A reminder whose moment passed more than LATE_GRACE ago when the sweep reaches
    it (the cadence was set late, or the worker was down) is recorded as skipped, not sent: a
    "starts in 3 days" DM sent 2 hours before the start is worse than none.

HOW IT CONNECTS
    Settings live on Event (discord_reminder_frequency, discord_reminder_note), declared once in
    event_contract.py so the create/edit forms and the readers carry them; the Actions tab saves
    them through views_discord_reminders.py (GET/POST events/<id>/discord-reminders/), which also
    returns the history. The sweep (tasks.discord_reminder_sweep, Celery beat every 10 minutes)
    calls send_due_reminders below. DMs go through afc_support.notify.send_discord_dm, the same
    two-call Discord client the support desk uses. The start instant comes from
    views_checkin._event_start_dt (the EVENT's timezone, never the server's).
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# cadence key -> hours before the start at which a DM goes out (descending)
FREQUENCIES = {
    "off": [],
    "once_24h": [24],
    "daily_3d": [72, 48, 24],
    "every_12h_2d": [48, 36, 24, 12],
    "every_6h_1d": [24, 18, 12, 6],
}
FREQUENCY_CHOICES = [
    ("off", "Off"),
    ("once_24h", "Once, 24 hours before"),
    ("daily_3d", "Every day for the last 3 days"),
    ("every_12h_2d", "Every 12 hours for the last 2 days"),
    ("every_6h_1d", "Every 6 hours on the last day"),
]
NOTE_MAX = 200
# A reminder is sent only within this long after its moment; older ones are recorded as skipped.
LATE_GRACE = timedelta(hours=1)
# The sweep looks this far ahead for events with a cadence, no further (cheap query).
LOOKAHEAD = timedelta(hours=96)

# Hand-written en/fr/pt (owner rule: every user-facing string in all three). {event}, {when}
# and {url} are filled in; the note rides on its own line when the organizer wrote one.
_COPY = {
    "en": {
        "line": "Reminder from AFC: {event} starts {when}.",
        "hours": "in {n} hours",
        "hour": "in 1 hour",
        "days": "in {n} days",
        "day": "in 1 day",
        "note": "From the organizer: {note}",
        "url": "Event page: {url}",
    },
    "fr": {
        "line": "Rappel d'AFC : {event} commence {when}.",
        "hours": "dans {n} heures",
        "hour": "dans 1 heure",
        "days": "dans {n} jours",
        "day": "dans 1 jour",
        "note": "De l'organisateur : {note}",
        "url": "Page de l'événement : {url}",
    },
    "pt": {
        "line": "Lembrete da AFC: {event} começa {when}.",
        "hours": "daqui a {n} horas",
        "hour": "daqui a 1 hora",
        "days": "daqui a {n} dias",
        "day": "daqui a 1 dia",
        "note": "Do organizador: {note}",
        "url": "Página do evento: {url}",
    },
}


def clean_frequency(raw):
    """The contract cleaner: an unknown cadence is refused, None/blank means off."""
    value = (raw or "off")
    if not isinstance(value, str) or value not in FREQUENCIES:
        raise ValueError(f"Unknown reminder frequency: {value!r}.")
    return value


def clean_note(raw):
    value = (raw or "").strip() if isinstance(raw, str) else ""
    if len(value) > NOTE_MAX:
        raise ValueError(f"The reminder note is limited to {NOTE_MAX} characters.")
    return value


def offsets_for(event):
    return list(FREQUENCIES.get(event.discord_reminder_frequency or "off", []))


def start_instant(event):
    from .views_checkin import _event_start_dt
    return _event_start_dt(event)


def _when(offset_hours, lang):
    c = _COPY.get(lang, _COPY["en"])
    if offset_hours % 24 == 0:
        days = offset_hours // 24
        return c["day"] if days == 1 else c["days"].format(n=days)
    return c["hour"] if offset_hours == 1 else c["hours"].format(n=offset_hours)


def compose(event, offset_hours, lang, url):
    c = _COPY.get(lang, _COPY["en"])
    lines = [c["line"].format(event=event.event_name, when=_when(offset_hours, lang))]
    note = (event.discord_reminder_note or "").strip()
    if note:
        lines.append(c["note"].format(note=note))
    lines.append(c["url"].format(url=url))
    return "\n".join(lines)


def recipients_for(event):
    """The users to DM: every member of an accepted, non-waitlisted roster, plus solo
    registrants, each once, only those with Discord connected. Mirrors the roster lock's idea
    of "playing" (afc_team.views._active_event_roster_blockers): waitlisted rows hold nobody."""
    from afc_auth.models import User
    from .models import RegisteredCompetitors, TournamentTeamMember

    ids = set(
        TournamentTeamMember.objects.filter(
            tournament_team__event=event, status="active",
            tournament_team__status="active", tournament_team__is_waitlisted=False,
        ).values_list("user_id", flat=True)
    )
    ids |= set(
        RegisteredCompetitors.objects.filter(
            event=event, user__isnull=False, is_waitlisted=False,
            status__in=("registered", "approved"),
        ).values_list("user_id", flat=True)
    )
    return list(
        User.objects.filter(pk__in=ids).exclude(discord_id__isnull=True).exclude(discord_id="")
        .exclude(status="deleted").order_by("user_id")
    )


def event_url(event):
    from afc_auth.views import SITE_URL
    slug = getattr(event, "slug", None)
    return f"{SITE_URL}/tournaments/{slug or event.event_id}"


def send_reminder(event, offset_hours, *, send=None):
    """Send one reminder now and record it. Returns the EventDiscordReminder row."""
    from afc_support.notify import send_discord_dm
    from .models import EventDiscordReminder

    send = send or send_discord_dm
    url = event_url(event)
    users = recipients_for(event)
    delivered = 0
    for user in users:
        lang = (user.language or "en")[:2]
        if send(user.discord_id, compose(event, offset_hours, lang, url)):
            delivered += 1
    row, _ = EventDiscordReminder.objects.update_or_create(
        event=event, offset_hours=offset_hours,
        defaults={"sent_at": timezone.now(), "recipients": len(users), "delivered": delivered,
                  "skipped": False, "reason": ""},
    )
    return row


def send_due_reminders(now=None, *, send=None):
    """The sweep. For every upcoming event with a cadence, send each offset whose moment has
    come and is not yet recorded; record as skipped the ones whose moment is older than
    LATE_GRACE. Returns {"sent": n, "skipped": n}. Idempotent: rows keyed on (event, offset)."""
    from .models import Event, EventDiscordReminder

    now = now or timezone.now()
    summary = {"sent": 0, "skipped": 0}
    candidates = (
        Event.objects.exclude(discord_reminder_frequency__in=["", "off"])
        .exclude(discord_reminder_frequency__isnull=True)
        .filter(is_draft=False, start_date__gte=(now - timedelta(days=1)).date(),
                start_date__lte=(now + LOOKAHEAD).date())
        .exclude(event_status__in=["cancelled", "completed"])
    )
    for event in candidates:
        start = start_instant(event)
        if start is None or start <= now:
            continue
        done = set(EventDiscordReminder.objects.filter(event=event).values_list("offset_hours", flat=True))
        for offset in offsets_for(event):
            if offset in done:
                continue
            due_at = start - timedelta(hours=offset)
            if due_at > now:
                continue
            if now - due_at > LATE_GRACE:
                EventDiscordReminder.objects.update_or_create(
                    event=event, offset_hours=offset,
                    defaults={"sent_at": None, "recipients": 0, "delivered": 0, "skipped": True,
                              "reason": "its moment had passed when the reminders were set or the sweep ran"},
                )
                summary["skipped"] += 1
                continue
            try:
                with transaction.atomic():
                    send_reminder(event, offset, send=send)
                summary["sent"] += 1
            except Exception:
                logger.exception("discord_reminders: event %s offset %sh failed", event.event_id, offset)
    return summary


def serialize_settings(event):
    """What the Actions tab reads: the cadence, the note, the plan and the history (R24, one shape)."""
    from .models import EventDiscordReminder

    start = start_instant(event)
    rows = {r.offset_hours: r for r in EventDiscordReminder.objects.filter(event=event)}
    plan = []
    for offset in offsets_for(event):
        row = rows.get(offset)
        plan.append({
            "offset_hours": offset,
            "due_at": (start - timedelta(hours=offset)).isoformat() if start else None,
            "state": "skipped" if (row and row.skipped) else "sent" if (row and row.sent_at) else "planned",
            "sent_at": row.sent_at.isoformat() if row and row.sent_at else None,
            "recipients": row.recipients if row else None,
            "delivered": row.delivered if row else None,
            "reason": row.reason if row else "",
        })
    return {
        "frequency": event.discord_reminder_frequency or "off",
        "note": event.discord_reminder_note or "",
        "frequencies": [{"key": k, "label": label, "offsets": FREQUENCIES[k]} for k, label in FREQUENCY_CHOICES],
        "note_max": NOTE_MAX,
        "start_at": start.isoformat() if start else None,
        "recipients_now": len(recipients_for(event)),
        "plan": plan,
    }
