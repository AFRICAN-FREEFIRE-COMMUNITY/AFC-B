# afc_organizers/tests.py
# Unit tests for the permission helper — the security core of the feature. Covers the four
# rules in permissions.org_can plus the event-scoped variant. Org/membership rows are real
# DB objects; for org_can_event we use a tiny stand-in object carrying only the attributes
# the helper reads (organization_id / organization) so we don't have to build a full Event
# (which has many required tournament fields unrelated to this check).
from types import SimpleNamespace

from django.test import TestCase

from afc_auth.models import Roles, UserRoles, User
from .models import Organization, OrganizationMember
from . import permissions


class OrgPermissionTests(TestCase):
    def setUp(self):
        # AFC oversight role + an AFC staff user holding it.
        self.organizer_admin_role, _ = Roles.objects.get_or_create(role_name="organizer_admin")
        self.afc_admin = User.objects.create_user(
            username="afcstaff", email="afc@x.com", password="x", full_name="AFC Staff", role="admin"
        )
        UserRoles.objects.create(user=self.afc_admin, role=self.organizer_admin_role)

        # An org with an owner and a limited sub-organizer.
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        self.owner = User.objects.create_user(
            username="owner", email="owner@x.com", password="x", full_name="Owner", role="player"
        )
        self.sub = User.objects.create_user(
            username="sub", email="sub@x.com", password="x", full_name="Sub", role="player"
        )
        self.outsider = User.objects.create_user(
            username="outsider", email="out@x.com", password="x", full_name="Out", role="player"
        )
        OrganizationMember.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationMember.objects.create(
            organization=self.org, user=self.sub, role="sub_organizer", can_view_metrics=True
        )

    def test_owner_has_every_permission(self):
        self.assertTrue(permissions.org_can(self.owner, "can_create_events", self.org))
        self.assertTrue(permissions.org_can(self.owner, "can_manage_members", self.org))

    def test_sub_only_has_granted_toggles(self):
        self.assertTrue(permissions.org_can(self.sub, "can_view_metrics", self.org))
        self.assertFalse(permissions.org_can(self.sub, "can_manage_members", self.org))
        self.assertFalse(permissions.org_can(self.sub, "can_upload_results", self.org))

    def test_non_member_has_nothing(self):
        self.assertFalse(permissions.org_can(self.outsider, "can_view_metrics", self.org))

    def test_platform_admin_bypasses_everything(self):
        self.assertTrue(permissions.is_platform_org_admin(self.afc_admin))
        self.assertTrue(permissions.org_can(self.afc_admin, "can_manage_members", self.org))

    def test_removed_member_loses_access(self):
        m = OrganizationMember.objects.get(organization=self.org, user=self.sub)
        m.status = "removed"
        m.save(update_fields=["status"])
        self.assertFalse(permissions.org_can(self.sub, "can_view_metrics", self.org))

    def test_org_can_event_native_event_is_admin_only(self):
        native_event = SimpleNamespace(organization_id=None, organization=None)
        self.assertTrue(permissions.org_can_event(self.afc_admin, "can_upload_results", native_event))
        self.assertFalse(permissions.org_can_event(self.owner, "can_upload_results", native_event))

    def test_org_can_event_org_event_resolves_to_org_can(self):
        # event_id is required by the F6 co-owner branch in org_can_event (it queries
        # EventCoOrganizer.filter(event_id=...)); the stub predates that feature, so we add
        # it here. 0 matches no co-owner rows -> the check falls through to org_can as intended.
        org_event = SimpleNamespace(organization_id=self.org.pk, organization=self.org, event_id=0)
        self.assertTrue(permissions.org_can_event(self.owner, "can_upload_results", org_event))
        self.assertFalse(permissions.org_can_event(self.sub, "can_upload_results", org_event))
        self.assertTrue(permissions.org_can_event(self.sub, "can_view_metrics", org_event))
