"""
Tests for afc_leaderboard.permissions.can_manage_standalone_lb / can_set_rankings_flag.

Covers: AFC admin manages anything + may set the rankings flag; an org member with
can_upload_results manages that org's leaderboard but may NOT set the rankings flag; an unrelated
user manages nothing.
"""
from django.test import TestCase

from afc_leaderboard.models import StandaloneLeaderboard
from afc_leaderboard.permissions import can_manage_standalone_lb, can_set_rankings_flag

from ._helpers import make_afc_admin, make_user, make_org, add_member


class PermissionTests(TestCase):
    def setUp(self):
        self.admin, _ = make_afc_admin()
        self.org = make_org()
        # Org member WITH can_upload_results.
        self.uploader, _ = make_user("uploader")
        add_member(self.org, self.uploader, role="sub_organizer", can_upload_results=True)
        # Org member WITHOUT can_upload_results.
        self.member, _ = make_user("plainmember")
        add_member(self.org, self.member, role="sub_organizer", can_upload_results=False)
        # Totally unrelated user.
        self.stranger, _ = make_user("stranger")

        self.org_lb = StandaloneLeaderboard.objects.create(
            name="OrgLB", format="team", placement_points={}, organization=self.org, creator=self.admin
        )
        self.native_lb = StandaloneLeaderboard.objects.create(
            name="NativeLB", format="team", placement_points={}, organization=None, creator=self.admin
        )

    def test_afc_admin_manages_anything(self):
        self.assertTrue(can_manage_standalone_lb(self.admin, self.org_lb))
        self.assertTrue(can_manage_standalone_lb(self.admin, self.native_lb))
        self.assertTrue(can_set_rankings_flag(self.admin))

    def test_org_uploader_manages_own_org_lb_only(self):
        self.assertTrue(can_manage_standalone_lb(self.uploader, self.org_lb))
        # Cannot manage an AFC-native leaderboard (no org).
        self.assertFalse(can_manage_standalone_lb(self.uploader, self.native_lb))
        # Organizer may NOT set the rankings flag.
        self.assertFalse(can_set_rankings_flag(self.uploader))

    def test_org_member_without_upload_cannot_manage(self):
        self.assertFalse(can_manage_standalone_lb(self.member, self.org_lb))

    def test_stranger_manages_nothing(self):
        self.assertFalse(can_manage_standalone_lb(self.stranger, self.org_lb))
        self.assertFalse(can_manage_standalone_lb(self.stranger, self.native_lb))
        self.assertFalse(can_set_rankings_flag(self.stranger))
