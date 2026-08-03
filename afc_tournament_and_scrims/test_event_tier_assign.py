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
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_auth.models import Roles, UserRoles, FxRate
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


# ── prize-pool currency conversion (owner 2026-08-03, DYNASTY CUP GRAND FINALS SSA) ──
# The EventTierRule thresholds are authored in NAIRA (spec §4: USD pools are converted to ₦ and
# THEN compared), but Event.prizepool_cash_value is stored in the event's own prize_currency, which
# defaults to USD. Before the fix a $400 event was compared as the bare number 400 against the
# ₦100,000 Tier-1 threshold, matched nothing, and fell through to the default Tier 3 - the real
# reason event 172 sat at tier_3. auto_classify_event now converts through the same FxRate table
# prize_sync uses.
class EventTierCurrencyTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create(username="ccy", email="ccy@x.com")
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})
        # The seeded production rule: pools of ₦100,000 or more are Tier 1.
        EventTierRule.objects.create(
            priority=0, match="all", tier=1, enabled=True,
            conditions=[{"field": "prize", "op": "gte", "value": 100000}],
        )
        # rate = units per 1 USD, matching afc_auth.FxRate's contract.
        FxRate.objects.create(currency="USD", rate=Decimal("1"))
        FxRate.objects.create(currency="NGN", rate=Decimal("1361.407563"))

    def test_usd_pool_converts_to_naira_before_matching(self):
        # $400 = ~₦544,563, comfortably over the ₦100,000 Tier-1 threshold. The pre-fix code
        # compared the bare 400 and returned tier_3.
        e = _event(self.creator, prize=400)
        e.prize_currency = "USD"
        e.save(update_fields=["prize_currency"])
        self.assertEqual(auto_classify_event(e), "tier_1")

    def test_naira_pool_is_used_verbatim(self):
        # An event already priced in NGN must not be converted again (DECA CUP SEASON 5 is this case).
        e = _event(self.creator, prize=1000000)
        e.prize_currency = "NGN"
        e.save(update_fields=["prize_currency"])
        self.assertEqual(auto_classify_event(e), "tier_1")

    def test_small_usd_pool_still_falls_through_to_default(self):
        # $50 = ~₦68,070, under the threshold -> no rule matches -> EventTierConfig.default_tier.
        e = _event(self.creator, prize=50)
        e.prize_currency = "USD"
        e.save(update_fields=["prize_currency"])
        self.assertEqual(auto_classify_event(e), "tier_3")

    def test_missing_fx_data_keeps_the_raw_amount(self):
        # No FX rows at all -> _amount_ngn returns the number unchanged rather than dropping it, so
        # classification degrades to the old behaviour instead of silently zeroing every pool.
        FxRate.objects.all().delete()
        e = _event(self.creator, prize=250000)
        e.prize_currency = "USD"
        e.save(update_fields=["prize_currency"])
        self.assertEqual(auto_classify_event(e), "tier_1")
