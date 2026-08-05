# ── afc_tournament_and_scrims/event_wording.py ───────────────────────────────────────────────
# What to CALL an event when writing a sentence to a player.
#
# WHY THIS EXISTS (owner backlog item 32: "a finished scrim should say scrims completed, not
# tournament completed"). AFC runs two kinds of competition, `Event.competition_type` is exactly
# "tournament" or "scrims" (models.py COMPETITION_TYPE_CHOICES), and every message that hardcodes
# the word "tournament" tells half of AFC's competitors they were in something they were not.
#
# It went unnoticed for a month because scrims only started auto-completing on 2026-07-06, so the
# completion notice was the first message most scrims players ever got. Fixing that one literal
# fixed one message; the disqualification notices had the same bug and were found by grep. A
# helper exists so the NEXT message to name an event cannot get it wrong, and so somebody
# grepping for the mistake finds one place rather than a habit.
#
# CALLED BY: afc_tournament_and_scrims/views.py, the completion notice in `complete_event` and
# both disqualification notices. Anything new that writes an event's KIND into a sentence should
# come here rather than repeat the conditional.
#
# ENGLISH ON PURPOSE. In-app notification rows are stored in English and localized at READ time
# by get_notifications (afc_auth/views.py, translate-on-read via afc_auth.translation), so the
# correct English literal carries the fix into French and Portuguese with no catalog change.


def is_scrims(event):
    """True when this event is a scrims block rather than a tournament."""
    return getattr(event, "competition_type", "") == "scrims"


def event_noun(event, *, capitalized=True):
    """The word for this event's KIND, for use inside a sentence.

    `capitalized` is what a notification TITLE wants ("Scrims Complete: ..."), and False is what
    the body of a sentence wants ("the scrims 'X' has concluded"). Two call sites already needed
    both, which is why it is a flag rather than two functions that could drift.
    """
    if is_scrims(event):
        return "Scrims" if capitalized else "scrims"
    return "Tournament" if capitalized else "tournament"
