"""
Event requirement waivers: what may be excused, and who has been excused.

THE VOCABULARY. A waiver names REFUSAL CODES, the same codes register_for_event puts in a 403 body,
so what an admin ticks and what the registration endpoint refuses are the same words. Three of the
codes below (roster_size, country_restricted, capacity_full) did not exist before this work: those
refusals returned a bare {"message": ...}. They have one now. The frontend already reads `code` when
present and falls back to `message` (8 branches then toast.error(message) in EventDetailsWrapper),
so adding them breaks nothing.

WHAT CANNOT BE WAIVED, and why the list is written out rather than implied: the owner chose the
widest useful scope, everything except bans and payment. Bans are a safety decision an admin should
have to lift properly, not step around for one event. Payment is money. The duplicate-registration
guard is a data-integrity lock rather than a rule about a team, so it is not waivable either.

TWO CONSEQUENCES OF THAT WIDTH, recorded here because they are downstream-visible: waiving
capacity_full puts an extra competitor into an event whose stages and groups were sized for a fixed
count, and waiving roster_size admits a team that cannot field a full squad. Both are legitimate
admin calls. Both are why `reason` is mandatory.

CONSUMED BY: afc_tournament_and_scrims/views.py (register_for_event and add_teams_to_event) and
waiver_views.py (the admin endpoints).
"""
from django.db import transaction
from django.utils import timezone

from .models import EventRequirementWaiver

#: Everything an admin may excuse.
WAIVABLE_CODES = frozenset({
    "team_logo_required",               # Event.require_team_logo
    "registration_requirements_unmet",  # the per-player asset set: uid, whatsapp, images, links
    "letter_avatars_required",          # Event.min_letter_avatars
    "discord_required",                 # connected Discord + server membership
    "roster_size",                      # min/max roster size
    "country_restricted",               # per-country registration rules
    "capacity_full",                    # max_teams_or_players, including waitlist overflow
    "sponsor_submission_invalid",       # sponsored-event engagement requirement
})

#: Named explicitly rather than left to "not in WAIVABLE_CODES", so a reader can see the refusal is
#: deliberate and a future code cannot become waivable merely by being forgotten.
NEVER_WAIVABLE = frozenset({
    "team_banned",
    "player_banned",
    "payment_required",
    "paid_terms_required",
    "team_already_registered",
    "team_disqualified",
    "has_results",
})


def clean_codes(raw):
    """Validate a list of codes. Returns a de-duplicated list, or raises ValueError naming the
    offender. Validating on write means an impossible waiver is refused at the door rather than
    sitting in the database excusing nothing."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one requirement must be selected")
    cleaned = []
    for item in raw:
        code = str(item or "").strip()
        if code in NEVER_WAIVABLE:
            raise ValueError(f"{code} can never be waived")
        if code not in WAIVABLE_CODES:
            raise ValueError(f"unknown requirement: {code}")
        if code not in cleaned:
            cleaned.append(code)
    return cleaned


def waived_codes(event, team=None, user=None):
    """The refusal codes excused for this competitor in this event. Empty set is the normal case.

    Costs one indexed query (event, active). Called at each waivable gate in register_for_event, so
    it returns a set rather than a queryset: every caller does a membership test.
    """
    if team is None and user is None:
        return set()
    query = EventRequirementWaiver.objects.filter(event=event, active=True)
    query = query.filter(team=team) if team is not None else query.filter(user=user)
    row = query.first()
    return set(row.waived_codes or []) if row else set()


@transaction.atomic
def grant(event, actor, reason, codes, team=None, user=None):
    """Create or replace the active waiver for one competitor.

    Granting twice EDITS the existing row rather than stacking a second one, which is what the
    unique constraint promises anyway, and it means a reader never has to union several rows to
    learn what a competitor was excused from.
    """
    if team is None and user is None:
        raise ValueError("a waiver must name a team or a user")
    if team is not None and user is not None:
        raise ValueError("a waiver names a team or a user, not both")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required")

    cleaned = clean_codes(codes)
    row, _created = EventRequirementWaiver.objects.update_or_create(
        event=event,
        team=team,
        user=user,
        active=True,
        defaults={"waived_codes": cleaned, "reason": reason, "created_by": actor},
    )
    return row


def revoke(waiver, actor):
    """Retire a waiver without deleting it, so the record of what was excused outlives the event.

    active=None is what frees the unique slot; see the model's comment for why a nullable marker
    rather than a partial index.
    """
    waiver.active = None
    waiver.revoked_at = timezone.now()
    waiver.revoked_by = actor
    waiver.save(update_fields=["active", "revoked_at", "revoked_by"])


def serialize(waiver):
    """The shape the admin list and the player-facing panel both read."""
    return {
        "waiver_id": waiver.waiver_id,
        "event_id": waiver.event_id,
        "team_id": waiver.team_id,
        "user_id": waiver.user_id,
        "waived_codes": list(waiver.waived_codes or []),
        "reason": waiver.reason,
        "created_by": getattr(waiver.created_by, "username", ""),
        "created_at": waiver.created_at.isoformat() if waiver.created_at else None,
    }
