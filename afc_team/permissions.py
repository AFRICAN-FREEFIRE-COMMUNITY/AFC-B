r"""Team role permissions: what a team OWNER lets each of their roles do.

WHY THIS EXISTS (owner 2026-08-08): "a way for team owners to decide what controls the other roles
in the team have over the team." Until now every one of those answers was a constant compiled into
the backend, and the constants did not even agree with each other:

    kick a member / edit the roster   -> the owner, or a member whose role is exactly 'coach'
                                         (_can_manage_roster). A TEAM CAPTAIN could not.
    register the team for an event    -> the owner, captain, vice-captain, manager or coach
                                         (TEAM_EVENT_REGISTER_ROLES)
    invite somebody                   -> the owner, nobody else
    approve a join request            -> the owner, nobody else
    edit the team profile             -> the owner, nobody else

So a captain could take the team into a tournament but could not invite the sixth player, while the
coach could quietly kick anybody. Those are the defaults every existing team is living with, and
this module keeps them EXACTLY as they are until an owner deliberately changes them.

──────────────────────────────────────────────────────────────────────────────────────────────
THE THREE RULES THAT MAKE THIS SAFE
──────────────────────────────────────────────────────────────────────────────────────────────

1. NO ROW MEANS TODAY'S BEHAVIOUR. Every team that exists right now has no TeamRolePermission row,
   and none will be created for them. The fallback below is not "allow" or "deny", it is the exact
   set each role holds today, so the day this ships nothing shifts under anybody. Rows appear only
   when an owner opens the settings screen and saves. The fallback is also PER ROLE, not per team:
   a team that has customised 'coach' but never touched 'manager' still gets the stock manager
   answer, so a partial save can never blank out a role nobody edited.

2. THE OWNER IS NOT IN THE MATRIX. The matrix is keyed by TeamMembers.management_role, and "owner"
   is not one of those six values. There is no row that could revoke the owner, so a team cannot
   lock itself out by construction rather than by a guard somebody might later delete.
   team_role_can() also answers True for the owner BEFORE it reads anything, which matters because
   create_team seats the creator as 'team_captain': an owner who revokes team_captain is still the
   owner and keeps every control.

3. THIS MODULE NEVER SPEAKS FOR AFC ADMINS. It answers one question only, "has this TEAM granted
   this role this control". Admin overrides live where they already live (afc_team._is_admin and
   the admin_* endpoints, _is_event_admin / org_can_event on the tournament side) and are checked
   by the caller BEFORE this, so a team's settings can never shut an AFC admin out.

──────────────────────────────────────────────────────────────────────────────────────────────
WHAT IS DELIBERATELY *NOT* A CAPABILITY
──────────────────────────────────────────────────────────────────────────────────────────────

  * Being fielded. Whether a member can play is decided by PLAYER_ROLES / STAFF_ROLES and the
    6-player cap (afc_team/views.py). That is a competitive-integrity rule the whole tournament
    stack depends on, not a team preference, so a coach cannot be granted "may play" here.
  * The one-staff-each and 9-member caps, the transfer window, ban guards and event-timing locks.
    A capability says WHO may attempt an action; every one of those rules still decides whether the
    action is allowed at all, and they all run after this check.
  * The team stats-privacy toggle (set_team_stats_visibility) and the letter-avatar picker
    (set_team_letters). Both already have their own, WIDER role sets: a manager can flip stats
    today but cannot edit the team profile. Folding them into can_edit_team_profile would take
    controls away from managers on every existing team, which rule 1 forbids.

CONNECTS TO:
  - Model      : afc_team.models.TeamRolePermission (one row per team+role).
  - Written by : afc_team.views_permissions.set_team_role_permissions
                 (POST /team/set-role-permissions/, owner only).
  - Read by    : afc_team.views_permissions.get_team_role_permissions (the settings screen),
                 afc_team.views.get_team_details (as `my_capabilities`, so the team page renders
                 only the buttons the viewer can actually use), and the six server-side gates:
                   invite_member, generate_invite_link        -> can_invite_members
                   view_join_requests, review_join_request    -> can_manage_join_requests
                   manage_team_roster                         -> can_edit_roster
                   kick_team_member                           -> can_remove_members
                   edit_team                                  -> can_edit_team_profile
                   afc_tournament_and_scrims.views._user_can_register_team
                     (register_for_event, and through it the event-invitation accept/decline in
                      event_invites.py)                       -> can_register_for_events
  - Frontend   : app/(user)/teams/[id]/permissions (the owner's screen).
"""
from afc_team.models import TeamMembers, TeamRolePermission


# ──────────────────────────────────────────────────────────────────────────────────────────────
# The capability catalogue
#
# Each entry is a BooleanField name on TeamRolePermission. The order is the order the settings
# screen shows them in, grouped roughly "who is on the team" then "what the team does".
#
# Every capability names a real, user-visible action that already has a server endpoint. There is
# no capability here that nothing enforces, because a switch that changes nothing is worse than no
# switch at all.
# ──────────────────────────────────────────────────────────────────────────────────────────────
TEAM_CAPABILITIES = (
    # Send a direct invite (POST /team/invite-member/) and mint a shareable join link
    # (POST /team/generate-invite-link/). One capability because they are the same act with two
    # delivery mechanisms - somebody who can hand out a link can already add whoever they like.
    "can_invite_members",

    # See the team's pending join requests and approve or deny them
    # (GET /team/view-join-requests/, POST /team/review-join-request/).
    "can_manage_join_requests",

    # Change a member's management role and in-game position (POST /team/manage-team-roster/).
    "can_edit_roster",

    # Remove somebody from the team (POST /team/kick-team-member/). Split from can_edit_roster
    # because they are very different in consequence: demoting somebody is reversible in a click,
    # removing them is not, and an owner may reasonably want a coach to shuffle positions without
    # being able to throw a player out.
    "can_remove_members",

    # Register the team for a tournament or scrim, INCLUDING answering an event invitation
    # (POST /events/register-for-event/, POST /events/team-invitations/<id>/accept/ + /decline/).
    #
    # These are ONE capability on purpose, and it is the one place this list departs from the
    # feature request, which named them separately. Accepting an event invitation is implemented
    # by calling register_for_event itself (event_invites._register_through_the_normal_path), so a
    # role granted "accept invitations" but not "register" would pass the accept gate and then be
    # refused 403 by the registration it triggers, halfway through, with the invitation already
    # read as answered. The existing code says the same thing in a comment at event_invites.py:575
    # ("Answering IS registering, so the permission must be the SAME one"). One switch cannot get
    # into that state.
    "can_register_for_events",

    # Edit the team profile: name, tag, logo, description, join settings, social links
    # (POST /team/edit-team/).
    "can_edit_team_profile",
)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# THE DEFAULTS - a transcription of what the code does TODAY, not an opinion about what it should
#
# Read this table as: "with no settings row, which non-owner roles pass each gate right now".
# Every entry is sourced from the gate it replaces, named beside it. If a line here is wrong, an
# existing team's behaviour changes on deploy day, which is the one outcome this feature must not
# have - so afc_team.tests_role_permissions asserts every cell of this table against the real
# endpoints rather than against this dict.
#
# The team OWNER is absent by design (rule 2 above): the owner is not a management_role.
# ──────────────────────────────────────────────────────────────────────────────────────────────
DEFAULT_ROLE_CAPABILITIES = {
    # Captain and vice-captain: today they may take the team into an event and nothing else.
    # (TEAM_EVENT_REGISTER_ROLES includes them; _can_manage_roster does not; invite/join-request/
    # edit-team are all owner-only.)
    "team_captain": frozenset({"can_register_for_events"}),
    "vice_captain": frozenset({"can_register_for_events"}),

    # A plain player holds nothing today.
    "member": frozenset(),

    # Coach is the outlier in the current code: _can_manage_roster allows exactly the owner and
    # 'coach', so a coach is the only non-owner who can edit the roster or kick anybody. They are
    # also in TEAM_EVENT_REGISTER_ROLES.
    "coach": frozenset({"can_edit_roster", "can_remove_members", "can_register_for_events"}),

    # Manager may register the team (TEAM_EVENT_REGISTER_ROLES) but, despite the name, cannot touch
    # the roster today.
    "manager": frozenset({"can_register_for_events"}),

    # Analyst holds nothing today: absent from every gate.
    "analyst": frozenset(),
}

# The roles the matrix covers, in the order the settings screen lists them (leadership, then the
# playing rank, then staff). Derived from the model's own choices so a seventh role added later
# cannot be silently missed here - it would raise a KeyError in _defaults_for below instead.
MANAGEABLE_ROLES = tuple(role for role, _label in TeamMembers.MANAGEMENT_ROLE_CHOICES)


def _defaults_for(management_role):
    """The stock capability set for a role. Unknown roles hold nothing (fail closed)."""
    return DEFAULT_ROLE_CAPABILITIES.get(management_role, frozenset())


def default_permission_map():
    """The full stock matrix as {role: {capability: bool}} - what a team gets with no rows.

    Returned to the settings screen beside the live values so the owner can see what "default"
    means and reset to it, and used by the tests as the no-change baseline.
    """
    return {
        role: {cap: cap in _defaults_for(role) for cap in TEAM_CAPABILITIES}
        for role in MANAGEABLE_ROLES
    }


def resolve_team_permissions(team):
    """The matrix actually in force for `team`, as {role: {capability: bool}}.

    Stored rows win per ROLE; every role without a row falls back to its stock set. One query.
    Used by the settings screen (GET /team/role-permissions/) and by the tests.
    """
    stored = {row.management_role: row for row in
              TeamRolePermission.objects.filter(team=team)}
    matrix = {}
    for role in MANAGEABLE_ROLES:
        row = stored.get(role)
        if row is None:
            defaults = _defaults_for(role)
            matrix[role] = {cap: cap in defaults for cap in TEAM_CAPABILITIES}
        else:
            matrix[role] = {cap: bool(getattr(row, cap)) for cap in TEAM_CAPABILITIES}
    return matrix


def team_role_can(user, team, capability):
    """THE gate. True when `user` may perform `capability` on `team`.

    Answers in this order, and the order is the safety property:
      1. No user            -> False.
      2. The team OWNER     -> True, always, for every capability. Read before anything else so an
                               owner who is also (say) team_captain is unaffected by whatever the
                               team_captain row says. This is why an owner cannot be locked out.
      3. Not on the roster  -> False. Note this deliberately does NOT consult Team.team_captain,
                               the FK that sits beside team_owner: no permission gate in the
                               codebase reads that FK today, and it frequently disagrees with the
                               roster row, so honouring it here would hand out access nobody has
                               right now.
      4. Otherwise          -> the stored row for that member's role, else the role's stock set.

    Callers that already have an AFC-admin or organizer override must check it BEFORE calling this
    (see rule 3 in the module docstring); this function knows nothing about admins.

    Cost: one FK compare, then at most two indexed lookups (membership, then the unique
    team+role row).
    """
    if user is None:
        return False

    # (2) Owner short-circuit. Compared by id so it works whether the caller holds a User instance
    # or a lazily-loaded reference, matching how _is_team_owner_or_manager does it.
    if team.team_owner_id == getattr(user, "user_id", None):
        return True

    membership = TeamMembers.objects.filter(team=team, member=user).only(
        "management_role").first()
    if membership is None:
        return False

    row = TeamRolePermission.objects.filter(
        team=team, management_role=membership.management_role).first()
    if row is None:
        return capability in _defaults_for(membership.management_role)
    return bool(getattr(row, capability, False))


def capabilities_for(user, team):
    """Every capability `user` holds on `team`, as {capability: bool}.

    Returned by get_team_details as `my_capabilities` so the team page can render exactly the
    controls this viewer can use, instead of the frontend re-deriving the rules from role strings
    (which is how the frontend and backend drifted apart in the first place). A hidden button is
    only a convenience: each endpoint still enforces the same answer server-side.
    """
    if user is None:
        return {cap: False for cap in TEAM_CAPABILITIES}
    if team.team_owner_id == getattr(user, "user_id", None):
        return {cap: True for cap in TEAM_CAPABILITIES}

    membership = TeamMembers.objects.filter(team=team, member=user).only(
        "management_role").first()
    if membership is None:
        return {cap: False for cap in TEAM_CAPABILITIES}

    row = TeamRolePermission.objects.filter(
        team=team, management_role=membership.management_role).first()
    if row is None:
        defaults = _defaults_for(membership.management_role)
        return {cap: cap in defaults for cap in TEAM_CAPABILITIES}
    return {cap: bool(getattr(row, cap)) for cap in TEAM_CAPABILITIES}
