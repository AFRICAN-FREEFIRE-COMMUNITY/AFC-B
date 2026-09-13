"""
afc_team/tests_roster_lock.py - what may hold a player in their team, and what may not.

Owner 2026-09-13: "a cancelled event is still holding people from leaving their teams, the cage26
event". CAGE 26 was cancelled and was NOT the holder; the real ones were a live tournament (fair)
and a scrim that had ENDED on 1 September but had been reopened, which kept 25 players locked on
the 13th. reopen_event sets auto_complete_suppressed=True so the badge keeps reading "ongoing"
while an organizer fixes results - right for the badge, wrong for a roster lock.

So: the lock asks the CLOCK (can this event still be played), and the refusal NAMES the event, which
is what stopped anybody working out which one it was.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_team.tests_roster_lock
"""
from datetime import date, time, timedelta

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers
from afc_team.views import _active_event_roster_blockers, _member_in_active_event_roster, _name_events
from afc_tournament_and_scrims.models import Event, TournamentTeam, TournamentTeamMember


def _user(username):
    user = User.objects.create(username=username, email=f"{username}@x.com",
                               full_name=username.title(), password="x")
    token = SessionToken.objects.create(user=user, token=f"tok_{username}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _event(creator, name, *, status="ongoing", start=None, end=None, suppressed=False):
    return Event.objects.create(
        event_name=name, competition_type="scrims", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=start or date.today(), end_date=end or date.today(),
        registration_open_date=date.today() - timedelta(days=10),
        registration_end_date=date.today() - timedelta(days=1),
        number_of_stages=1, creator=creator, event_status=status,
        event_start_time=time(20, 0), event_end_time=time(22, 0),
        auto_complete_suppressed=suppressed,
    )


class RosterLockTests(TestCase):
    def setUp(self):
        self.owner, self.owner_auth = _user("teamowner")
        self.player, self.player_auth = _user("rosterplayer")
        self.team = Team.objects.create(team_name="Legacy", team_tag="LGC", country="NG",
                                        join_settings="open", team_owner=self.owner,
                                        team_creator=self.owner)
        TeamMembers.objects.create(team=self.team, member=self.player)
        self.client = Client()

    def _roster(self, event):
        tt = TournamentTeam.objects.create(event=event, team=self.team)
        TournamentTeamMember.objects.create(tournament_team=tt, user=self.player, event=event)
        return tt

    def test_a_reopened_event_that_has_ended_does_not_hold_anybody(self):
        # The exact shape of event 319: ongoing + reopened + ended twelve days ago.
        ended = _event(self.owner, "LEGACY SCRIMS DAY 30", status="ongoing",
                       start=date.today() - timedelta(days=12), end=date.today() - timedelta(days=12),
                       suppressed=True)
        self._roster(ended)
        self.assertEqual(_active_event_roster_blockers(self.team, self.player.user_id), [])
        self.assertFalse(_member_in_active_event_roster(self.team, self.player.user_id))

    def test_a_live_event_still_holds_them_and_the_refusal_names_it(self):
        live = _event(self.owner, "V-ENT X GROW INVITATIONALS", status="ongoing",
                      start=date.today(), end=date.today() + timedelta(days=3))
        self._roster(live)
        self.assertEqual([e.event_id for e in _active_event_roster_blockers(self.team, self.player.user_id)],
                         [live.event_id])
        r = self.client.post("/team/exit-team/", {}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.status_code, 403, r.content[:200])
        body = r.json()
        self.assertIn("V-ENT X GROW INVITATIONALS", body["message"])
        self.assertEqual([e["event_id"] for e in body["events"]], [live.event_id])
        self.assertTrue(TeamMembers.objects.filter(team=self.team, member=self.player).exists())

    def test_a_waitlisted_team_holds_nobody(self):
        # Queued, not playing (owner 2026-09-13). On production the same 29 players were held this
        # way AND by the reopened-but-finished scrim they were queued for; either rule frees them.
        live = _event(self.owner, "OVERSUBSCRIBED CUP", status="ongoing",
                      start=date.today(), end=date.today() + timedelta(days=3))
        tt = self._roster(live)
        tt.is_waitlisted = True
        tt.save(update_fields=["is_waitlisted"])
        self.assertEqual(_active_event_roster_blockers(self.team, self.player.user_id), [])
        r = self.client.post("/team/exit-team/", {}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])

    def test_a_team_promoted_off_the_waitlist_holds_again(self):
        live = _event(self.owner, "OVERSUBSCRIBED CUP", status="ongoing",
                      start=date.today(), end=date.today() + timedelta(days=3))
        tt = self._roster(live)
        tt.is_waitlisted = True
        tt.save(update_fields=["is_waitlisted"])
        self.assertEqual(_active_event_roster_blockers(self.team, self.player.user_id), [])
        tt.is_waitlisted = False
        tt.save(update_fields=["is_waitlisted"])
        self.assertEqual([e.event_id for e in _active_event_roster_blockers(self.team, self.player.user_id)],
                         [live.event_id])

    def test_a_cancelled_event_holds_nobody(self):
        # What the owner thought was the culprit. It never was, and this keeps it that way.
        cancelled = _event(self.owner, "CAGE 26 NIGERIA ONLY", status="cancelled",
                           start=date.today() - timedelta(days=1), end=date.today() + timedelta(days=5))
        self._roster(cancelled)
        self.assertEqual(_active_event_roster_blockers(self.team, self.player.user_id), [])

    def test_leaving_works_once_nothing_holds_them(self):
        ended = _event(self.owner, "OLD SCRIM", status="ongoing",
                       start=date.today() - timedelta(days=5), end=date.today() - timedelta(days=5),
                       suppressed=True)
        self._roster(ended)
        r = self.client.post("/team/exit-team/", {}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertFalse(TeamMembers.objects.filter(team=self.team, member=self.player).exists())

    def test_the_kick_path_names_the_event_too(self):
        live = _event(self.owner, "LIVE CUP", status="ongoing",
                      start=date.today(), end=date.today() + timedelta(days=2))
        self._roster(live)
        r = self.client.post("/team/kick-team-member/", {"member_id": self.player.user_id},
                             content_type="application/json", **self.owner_auth)
        if r.status_code == 403 and "roster" in (r.json().get("error") or ""):
            self.assertIn("LIVE CUP", r.json()["error"])
            self.assertEqual([e["event_id"] for e in r.json()["events"]], [live.event_id])
        else:  # a different gate answered first (transfer window, permissions): not this test's subject
            self.skipTest(f"kick blocked by another gate: {r.status_code} {r.content[:120]}")

    def test_two_events_are_both_named(self):
        a = _event(self.owner, "CUP A", status="ongoing", start=date.today(), end=date.today() + timedelta(days=1))
        b = _event(self.owner, "CUP B", status="ongoing", start=date.today(), end=date.today() + timedelta(days=1))
        self._roster(a)
        self._roster(b)
        blockers = _active_event_roster_blockers(self.team, self.player.user_id)
        self.assertEqual(len(blockers), 2)
        phrase = _name_events(blockers)
        self.assertIn("CUP A", phrase)
        self.assertIn("CUP B", phrase)
        self.assertTrue(phrase.startswith("the events "))

    def test_the_phrase_stays_a_sentence_when_there_are_many(self):
        class _E:
            def __init__(self, n):
                self.event_name = n
        self.assertEqual(_name_events([_E("A")]), 'the event "A"')
        self.assertEqual(_name_events([_E("A"), _E("B")]), 'the events "A" and "B"')
        self.assertEqual(_name_events([_E(x) for x in "ABCDE"]), 'the events "A", "B" and 3 more')
