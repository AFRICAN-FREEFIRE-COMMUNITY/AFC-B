"""
test_room_visibility.py
───────────────────────
Covers the all-groups ROOM-DETAILS visibility policy (owner 2026-06-29: "Only admins not
organizers... only super, head and event admins" may see every group's room id/name/password).

The policy lives in afc_tournament_and_scrims.views._can_see_all_group_rooms, which feeds the
get_event_details _can_see_room gate. These assert exactly WHO counts as "sees all rooms":
  - super-admin (User.role == "admin"), granular head_admin, granular event_admin -> True
  - organizer, moderator, support, plain player, anonymous -> False (they fall through to the
    per-group path and only ever see their OWN group's room).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_auth.models import Roles, UserRoles
from afc_tournament_and_scrims.views import _can_see_all_group_rooms

User = get_user_model()


def _grant(user, role_name):
    role, _ = Roles.objects.get_or_create(role_name=role_name)
    UserRoles.objects.create(user=user, role=role)


class CanSeeAllGroupRoomsTests(TestCase):
    def _user(self, name, role="player"):
        return User.objects.create(username=name, email=f"{name}@x.com", role=role)

    def test_super_admin_sees_all(self):
        self.assertTrue(_can_see_all_group_rooms(self._user("super", role="admin")))

    def test_head_admin_sees_all(self):
        u = self._user("head")
        _grant(u, "head_admin")
        self.assertTrue(_can_see_all_group_rooms(u))

    def test_event_admin_sees_all(self):
        u = self._user("eventadmin")
        _grant(u, "event_admin")
        self.assertTrue(_can_see_all_group_rooms(u))

    def test_organizer_does_not_see_all(self):
        # The core of the bug: an organizer (even with the organizer granular role) is NOT staff
        # for room visibility -> must not see every group's room.
        u = self._user("org")
        _grant(u, "organizer")
        self.assertFalse(_can_see_all_group_rooms(u))

    def test_moderator_and_support_do_not_see_all(self):
        # Not in the owner's "super/head/event" list -> excluded here (tighter than _is_event_admin).
        self.assertFalse(_can_see_all_group_rooms(self._user("mod", role="moderator")))
        self.assertFalse(_can_see_all_group_rooms(self._user("sup", role="support")))

    def test_plain_player_and_anonymous_do_not_see_all(self):
        self.assertFalse(_can_see_all_group_rooms(self._user("player")))
        self.assertFalse(_can_see_all_group_rooms(None))
