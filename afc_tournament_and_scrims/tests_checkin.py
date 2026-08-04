# afc_tournament_and_scrims/tests_checkin.py
# ──────────────────────────────────────────────────────────────────────────────
# EVENT CHECK-IN, end to end (owner 2026-07-04, extended 2026-08-04).
#
# The rule in the owner's words: every registered competitor must come back to the site and tap
# check-in inside the window. A squad is eligible only when EVERY one of its players checks in.
# The waitlist has to check in too. A team that does not complete check-in is REPLACED by a
# waitlisted team that did.
#
# Three things here had never been covered by a test, and all three were broken or missing:
#
#   1. THE WINDOW VALIDATION IGNORED THE EVENT'S TIMEZONE. An organizer set registration to end
#      at 18:59 and check-in to open at 19:15 and was refused. The check compared the admin's
#      instant against a registration end built in the SERVER timezone (UTC), so for a Lagos
#      event it was an hour late and rejected a valid window.
#
#   2. NO REPLACEMENT. Relegation moved the incomplete team to the waitlist and stopped there, so
#      an event that lost three teams to a missed check-in simply ran three teams short while
#      checked-in waitlisted teams waited for a promotion that never came.
#
#   3. THE SCHEDULE WAS INVISIBLE to anyone not already registered, which is precisely the person
#      deciding whether to enter.
#
# Squad rosters here are given DISTINCT players per team on purpose. Sharing a player between two
# teams makes the check-in table ambiguous (EventCheckIn is unique per event+user), and an earlier
# hand-run of this scenario produced nonsense results for exactly that reason.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import json
import uuid

from django.test import Client, TestCase
from django.utils import timezone
from zoneinfo import ZoneInfo

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import (
    Event, EventCheckIn, RegisteredCompetitors, TournamentTeam, TournamentTeamMember,
)
from .views_checkin import promote_checked_in_waitlist, relegate_unchecked_competitors

LAGOS = ZoneInfo("Africa/Lagos")
SETTINGS_URL = "/events/checkin/settings/"
STATUS_URL = "/events/checkin/status/"
CHECKIN_URL = "/events/checkin/"


class CheckinTests(TestCase):
    def _user(self, name, role="player"):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role=role, status="active", is_active=True, country="Nigeria",
        )

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"t-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _squad(self, label, size=4):
        """A team with `size` players of its OWN, registered onto the event."""
        owner = self._user(f"{label}_owner")
        team = Team.objects.create(
            team_name=f"Team {label}", join_settings="open",
            team_creator=owner, team_owner=owner, country="Nigeria",
        )
        tt = TournamentTeam.objects.create(event=self.event, team=team, status="active")
        members = [owner] + [self._user(f"{label}_p{i}") for i in range(size - 1)]
        for u in members:
            TeamMembers.objects.create(team=team, member=u, management_role="member")
            TournamentTeamMember.objects.create(
                tournament_team=tt, user=u, event=self.event, status="active")
        RegisteredCompetitors.objects.create(event=self.event, team=team, is_waitlisted=False)
        return tt, members

    def _check_in_at(self, user, when):
        """Check `user` in with an EXPLICIT timestamp.

        EventCheckIn.checked_in_at is auto_now_add, so a value passed to objects.create() is
        silently discarded and every row lands on the same instant. A test that ordered by such
        a column would be comparing ties and would pass whatever the ordering code did. .update()
        is the one write that bypasses auto_now_add, so it is used here on purpose.
        """
        row = EventCheckIn.objects.create(event=self.event, user=user)
        EventCheckIn.objects.filter(pk=row.pk).update(checked_in_at=when)
        return row

    def setUp(self):
        self.admin = self._user("checkin_admin", role="admin")
        # A LAGOS event: registration closes 18:59 local, the event starts 21:00 local. The
        # timezone is the whole point of the first test, so it is set explicitly.
        self.event = Event.objects.create(
            event_name="Check-in Cup", slug="checkin-cup",
            participant_type="squad", competition_type="tournament", event_type="virtual",
            max_teams_or_players=16, is_public=True, is_draft=False, number_of_stages=1,
            timezone="Africa/Lagos",
            start_date=datetime.date(2026, 8, 4), event_start_time=datetime.time(21, 0),
            end_date=datetime.date(2026, 8, 4),
            registration_open_date=datetime.date(2026, 8, 1),
            registration_end_date=datetime.date(2026, 8, 4),
            registration_end_time=datetime.time(18, 59),
        )
        self.client = Client()

    # ══════════════════════════════════════════════════════════════════════════
    # 1. The window an organizer is allowed to set
    # ══════════════════════════════════════════════════════════════════════════
    def _set_window(self, start_local, end_local):
        return self.client.patch(
            SETTINGS_URL,
            data=json.dumps({
                "event_id": self.event.event_id, "checkin_enabled": True,
                "checkin_start": start_local.isoformat(), "checkin_end": end_local.isoformat(),
            }),
            content_type="application/json", **self._auth(self.admin),
        )

    def test_a_window_just_after_registration_is_accepted(self):
        """THE REPORTED BUG. Registration ends 18:59 Lagos, check-in opens 19:15 Lagos. This was
        refused with "Check-in can only begin after registration ends", because the boundary was
        computed in the server timezone (UTC) and came out an hour late."""
        resp = self._set_window(
            datetime.datetime(2026, 8, 4, 19, 15, tzinfo=LAGOS),
            datetime.datetime(2026, 8, 4, 19, 40, tzinfo=LAGOS),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertTrue(self.event.checkin_enabled)

    def test_a_window_that_starts_before_registration_ends_is_refused_and_says_when(self):
        resp = self._set_window(
            datetime.datetime(2026, 8, 4, 18, 30, tzinfo=LAGOS),
            datetime.datetime(2026, 8, 4, 19, 0, tzinfo=LAGOS),
        )
        self.assertEqual(resp.status_code, 400)
        # The refusal names the boundary, in the event's own timezone. Without it an organizer
        # hunts for a mistake they cannot see.
        self.assertIn("18:59", resp.json()["message"])
        self.assertIn("Africa/Lagos", resp.json()["message"])

    def test_a_window_that_runs_into_the_event_is_refused_and_says_when(self):
        resp = self._set_window(
            datetime.datetime(2026, 8, 4, 20, 30, tzinfo=LAGOS),
            datetime.datetime(2026, 8, 4, 21, 30, tzinfo=LAGOS),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("21:00", resp.json()["message"])

    # ══════════════════════════════════════════════════════════════════════════
    # 2. Checking in
    # ══════════════════════════════════════════════════════════════════════════
    def _open_window_now(self):
        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(minutes=10)
        self.event.checkin_end = timezone.now() + datetime.timedelta(minutes=10)
        self.event.save()

    def test_a_rostered_player_can_check_in_and_it_is_idempotent(self):
        tt, members = self._squad("Alpha")
        self._open_window_now()
        first = self.client.post(
            CHECKIN_URL, data=json.dumps({"event_id": self.event.event_id}),
            content_type="application/json", **self._auth(members[0]))
        self.assertEqual(first.status_code, 200, first.content)
        again = self.client.post(
            CHECKIN_URL, data=json.dumps({"event_id": self.event.event_id}),
            content_type="application/json", **self._auth(members[0]))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(EventCheckIn.objects.filter(event=self.event, user=members[0]).count(), 1)

    def test_a_waitlisted_player_can_check_in(self):
        """The waitlist has to be able to check in, or the replacement rule below is dead code:
        promotion only ever goes to a competitor who checked in."""
        tt, members = self._squad("Bench")
        tt.is_waitlisted = True
        tt.save(update_fields=["is_waitlisted"])
        self._open_window_now()
        resp = self.client.post(
            CHECKIN_URL, data=json.dumps({"event_id": self.event.event_id}),
            content_type="application/json", **self._auth(members[0]))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_check_in_is_refused_outside_the_window(self):
        tt, members = self._squad("Early")
        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() + datetime.timedelta(hours=1)
        self.event.checkin_end = timezone.now() + datetime.timedelta(hours=2)
        self.event.save()
        resp = self.client.post(
            CHECKIN_URL, data=json.dumps({"event_id": self.event.event_id}),
            content_type="application/json", **self._auth(members[0]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not opened", resp.json()["message"])

    def test_a_stranger_cannot_check_in(self):
        self._squad("Gamma")
        self._open_window_now()
        outsider = self._user("outsider")
        resp = self.client.post(
            CHECKIN_URL, data=json.dumps({"event_id": self.event.event_id}),
            content_type="application/json", **self._auth(outsider))
        self.assertEqual(resp.status_code, 403)

    # ══════════════════════════════════════════════════════════════════════════
    # 3. The replacement rule: incomplete out, checked-in waitlist in
    # ══════════════════════════════════════════════════════════════════════════
    def test_an_incomplete_squad_is_replaced_by_a_checked_in_waitlisted_squad(self):
        """The owner's rule, end to end and in one test, because the two halves are only correct
        together: relegation alone shrinks the event, promotion alone would seat teams over
        capacity."""
        complete, complete_players = self._squad("Complete")
        short, short_players = self._squad("Short")
        bench, bench_players = self._squad("Bench")
        bench.is_waitlisted = True
        bench.save(update_fields=["is_waitlisted"])

        # Everyone checks in EXCEPT one player on the "Short" squad.
        for u in complete_players + short_players[:-1] + bench_players:
            EventCheckIn.objects.create(event=self.event, user=u)

        # The window has closed, which is the only state relegation runs in.
        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(hours=2)
        self.event.checkin_end = timezone.now() - datetime.timedelta(minutes=1)
        self.event.save()

        freed = relegate_unchecked_competitors(self.event)
        promoted = promote_checked_in_waitlist(self.event, freed)

        complete.refresh_from_db(); short.refresh_from_db(); bench.refresh_from_db()
        self.assertFalse(complete.is_waitlisted, "a fully checked-in squad keeps its slot")
        self.assertTrue(short.is_waitlisted, "one missing player relegates the whole squad")
        self.assertFalse(bench.is_waitlisted, "the checked-in waitlisted squad takes the slot")
        self.assertEqual(freed, 1)
        self.assertEqual(promoted, 1)

    def test_a_waitlisted_squad_that_did_not_check_in_is_not_promoted(self):
        """Promoting a team that never checked in would seat somebody showing no sign of turning
        up, which is the exact thing check-in exists to detect."""
        short, short_players = self._squad("Short")
        bench, _bench_players = self._squad("Bench")   # deliberately checks in nobody
        bench.is_waitlisted = True
        bench.save(update_fields=["is_waitlisted"])

        for u in short_players[:-1]:
            EventCheckIn.objects.create(event=self.event, user=u)

        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(hours=2)
        self.event.checkin_end = timezone.now() - datetime.timedelta(minutes=1)
        self.event.save()

        freed = relegate_unchecked_competitors(self.event)
        promoted = promote_checked_in_waitlist(self.event, freed)
        bench.refresh_from_db()
        self.assertEqual(promoted, 0)
        self.assertTrue(bench.is_waitlisted)

    def test_promotion_never_seats_more_teams_than_check_in_freed(self):
        """`freed` is the cap. Otherwise a single missed check-in could pull the entire waitlist
        in and take the event over the size the organizer set."""
        short, short_players = self._squad("Short")
        benches = []
        for i in range(3):
            tt, players = self._squad(f"Bench{i}")
            tt.is_waitlisted = True
            tt.save(update_fields=["is_waitlisted"])
            benches.append(tt)
            for u in players:
                EventCheckIn.objects.create(event=self.event, user=u)
        for u in short_players[:-1]:
            EventCheckIn.objects.create(event=self.event, user=u)

        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(hours=2)
        self.event.checkin_end = timezone.now() - datetime.timedelta(minutes=1)
        self.event.save()

        freed = relegate_unchecked_competitors(self.event)
        promoted = promote_checked_in_waitlist(self.event, freed)
        self.assertEqual(freed, 1)
        self.assertEqual(promoted, 1, "one seat freed, one team promoted, not all three")

    def test_the_organizers_own_waitlist_setting_decides_who_is_promoted(self):
        """Event.waitlist_mode has three settings and promotion honours all three (owner
        2026-08-04). It used to promote in registration order no matter which one the organizer
        had picked, which is worse than having no setting at all: the screen said one thing and
        the event did another.

        Under `fcfs_room` the ordering is by CHECK-IN time, and for a squad that means its LAST
        player, because a squad is only available for the slot once everybody is in. So the test
        registers Early first and still expects Late to be promoted, which is the opposite of
        what the default would do and is the only way to tell the two settings apart.
        """
        short, short_players = self._squad("Short")
        early, early_players = self._squad("Early")     # registered first
        late, late_players = self._squad("Late")        # registered second
        for tt in (early, late):
            tt.is_waitlisted = True
            tt.save(update_fields=["is_waitlisted"])

        for u in short_players[:-1]:
            EventCheckIn.objects.create(event=self.event, user=u)
        # Late's squad completes check-in BEFORE Early's does. Stamps are written explicitly
        # rather than relying on insert order, so the ordering under test is the real column.
        base = timezone.now() - datetime.timedelta(hours=3)
        for i, u in enumerate(late_players):
            self._check_in_at(u, base + datetime.timedelta(minutes=i))
        for i, u in enumerate(early_players):
            self._check_in_at(u, base + datetime.timedelta(minutes=30 + i))

        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(hours=2)
        self.event.checkin_end = timezone.now() - datetime.timedelta(minutes=1)
        self.event.save()

        freed = relegate_unchecked_competitors(self.event)
        self.assertEqual(freed, 1)

        # ── manual_admin: the organizer asked to choose, so NOBODY is promoted ──
        self.event.waitlist_mode = "manual_admin"
        self.event.save(update_fields=["waitlist_mode"])
        self.assertEqual(promote_checked_in_waitlist(self.event, freed), 0)
        early.refresh_from_db(); late.refresh_from_db()
        self.assertTrue(early.is_waitlisted)
        self.assertTrue(late.is_waitlisted, "the seat is left for the organizer to fill")

        # ── fcfs_room: the squad that finished checking in first, not the one that registered
        #    first. Early registered before Late and must still lose. ──
        self.event.waitlist_mode = "fcfs_room"
        self.event.save(update_fields=["waitlist_mode"])
        self.assertEqual(promote_checked_in_waitlist(self.event, freed), 1)
        early.refresh_from_db(); late.refresh_from_db()
        self.assertFalse(late.is_waitlisted, "Late checked in first, so Late takes the slot")
        self.assertTrue(early.is_waitlisted)

    def test_the_default_waitlist_setting_still_promotes_the_earliest_registered(self):
        """The counterpart to the test above, and the reason it proves anything: with the default
        setting the SAME check-in times produce the OTHER team. Without this, ordering by
        check-in time and ordering by registration could both be passing by coincidence."""
        short, short_players = self._squad("Short")
        early, early_players = self._squad("Early")
        late, late_players = self._squad("Late")
        for tt in (early, late):
            tt.is_waitlisted = True
            tt.save(update_fields=["is_waitlisted"])

        for u in short_players[:-1]:
            EventCheckIn.objects.create(event=self.event, user=u)
        base = timezone.now() - datetime.timedelta(hours=3)
        for i, u in enumerate(late_players):     # Late checks in FIRST, as above
            self._check_in_at(u, base + datetime.timedelta(minutes=i))
        for i, u in enumerate(early_players):
            self._check_in_at(u, base + datetime.timedelta(minutes=30 + i))

        self.event.checkin_enabled = True
        self.event.checkin_start = timezone.now() - datetime.timedelta(hours=2)
        self.event.checkin_end = timezone.now() - datetime.timedelta(minutes=1)
        self.event.save()
        self.assertEqual(self.event.waitlist_mode, "first_registered")

        freed = relegate_unchecked_competitors(self.event)
        self.assertEqual(promote_checked_in_waitlist(self.event, freed), 1)
        early.refresh_from_db(); late.refresh_from_db()
        self.assertFalse(early.is_waitlisted, "registered first wins under the default setting")
        self.assertTrue(late.is_waitlisted)

    def test_relegation_does_not_run_while_the_window_is_still_open(self):
        short, short_players = self._squad("Short")
        for u in short_players[:-1]:
            EventCheckIn.objects.create(event=self.event, user=u)
        self._open_window_now()
        self.assertEqual(relegate_unchecked_competitors(self.event), 0)
        short.refresh_from_db()
        self.assertFalse(short.is_waitlisted)

    # ══════════════════════════════════════════════════════════════════════════
    # 4. What the event page can show
    # ══════════════════════════════════════════════════════════════════════════
    def test_the_schedule_is_visible_without_signing_in(self):
        """Owner: if check-in is enabled, the event page shows it. The endpoint used to 401 an
        anonymous caller, so the requirement was invisible to the people deciding whether to
        enter, who are exactly the ones who lose a slot by not knowing."""
        self._open_window_now()
        resp = self.client.get(f"{STATUS_URL}?event_id={self.event.event_id}")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["checkin_enabled"])
        self.assertTrue(body["window_open"])
        self.assertIsNotNone(body["checkin_start"])
        self.assertIsNotNone(body["checkin_end"])
        # and NOTHING about any individual competitor
        self.assertNotIn("me", body)
        self.assertNotIn("teams", body)
        self.assertNotIn("solos", body)

    def test_a_signed_in_registrant_sees_their_own_state(self):
        tt, members = self._squad("Alpha")
        self._open_window_now()
        resp = self.client.get(
            f"{STATUS_URL}?event_id={self.event.event_id}", **self._auth(members[0]))
        self.assertEqual(resp.status_code, 200)
        me = resp.json()["me"]
        self.assertTrue(me["registered"])
        self.assertFalse(me["checked_in"])
        self.assertTrue(me["is_squad"])
        self.assertEqual(me["roster_total"], 4)

    def test_an_organizer_sees_which_players_have_checked_in_by_name(self):
        """Owner 2026-08-04: "admins or organizers should also be able to see who has checked in
        for each team and who hasn't". A squad reading 3 of 4 says a problem exists and nothing
        about who to message, which leaves the organizer to work out the missing name from a
        roster page in another tab while the window is running out.

        The absent players are listed as plainly as the present ones, with checked_in false and
        no timestamp, rather than being left as a subtraction the reader performs.
        """
        tt, members = self._squad("Alpha")
        self._open_window_now()
        when = timezone.now() - datetime.timedelta(minutes=5)
        for u in members[:3]:
            self._check_in_at(u, when)

        body = self.client.get(
            f"{STATUS_URL}?event_id={self.event.event_id}", **self._auth(self.admin)).json()
        squad = next(t for t in body["teams"] if t["tournament_team_id"] == tt.pk)
        self.assertEqual(squad["roster_checked_in"], 3)
        self.assertEqual(squad["roster_total"], 4)

        players = {p["username"]: p for p in squad["players"]}
        self.assertEqual(len(players), 4, "every rostered player is listed, present or not")
        for u in members[:3]:
            self.assertTrue(players[u.username]["checked_in"])
            self.assertIsNotNone(players[u.username]["checked_in_at"])
        missing = players[members[3].username]
        self.assertFalse(missing["checked_in"])
        self.assertIsNone(missing["checked_in_at"], "nobody gets a time they did not earn")

    def test_a_player_cannot_see_who_else_checked_in(self):
        """The named list is an organizer tool. It is also a roster of who is present, so it stays
        behind the same manager gate the rest of the breakdown is behind: adding names to that
        payload must not quietly widen who can read it."""
        tt, members = self._squad("Alpha")
        self._open_window_now()
        self._check_in_at(members[0], timezone.now())

        body = self.client.get(
            f"{STATUS_URL}?event_id={self.event.event_id}", **self._auth(members[1])).json()
        self.assertNotIn("teams", body)
        self.assertNotIn("solos", body)
        self.assertFalse(body.get("is_manager", False))

    def test_an_organizer_sees_every_competitor_but_a_player_does_not(self):
        tt, members = self._squad("Alpha")
        self._open_window_now()
        as_admin = self.client.get(
            f"{STATUS_URL}?event_id={self.event.event_id}", **self._auth(self.admin)).json()
        self.assertIn("teams", as_admin)
        self.assertTrue(as_admin.get("is_manager"))

        as_player = self.client.get(
            f"{STATUS_URL}?event_id={self.event.event_id}", **self._auth(members[0])).json()
        self.assertNotIn("teams", as_player)
        self.assertNotIn("solos", as_player)
