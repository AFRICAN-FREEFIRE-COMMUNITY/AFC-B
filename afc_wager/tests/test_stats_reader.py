"""Tests for stats_reader auto-graders.

The graders read tournament data structures that may evolve. These tests
exercise the registry + the resolution path for missing/ambiguous data.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Match,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentPlayerMatchStats,
)
from afc_wager.adapters.stats_reader import (
    GRADERS,
    grade_market,
)
from afc_wager.models import (
    Market,
    MarketOption,
    MarketStatus,
    MarketTemplate,
    OptionSource,
)


User = get_user_model()


class StatsReaderTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            email="a@x.com",
            password="x",
            full_name="A",
            country="NG",
        )

        self.event = Event.objects.create(
            event_name="Test Event",
            slug="test-event-stats",
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=12,
            event_mode="virtual",
            start_date=timezone.now().date(),
            end_date=(timezone.now() + timedelta(days=7)).date(),
            registration_open_date=timezone.now().date(),
            registration_end_date=(timezone.now() + timedelta(days=1)).date(),
            prizepool="N1M",
            event_rules="-",
            event_status="upcoming",
            registration_link="https://example.com/r",
            number_of_stages=1,
        )

        self.team_a = Team.objects.create(
            team_name="Team A",
            team_tag="TA",
            join_settings="open",
            team_creator=self.admin,
            team_owner=self.admin,
            country="NG",
        )
        self.team_b = Team.objects.create(
            team_name="Team B",
            team_tag="TB",
            join_settings="open",
            team_creator=self.admin,
            team_owner=self.admin,
            country="NG",
        )

        self.tt_a = TournamentTeam.objects.create(
            event=self.event, team=self.team_a
        )
        self.tt_b = TournamentTeam.objects.create(
            event=self.event, team=self.team_b
        )

        self.match = Match.objects.create(
            match_number=1, match_map="bermuda"
        )

        self.template_winner = MarketTemplate.objects.create(
            code="match_winner",
            display_name="Match Winner",
            option_source=OptionSource.TEAMS,
            auto_gradable=True,
            grader_key="match_winner",
        )
        self.template_kills = MarketTemplate.objects.create(
            code="most_kills",
            display_name="Most Kills",
            option_source=OptionSource.TEAMS,
            auto_gradable=True,
            grader_key="most_kills",
        )
        self.template_custom = MarketTemplate.objects.create(
            code="custom",
            display_name="Custom",
            option_source=OptionSource.FREEFORM,
            auto_gradable=False,
            grader_key=None,
        )

        self.market_winner = Market.objects.create(
            event=self.event,
            match=self.match,
            template=self.template_winner,
            title="Match 1 Winner",
            description="",
            status=MarketStatus.PENDING_SETTLEMENT,
            opens_at=timezone.now(),
            lock_at=timezone.now() + timedelta(hours=1),
            created_by_admin=self.admin,
        )
        self.opt_a = MarketOption.objects.create(
            market=self.market_winner,
            label="Team A",
            ref_team_id=self.team_a.team_id,
            sort_order=0,
        )
        self.opt_b = MarketOption.objects.create(
            market=self.market_winner,
            label="Team B",
            ref_team_id=self.team_b.team_id,
            sort_order=1,
        )

    # ── Registry shape ────────────────────────────────────────────────

    def test_graders_registry_has_all_8_presets_plus_custom(self):
        expected = {
            "match_winner",
            "first_blood",
            "mvp",
            "most_kills",
            "most_damage",
            "top_3",
            "booyah_count",
            "survival_time",
            "custom",
        }
        self.assertEqual(set(GRADERS.keys()), expected)

    # ── match_winner ──────────────────────────────────────────────────

    def test_match_winner_returns_none_when_no_placements_recorded(self):
        opt_id, conf = grade_market(self.market_winner)
        self.assertIsNone(opt_id)
        self.assertIsNone(conf)

    def test_match_winner_picks_team_with_placement_1(self):
        TournamentTeamMatchStats.objects.create(
            match=self.match,
            tournament_team=self.tt_a,
            placement=1,
        )
        TournamentTeamMatchStats.objects.create(
            match=self.match,
            tournament_team=self.tt_b,
            placement=2,
        )
        opt_id, conf = grade_market(self.market_winner)
        self.assertEqual(opt_id, self.opt_a.pk)
        self.assertEqual(conf, "high")

    # ── most_kills ────────────────────────────────────────────────────

    def test_most_kills_picks_team_with_unique_max(self):
        # Switch market template to most_kills.
        self.market_winner.template = self.template_kills
        self.market_winner.save()
        # Team A: 10 kills, Team B: 5 kills.
        tts_a = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tt_a, placement=1, kills=10
        )
        tts_b = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tt_b, placement=2, kills=5
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=tts_a, player=self.admin, kills=10
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=tts_b, player=self.admin, kills=5
        )
        opt_id, conf = grade_market(self.market_winner)
        self.assertEqual(opt_id, self.opt_a.pk)
        self.assertEqual(conf, "high")

    def test_most_kills_returns_none_on_tie(self):
        self.market_winner.template = self.template_kills
        self.market_winner.save()
        tts_a = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tt_a, placement=1, kills=10
        )
        tts_b = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tt_b, placement=2, kills=10
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=tts_a, player=self.admin, kills=10
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=tts_b, player=self.admin, kills=10
        )
        opt_id, conf = grade_market(self.market_winner)
        self.assertIsNone(opt_id)
        self.assertIsNone(conf)

    # ── custom ────────────────────────────────────────────────────────

    def test_custom_template_returns_none(self):
        self.market_winner.template = self.template_custom
        self.market_winner.save()
        self.market_winner.refresh_from_db()
        opt_id, conf = grade_market(self.market_winner)
        self.assertIsNone(opt_id)
        self.assertIsNone(conf)

    # ── Missing match ─────────────────────────────────────────────────

    def test_market_with_no_match_returns_none(self):
        self.market_winner.match = None
        self.market_winner.save()
        opt_id, conf = grade_market(self.market_winner)
        self.assertIsNone(opt_id)

    # ── Resilience: broken grader doesn't propagate ─────────────────

    def test_grade_market_swallows_unexpected_exceptions(self):
        # Even if a grader internally explodes, grade_market returns None.
        # We can't easily inject one, so assert the contract by faking the
        # GRADERS lookup.
        from afc_wager.adapters import stats_reader

        original = stats_reader.GRADERS.get("match_winner")

        def boom(market):
            raise RuntimeError("boom")

        stats_reader.GRADERS["match_winner"] = boom
        try:
            opt_id, conf = grade_market(self.market_winner)
            self.assertIsNone(opt_id)
            self.assertIsNone(conf)
        finally:
            stats_reader.GRADERS["match_winner"] = original
