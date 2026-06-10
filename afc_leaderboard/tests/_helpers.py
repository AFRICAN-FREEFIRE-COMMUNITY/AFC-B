"""
Shared test fixtures/helpers for afc_leaderboard tests.

Follows the afc_auth.test_audit_log pattern: mint a User + a live SessionToken, then drive the
endpoints with the Django test Client using a `Bearer <token>` Authorization header.
"""
from afc_auth.models import User, SessionToken, Roles, UserRoles
from afc_team.models import Team
from afc_organizers.models import Organization, OrganizationMember


def make_user(username, role="player", email=None, granular=None):
    """Create a User (+ optional granular UserRoles) and a live SessionToken.
    Returns (user, token_string)."""
    u = User.objects.create(
        username=username,
        email=email or f"{username}@x.com",
        full_name=username.title(),
        role=role,
        password="x",
    )
    for role_name in (granular or []):
        r, _ = Roles.objects.get_or_create(role_name=role_name)
        UserRoles.objects.create(user=u, role=r)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def make_afc_admin(username="afcadmin"):
    """An AFC event-admin (base role 'admin' satisfies _is_event_admin). Returns (user, token)."""
    return make_user(username, role="admin")


def make_team(name, owner, country="NG"):
    """Create a real Team owned/created by `owner`."""
    return Team.objects.create(
        team_name=name,
        team_owner=owner,
        team_creator=owner,
        join_settings="open",
        country=country,
    )


def make_org(name="Org One", slug="org-one"):
    """Create an active Organization."""
    return Organization.objects.create(name=name, slug=slug, status="active")


def add_member(org, user, role="sub_organizer", **perms):
    """Add `user` to `org` with optional granular permission toggles (e.g. can_upload_results=True)."""
    return OrganizationMember.objects.create(
        organization=org, user=user, role=role, status="active", **perms
    )


def bearer(token):
    """The HTTP_AUTHORIZATION kwarg for the Django test Client."""
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
