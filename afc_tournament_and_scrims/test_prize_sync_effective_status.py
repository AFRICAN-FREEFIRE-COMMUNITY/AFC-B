# afc_tournament_and_scrims/test_prize_sync_effective_status.py
# ──────────────────────────────────────────────────────────────────────────────
# Regression tests for the prize auto-sync sweep (prize_sync.sync_completed_events).
#
# BUG (owner 2026-07-14 — DYNASTY CUP GRAND FINALS SSA): the sweep gated on the RAW
# Event.event_status == "completed". The ongoing->completed auto-complete beat is not
# scheduled live, so a finished event can sit at raw status "ongoing"/"upcoming" while
# effective_event_status() (read-time derivation, used by every badge/leaderboard) already
# reads "completed". Those events were skipped -> their prize pool never attributed to the
# winners, and the team/player earnings panel showed "-".
#
# FIX: the sweep now selects EFFECTIVELY-completed events (raw-completed OR past end_date,
# confirmed via effective_event_status). These tests lock that in:
#   1. raw "ongoing" + past end instant  -> payouts ARE created (the bug).
#   2. raw "upcoming" + FUTURE end        -> payouts are NOT created (control).
#   3. raw "completed"                    -> still created (original path preserved).
#
# The sweep needs FINAL standings, so each fixture builds the minimal object graph the
# standings aggregator walks: Event -> Stage -> StageGroups -> Match -> TournamentTeam +
# one TournamentTeamMatchStats row. prize_currency="NGN" so no FxRate table is needed
# (NGN amounts pass through _amount_ngn unconverted).
# ──────────────────────────────────────────────────────────────────────────────
from datetime import date, time, timedelta

from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team

from .models import (
    Event,
    EventPrizePayout,
    Leaderboard,
    Match,
    PlayerWinning,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)
from .prize_sync import sync_completed_events


class PrizeSyncEffectiveStatusTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username="pcreator", email="pcreator@example.com", password="x", role="admin"
        )

    def _event_with_one_ranked_team(self, *, raw_status, end_offset_days, dist=None):
        """Build an event whose single team places 1st, with a prize_distribution.

        end_offset_days < 0 -> the event's end instant is in the PAST (effectively completed);
        > 0 -> in the FUTURE. prize_currency is NGN so the payout amount is used verbatim.
        Returns the Event.
        """
        today = date.today()
        end_date = today + timedelta(days=end_offset_days)
        event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=16,
            event_name="Prize Sync Cup",
            event_mode="virtual",
            start_date=today - timedelta(days=5),
            end_date=end_date,
            event_end_time=time(23, 59),          # end instant = end_date 23:59 in the event tz
            registration_open_date=today - timedelta(days=6),
            registration_end_date=today - timedelta(days=5),
            prizepool="100 NGN",
            prizepool_cash_value=100,
            prize_currency="NGN",                 # NGN -> no FX conversion needed in tests
            prize_distribution=dist or {"1": "100"},
            event_rules="No cheating",
            event_status=raw_status,
            registration_link="https://example.com/reg",
            tournament_tier="tier_1",
            number_of_stages=1,
            creator=self.creator,
            is_draft=False,
            is_public=True,
        )
        stage = Stages.objects.create(
            event=event,
            stage_name="Finals",
            start_date=today - timedelta(days=5),
            end_date=end_date,
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage,
            group_name="Group A",
            playing_date=today - timedelta(days=5),
            playing_time=time(19, 0),
            teams_qualifying=1,
            match_count=1,
            match_maps=["bermuda"],
        )
        leaderboard = Leaderboard.objects.create(
            leaderboard_name="Finals - Group A",
            event=event,
            stage=stage,
            group=group,
            creator=self.creator,
            leaderboard_method="manual",
            placement_points={},
            kill_point=1.0,
        )
        match = Match.objects.create(
            leaderboard=leaderboard, group=group, match_map="bermuda", match_number=1
        )
        team = Team.objects.create(
            team_name="Prize Team",
            join_settings="open",
            team_creator=self.creator,
            team_owner=self.creator,
            team_captain=self.creator,
            country="Nigeria",
        )
        tt = TournamentTeam.objects.create(
            event=event, team=team, registered_by=self.creator, status="active"
        )
        # One active roster member so the payout can be split into a PlayerWinning share.
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.creator, event=event, status="active"
        )
        # One match-stats row so the standings aggregator has a #1 team to award.
        TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=10, total_points=50,
        )
        return event, tt

    def test_effectively_completed_ongoing_event_is_synced(self):
        """THE BUG: raw 'ongoing' but past its end instant -> the sweep must attribute the prize."""
        event, tt = self._event_with_one_ranked_team(raw_status="ongoing", end_offset_days=-2)
        created = sync_completed_events()
        self.assertGreaterEqual(created, 1)
        payout = EventPrizePayout.objects.filter(event=event).first()
        self.assertIsNotNone(payout, "an effectively-completed event must get its auto payout")
        self.assertEqual(payout.tournament_team_id, tt.pk)
        self.assertEqual(str(payout.amount), "100.00")
        self.assertTrue(payout.auto_synced)
        # The team payout must ALSO distribute to the roster so the prize shows on player profiles:
        # the single active member gets the full share (owner 2026-06-15 feature, previously skipped
        # by the auto-sync path).
        share = PlayerWinning.objects.filter(event=event, player=self.creator).first()
        self.assertIsNotNone(share, "auto-synced team payout must write a PlayerWinning share")
        self.assertEqual(str(share.amount), "100.00")

    def test_not_yet_ended_event_is_not_synced(self):
        """CONTROL: raw 'upcoming' with a FUTURE end -> no prize is attributed early."""
        event, _ = self._event_with_one_ranked_team(raw_status="upcoming", end_offset_days=+3)
        sync_completed_events()
        self.assertEqual(EventPrizePayout.objects.filter(event=event).count(), 0)

    def test_raw_completed_event_still_synced(self):
        """REGRESSION: the original raw-'completed' path must keep working."""
        event, _ = self._event_with_one_ranked_team(raw_status="completed", end_offset_days=-1)
        sync_completed_events()
        self.assertEqual(EventPrizePayout.objects.filter(event=event, auto_synced=True).count(), 1)
