"""
afc_fantasy.permissions - WHO may create and manage a fantasy league.

The same composition afc_polls.permissions settled on, and for the same reasons: head admins, plus
event organizers for leagues on their OWN events. Reuse the gate the event admin pages already use;
do not invent a new one.

    afc_tournament_and_scrims.views._is_event_admin(user)
    afc_organizers.permissions.org_can_event(user, "can_edit_events", event)

A fantasy league is always attached to an event, so unlike a poll there is no site-wide case to
decide: the event decides. That means this inherits, for free and with no new code, everything
org_can_event already enforces - AFC oversight over an organizer's league, org owners implicitly
holding it, sub-organizers holding only what their row granted, co-owned events reachable by both
organizations, and AFC-run events staying admin-only.

WHAT THIS DOES NOT DECIDE: who may ENTER. Authorship and audience are separate questions, and
entry is governed by FantasyLeague.eligibility_spec through afc_auth.audience. Attaching a league
to an event is what makes it THEIRS, not what makes it narrow.

CONSUMED BY: every endpoint in afc_fantasy.admin_views, and the public league detail endpoint,
which uses it to decide whether a draft league is visible at all.
"""
from afc_organizers.permissions import org_can_event
from afc_tournament_and_scrims.views import _is_event_admin


def is_fantasy_admin(user):
    """AFC staff who may create a fantasy league on any event."""
    return bool(user) and _is_event_admin(user)


def can_manage_league(user, league):
    """Whether `user` may edit, open, lock, settle or delete `league`.

    Also the gate for CREATING one: build the unsaved FantasyLeague (or a stand-in carrying the
    intended `event`) and ask this, so creation and editing can never disagree about who is allowed.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_fantasy_admin(user):
        return True
    event = getattr(league, "event", None)
    if event is None:
        return False
    return org_can_event(user, "can_edit_events", event)
