r"""Read and write a team's role-permission matrix.

Owner 2026-08-08: "a way for team owners to decide what controls the other roles in the team have
over the team." This module is the two endpoints behind that screen; the rules themselves, the
capability catalogue and the stock defaults all live in afc_team/permissions.py, and the six gates
that ENFORCE the answers live in afc_team/views.py and afc_tournament_and_scrims/views.py.

It sits in its own module rather than in views.py for the same reason views_transfers.py does:
views.py is already 180kB, and a permissions surface is easier to audit when it is the only thing
in the file.

CONNECTS TO:
  - Rules    : afc_team.permissions (TEAM_CAPABILITIES, default_permission_map,
               resolve_team_permissions, team_role_can).
  - Model    : afc_team.models.TeamRolePermission.
  - Routes   : afc_team/urls.py -> /team/role-permissions/ and /team/set-role-permissions/.
  - Frontend : app/(user)/teams/[id]/permissions/page.tsx (the owner's screen), reached from the
               "Team Owner Controls" card on app/(user)/teams/[id].
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Team, TeamMembers, TeamRolePermission
from .permissions import (
    MANAGEABLE_ROLES,
    TEAM_CAPABILITIES,
    default_permission_map,
    resolve_team_permissions,
)
from .views import _get_authed_user, _is_admin


@api_view(["GET"])
def get_team_role_permissions(request):
    """GET /team/role-permissions/?team_name=<name> (or ?team_id=<id>) - the role/capability matrix.

    Keyed by team_name like get_team_details, because the team routes are /teams/<team_name>/... and
    every sibling page (detail, roster, edit) already resolves its team that way. team_id is also
    accepted, which is what the settings screen sends back on save (it has the numeric id from this
    response by then).

    RESPONSE 200 {
        team_id, team_name,
        roles: ["team_captain", ...],              # the rows, in display order
        capabilities: ["can_invite_members", ...], # the columns, in display order
        permissions: {role: {capability: bool}},   # what is in force right now
        defaults:    {role: {capability: bool}},   # the stock matrix, for the "Reset" button
        is_customised: bool,                       # False when the team has never saved
        can_edit: bool                             # True only for the team owner
    }
    AUTH  Bearer session token. Readable by the team OWNER, any member of the team, and AFC admins:
          a player being able to see what their own role may do is the point of the screen, and the
          matrix is not sensitive. `can_edit` is what gates the switches, and only the owner gets
          it (an AFC admin can read but not rewrite a team's own settings - admins have their own
          admin_* endpoints and are never blocked by this matrix in the first place).
    CONSUMED BY  app/(user)/teams/[id]/permissions/page.tsx.

    Never 404s on "no settings": a team with no rows is the normal case and returns the stock
    matrix with is_customised false.
    """
    user, err = _get_authed_user(request)
    if err:
        return err

    team_id = request.GET.get("team_id")
    team_name = request.GET.get("team_name")
    if not team_id and not team_name:
        return Response({"message": "team_id or team_name is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        team = (Team.objects.get(team_id=team_id) if team_id
                else Team.objects.get(team_name=team_name))
    except (Team.DoesNotExist, ValueError):
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    is_owner = team.team_owner_id == user.user_id
    is_member = TeamMembers.objects.filter(team=team, member=user).exists()
    if not (is_owner or is_member or _is_admin(user)):
        return Response({"message": "You are not a member of this team."},
                        status=status.HTTP_403_FORBIDDEN)

    return Response({
        "team_id": team.team_id,
        "team_name": team.team_name,
        "roles": list(MANAGEABLE_ROLES),
        "capabilities": list(TEAM_CAPABILITIES),
        "permissions": resolve_team_permissions(team),
        "defaults": default_permission_map(),
        "is_customised": TeamRolePermission.objects.filter(team=team).exists(),
        "can_edit": is_owner,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def set_team_role_permissions(request):
    """POST /team/set-role-permissions/ - the owner rewrites the matrix.

    REQUEST  {team_id: int, permissions: {<role>: {<capability>: bool, ...}, ...}}
             Partial bodies are fine: a role the body omits is left exactly as it was, and a
             capability a role omits keeps its current value. That is what lets the screen save one
             switch without having to resend the whole grid.
    RESPONSE 200 {message, permissions: {role: {capability: bool}}, is_customised: true}
             The full matrix as it now stands, so the client re-seeds from server truth rather than
             trusting its own optimistic state.
    AUTH     Bearer session token, TEAM OWNER ONLY.

    WHY OWNER-ONLY, and why the owner cannot lock themselves out:
      Letting anyone else write here would be a privilege-escalation hole - a role granted
      can_edit_roster could otherwise grant itself everything else. And the matrix is keyed by
      management_role, a set that does not contain "owner", so there is no row an owner could write
      that would restrict themselves. team_role_can() answers True for the owner before it reads
      any row at all (afc_team/permissions.py), which also covers the common case of an owner who
      is additionally seated as 'team_captain'. Both facts are asserted in
      afc_team/tests_role_permissions.py.

    Unknown roles and unknown capability names are REJECTED with 400 rather than ignored, so a
    typo in a client fails loudly instead of silently leaving a control switched off.
    CONSUMED BY  app/(user)/teams/[id]/permissions/page.tsx.
    """
    user, err = _get_authed_user(request)
    if err:
        return err

    team_id = request.data.get("team_id")
    payload = request.data.get("permissions")

    if not team_id:
        return Response({"message": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict) or not payload:
        return Response({"message": "permissions must be an object of {role: {capability: bool}}."},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        team = Team.objects.get(team_id=team_id)
    except (Team.DoesNotExist, ValueError):
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # The single write gate. Not _is_admin: an AFC admin is never blocked BY this matrix, so they
    # have no reason to rewrite a team's own preferences, and letting them would put admin edits and
    # owner edits into the same audit field with no way to tell them apart.
    if team.team_owner_id != user.user_id:
        return Response({"message": "Only the team owner can change role permissions."},
                        status=status.HTTP_403_FORBIDDEN)

    # ── validate the whole body BEFORE writing anything ──
    # An all-or-nothing check means a body with one bad key cannot leave the matrix half-applied.
    for role, caps in payload.items():
        if role not in MANAGEABLE_ROLES:
            return Response({"message": f"Unknown role '{role}'."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(caps, dict):
            return Response({"message": f"Permissions for '{role}' must be an object."},
                            status=status.HTTP_400_BAD_REQUEST)
        for cap in caps:
            if cap not in TEAM_CAPABILITIES:
                return Response({"message": f"Unknown capability '{cap}'."},
                                status=status.HTTP_400_BAD_REQUEST)

    # ── apply ──
    # get_or_create seeds a brand-new row from the STOCK defaults for that role, not from all-false,
    # so saving a screen that only toggles one switch cannot silently strip the other five controls
    # the role already had. After that the body's values are laid on top.
    current = resolve_team_permissions(team)
    for role, caps in payload.items():
        row, _created = TeamRolePermission.objects.get_or_create(
            team=team, management_role=role,
            defaults={cap: current[role][cap] for cap in TEAM_CAPABILITIES},
        )
        for cap, value in caps.items():
            setattr(row, cap, bool(value))
        row.updated_by = user
        row.save()

    return Response({
        "message": "Role permissions updated.",
        "permissions": resolve_team_permissions(team),
        "is_customised": True,
    }, status=status.HTTP_200_OK)
