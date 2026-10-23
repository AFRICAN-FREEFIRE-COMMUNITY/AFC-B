"""A team page keeps every address it has had, and says how much the team has played (2026-10-16).

Found reading Google Search Console's "Not found (404)" drilldown (inbox #263): team pages are
addressed by the NAME, and a renamed team (MOONLIGHT, BLITZ PROTOCOL on production) turned every
old link into a 404, because teams kept no address history (owner rule R22: a link shared in June
opens the right page in December).

  - Team._keep_retired_name (afc_team/models.py) writes the old name to SlugHistory on a rename.
  - get_team_details resolves it through afc_auth.slugs.resolve_or_redirect and answers the team
    plus `moved_to`; the frontend layout turns that into a permanent redirect.
  - events_played on get_team_details comes from team_activity(), the same function behind
    get_all_teams' events_played, so the team page's index decision and the sitemap agree.

Run: .venv\\Scripts\\python.exe manage.py test afc_team.tests_team_address_history
"""
from django.test import TestCase
from django.utils import timezone

from afc_auth.models import SlugHistory, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
)

DETAILS = "/team/get-team-details/"
ALL = "/team/get-all-teams/"


class TeamAddressHistoryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="tahowner", email="tah@x.com", full_name="TAH",
                                         role="player", password="x")
        self.team = Team.objects.create(team_name="Accra Hawks", team_tag="ACH", join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)

    def _details(self, name):
        return self.client.post(DETAILS, {"team_name": name}, content_type="application/json")

    def test_current_name_answers_without_a_move(self):
        res = self._details("Accra Hawks")
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.assertEqual(res.json()["team"]["team_name"], "Accra Hawks")
        self.assertIsNone(res.json()["moved_to"])

    def test_a_renamed_team_keeps_its_old_address(self):
        """THE BUG: the old name answered 404 after a rename."""
        self.team.team_name = "Accra Falcons"
        self.team.save()

        res = self._details("Accra Hawks")

        self.assertEqual(res.status_code, 200, res.content[:300])
        self.assertEqual(res.json()["team"]["team_name"], "Accra Falcons")
        self.assertEqual(res.json()["moved_to"], "Accra Falcons")

    def test_a_rename_through_a_narrowed_save_is_still_recorded(self):
        self.team.team_name = "Accra Falcons"
        self.team.save(update_fields=["team_name"])
        self.assertTrue(SlugHistory.objects.filter(app_label="afc_team", model="team",
                                                   old_slug="Accra Hawks").exists())

    def test_a_save_that_does_not_touch_the_name_writes_nothing(self):
        self.team.team_description = "New words"
        self.team.save(update_fields=["team_description"])
        self.team.save()
        self.assertEqual(SlugHistory.objects.filter(app_label="afc_team").count(), 0)

    def test_a_change_of_case_alone_is_not_a_new_address(self):
        """Production compares names case-insensitively, so ACCRA HAWKS was never a different page."""
        self.team.team_name = "ACCRA HAWKS"
        self.team.save()
        self.assertEqual(SlugHistory.objects.filter(app_label="afc_team").count(), 0)

    def test_a_team_that_now_holds_the_old_name_wins(self):
        """People choose names: once another team takes the old name, the address is theirs."""
        self.team.team_name = "Accra Falcons"
        self.team.save()
        newcomer = Team.objects.create(team_name="Accra Hawks", team_tag="NEW", join_settings="open",
                                       team_creator=self.owner, team_owner=self.owner)

        res = self._details("Accra Hawks")

        self.assertEqual(res.json()["team"]["team_id"], newcomer.team_id)
        self.assertIsNone(res.json()["moved_to"])

    def test_the_last_team_to_leave_a_name_owns_its_history(self):
        self.team.team_name = "Accra Falcons"
        self.team.save()
        other = Team.objects.create(team_name="Accra Hawks", team_tag="OTH", join_settings="open",
                                    team_creator=self.owner, team_owner=self.owner)
        other.team_name = "Kumasi Kings"
        other.save()

        res = self._details("Accra Hawks")

        self.assertEqual(res.json()["team"]["team_id"], other.team_id)
        self.assertEqual(res.json()["moved_to"], "Kumasi Kings")

    def test_a_deleted_team_is_still_not_found(self):
        self.team.team_name = "Accra Falcons"
        self.team.save()
        self.team.delete()
        self.assertEqual(self._details("Accra Hawks").status_code, 404)
        self.assertEqual(self._details("Accra Falcons").status_code, 404)

    def test_outer_spaces_in_the_address_are_ignored(self):
        """Links built from names that once ended in a space still open the page."""
        res = self._details("Accra Hawks ")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["team"]["team_name"], "Accra Hawks")


class TeamEventsPlayedIsOneNumberTests(TestCase):
    """events_played on the team page == events_played on the team list (team_activity)."""

    def setUp(self):
        self.admin = User.objects.create(username="tepadm2", email="tepadm2@x.com", full_name="A",
                                         role="admin", password="x")
        self.team = Team.objects.create(team_name="Lagos Lions", team_tag="LGL", join_settings="open",
                                        team_creator=self.admin, team_owner=self.admin)

    def _event(self, name):
        today = timezone.localdate()
        return Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name=name, event_mode="virtual", start_date=today,
            end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="r", event_status="completed",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)

    def _play(self, event):
        today = timezone.localdate()
        tt = TournamentTeam.objects.create(event=event, team=self.team, status="active")
        stage = Stages.objects.create(event=event, stage_name="Stage 1", start_date=today, end_date=today,
                                      number_of_groups=1, stage_format="battle_royale",
                                      teams_qualifying_from_stage=1)
        lb = Leaderboard.objects.create(leaderboard_name=f"LB {event.event_name}", event=event, stage=stage,
                                        creator=self.admin, leaderboard_method="automatic")
        # Every real match sits in a stage group (production 2026-10-16: 0 of 2,901 team lines have
        # a group-less match), and team_activity counts events through match -> group -> stage.
        group = StageGroups.objects.create(stage=stage, group_name="Group A", playing_date=today,
                                           playing_time="18:00", teams_qualifying=1, match_count=1)
        match = Match.objects.create(leaderboard=lb, group=group, match_number=1)
        TournamentTeamMatchStats.objects.create(match=match, tournament_team=tt, placement=12, kills=0,
                                                total_points=0)

    def _numbers(self):
        page = self.client.post(DETAILS, {"team_name": "Lagos Lions"}, content_type="application/json")
        listed = self.client.get(ALL)
        row = next(t for t in listed.json()["teams"] if t["team_name"] == "Lagos Lions")
        return page.json()["team"]["events_played"], row["events_played"]

    def test_a_team_that_never_played_reads_zero_on_both(self):
        TournamentTeam.objects.create(event=self._event("Entered Only Cup"), team=self.team, status="active")
        self.assertEqual(self._numbers(), (0, 0))

    def test_a_played_event_counts_once_on_both_even_with_a_zero_score(self):
        self._play(self._event("Played Cup"))
        TournamentTeam.objects.create(event=self._event("Entered Only Cup"), team=self.team, status="active")
        self.assertEqual(self._numbers(), (1, 1))

    def test_events_played_is_public(self):
        """No viewer token: the number still comes back (the /teams list already shows it)."""
        self._play(self._event("Public Cup"))
        page = self.client.post(DETAILS, {"team_name": "Lagos Lions"}, content_type="application/json")
        self.assertFalse(page.json()["team"]["stats_visible"])
        self.assertEqual(page.json()["team"]["events_played"], 1)
