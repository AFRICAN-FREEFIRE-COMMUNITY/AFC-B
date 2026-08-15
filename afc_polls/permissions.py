"""
afc_polls.permissions - WHO may create and manage a poll.

Decision 7 (polls spec 1.11): head admins, plus event organizers for polls scoped to their OWN
events. Reuse the gate the event admin pages already use. Do not invent a new one.

    afc_tournament_and_scrims.views._is_event_admin(user)
    afc_organizers.permissions.org_can_event(user, "can_edit_events", event)

`can_manage_poll` is the composition of those two and nothing more:

    poll.event is None  ->  _is_event_admin(user) only. No organizer may create a site-wide poll.
    poll.event is set   ->  _is_event_admin(user) OR org_can_event(user, "can_edit_events", event)

FOUR THINGS THIS INHERITS FOR FREE, all already true of every other event surface:
  1. AFC oversight. org_can returns True for head_admin / organizer_admin before it looks at
     membership, so AFC staff can always reach an organizer's poll. Polls must not be the one
     place that stops holding.
  2. Owners and sub-organizers. An org owner implicitly holds every permission; a sub-organizer
     holds only what was granted on their OrganizationMember row, so an organization can let one
     person run polls without letting them run everything.
  3. Co-owned events. org_can_event already walks accepted EventCoOrganizer rows, so a poll on a
     co-owned event is manageable by both organizations with no new code.
  4. Native AFC events are admin-only. org_can_event returns is_platform_org_admin(user) when
     event.organization_id is None, so an organizer cannot attach a poll to an AFC-run event.
     Nobody has to remember that rule, because the existing function already enforces it.

WHAT THIS DOES NOT DECIDE: eligibility. Authorship and audience are separate questions. An
organizer's poll is still free to be open to the whole site (afc_polls.eligibility governs that).
Scoping a poll to an event is what makes it THEIRS, not what makes it NARROW.

CONSUMED BY: every admin endpoint in afc_polls.views (create / update / delete / results), and
the poll detail endpoint, which uses it to decide whether a draft is visible at all.
"""
from afc_organizers.permissions import org_can_event
from afc_tournament_and_scrims.views import _is_event_admin


def is_polls_admin(user):
    """AFC staff who may create polls anywhere, including site-wide ones."""
    return bool(user) and _is_event_admin(user)


def can_manage_poll(user, poll):
    """Whether `user` may edit, publish, delete or read the results of `poll`.

    Also the gate for CREATING a poll: build the unsaved Poll (or a stand-in carrying the intended
    `event`) and ask this, so creation and editing can never disagree about who is allowed."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_polls_admin(user):
        return True
    event = getattr(poll, "event", None)
    if event is None:
        # A site-wide poll belongs to AFC. An organizer with no event to point at has no claim.
        return False
    return org_can_event(user, "can_edit_events", event)
