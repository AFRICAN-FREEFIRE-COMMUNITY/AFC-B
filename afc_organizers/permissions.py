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
    """Event-scoped variant: resolves the event's owning org. Native AFC events (no
    organization) are admin-only — organizers never touch events outside their own org."""
    if event.organization_id is None:
        return is_platform_org_admin(user)
    return org_can(user, perm, event.organization)
