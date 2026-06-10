"""
afc_leaderboard.permissions — who may manage a standalone leaderboard, and who may flip the
AFC-only `counts_toward_rankings` toggle.

PURPOSE
    Single source of truth for the two permission questions the standalone-leaderboard views ask:
      1. can_manage_standalone_lb(user, lb) — may this user create/edit/delete THIS leaderboard,
         add participants/maps/results, and create ghosts inline for it?
      2. can_set_rankings_flag(user)        — may this user set `counts_toward_rankings`? (AFC-admin only)

HOW IT CONNECTS
    - Reuses `afc_tournament_and_scrims.views._is_event_admin` (the AFC event-admin predicate:
      base role admin/moderator/support OR granular event_admin/head_admin) so the "AFC admin"
      definition stays identical to the rest of the events surface.
    - Reuses `afc_organizers.permissions.org_can` for the organizer path: an org member with the
      `can_upload_results` toggle (or the org owner / AFC platform-org-admin) may manage a leaderboard
      owned by THAT org.
    - Called from afc_leaderboard.views on every mutation and from inline-ghost creation. Note the
      spec decision (§5): inline ghost creation is gated by THIS helper (the leaderboard-edit
      permission), NOT the stricter head_admin/metrics_admin gate the afc_rankings ghost endpoints use.
"""
from afc_tournament_and_scrims.views import _is_event_admin
from afc_organizers.permissions import org_can


def can_manage_standalone_lb(user, lb):
    """
    True if `user` may manage standalone leaderboard `lb`.

    - AFC event admins manage ANY leaderboard (native or org-owned) — oversight layer.
    - For an org-owned leaderboard, an org member with `can_upload_results` (owner implicitly, or
      AFC platform-org-admin via org_can's bypass) may manage it.
    - An AFC-native leaderboard (organization_id is None) is manageable only by AFC admins — an
      organizer can never touch a leaderboard that is not owned by an org they belong to.
    """
    if not user:
        return False
    if _is_event_admin(user):
        return True
    if lb.organization_id:
        return org_can(user, "can_upload_results", lb.organization)
    return False


def can_set_rankings_flag(user):
    """True only for AFC admins. The `counts_toward_rankings` toggle is AFC-admin-only (spec §1.4);
    organizers never set it (the view forces it False for them)."""
    return _is_event_admin(user)
