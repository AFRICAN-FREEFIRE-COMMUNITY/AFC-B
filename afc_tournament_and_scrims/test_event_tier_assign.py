"""
test_event_tier_assign.py
─────────────────────────
Covers automatic tournament-tier classification + the head/super-admin manual override
(owner 2026-06-30: "both, but only head/super can override").

Targets afc_tournament_and_scrims.views.apply_event_tier / auto_classify_event:
  - a super (User.role=="admin") or head_admin who passes tournament_tier OVERRIDES + pins it
    (tier_overridden=True);
  - any other actor's tournament_tier is ignored -> the tier is auto-classified from the
    afc_rankings EventTierRule rules (default tier when no rule matches);
  - a pinned (overridden) event is NOT re-classified by a non-privileged editor.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_auth.models import Roles, UserRoles
from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.views import apply_event_tier, auto_classify_event
from afc_rankings.models import EventTierRule, EventTierConfig

User = get_user_model()


def _grant(user, role_name):
    role, _ = Roles.objects.get_or_create(role_name=role_name)
    UserRoles.objects.create(user=user, role=role)


def _event(creator, prize=0, participant_type="squad", mode="virtual"):
    return Event.objects.create(
        event_name="Tier Cup", competition_type="tournament", participant_type=participant_type,
        event_type="online", max_teams_or_players=12, event_mode=mode,
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator, prizepool_cash_value=prize,
    )


class EventTierAssignTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create(username="super", email="s@x.com", role="admin")
        self.head = User.objects.create(username="head", email="h@x.com")
        _grant(self.head, "head_admin")
        self.eventadmin = User.objects.create(username="ea", email="ea@x.com")
        _grant(self.eventadmin, "event_admin")
        # No tier rules by default -> auto-classify falls through to the config default tier.
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})

    def test_super_admin_override_pins(self):
        e = _event(self.superadmin)
        apply_event_tier(e, self.superadmin, {"tournament_tier": "tier_1"})
        e.refresh_from_db()
        self.assertEqual(e.tournament_tier, "tier_1")
        self.assertTrue(e.tier_overridden)

    def test_head_admin_override_pins(self):
        e = _event(self.head)
        apply_event_tier(e, self.head, {"tournament_tier": "tier_2"})
        e.refresh_from_db()
        self.assertEqual(e.tournament_tier, "tier_2")
        self.assertTrue(e.tier_overridden)

    def test_event_admin_cannot_override_autoclassifies(self):
        # event_admin is NOT head/super -> their tournament_tier is ignored, tier auto-classified.
        e = _event(self.eventadmin)
        apply_event_tier(e, self.eventadmin, {"tournament_tier": "tier_1"})
        e.refresh_from_db()
        self.assertEqual(e.tournament_tier, "tier_3")   # default, no rule matched
        self.assertFalse(e.tier_overridden)

    def test_pinned_event_not_reclassified_by_non_privileged(self):
        e = _event(self.head)
        apply_event_tier(e, self.head, {"tournament_tier": "tier_1"})   # head pins tier_1
        apply_event_tier(e, self.eventadmin, {"tournament_tier": "tier_3"})  # ea tries to change
        e.refresh_from_db()
        self.assertEqual(e.tournament_tier, "tier_1")   # stayed pinned
        self.assertTrue(e.tier_overridden)

    def test_auto_classify_uses_rules(self):
        # A rule "prize >= 1000 -> tier_1"; an event with a 5000 prize auto-classifies to tier_1.
        EventTierRule.objects.create(
            priority=1, match="all", tier=1, enabled=True,
            conditions=[{"field": "prize", "op": "gte", "value": 1000}],
        )
        e = _event(self.eventadmin, prize=5000)
        self.assertEqual(auto_classify_event(e), "tier_1")
        apply_event_tier(e, self.eventadmin, {})   # no override -> auto
        e.refresh_from_db()
        self.assertEqual(e.tournament_tier, "tier_1")
        self.assertFalse(e.tier_overridden)
