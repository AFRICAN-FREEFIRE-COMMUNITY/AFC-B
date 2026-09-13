"""
afc_tournament_and_scrims/event_names.py - how a lock says WHICH event is doing the locking.

Owner 2026-09-13: "it should also tell people what event they are locked into." Every lock in AFC
used to say only "an active tournament", and a player could not act on that. The owner spent a day
chasing a CANCELLED event that was locking nobody for exactly that reason.

Kept in its own module with NO imports so both sides can use the same wording without an import
cycle: afc_team.views (leaving a team, being kicked) and afc_auth.views (the in-game name + UID
identity lock) both import it.
"""


def name_events(events) -> str:
    """'the event "X"' / 'the events "X" and "Y"' / 'the events "X", "Y" and 2 more'.

    Capped at three names: a player on a dozen rosters should get a sentence, not a list. `events`
    is any iterable of objects with an `event_name`.
    """
    names = [f'"{getattr(e, "event_name", "") or "an event"}"' for e in events]
    if not names:
        return "an event"
    if len(names) == 1:
        return f"the event {names[0]}"
    if len(names) == 2:
        return f"the events {names[0]} and {names[1]}"
    if len(names) == 3:
        return f"the events {names[0]}, {names[1]} and {names[2]}"
    return f"the events {names[0]}, {names[1]} and {len(names) - 2} more"


def event_refs(events):
    """The same events as the small dicts a response body carries: [{event_id, event_name, slug}].
    The frontend links them, so a player can open the event that is holding them."""
    return [
        {"event_id": e.event_id, "event_name": e.event_name, "slug": e.slug}
        for e in events
    ]
