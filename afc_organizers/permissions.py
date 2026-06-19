# afc_organizers/permissions.py
# ──────────────────────────────────────────────────────────────────────────────
# Single source of truth for "can this user do X in this org?".
#
# Rules (in priority order):
#   1. AFC platform admins (head_admin / organizer_admin) bypass EVERY org-scope gate.
#      This is the oversight layer — AFC has full access to all org data by design.
#   2. An org owner implicitly has every permission.
#   3. A sub_organizer has only the granular toggles granted on their OrganizationMember row.
#   4. A non-member has nothing.
#
# Every org-scoped endpoint (member management now; event/results endpoints in later
# phases) calls org_can / org_can_event instead of reading membership rows directly, so
# these rules stay in one place. Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from .models import OrganizationMember

# AFC staff roles that get unconditional access to every organization.
PLATFORM_ADMIN_ROLES = ("head_admin", "organizer_admin")


def member_or_403(user, org):
    """Return the caller's ACTIVE OrganizationMember row for `org`, or None if they are not an
    active member (the view turns None into a 403). Shared here (cleanup 2026-06-14) so the
    org view modules that each had an identical private copy import the one definition."""
    return OrganizationMember.objects.filter(
        organization=org, user=user, status="active").first()


def is_platform_org_admin(user) -> bool:
    """True for AFC staff who oversee organizations (full bypass of org-scope gates)."""
    return bool(user) and user.role == "admin" and \
        user.userroles.filter(role__role_name__in=PLATFORM_ADMIN_ROLES).exists()


def org_can(user, perm, organization) -> bool:
    """Whether `user` may perform `perm` (a can_* field name) within `organization`."""
    # 1) AFC oversight bypass.
    if is_platform_org_admin(user):
        return True
    # 2) Must be an active member of THIS org.
    member = OrganizationMember.objects.filter(
        organization=organization, user=user, status="active"
    ).first()
    if not member:
        return False
    # 3) Owner has everything; 4) sub_organizer has only what was granted.
    if member.role == "owner":
        return True
    return bool(getattr(member, perm, False))


def org_can_event(user, perm, event) -> bool:
    """Event-scoped variant: resolves the event's owning org(s). Native AFC events (no
    organization) are admin-only — organizers never touch events outside their own org.

    Multi-org co-ownership (F6, owner 2026-06-19): the action is allowed if the user can do `perm`
    in the PRIMARY org (Event.organization) OR in any ACCEPTED CO-OWNING org whose scoped grant
    includes `perm`. The co-owner check runs ONLY when the primary check fails, and only queries
    when an event actually has co-owners — so events with no co-owners (the overwhelming majority)
    keep the exact single-org behaviour + cost. This one change lets co-ownership flow through every
    endpoint that already gates on org_can_event (edit/results/registrations/broadcast/seeding/…)."""
    if event.organization_id is None:
        return is_platform_org_admin(user)
    # 1) Primary org (creator) — unchanged fast path.
    if org_can(user, perm, event.organization):
        return True
    # 2) Accepted co-owners: the co-org's grant must include `perm` AND the user must be able to do
    #    `perm` within that co-org (owner implicitly, or a sub_organizer who holds it).
    from .models import EventCoOrganizer
    for co in EventCoOrganizer.objects.filter(
        event_id=event.event_id, status="accepted",
    ).select_related("organization"):
        if getattr(co, perm, False) and org_can(user, perm, co.organization):
            return True
    return False
