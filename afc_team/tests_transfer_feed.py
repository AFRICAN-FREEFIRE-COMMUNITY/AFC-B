"""
tests_transfer_feed.py
──────────────────────
The automatic transfer feed (backlog item 21, owner 2026-08-08: "Public automatic transfer news
showing players joining and leaving teams").

WHAT THESE LOCK DOWN, and why each one exists:

  • The feed writes ITSELF. Nothing in afc_team/views.py calls record_transfer; the entries come
    from the TeamMembers post_save/post_delete receivers in afc_team/signals.py. If somebody later
    "tidies up" those receivers, the feed goes quiet with no other symptom, so a join and a leave
    each have a test.
  • A ROLE CHANGE IS NOT A TRANSFER. post_save fires on every edit, so the created=True guard is
    the only thing stopping a captain adjusting a position from publishing "X joined Y" again.
  • The HAS-COMPETED rule (afc_team.transfers.HAS_COMPETED_RULE) is the whole reason the feed is
    readable rather than a churn log, and it is invisible in the response when it works - the only
    way to see it is a team that should NOT be there.
  • The transfer-window flag is CAPTURED AT WRITE TIME, not derived on read, because admins can
    move the window afterwards. Three states: open, closed, and no season at all.
  • A DISBANDED team's rows must survive (SET_NULL, not CASCADE) and must not appear in the feed.
  • THE NAMES IN THE FEED ARE ITS LINKS. Both /teams/<team_name> and /players/<username> are
    addressed by name, so an entry rendered under a name somebody has since changed links nowhere.
  • DELETING AN ACCOUNT MUST STILL BE POSSIBLE. The membership cascade writes a feed entry mid
    delete, and pointed at the row being deleted it fails the foreign key outright (MySQL 1451).

Run: .venv\\Scripts\\python.exe manage.py test afc_team.tests_transfer_feed
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_rankings.models import Season
from afc_team.models import Team, TeamMembers, TeamTransfer
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam, TournamentTeamMatchStats,
)

User = get_user_model()

PLAY_DAY = datetime.date(2099, 5, 10)


class TransferFeedTestCase(TestCase):
    """Shared fixture: two teams, one of which has actually competed, plus builders."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="feed_owner", email="feed_owner@example.com", password="x")

        # `competed` has a real match result, so it passes the HAS-COMPETED rule.
        self.competed = self._team("Competed Kings", self.owner)
        self._record_a_played_match(self.competed)
        # `unknown` has never played anything - the exact case the rule exists to keep out.
        self.unknown = self._team("Unknown Rookies", self.owner)

    # ── builders ────────────────────────────────────────────────────────────────────────────
    def _team(self, name, owner):
        return Team.objects.create(
            team_name=name, team_tag=name[:4], join_settings="open",
            team_creator=owner, team_owner=owner,
        )

    def _player(self, username):
        return User.objects.create_user(
            username=username, email=f"{username}@example.com", password="x")

    def _record_a_played_match(self, team):
        """The minimal object graph that makes `team` count as having competed:
        Event -> Stages -> StageGroups -> Match -> TournamentTeam -> TournamentTeamMatchStats."""
        event = Event.objects.create(
            event_name=f"Cup for {team.team_name}", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=12,
            event_mode="virtual", start_date=PLAY_DAY, end_date=PLAY_DAY,
            registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
            registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.owner, is_draft=False,
        )
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=PLAY_DAY, end_date=PLAY_DAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=PLAY_DAY,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=PLAY_DAY)
        tt = TournamentTeam.objects.create(
            event=event, team=team, registered_by=self.owner, status="active")
        TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=10)

    def _season(self, *, window_open):
        """An ACTIVE season whose transfer window is open or closed today."""
        today = timezone.localdate()
        if window_open:
            opens, closes = today - datetime.timedelta(days=1), today + datetime.timedelta(days=1)
        else:
            opens, closes = today - datetime.timedelta(days=30), today - datetime.timedelta(days=10)
        return Season.objects.create(
            name="Season under test", year=2099, quarter=2, is_active=True,
            start_date=today - datetime.timedelta(days=90),
            end_date=today + datetime.timedelta(days=90),
            transfer_window_open=opens, transfer_window_close=closes,
        )

    def _feed(self, **params):
        response = self.client.get("/team/transfers/", params)
        self.assertEqual(response.status_code, 200)
        return response.json()


# ─────────────────────────── §1 the feed writes itself ───────────────────────────
class TransfersAreRecordedAutomaticallyTests(TransferFeedTestCase):

    def test_a_join_appears_in_the_feed(self):
        # Arrange
        player = self._player("joiner_one")

        # Act - nothing calls the transfer code; adding the membership is the whole action.
        TeamMembers.objects.create(team=self.competed, member=player, management_role="member")

        # Assert
        entry = self._feed()["results"][0]
        self.assertEqual(entry["direction"], "joined")
        self.assertEqual(entry["player_username"], "joiner_one")
        self.assertEqual(entry["team_name"], "Competed Kings")

    def test_a_leave_appears_in_the_feed(self):
        # Arrange
        player = self._player("leaver_one")
        membership = TeamMembers.objects.create(
            team=self.competed, member=player, management_role="member")

        # Act
        membership.delete()

        # Assert - newest first, so the departure leads and the earlier arrival is still there.
        results = self._feed()["results"]
        self.assertEqual([r["direction"] for r in results], ["left", "joined"])

    def test_the_role_a_member_joined_in_is_recorded(self):
        """"Joined as Coach" must not read as "signed as a player"."""
        # Arrange
        player = self._player("new_coach")

        # Act
        TeamMembers.objects.create(team=self.competed, member=player, management_role="coach")

        # Assert
        self.assertEqual(self._feed()["results"][0]["management_role"], "coach")

    def test_changing_a_members_role_is_not_a_transfer(self):
        """post_save fires on every edit; only created=True is a transfer. Without that guard a
        captain adjusting a position would republish "X joined Y" to the whole community."""
        # Arrange
        player = self._player("role_changer")
        membership = TeamMembers.objects.create(
            team=self.competed, member=player, management_role="member")

        # Act
        membership.management_role = "vice_captain"
        membership.save()

        # Assert - still exactly the one entry from the join.
        self.assertEqual(self._feed()["total_count"], 1)

    def test_a_move_between_teams_reads_as_a_leave_and_a_join(self):
        # Arrange
        self._record_a_played_match(self.unknown)   # make the destination newsworthy too
        player = self._player("mover")
        membership = TeamMembers.objects.create(
            team=self.competed, member=player, management_role="member")

        # Act - a member may only be on one team, so a move is a delete then a create.
        membership.delete()
        TeamMembers.objects.create(team=self.unknown, member=player, management_role="member")

        # Assert
        results = self._feed()["results"]
        self.assertEqual(
            [(r["direction"], r["team_name"]) for r in results],
            [("joined", "Unknown Rookies"), ("left", "Competed Kings"), ("joined", "Competed Kings")],
        )


# ─────────────────────────── §2 the HAS-COMPETED rule ───────────────────────────
class OnlyTeamsThatHaveCompetedAppearTests(TransferFeedTestCase):

    def test_a_team_that_has_never_played_a_match_is_excluded(self):
        # Arrange
        player = self._player("nobody_knows_me")

        # Act
        TeamMembers.objects.create(team=self.unknown, member=player, management_role="member")

        # Assert - the row was still WRITTEN (history is complete), it is just not published.
        self.assertEqual(TeamTransfer.objects.filter(team=self.unknown).count(), 1)
        self.assertEqual(self._feed()["total_count"], 0)

    def test_the_same_move_appears_once_that_team_has_played(self):
        """The rule is about the team, not the entry: a back catalogue becomes visible the day the
        team's first result lands, which is the behaviour an editor would expect."""
        # Arrange
        player = self._player("early_signing")
        TeamMembers.objects.create(team=self.unknown, member=player, management_role="member")
        self.assertEqual(self._feed()["total_count"], 0)

        # Act
        self._record_a_played_match(self.unknown)

        # Assert
        self.assertEqual(self._feed()["total_count"], 1)

    def test_an_uncompeted_team_is_not_offered_in_the_team_filter(self):
        # Arrange
        TeamMembers.objects.create(
            team=self.unknown, member=self._player("hidden_one"), management_role="member")
        TeamMembers.objects.create(
            team=self.competed, member=self._player("shown_one"), management_role="member")

        # Act
        teams = self._feed()["teams"]

        # Assert
        self.assertEqual([t["team_name"] for t in teams], ["Competed Kings"])


# ─────────────────────── §3 the transfer-window distinction ───────────────────────
class TransferWindowStateIsCapturedTests(TransferFeedTestCase):

    def test_a_move_while_the_window_is_open_is_marked_routine(self):
        # Arrange
        self._season(window_open=True)

        # Act
        TeamMembers.objects.create(
            team=self.competed, member=self._player("in_window"), management_role="member")

        # Assert
        self.assertIs(self._feed()["results"][0]["in_transfer_window"], True)

    def test_a_move_while_the_window_is_closed_is_marked_notable(self):
        """Roster moves are frozen server-side while the window is closed, so one that lands anyway
        is the story - the feed has to be able to tell the reader that."""
        # Arrange
        self._season(window_open=False)

        # Act
        TeamMembers.objects.create(
            team=self.competed, member=self._player("out_of_window"), management_role="member")

        # Assert
        self.assertIs(self._feed()["results"][0]["in_transfer_window"], False)

    def test_with_no_active_season_the_feed_says_nothing_either_way(self):
        """Null, not False. Claiming a move broke a window that does not exist would be a lie."""
        # Arrange - setUp creates no Season on purpose.

        # Act
        TeamMembers.objects.create(
            team=self.competed, member=self._player("no_season"), management_role="member")

        # Assert
        self.assertIsNone(self._feed()["results"][0]["in_transfer_window"])

    def test_the_flag_does_not_change_when_an_admin_moves_the_window_afterwards(self):
        """The reason the flag is a stored column and not a read-time comparison: an admin can
        extend or reopen a window later, and that must not rewrite what happened last month."""
        # Arrange
        season = self._season(window_open=False)
        TeamMembers.objects.create(
            team=self.competed, member=self._player("history_keeper"), management_role="member")

        # Act - the admin reopens the window around today.
        today = timezone.localdate()
        season.transfer_window_open = today - datetime.timedelta(days=1)
        season.transfer_window_close = today + datetime.timedelta(days=30)
        season.save()

        # Assert - the entry still records the world as it was.
        self.assertIs(self._feed()["results"][0]["in_transfer_window"], False)


# ─────────────────────── §4 per-team view, paging, disbands ───────────────────────
class FeedFilteringAndPagingTests(TransferFeedTestCase):

    def test_filtering_by_team_returns_only_that_teams_moves(self):
        # Arrange
        self._record_a_played_match(self.unknown)
        TeamMembers.objects.create(
            team=self.competed, member=self._player("kings_player"), management_role="member")
        TeamMembers.objects.create(
            team=self.unknown, member=self._player("rookies_player"), management_role="member")

        # Act
        data = self._feed(team_id=self.competed.team_id)

        # Assert
        self.assertEqual([r["player_username"] for r in data["results"]], ["kings_player"])

    def test_a_non_numeric_team_id_is_rejected_rather_than_ignored(self):
        response = self.client.get("/team/transfers/", {"team_id": "; drop"})
        self.assertEqual(response.status_code, 400)

    def test_the_feed_pages_and_reports_what_is_left(self):
        # Arrange
        for i in range(5):
            TeamMembers.objects.create(
                team=self.competed, member=self._player(f"paged_{i}"), management_role="member")

        # Act
        first = self._feed(limit=2)

        # Assert
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(first["total_count"], 5)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)

    def test_the_last_page_reports_no_more(self):
        # Arrange
        TeamMembers.objects.create(
            team=self.competed, member=self._player("only_one"), management_role="member")

        # Act
        data = self._feed(limit=20)

        # Assert
        self.assertFalse(data["has_more"])
        self.assertIsNone(data["next_offset"])

    def test_an_oversized_limit_is_clamped_rather_than_honoured(self):
        self.assertEqual(self._feed(limit=5000)["limit"], 50)

    def test_a_renamed_team_reads_and_LINKS_under_its_current_name(self):
        """THE FEED'S NAMES ARE ITS LINKS. /teams/<x> resolves x as an EXACT team_name (backend
        get_team_details), so an entry showing the name the team had at the time of the move would
        link somewhere that no longer exists. That is precisely how the event-invitation deep link
        404'd for all 24 invitations ever sent, and the same mistake is one line away here."""
        # Arrange
        TeamMembers.objects.create(
            team=self.competed, member=self._player("stayed_through_rebrand"),
            management_role="member")

        # Act - the org rebrands.
        self.competed.team_name = "Renamed Kings"
        self.competed.save()

        # Assert - the entry shows the CURRENT name (which is also the address of its page)...
        data = self._feed()
        self.assertEqual(data["results"][0]["team_name"], "Renamed Kings")
        # ...and the team appears ONCE in the filter, not once per name it has ever had.
        self.assertEqual([t["team_name"] for t in data["teams"]], ["Renamed Kings"])

    def test_a_renamed_player_reads_under_their_current_username(self):
        """Same rule on the player side: /players/<username> is addressed by the live username."""
        # Arrange
        player = self._player("old_handle")
        TeamMembers.objects.create(
            team=self.competed, member=player, management_role="member")

        # Act
        player.username = "new_handle"
        player.save()

        # Assert
        entry = self._feed()["results"][0]
        self.assertEqual(entry["player_username"], "new_handle")
        self.assertTrue(entry["player_exists"])

    def test_deleting_an_account_leaves_the_history_readable_and_unlinked(self):
        """Two things at once, and the first is the one that bites:

        1. TeamMembers.member is a CASCADE, so deleting a user deletes their membership, which
           fires the leave receiver, which writes a TeamTransfer. Pointed at the user being
           deleted, that new row makes the delete itself fail with MySQL 1451 - i.e. WITHOUT the
           guard in transfers.py §0 no account that is on a team can be deleted at all, from the
           admin or anywhere else. This test is that guard's only alarm.
        2. The entry must still READ afterwards, from the recorded name, and must stop pretending
           there is a profile to link to."""
        # Arrange
        player = self._player("about_to_vanish")
        TeamMembers.objects.create(team=self.competed, member=player, management_role="member")

        # Act - this raised IntegrityError (1451) before the player-side guard existed.
        player.delete()

        # Assert - both the join and the cascade's leave survive, named, and unlinked.
        results = self._feed()["results"]
        self.assertEqual([r["direction"] for r in results], ["left", "joined"])
        self.assertTrue(all(r["player_username"] == "about_to_vanish" for r in results))
        self.assertTrue(all(r["player_exists"] is False for r in results))
        # The team side is untouched, so those entries still link to a live team page.
        self.assertTrue(all(r["team_name"] == "Competed Kings" for r in results))

    def test_the_feed_never_hands_out_a_user_id(self):
        """This endpoint is PUBLIC and unauthenticated. The username is the address of the profile
        page, so nothing here needs a user id, and an open endpoint should hand out no identifier
        it does not need (the join-request leak, 2026-08-08)."""
        # Arrange
        TeamMembers.objects.create(
            team=self.competed, member=self._player("privacy_check"), management_role="member")

        # Assert
        entry = self._feed()["results"][0]
        self.assertNotIn("player_id", entry)

    def test_a_disbanded_teams_entries_survive_but_leave_the_feed(self):
        """SET_NULL, not CASCADE: deleting the Team must not wipe the rows the disband just wrote.
        They stay in the table (history is not rewritten) and drop out of the feed, because a
        disband is not a transfer and there is no team page left to link to."""
        # Arrange
        player = self._player("disband_victim")
        TeamMembers.objects.create(team=self.competed, member=player, management_role="member")
        self.assertEqual(self._feed()["total_count"], 1)

        # Act
        self.competed.delete()

        # Assert - two rows on file (the join, and the leave the cascade produced), none published.
        self.assertEqual(TeamTransfer.objects.count(), 2)
        self.assertEqual(
            sorted(TeamTransfer.objects.values_list("direction", flat=True)), ["joined", "left"])
        self.assertTrue(all(t.team_id is None for t in TeamTransfer.objects.all()))
        self.assertEqual(TeamTransfer.objects.first().team_name_at_move, "Competed Kings")
        self.assertEqual(self._feed()["total_count"], 0)
