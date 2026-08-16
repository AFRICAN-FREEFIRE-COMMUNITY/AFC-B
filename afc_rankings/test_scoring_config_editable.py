"""
afc_rankings.test_scoring_config_editable - the admin-editable scoring and tiering config.

Built to WEBSITE/tasks/ranking-config-editable-plan.md. Each class pins one of the owner's
decisions, so a future change to any of them is a deliberate, visible edit rather than a
silent regression:

  * ValidationTests            - a config that would corrupt scoring is REFUSED, not warned about.
  * ContradictionTests         - the owner's own example: two rules both reading "above 100,000"
                                 means the second can never fire. Reported, never blocking.
  * SeasonScopeTests           - a change is not retroactive. The CURRENT season is recalculated;
                                 a closed season keeps the rules it was scored under, even when
                                 something else forces it to recalculate later. Choosing that
                                 season explicitly does rewrite it.
  * PublishedSeasonGuardTests  - rewriting a published season is possible but never accidental.
  * PermissionTests            - head admin only.
  * RetireTests                - a rule past events were classified under is retired, never deleted.
  * AuditTests                 - the audit entry records which seasons the change was applied to.
  * ConfigReachesScoringTests  - the saved numbers actually change what a team scores, and each
                                 season is scored under its own version.

HOW IT CONNECTS
    Drives afc_rankings.admin_scoring_config and afc_rankings.admin_tournament_tiers through the
    Django test client with the house Bearer-token idiom, and reads the results back out of the
    score models the public ranking pages render (TeamQuarterlyScore). The pure validation half
    (afc_rankings.scoring.validation) is exercised directly, without a database, because it is
    Django-free by design.
"""
import copy
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, UserRoles
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam, TournamentTeamMatchStats,
)
from afc_rankings import recalc
from afc_rankings.models import (
    EventTierRule, RankingAuditLog, ScoringConfig, Season, SeasonScoringConfig,
    TeamQuarterlyScore,
)
from afc_rankings.scoring.tables import defaults_config
from afc_rankings.scoring.validation import rule_contradictions, validate_config

User = get_user_model()

REASON = "editable scoring config test"   # >= the 10-char audit-reason minimum


# ───────────────────────── shared fixture helpers ─────────────────────────
def _token(user, label):
    """A live SessionToken string (the house Bearer idiom, same as test_ghost_claims)."""
    return SessionToken.objects.create(user=user, token=f"tok_{label}").token


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _user_with_role(username, role_name):
    user = User.objects.create(username=username, email=f"{username}@example.com")
    role, _ = Roles.objects.get_or_create(role_name=role_name)
    UserRoles.objects.create(user=user, role=role)
    return user, _token(user, username)


def _season(name, year, quarter, start, end, *, active=False, published=False):
    return Season.objects.create(
        name=name, year=year, quarter=quarter, start_date=start, end_date=end,
        transfer_window_open=start, transfer_window_close=start + datetime.timedelta(days=7),
        is_active=active, rankings_published=published,
    )


def _current_season_dates():
    """A season range that contains today, so it is genuinely the current one.

    Season.auto_rollover_seasons activates whichever season the calendar is inside, so a
    hardcoded range would be silently deactivated (or activate a different one) depending on
    the day the suite runs. Deriving it from today keeps the test honest all year.
    """
    today = timezone.localdate()
    quarter = (today.month - 1) // 3 + 1
    start = datetime.date(today.year, 3 * (quarter - 1) + 1, 1)
    end = (datetime.date(today.year + (quarter == 4), (3 * quarter) % 12 + 1, 1)
           - datetime.timedelta(days=1))
    return quarter, start, end


class _ScoredFixture(TestCase):
    """One team with a real tournament result in a CLOSED season and in the CURRENT season.

    Both seasons are scored before each test, so an assertion that a season did or did not
    change is comparing two real numbers rather than the presence of a row.
    """

    def setUp(self):
        self.admin, self.admin_token = _user_with_role("cfg_head", "head_admin")
        self.team = Team.objects.create(
            team_name="Config FC", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )

        # A season that is over. 2024 is safely in the past whenever this suite runs.
        self.closed = _season("Season 1 2024", 2024, 1,
                              datetime.date(2024, 1, 1), datetime.date(2024, 3, 31))
        self.closed_play_day = datetime.date(2024, 2, 15)

        quarter, start, end = _current_season_dates()
        self.current = _season(f"Season {quarter} {start.year}", start.year, quarter,
                               start, end, active=True)
        self.current_play_day = start + datetime.timedelta(days=14)

        self._result(self.closed_play_day, "Old Cup")
        self._result(self.current_play_day, "New Cup")

        recalc.recalc_season(self.closed)
        recalc.recalc_season(self.current)

    def _result(self, play_day, event_name):
        """One completed tier_3 match where the team finishes 1st with 10 kills."""
        event = Event.objects.create(
            event_name=event_name, competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=play_day, end_date=play_day,
            registration_open_date=play_day - datetime.timedelta(days=5),
            registration_end_date=play_day - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.admin, is_draft=False,
        )
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=play_day, end_date=play_day,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=play_day,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=play_day,
        )
        tt = TournamentTeam.objects.create(
            event=event, team=self.team, registered_by=self.admin, status="active",
        )
        TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=10,
        )
        return event

    # ── reading the scores back ──
    def _score(self, season):
        row = TeamQuarterlyScore.objects.filter(team=self.team, season=season).first()
        return row.total_score if row else None

    def _tournament_pts(self, season):
        """The component the tier multiplier actually scales.

        A quarterly total also carries the prize-money and social-media bands, and both award
        their lowest band at zero, so the total is not a clean multiple of the tournament
        points. Asserting on the component keeps the arithmetic in these tests obvious.
        """
        row = TeamQuarterlyScore.objects.filter(team=self.team, season=season).first()
        return row.tournament_pts if row else None

    # ── the change under test: a tier_3 result becomes worth five times as much ──
    def _boosted_config(self):
        config = copy.deepcopy(defaults_config())
        for tier in config["tiers"]:
            if tier["key"] == "tier_3":
                tier["multiplier"] = 5.0
        return config

    def _save(self, config=None, **body):
        payload = {"config": config or self._boosted_config(), "reason": REASON}
        payload.update(body)
        return self.client.post(
            reverse("rankings_scoring_config"), payload,
            content_type="application/json", **_bearer(self.admin_token),
        )


# ═════════════════════════ validation: refuse, do not warn ═════════════════════════
class ValidationTests(TestCase):
    """A bad config silently mis-scores every team on the site, so it is refused outright."""

    def setUp(self):
        self.admin, self.token = _user_with_role("val_head", "head_admin")

    def _post(self, config):
        return self.client.post(
            reverse("rankings_scoring_config"),
            {"config": config, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )

    def _codes(self, response):
        return {e["code"] for e in response.json()["errors"]}

    def test_a_compression_table_with_no_open_top_band_is_refused(self):
        """Without an open top band, a team above the last threshold scores nothing at all."""
        config = copy.deepcopy(defaults_config())
        config["kill_compression"][-1]["max"] = 9999
        response = self._post(config)
        self.assertEqual(response.status_code, 400)
        self.assertIn("no_open_top_band", self._codes(response))

    def test_a_negative_multiplier_is_refused(self):
        """A negative multiplier makes a better result score worse."""
        config = copy.deepcopy(defaults_config())
        config["tiers"][0]["multiplier"] = -1.0
        response = self._post(config)
        self.assertEqual(response.status_code, 400)
        self.assertIn("out_of_range", self._codes(response))

    def test_a_zero_multiplier_is_refused(self):
        """Zero silently voids every result at that tier, which looks like missing data."""
        config = copy.deepcopy(defaults_config())
        config["tiers"][0]["multiplier"] = 0
        self.assertEqual(self._post(config).status_code, 400)

    def test_a_cutoff_no_team_could_ever_reach_is_refused(self):
        """A floor above any achievable score would pin every team to the default tier."""
        config = copy.deepcopy(defaults_config())
        config["tier_thresholds"]["brackets"] = [{"min": 1_000_000, "tier": 0}]
        response = self._post(config)
        self.assertEqual(response.status_code, 400)
        self.assertIn("unreachable_scale", self._codes(response))

    def test_a_misspelled_setting_is_refused_rather_than_ignored(self):
        """The worst outcome is a save that appears to work and changes nothing."""
        config = copy.deepcopy(defaults_config())
        config["scrimm"] = {"weight": 0.5}
        response = self._post(config)
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown_key", self._codes(response))

    def test_a_refused_save_writes_nothing(self):
        config = copy.deepcopy(defaults_config())
        config["tiers"][0]["multiplier"] = -1.0
        self._post(config)
        self.assertEqual(ScoringConfig.objects.count(), 0)
        self.assertEqual(RankingAuditLog.objects.count(), 0)

    def test_the_shipped_defaults_are_valid(self):
        """The factory reset must always be saveable, or the editor has no safe fallback."""
        self.assertEqual(validate_config(defaults_config())["errors"], [])

    def test_validate_endpoint_reports_without_writing(self):
        config = copy.deepcopy(defaults_config())
        config["kill_compression"][-1]["max"] = 9999
        response = self.client.post(
            reverse("rankings_scoring_config_validate"),
            {"config": config, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["errors"])
        self.assertEqual(ScoringConfig.objects.count(), 0)


# ═════════════════════════ contradictions: report, do not block ═════════════════════════
class ContradictionTests(TestCase):
    """The owner's example: two rules that both read "above 100,000"."""

    def setUp(self):
        self.admin, self.token = _user_with_role("con_head", "head_admin")

    def _rule(self, rule_id, priority, threshold, tier, name):
        return {
            "id": rule_id, "name": name, "priority": priority, "match": "all",
            "conditions": [{"field": "prize", "op": "gte", "value": threshold}],
            "tier": tier, "enabled": True, "retired": False,
        }

    def test_a_second_rule_with_the_same_threshold_can_never_fire(self):
        found = rule_contradictions(
            [self._rule(1, 0, 100_000, 1, "Big money"),
             self._rule(2, 1, 100_000, 2, "Also big money")],
            default_tier=3,
        )
        kinds = [c["kind"] for c in found]
        self.assertIn("unreachable_rule", kinds)
        report = next(c for c in found if c["kind"] == "unreachable_rule")
        # Both offending entries are named, so the admin knows which rule to move or edit.
        roles = {e["role"]: e for e in report["entries"]}
        self.assertEqual(roles["unreachable"]["id"], 2)
        self.assertEqual(roles["shadowed_by"]["id"], 1)

    def test_a_stricter_rule_below_a_looser_one_can_never_fire(self):
        """Anything above 500,000 already matched the 100,000 rule sitting above it."""
        found = rule_contradictions(
            [self._rule(1, 0, 100_000, 3, "Any prize"),
             self._rule(2, 1, 500_000, 1, "Major")],
            default_tier=3,
        )
        self.assertIn("unreachable_rule", [c["kind"] for c in found])

    def test_the_same_rules_in_the_right_order_are_not_flagged(self):
        """Highest threshold first is the correct way to write these, and must stay silent."""
        found = rule_contradictions(
            [self._rule(1, 0, 500_000, 1, "Major"),
             self._rule(2, 1, 100_000, 3, "Any prize")],
            default_tier=3,
        )
        self.assertEqual([c["kind"] for c in found if c["kind"] == "unreachable_rule"], [])

    def test_a_range_no_rule_covers_is_reported(self):
        found = rule_contradictions(
            [self._rule(1, 0, 500_000, 1, "Major"),
             {"id": 2, "name": "Small", "priority": 1, "match": "all",
              "conditions": [{"field": "prize", "op": "lte", "value": 100_000}],
              "tier": 3, "enabled": True, "retired": False}],
            default_tier=3,
        )
        gaps = [c for c in found if c["kind"] == "uncovered_range"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["entries"][0]["currency"], "NGN")

    def test_a_retired_rule_is_not_considered(self):
        rules = [self._rule(1, 0, 100_000, 1, "Big money"),
                 self._rule(2, 1, 100_000, 2, "Also big money")]
        rules[0]["retired"] = True
        self.assertEqual(
            [c for c in rule_contradictions(rules, default_tier=3)
             if c["kind"] == "unreachable_rule"], [],
        )

    def test_the_rules_endpoint_reports_the_contradiction(self):
        """End to end: the admin page gets told, and both rules are still saved."""
        for name, tier in (("Big money", 1), ("Also big money", 2)):
            response = self.client.post(
                reverse("rankings_event_tier_rules"),
                {"name": name, "match": "all",
                 "conditions": [{"field": "prize", "op": "gte", "value": 100000}],
                 "tier": tier, "reason": REASON},
                content_type="application/json", **_bearer(self.token),
            )
            self.assertEqual(response.status_code, 201)

        listing = self.client.get(reverse("rankings_event_tier_rules"), **_bearer(self.token))
        body = listing.json()
        self.assertEqual(len(body["results"]), 2, "both rules are saved, not rejected")
        kinds = [c["kind"] for c in body["contradictions"]]
        self.assertIn("unreachable_rule", kinds)
        message = next(c["message"] for c in body["contradictions"]
                       if c["kind"] == "unreachable_rule")
        self.assertIn("Also big money", message)

    def test_the_prize_threshold_currency_is_stated(self):
        """A naira threshold rendered as a bare number is what mis-tiered a $400 event."""
        self.client.post(
            reverse("rankings_event_tier_rules"),
            {"name": "Big money", "match": "all",
             "conditions": [{"field": "prize", "op": "gte", "value": 100000}],
             "tier": 1, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        body = self.client.get(reverse("rankings_event_tier_rules"),
                               **_bearer(self.token)).json()
        self.assertEqual(body["field_meta"]["event_tier_rule_prize"]["currency"], "NGN")
        self.assertEqual(body["results"][0]["condition_currency"]["prize"], "NGN")

    def test_the_scoring_config_states_the_currency_of_money_thresholds(self):
        body = self.client.get(reverse("rankings_scoring_config"),
                               **_bearer(self.token)).json()
        self.assertEqual(body["field_meta"]["prize_money_points"]["currency"], "NGN")
        # A non-money scale must say so explicitly rather than leaving the UI to guess.
        self.assertIsNone(body["field_meta"]["social_media_points"]["currency"])


# ═════════════════ a condition INSIDE a rule that can never decide anything ═════════════════
# Owner, 2026-08-16, looking at a live rule reading "prize >= 1,000,000 naira OR prize >= 1,000
# USD": the dollar line converts to about 1,358,704 naira, so every event clearing it had already
# cleared the naira line. The rule classified correctly and nothing on screen said the dollar line
# was doing nothing. The checks above look for dead RULES; these look inside one.
class RedundantConditionTests(TestCase):
    RATES = {"NGN": 1358.704, "USD": 1.0}

    def _any_rule(self, conditions):
        return {"id": 1, "name": "Tier 1 money", "priority": 0, "match": "any",
                "conditions": conditions, "tier": 1, "enabled": True, "retired": False}

    def test_on_match_any_the_STRICTER_branch_is_the_dead_one(self):
        """It catches nothing the looser branch has not already caught."""
        found = rule_contradictions([self._any_rule([
            {"field": "prize", "op": "gte", "value": 1_000_000, "currency": "NGN"},
            {"field": "prize", "op": "gte", "value": 1_000, "currency": "USD"},
        ])], rate_map=self.RATES)
        report = next(c for c in found if c["kind"] == "redundant_condition")
        # The DOLLAR line is the dead one. Naming the naira line instead would send the admin to
        # edit the condition that is doing all the work.
        self.assertEqual(report["entries"][0]["condition"]["currency"], "USD")
        self.assertEqual(report["entries"][0]["covered_by"]["currency"], "NGN")

    def test_the_message_quotes_what_the_admin_TYPED_not_the_converted_number(self):
        found = rule_contradictions([self._any_rule([
            {"field": "prize", "op": "gte", "value": 1_000_000, "currency": "NGN"},
            {"field": "prize", "op": "gte", "value": 1_000, "currency": "USD"},
        ])], rate_map=self.RATES)
        message = next(c for c in found if c["kind"] == "redundant_condition")["message"]
        self.assertIn("1,000 USD", message)
        # 1,358,704 is an implementation detail of the comparison, not a number on their screen.
        self.assertNotIn("1,358,704", message)

    def test_on_match_all_the_LOOSER_condition_is_the_dead_one(self):
        """The stricter condition has already excluded everything the looser one would have."""
        found = rule_contradictions([{
            "id": 7, "name": "Tier 1", "priority": 0, "match": "all",
            "conditions": [{"field": "prize", "op": "gte", "value": 500_000},
                           {"field": "prize", "op": "gte", "value": 100_000}],
            "tier": 1, "enabled": True, "retired": False,
        }], rate_map=self.RATES)
        report = next(c for c in found if c["kind"] == "redundant_condition")
        self.assertEqual(report["entries"][0]["condition"]["value"], 100_000)
        self.assertEqual(report["entries"][0]["covered_by"]["value"], 500_000)

    def test_two_conditions_on_DIFFERENT_fields_are_never_redundant(self):
        """Prize and format constrain different things, so neither covers the other. This is the
        case that must stay silent: warning here would train an admin to ignore the panel."""
        for mode in ("all", "any"):
            found = rule_contradictions([{
                "id": 9, "name": "Tier 1", "priority": 0, "match": mode,
                "conditions": [{"field": "prize", "op": "gte", "value": 500_000},
                               {"field": "format", "op": "eq", "value": "physical"}],
                "tier": 1, "enabled": True, "retired": False,
            }], rate_map=self.RATES)
            self.assertEqual(
                [c for c in found if c["kind"] == "redundant_condition"], [],
                f"match {mode} should not report a redundant condition")

    def test_a_single_condition_rule_is_never_reported(self):
        found = rule_contradictions([{
            "id": 11, "name": "Tier 1", "priority": 0, "match": "all",
            "conditions": [{"field": "prize", "op": "gte", "value": 500_000}],
            "tier": 1, "enabled": True, "retired": False,
        }], rate_map=self.RATES)
        self.assertEqual([c for c in found if c["kind"] == "redundant_condition"], [])


# ═════════════════════════ season scope: the owner's central decision ═════════════════════════
class SeasonScopeTests(_ScoredFixture):
    def test_a_change_does_not_alter_a_closed_season(self):
        before = self._score(self.closed)
        self.assertIsNotNone(before)

        response = self._save()
        self.assertEqual(response.status_code, 201, response.content)

        self.assertEqual(self._score(self.closed), before)

    def test_a_closed_season_stays_on_its_own_rules_even_when_recalculated_later(self):
        """The real test of 'frozen'. Skipping the season would prove nothing on its own:
        any later recalculation - a result correction, a nightly sweep, Run evaluation -
        must also reproduce the old numbers."""
        before = self._score(self.closed)
        self._save()

        recalc.recalc_season(self.closed)   # force it, the way an unrelated edit would

        self.assertEqual(self._score(self.closed), before)

    def test_a_change_recalculates_the_current_season(self):
        """Never half old rules and half new."""
        before = self._score(self.current)
        response = self._save()
        self.assertEqual(response.status_code, 201, response.content)

        after = self._score(self.current)
        self.assertNotEqual(after, before)
        self.assertGreater(after, before)   # tier_3 is now worth five times as much
        self.assertGreaterEqual(response.json()["recalculated"]["seasons"], 1)

    def test_choosing_a_closed_season_explicitly_does_rewrite_it(self):
        before = self._score(self.closed)
        response = self._save(apply_to_seasons=[self.closed.season_id],
                              acknowledge_published=True)
        self.assertEqual(response.status_code, 201, response.content)

        after = self._score(self.closed)
        self.assertNotEqual(after, before)
        self.assertGreater(after, before)

    def test_the_response_names_every_affected_season_with_its_flags(self):
        response = self._save(apply_to_seasons=[self.closed.season_id],
                              acknowledge_published=True)
        rows = {r["season_id"]: r for r in response.json()["applied_seasons"]}
        self.assertEqual(set(rows), {self.closed.season_id, self.current.season_id})
        self.assertTrue(rows[self.closed.season_id]["is_closed"])
        self.assertFalse(rows[self.closed.season_id]["in_default_scope"])
        self.assertTrue(rows[self.current.season_id]["in_default_scope"])
        # Every row states whether the standings are public, so the UI never has to guess.
        for row in rows.values():
            self.assertIn("rankings_published", row)

    def test_each_season_is_pinned_to_the_version_that_governs_it(self):
        self._save()
        closed_pin = SeasonScoringConfig.objects.get(season=self.closed)
        current_pin = SeasonScoringConfig.objects.get(season=self.current)
        # The closed season was frozen at what was in force before the save (nothing saved
        # yet, so the shipped defaults); the current season moved to the new version.
        self.assertIsNone(closed_pin.config)
        self.assertEqual(closed_pin.origin, SeasonScoringConfig.FROZEN)
        self.assertEqual(current_pin.config.version, 1)
        self.assertEqual(current_pin.origin, SeasonScoringConfig.APPLIED)

    def test_every_save_creates_a_new_version_rather_than_editing_the_active_one(self):
        self._save()
        second = copy.deepcopy(self._boosted_config())
        second["finals_base"] = 9
        self._save(config=second)

        versions = list(ScoringConfig.objects.order_by("version")
                        .values_list("version", "is_active"))
        self.assertEqual(versions, [(1, False), (2, True)])
        # The superseded version is still readable in full - that is what frozen history means.
        first = ScoringConfig.objects.get(version=1)
        self.assertEqual(first.config["finals_base"], defaults_config()["finals_base"])

    def test_the_seasons_endpoint_marks_the_default_scope(self):
        body = self.client.get(reverse("rankings_scoring_config_seasons"),
                               **_bearer(self.admin_token)).json()
        rows = {r["season_id"]: r for r in body["results"]}
        self.assertTrue(rows[self.current.season_id]["in_default_scope"])
        self.assertFalse(rows[self.closed.season_id]["in_default_scope"])
        self.assertTrue(rows[self.closed.season_id]["is_closed"])
        self.assertEqual(body["current_season_id"], self.current.season_id)


# ═════════════════════════ the published-season guard ═════════════════════════
class PublishedSeasonGuardTests(_ScoredFixture):
    def setUp(self):
        super().setUp()
        self.closed.rankings_published = True
        self.closed.save(update_fields=["rankings_published"])

    def test_rewriting_a_published_season_needs_an_explicit_acknowledgement(self):
        response = self._save(apply_to_seasons=[self.closed.season_id])
        self.assertEqual(response.status_code, 409)
        body = response.json()
        # The refusal names the season rather than saying "some seasons are published".
        self.assertIn(self.closed.name, body["message"])
        self.assertIn(self.closed.season_id, body["impact"]["published_seasons"])
        self.assertEqual(ScoringConfig.objects.count(), 0, "nothing was written")

    def test_acknowledging_lets_it_through(self):
        response = self._save(apply_to_seasons=[self.closed.season_id],
                              acknowledge_published=True)
        self.assertEqual(response.status_code, 201, response.content)

    def test_the_current_season_never_needs_an_acknowledgement(self):
        """Recalculating the season in progress is the agreed default; requiring a
        confirmation for it would put a warning in front of the safe path."""
        self.current.rankings_published = True
        self.current.save(update_fields=["rankings_published"])
        self.assertEqual(self._save().status_code, 201)

    def test_validate_previews_the_same_warning_without_writing(self):
        response = self.client.post(
            reverse("rankings_scoring_config_validate"),
            {"config": self._boosted_config(),
             "apply_to_seasons": [self.closed.season_id], "reason": REASON},
            content_type="application/json", **_bearer(self.admin_token),
        )
        body = response.json()
        self.assertTrue(body["impact"]["requires_acknowledgement"])
        self.assertIn(self.closed.season_id, body["impact"]["published_seasons"])
        self.assertEqual(ScoringConfig.objects.count(), 0)


# ═════════════════════════ permissions ═════════════════════════
class PermissionTests(TestCase):
    """Head admin only: these controls decide every team's rank."""

    def setUp(self):
        _, self.head_token = _user_with_role("perm_head", "head_admin")
        _, self.metrics_token = _user_with_role("perm_metrics", "metrics_admin")
        plain = User.objects.create(username="perm_plain", email="pp@example.com")
        self.plain_token = _token(plain, "perm_plain")

    def _save(self, token):
        return self.client.post(
            reverse("rankings_scoring_config"),
            {"config": defaults_config(), "reason": REASON},
            content_type="application/json", **_bearer(token),
        )

    def test_a_metrics_admin_cannot_change_the_scoring_config(self):
        self.assertEqual(self._save(self.metrics_token).status_code, 403)
        self.assertEqual(ScoringConfig.objects.count(), 0)

    def test_a_player_cannot_change_the_scoring_config(self):
        self.assertEqual(self._save(self.plain_token).status_code, 403)

    def test_a_request_with_no_token_is_refused(self):
        response = self.client.post(
            reverse("rankings_scoring_config"),
            {"config": defaults_config(), "reason": REASON},
            content_type="application/json",
        )
        self.assertIn(response.status_code, (400, 401))
        self.assertEqual(ScoringConfig.objects.count(), 0)

    def test_a_head_admin_can(self):
        self.assertEqual(self._save(self.head_token).status_code, 201)

    def test_a_metrics_admin_can_still_read_the_rules_in_force(self):
        """Refusing the write must not blind the people who work with the numbers."""
        self.assertEqual(
            self.client.get(reverse("rankings_scoring_config"),
                            **_bearer(self.metrics_token)).status_code,
            200,
        )

    def test_a_metrics_admin_cannot_change_a_tier_rule(self):
        response = self.client.post(
            reverse("rankings_event_tier_rules"),
            {"name": "Nope", "conditions": [{"field": "prize", "op": "gte", "value": 1}],
             "tier": 1, "reason": REASON},
            content_type="application/json", **_bearer(self.metrics_token),
        )
        self.assertEqual(response.status_code, 403)

    def test_a_save_without_a_reason_is_refused(self):
        """Every ranking write carries a human reason - it is the body of the audit row."""
        response = self.client.post(
            reverse("rankings_scoring_config"), {"config": defaults_config()},
            content_type="application/json", **_bearer(self.head_token),
        )
        self.assertEqual(response.status_code, 400)


# ═════════════════════════ retire, never delete ═════════════════════════
class RetireTests(TestCase):
    """A rule past events were classified under must stay readable, or those events are
    orphaned: nothing explains the tier they sit in."""

    def setUp(self):
        self.admin, self.token = _user_with_role("ret_head", "head_admin")
        response = self.client.post(
            reverse("rankings_event_tier_rules"),
            {"name": "Major prize", "match": "all",
             "conditions": [{"field": "prize", "op": "gte", "value": 100000}],
             "tier": 1, "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        self.rule_id = response.json()["id"]

    def _classify(self, prize):
        return self.client.post(
            reverse("rankings_event_tier_rules_classify"),
            {"prize": prize, "teams": 16, "players": 0, "format": "virtual"},
            content_type="application/json", **_bearer(self.token),
        ).json()

    def _retire(self):
        return self.client.delete(
            reverse("rankings_event_tier_rule_detail", args=[self.rule_id]),
            {"reason": REASON}, content_type="application/json", **_bearer(self.token),
        )

    def test_the_rule_classifies_before_it_is_retired(self):
        self.assertEqual(self._classify(500000)["tier"], 1)

    def test_retiring_keeps_the_row_and_everything_on_it(self):
        self._retire()
        rule = EventTierRule.objects.filter(pk=self.rule_id).first()
        self.assertIsNotNone(rule, "the rule must not be destroyed")
        self.assertIsNotNone(rule.retired_at)
        self.assertEqual(rule.retired_by, self.admin)
        # The conditions are intact, so a past event's tier can still be explained.
        # `currency` is written by the create endpoint (2026-08-07: a prize threshold names the
        # currency it is authored in). This rule was posted without one, and an omitted currency
        # means naira, so the threshold means exactly what it did before - the write path just
        # spells it out rather than leaving the next reader to know the default.
        self.assertEqual(rule.conditions,
                         [{"field": "prize", "op": "gte", "value": 100000, "currency": "NGN"}])
        self.assertEqual(rule.name, "Major prize")

    def test_a_retired_rule_classifies_nothing_new(self):
        self._retire()
        self.assertEqual(self._classify(500000)["tier"], 3)   # falls through to the default

    def test_a_retired_rule_is_hidden_by_default_and_readable_on_request(self):
        self._retire()
        default_list = self.client.get(reverse("rankings_event_tier_rules"),
                                       **_bearer(self.token)).json()
        self.assertEqual(default_list["results"], [])

        with_retired = self.client.get(
            reverse("rankings_event_tier_rules") + "?include_retired=1",
            **_bearer(self.token),
        ).json()
        self.assertEqual(len(with_retired["results"]), 1)
        self.assertTrue(with_retired["results"][0]["retired"])
        self.assertEqual(with_retired["results"][0]["retired_by"], self.admin.username)

    def test_retiring_is_reversible(self):
        self._retire()
        response = self.client.post(
            reverse("rankings_event_tier_rule_restore", args=[self.rule_id]),
            {"reason": REASON}, content_type="application/json", **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._classify(500000)["tier"], 1)

    def test_retiring_twice_is_refused(self):
        self._retire()
        self.assertEqual(self._retire().status_code, 400)

    def test_retiring_is_audited(self):
        self._retire()
        entry = RankingAuditLog.objects.filter(object_type="event_tier",
                                               action="retire").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.changed_by, self.admin)
        self.assertFalse(entry.before_snapshot["retired"])
        self.assertTrue(entry.after_snapshot["retired"])

    def test_a_retired_tier_still_scores_the_events_that_used_it(self):
        """The same rule one level down: retiring tier_2 must not break every past tier_2
        result. Retirement removes a tier from new work, it does not delete it."""
        from afc_rankings.scoring.engine import tier_multiplier
        from afc_rankings.scoring.tables import tables_from_config

        config = copy.deepcopy(defaults_config())
        for tier in config["tiers"]:
            if tier["key"] == "tier_2":
                tier["retired"] = True
        tables = tables_from_config(config)

        self.assertEqual(tier_multiplier("tier_2", tables), 1.5)
        self.assertNotIn("tier_2", tables.active_tier_keys)


# ═════════════════════════ audit ═════════════════════════
class AuditTests(_ScoredFixture):
    def test_the_audit_entry_records_who_what_and_which_seasons(self):
        response = self._save(apply_to_seasons=[self.closed.season_id],
                              acknowledge_published=True)
        self.assertEqual(response.status_code, 201, response.content)

        entry = RankingAuditLog.objects.get(object_type="scoring_config", action="save")
        self.assertEqual(entry.changed_by, self.admin)
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(entry.before_snapshot["version"], None)     # was the defaults
        self.assertEqual(entry.after_snapshot["version"], 1)

        # "Who moved my June placement" must always have an answer.
        applied = {row["season_id"]: row for row in entry.after_snapshot["applied_seasons"]}
        self.assertEqual(set(applied), {self.closed.season_id, self.current.season_id})
        self.assertTrue(applied[self.closed.season_id]["chosen_explicitly"])
        self.assertFalse(applied[self.current.season_id]["chosen_explicitly"])
        self.assertTrue(entry.after_snapshot["acknowledged_published"])

    def test_the_audit_entry_records_the_seasons_frozen_at_the_old_rules(self):
        self._save()
        entry = RankingAuditLog.objects.get(object_type="scoring_config", action="save")
        frozen = {row["season_id"] for row in entry.after_snapshot["frozen_seasons"]}
        self.assertIn(self.closed.season_id, frozen)

    def test_the_audit_entry_carries_the_contradictions_that_were_reported(self):
        config = copy.deepcopy(self._boosted_config())
        config["tier_thresholds"]["brackets"] = [
            {"min": 150, "tier": 0}, {"min": 150, "tier": 1}, {"min": 40, "tier": 2},
        ]
        response = self._save(config=config)
        self.assertEqual(response.status_code, 201, "a contradiction never blocks a save")
        self.assertTrue(response.json()["contradictions"])
        entry = RankingAuditLog.objects.get(object_type="scoring_config", action="save")
        self.assertTrue(entry.after_snapshot["contradictions"])


# ═════════════════════════ the config actually reaches the engine ═════════════════════════
class ConfigReachesScoringTests(_ScoredFixture):
    def test_the_saved_numbers_change_what_a_team_scores(self):
        before = self._tournament_pts(self.current)
        total_before = self._score(self.current)
        self._save()
        # tier_3 went from 1.0x to 5.0x, and it multiplies the tournament component.
        self.assertAlmostEqual(self._tournament_pts(self.current), before * 5, places=4)
        self.assertGreater(self._score(self.current), total_before)

    def test_two_seasons_can_be_scored_under_two_different_versions(self):
        """The point of pinning: history and the present can legitimately disagree."""
        closed_before = self._tournament_pts(self.closed)
        self._save()

        recalc.recalc_season(self.closed)
        recalc.recalc_season(self.current)

        self.assertEqual(self._tournament_pts(self.closed), closed_before)
        self.assertAlmostEqual(self._tournament_pts(self.current), closed_before * 5, places=4)

    def test_a_version_can_be_read_back_in_full(self):
        self._save()
        body = self.client.get(
            reverse("rankings_scoring_config_version", args=[1]),
            **_bearer(self.admin_token),
        ).json()
        self.assertEqual(body["version"], 1)
        multipliers = {t["key"]: t["multiplier"] for t in body["config"]["tiers"]}
        self.assertEqual(multipliers["tier_3"], 5.0)
        self.assertIn(self.current.season_id, [s["season_id"] for s in body["seasons"]])
