"""
afc_rankings.test_tier_rule_currency - a tier-rule prize threshold can name its own currency.

Owner, 2026-08-07: "should be able to select currency there". A prize threshold used to be a bare
number every reader agreed to treat as naira; an admin who thinks in dollars had to convert in their
head and re-do it whenever the rate moved.

THE TEST THAT MATTERS MOST IS THE FIRST CLASS. Adding a currency to a threshold is only safe if
every rule already written keeps the exact meaning it had, because a rule that quietly changes
meaning re-tiers every event on the platform and therefore every team's score. ``PreservationTests``
pins that from several directions: a legacy condition with no currency key, the same condition with
an explicit "NGN", and the same condition with the FX table wiped, all classify identically.

  * PreservationTests    - nothing an admin has already saved changes meaning.
  * ForeignCurrencyTests - a threshold in USD matches the events it should, converted at the rate.
  * FailClosedTests      - a threshold that cannot be converted matches nothing, and says so.
  * ValidationTests      - what the write endpoint accepts, refuses, and normalizes.
  * ContradictionTests   - shadowed rules are still detected, including across currencies.
  * ApiTests             - the admin surface: save a non-naira rule, read it back, preview it.

HOW IT CONNECTS
    Exercises afc_rankings.scoring.currency (pure), afc_rankings.admin_tournament_tiers (the
    classifier + the admin write API), afc_rankings.scoring.validation.rule_contradictions, and
    afc_tournament_and_scrims.views.auto_classify_event, which is the function that actually stamps
    Event.tournament_tier on create/edit and therefore decides the scoring multiplier every result
    in that event is worth.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from afc_auth.models import FxRate, Roles, SessionToken, UserRoles
from afc_rankings.admin_tournament_tiers import _fx_rate_map, classify
from afc_rankings.models import EventTierConfig, EventTierRule
from afc_rankings.scoring.currency import condition_currency, convert_to_ngn, threshold_ngn
from afc_rankings.scoring.validation import rule_contradictions
from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.views import auto_classify_event

User = get_user_model()

REASON = "tier rule currency test"    # >= the 10-char audit-reason minimum

# The live rate at the time this suite was written, so the expected naira figures below are
# arithmetic a reader can check rather than magic numbers: 1 USD = 1364.22007900 NGN.
USD_NGN = Decimal("1364.220079")


# ───────────────────────── fixtures ─────────────────────────
def _seed_fx():
    """The two rows every conversion in this file needs. rate = units per 1 USD (FxRate's contract)."""
    FxRate.objects.create(currency="USD", rate=Decimal("1"))
    FxRate.objects.create(currency="NGN", rate=USD_NGN)


def _rule(conditions, tier=1, priority=0, match="all", name="", enabled=True):
    return EventTierRule.objects.create(
        priority=priority, match=match, tier=tier, enabled=enabled, name=name,
        conditions=conditions,
    )


def _event(creator, prize=0, currency="NGN", mode="virtual"):
    event = Event.objects.create(
        event_name="Currency Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=12, event_mode=mode,
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator, prizepool_cash_value=prize,
    )
    event.prize_currency = currency
    event.save(update_fields=["prize_currency"])
    return event


def _sample(prize_ngn, fmt="virtual"):
    return {"prize": prize_ngn, "teams": 16, "players": 0, "format": fmt}


def _user_with_role(username, role_name):
    user = User.objects.create(username=username, email=f"{username}@example.com")
    role, _ = Roles.objects.get_or_create(role_name=role_name)
    UserRoles.objects.create(user=user, role=role)
    token = SessionToken.objects.create(user=user, token=f"tok_{username}").token
    return user, token


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ═════════════════════════ nothing already saved changes meaning ═════════════════════════
class PreservationTests(TestCase):
    """The regression that matters. Every rule on the platform today is a bare number meaning naira.

    All three shapes below must produce the SAME answer as each other and as the pre-change
    behaviour (a bare-number comparison against the naira sample):
      1. the legacy shape, no ``currency`` key at all;
      2. the same threshold with an explicit ``"currency": "NGN"``, which is what the write path
         now normalizes a legacy rule to the next time an admin re-saves it;
      3. the legacy shape with no FX data in the database at all.
    """

    def setUp(self):
        self.creator = User.objects.create(username="pres", email="pres@x.com")
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})
        _seed_fx()

    def test_a_legacy_condition_with_no_currency_key_is_read_as_naira(self):
        self.assertEqual(condition_currency({"field": "prize", "op": "gte", "value": 100000}), "NGN")

    def test_the_seeded_production_rules_classify_exactly_as_before(self):
        # The four rules actually stored on this platform, in their stored order and stored shape
        # (no currency key anywhere). Their answers are the pre-change answers.
        _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1, priority=0)
        _rule([{"field": "prize", "op": "gte", "value": 1000000},
               {"field": "format", "op": "is_lan", "value": None}], tier=1, priority=1)
        _rule([{"field": "format", "op": "is_lan", "value": None}], tier=2, priority=2)
        _rule([{"field": "prize", "op": "gte", "value": 300000}], tier=2, priority=3)
        rules = list(EventTierRule.objects.order_by("priority"))
        rate_map = _fx_rate_map()

        for prize, fmt, expected in [
            (0, "virtual", 3),            # nothing matches -> default
            (99_999, "virtual", 3),       # just under the first bar
            (100_000, "virtual", 1),      # exactly the first bar -> Tier 1
            (500_000, "virtual", 1),
            (50_000, "lan", 2),           # LAN under every prize bar -> rule 3
            (2_000_000, "lan", 1),
        ]:
            with self.subTest(prize=prize, fmt=fmt):
                self.assertEqual(
                    classify(rules, 3, _sample(prize, fmt), rate_map)["tier"], expected)

    def test_an_explicit_naira_currency_is_identical_to_no_currency(self):
        legacy = _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1, priority=0)
        explicit = _rule([{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}],
                         tier=1, priority=0)
        rate_map = _fx_rate_map()
        for prize in (0, 99_999, 100_000, 5_000_000):
            with self.subTest(prize=prize):
                self.assertEqual(
                    classify([legacy], 3, _sample(prize), rate_map)["tier"],
                    classify([explicit], 3, _sample(prize), rate_map)["tier"],
                )

    def test_a_naira_threshold_never_touches_the_fx_layer(self):
        """With NO exchange rates at all, a naira rule still classifies exactly the same.

        This is the property that makes the change safe to deploy: an FX outage, an empty table on a
        fresh environment, or a stale rate cannot move a single rule that exists today.
        """
        FxRate.objects.all().delete()
        rule = _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1)
        self.assertEqual(classify([rule], 3, _sample(100_000), {})["tier"], 1)
        self.assertEqual(classify([rule], 3, _sample(99_999), {})["tier"], 3)
        # And through the real entry point, with no rate_map threaded in at all.
        self.assertEqual(classify([rule], 3, _sample(100_000))["tier"], 1)

    def test_auto_classify_event_is_unchanged_for_a_naira_rule(self):
        """End to end: the function that actually stamps Event.tournament_tier."""
        _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1)
        # A $400 pool converts to ~545,688 naira, over the 100,000 bar (the DYNASTY CUP case).
        self.assertEqual(auto_classify_event(_event(self.creator, 400, "USD")), "tier_1")
        # A 50,000 naira pool is under it.
        self.assertEqual(auto_classify_event(_event(self.creator, 50_000, "NGN")), "tier_3")


# ═════════════════════════ a threshold in another currency ═════════════════════════
class ForeignCurrencyTests(TestCase):
    """A rule written in USD must match the events it means, converted at the stored rate."""

    def setUp(self):
        self.creator = User.objects.create(username="fx", email="fx@x.com")
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})
        _seed_fx()
        # "A pool of $1,000 or more is Tier 1." $1,000 = 1,364,220 naira at the seeded rate.
        self.rule = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}], tier=1)
        self.rate_map = _fx_rate_map()

    def test_the_threshold_converts_to_the_expected_naira_figure(self):
        self.assertEqual(
            threshold_ngn({"field": "prize", "op": "gte", "value": 1000, "currency": "USD"},
                          self.rate_map),
            Decimal("1000") * USD_NGN,
        )

    def test_it_matches_a_naira_pool_worth_more_than_the_bar(self):
        # 1,400,000 naira is above $1,000 (= 1,364,220 naira).
        self.assertEqual(classify([self.rule], 3, _sample(1_400_000), self.rate_map)["tier"], 1)

    def test_it_does_not_match_a_naira_pool_worth_less_than_the_bar(self):
        self.assertEqual(classify([self.rule], 3, _sample(1_300_000), self.rate_map)["tier"], 3)

    def test_it_is_not_compared_as_a_bare_number(self):
        """The bug this feature could most easily introduce: reading "$1,000" as "1,000 naira".

        A 5,000 naira pool is a rounding error against a $1,000 bar. If the threshold were compared
        raw it would match, and nearly every event on the platform would be promoted to Tier 1.
        """
        self.assertEqual(classify([self.rule], 3, _sample(5_000), self.rate_map)["tier"], 3)

    def test_end_to_end_a_usd_rule_tiers_a_usd_event(self):
        # $1,500 pool against a $1,000 bar. Both sides convert through the same map and the same
        # formula, so the answer does not depend on the rate at all in this case.
        self.assertEqual(auto_classify_event(_event(self.creator, 1500, "USD")), "tier_1")
        self.assertEqual(auto_classify_event(_event(self.creator, 900, "USD")), "tier_3")

    def test_a_naira_rule_and_a_usd_rule_coexist_in_one_list(self):
        # Naira rule first (Tier 2 from 200,000), USD rule second (Tier 1 from $1,000). First match
        # wins, so an event over both bars gets Tier 2: this pins that the ORDER still decides,
        # currency or not.
        naira = _rule([{"field": "prize", "op": "gte", "value": 200000}], tier=2, priority=0)
        self.rule.priority = 1
        self.rule.save(update_fields=["priority"])
        rules = list(EventTierRule.objects.order_by("priority"))
        self.assertEqual(rules[0].id, naira.id)
        self.assertEqual(classify(rules, 3, _sample(2_000_000), self.rate_map)["tier"], 2)
        self.assertEqual(classify(rules, 3, _sample(100_000), self.rate_map)["tier"], 3)


# ═════════════════════════ what happens when FX is unavailable ═════════════════════════
class FailClosedTests(TestCase):
    """An unconvertible threshold matches NOTHING, and the admin is told.

    The alternative (falling back to the raw number, which is what prize_sync._amount_ngn does for
    an AMOUNT) would read a "$1,000" bar as "1,000 naira" and silently promote almost every event.
    Under-tiering is recoverable and visible; a platform-wide silent promotion is neither.
    """

    def setUp(self):
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})
        # Deliberately NO FxRate rows.

    def test_convert_returns_none_without_rates(self):
        self.assertIsNone(convert_to_ngn(1000, "USD", {}))

    def test_the_condition_fails_closed(self):
        rule = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}], tier=1)
        # Would have matched at any sensible rate; without rates it matches nothing.
        self.assertEqual(classify([rule], 3, _sample(999_999_999), {})["tier"], 3)

    def test_the_contradiction_checker_reports_it(self):
        rule = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}],
                     tier=1, name="Dollar major")
        found = rule_contradictions(
            [{"id": rule.id, "name": rule.name, "priority": 0, "match": "all",
              "conditions": rule.conditions, "tier": 1, "enabled": True, "retired": False}],
            default_tier=3, rate_map={},
        )
        kinds = [c["kind"] for c in found]
        self.assertIn("unconvertible_threshold", kinds)
        message = next(c["message"] for c in found if c["kind"] == "unconvertible_threshold")
        self.assertIn("USD", message)
        self.assertIn("'Dollar major'", message)


# ═════════════════════════ the write endpoint's contract ═════════════════════════
class ValidationTests(TestCase):
    """What the API accepts, refuses, and normalizes."""

    def setUp(self):
        self.admin, self.token = _user_with_role("val_head", "head_admin")
        _seed_fx()

    def _create(self, conditions):
        return self.client.post(
            reverse("rankings_event_tier_rules"),
            {"name": "Test", "match": "all", "conditions": conditions, "tier": 1, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )

    def test_a_supported_currency_is_accepted_and_stored(self):
        response = self._create([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}])
        self.assertEqual(response.status_code, 201)
        stored = EventTierRule.objects.get(pk=response.json()["id"]).conditions
        self.assertEqual(stored, [{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}])

    def test_an_omitted_currency_is_normalized_to_naira(self):
        """A client that never sends the key keeps working, and the stored rule says what it means."""
        response = self._create([{"field": "prize", "op": "gte", "value": 100000}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            EventTierRule.objects.get(pk=response.json()["id"]).conditions,
            [{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}],
        )

    def test_a_lowercase_code_is_upper_cased(self):
        response = self._create([{"field": "prize", "op": "gte", "value": 5, "currency": "usd"}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            EventTierRule.objects.get(pk=response.json()["id"]).conditions[0]["currency"], "USD")

    def test_an_unknown_currency_is_refused(self):
        response = self._create([{"field": "prize", "op": "gte", "value": 5, "currency": "XXX"}])
        self.assertEqual(response.status_code, 400)
        self.assertIn("XXX", response.json()["message"])

    def test_a_currency_on_a_team_count_is_refused(self):
        """Refused rather than dropped: dropping it leaves the admin believing it did something."""
        response = self._create([{"field": "teams", "op": "gte", "value": 16, "currency": "USD"}])
        self.assertEqual(response.status_code, 400)
        self.assertIn("count", response.json()["message"])

    def test_a_count_condition_keeps_its_original_three_key_shape(self):
        response = self._create([{"field": "teams", "op": "gte", "value": 16}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            EventTierRule.objects.get(pk=response.json()["id"]).conditions,
            [{"field": "teams", "op": "gte", "value": 16}],
        )


# ═════════════════════════ contradictions still work ═════════════════════════
class ContradictionTests(TestCase):
    """The owner's "two rules both above 100,000" check must survive the currency change, and must
    now also work when the two rules are written in DIFFERENT currencies."""

    def _dicts(self, *rules):
        return [
            {"id": r.id, "name": r.name, "priority": r.priority, "match": r.match,
             "conditions": r.conditions, "tier": r.tier, "enabled": True, "retired": False}
            for r in rules
        ]

    def setUp(self):
        _seed_fx()
        self.rate_map = _fx_rate_map()

    def test_the_original_naira_only_case_is_still_detected(self):
        first = _rule([{"field": "prize", "op": "gte", "value": 100000}],
                      tier=1, priority=0, name="First")
        second = _rule([{"field": "prize", "op": "gte", "value": 500000}],
                       tier=2, priority=1, name="Second")
        found = rule_contradictions(self._dicts(first, second), default_tier=3,
                                    rate_map=self.rate_map)
        unreachable = [c for c in found if c["kind"] == "unreachable_rule"]
        self.assertEqual(len(unreachable), 1)
        self.assertIn("'Second'", unreachable[0]["message"])
        self.assertIn("'First'", unreachable[0]["message"])

    def test_it_is_detected_across_currencies(self):
        """"prize >= 100,000 naira" shadows "prize >= $1,000" ( = 1,364,220 naira).

        Compared as bare numbers, 100000 vs 1000, the check would have concluded the opposite.
        """
        naira = _rule([{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}],
                      tier=1, priority=0, name="Naira bar")
        dollars = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}],
                        tier=2, priority=1, name="Dollar bar")
        found = rule_contradictions(self._dicts(naira, dollars), default_tier=3,
                                    rate_map=self.rate_map)
        unreachable = [c for c in found if c["kind"] == "unreachable_rule"]
        self.assertEqual(len(unreachable), 1)
        self.assertIn("'Dollar bar'", unreachable[0]["message"])

    def test_the_reverse_order_is_not_falsely_flagged(self):
        """$1,000 first, then 100,000 naira. The naira rule is LOWER, so it is still reachable and
        nothing should be reported. A bare-number comparison would have flagged it."""
        dollars = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}],
                        tier=1, priority=0, name="Dollar bar")
        naira = _rule([{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}],
                      tier=2, priority=1, name="Naira bar")
        found = rule_contradictions(self._dicts(dollars, naira), default_tier=3,
                                    rate_map=self.rate_map)
        self.assertEqual([c for c in found if c["kind"] == "unreachable_rule"], [])

    def test_entries_quote_the_rule_as_the_admin_wrote_it(self):
        first = _rule([{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}],
                      tier=1, priority=0, name="First")
        second = _rule([{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}],
                       tier=2, priority=1, name="Second")
        found = rule_contradictions(self._dicts(first, second), default_tier=3,
                                    rate_map=self.rate_map)
        entry = next(e for c in found if c["kind"] == "unreachable_rule"
                     for e in c["entries"] if e["role"] == "unreachable")
        # The stored 1000 USD, not the converted 1,364,220 naira.
        self.assertEqual(entry["conditions"],
                         [{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}])


# ═════════════════════════ the admin surface ═════════════════════════
class ApiTests(TestCase):
    """Save a rule in a non-naira currency, read it back, and preview it, through the real API."""

    def setUp(self):
        self.admin, self.token = _user_with_role("api_head", "head_admin")
        EventTierConfig.objects.get_or_create(id=1, defaults={"default_tier": 3})
        _seed_fx()

    def test_a_saved_usd_rule_reads_back_with_its_currency_and_naira_equivalent(self):
        created = self.client.post(
            reverse("rankings_event_tier_rules"),
            {"name": "Dollar major", "match": "all",
             "conditions": [{"field": "prize", "op": "gte", "value": 1000, "currency": "USD"}],
             "tier": 1, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        self.assertEqual(created.status_code, 201)

        listing = self.client.get(reverse("rankings_event_tier_rules"),
                                  **_bearer(self.token)).json()
        condition = listing["results"][0]["conditions"][0]
        self.assertEqual(condition["currency"], "USD")
        self.assertEqual(condition["value"], 1000)
        # The naira figure the classifier will compare against, so the page can print both.
        self.assertEqual(condition["value_ngn"], int(Decimal("1000") * USD_NGN))
        self.assertEqual(listing["base_currency"], "NGN")
        self.assertIn("exchange rate", listing["fx_note"])

    def test_a_legacy_rule_reads_back_as_naira_without_being_rewritten(self):
        """The API spells the currency out; the stored row is left exactly as it was."""
        rule = _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1)
        listing = self.client.get(reverse("rankings_event_tier_rules"),
                                  **_bearer(self.token)).json()
        condition = listing["results"][0]["conditions"][0]
        self.assertEqual(condition["currency"], "NGN")
        self.assertEqual(condition["value_ngn"], 100000)
        rule.refresh_from_db()
        self.assertEqual(rule.conditions, [{"field": "prize", "op": "gte", "value": 100000}])

    def _classify(self, body):
        return self.client.post(
            reverse("rankings_event_tier_rules_classify"), body,
            content_type="application/json", **_bearer(self.token),
        )

    def test_the_preview_defaults_to_naira_exactly_as_before(self):
        _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1)
        body = self._classify({"prize": 150000, "teams": 16, "players": 0,
                               "format": "virtual"}).json()
        self.assertEqual(body["tier"], 1)
        self.assertEqual(body["prize_ngn"], 150000)
        self.assertFalse(body["prize_converted"])

    def test_the_preview_accepts_a_pool_in_another_currency(self):
        _rule([{"field": "prize", "op": "gte", "value": 100000}], tier=1)
        body = self._classify({"prize": 400, "prize_currency": "USD", "teams": 16,
                               "players": 0, "format": "virtual"}).json()
        # $400 = 545,688 naira, over the 100,000 bar.
        self.assertEqual(body["prize_ngn"], int(Decimal("400") * USD_NGN))
        self.assertTrue(body["prize_converted"])
        self.assertEqual(body["tier"], 1)

    def test_the_preview_refuses_an_unknown_currency(self):
        self.assertEqual(
            self._classify({"prize": 400, "prize_currency": "XXX"}).status_code, 400)
