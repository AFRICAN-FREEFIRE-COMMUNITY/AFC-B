"""
afc_tournament_and_scrims/test_discord_reminders.py - Discord reminders an organizer sets on an
event (owner 2026-09-14, inbox #22): "organizers should be able to set it on the website, where
they pick frequency of how the reminders send (this option is for discord only)".

Pins: the cadence is saved through the contract's cleaners (an unknown key is refused), the sweep
DMs every rostered player with Discord connected at each moment of the cadence, never the same
moment twice, never a moment older than an hour, never a waitlisted or Discord-less player, and
the Actions tab's read carries the plan and the history in one shape.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_discord_reminders
"""
import datetime

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import discord_reminders as dr
from afc_tournament_and_scrims.models import (
    Event, EventDiscordReminder, RegisteredCompetitors, TournamentTeam, TournamentTeamMember,
)


def _user(name, discord=None, lang="en"):
    return User.objects.create(username=name, email=f"{name}@x.com", full_name=name.title(),
                               password="x", role="player", discord_id=discord, language=lang)


def _event(start_dt, name="Reminder Cup", tz="UTC"):
    return Event.objects.create(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=start_dt.date(), end_date=start_dt.date(), event_start_time=start_dt.time(),
        timezone=tz,
        registration_open_date=start_dt.date() - datetime.timedelta(days=3),
        registration_end_date=start_dt.date() - datetime.timedelta(days=1),
        prizepool="0", event_rules="r", event_status="upcoming",
        registration_link="https://example.com/r", number_of_stages=1, is_draft=False,
    )


class _Base(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(username="eventboss", email="eb@x.com", full_name="Boss",
                                         password="x", role="admin")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {SessionToken.objects.create(user=self.admin, token='tok_eb').token}"}
        self.now = timezone.now().replace(minute=0, second=0, microsecond=0)
        self.start = self.now + datetime.timedelta(hours=30)
        self.event = _event(self.start)
        # a roster: two players with Discord, one without, one waitlisted team with Discord
        owner = _user("cap", discord="1001")
        self.p_fr = _user("pierre", discord="1002", lang="fr")
        self.p_none = _user("nodiscord", discord=None)
        team = Team.objects.create(team_name="Ghosts", join_settings="open", team_creator=owner,
                                   team_owner=owner, country="NG")
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=owner, status="active")
        for u in (owner, self.p_fr, self.p_none):
            TournamentTeamMember.objects.create(tournament_team=tt, user=u, event=self.event, status="active")
        waiter = _user("waiter", discord="1003")
        wteam = Team.objects.create(team_name="Waiting", join_settings="open", team_creator=waiter,
                                    team_owner=waiter, country="NG")
        wtt = TournamentTeam.objects.create(event=self.event, team=wteam, registered_by=waiter,
                                            status="active", is_waitlisted=True)
        TournamentTeamMember.objects.create(tournament_team=wtt, user=waiter, event=self.event, status="active")
        self.sent = []
        self.fake_send = lambda discord_id, content: (self.sent.append((discord_id, content)) or True)

    def _save(self, **body):
        return self.client.post(f"/events/{self.event.event_id}/discord-reminders/", body,
                                content_type="application/json", **self.auth)

    def _read(self):
        return self.client.get(f"/events/{self.event.event_id}/discord-reminders/", **self.auth)


class SettingsTests(_Base):
    def test_save_and_read_the_cadence_and_the_plan(self):
        r = self._save(frequency="daily_3d", note="Bring your A game")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["frequency"], body["note"]), ("daily_3d", "Bring your A game"))
        self.assertEqual([p["offset_hours"] for p in body["plan"]], [72, 48, 24])
        self.assertEqual({p["state"] for p in body["plan"]}, {"planned"})
        self.assertEqual(body["recipients_now"], 2)          # cap + pierre; nodiscord and the waitlist are out
        self.assertEqual(len(body["frequencies"]), 5)
        self.event.refresh_from_db()
        self.assertEqual(self.event.discord_reminder_frequency, "daily_3d")
        self.assertEqual(self._read().json()["frequency"], "daily_3d")

    def test_an_unknown_cadence_or_a_long_note_is_refused(self):
        r = self._save(frequency="every_minute")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "reminder_invalid"))
        r = self._save(frequency="off", note="x" * 201)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "reminder_invalid"))

    def test_the_card_is_for_admins_and_editing_organizers_only(self):
        stranger = _user("stranger")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {SessionToken.objects.create(user=stranger, token='tok_s').token}"}
        self.assertEqual(self.client.get(f"/events/{self.event.event_id}/discord-reminders/", **auth).status_code, 403)
        self.assertEqual(self.client.get(f"/events/{self.event.event_id}/discord-reminders/").status_code, 401)

    def test_the_contract_carries_the_two_fields_for_an_organizer_reader(self):
        from afc_tournament_and_scrims.event_contract import EVENT_FIELDS
        names = {f.name for f in EVENT_FIELDS}
        self.assertIn("discord_reminder_frequency", names)
        self.assertIn("discord_reminder_note", names)


class SweepTests(_Base):
    def test_a_due_moment_dms_every_rostered_player_with_discord_once(self):
        self.event.discord_reminder_frequency = "every_6h_1d"   # 24, 18, 12, 6
        self.event.discord_reminder_note = "Room details on the site at 19:30."
        self.event.save()
        # 24h before the start is exactly 6h from "now" (start = now + 30h): nothing due yet
        self.assertEqual(dr.send_due_reminders(now=self.now, send=self.fake_send), {"sent": 0, "skipped": 0})
        self.assertEqual(self.sent, [])
        # move to 24h before: the 24h moment is due
        at = self.start - datetime.timedelta(hours=24)
        self.assertEqual(dr.send_due_reminders(now=at, send=self.fake_send), {"sent": 1, "skipped": 0})
        self.assertEqual(sorted(d for d, _ in self.sent), ["1001", "1002"])
        english = next(c for d, c in self.sent if d == "1001")
        french = next(c for d, c in self.sent if d == "1002")
        self.assertIn("Reminder Cup starts in 1 day.", english)
        self.assertIn("From the organizer: Room details on the site at 19:30.", english)
        self.assertIn("Reminder Cup commence dans 1 jour.", french)
        row = EventDiscordReminder.objects.get(event=self.event, offset_hours=24)
        self.assertEqual((row.recipients, row.delivered, row.skipped), (2, 2, False))
        # the same sweep again: nothing more goes out
        self.assertEqual(dr.send_due_reminders(now=at + datetime.timedelta(minutes=10), send=self.fake_send),
                         {"sent": 0, "skipped": 0})
        self.assertEqual(len(self.sent), 2)

    def test_a_moment_older_than_the_grace_is_skipped_not_sent_late(self):
        self.event.discord_reminder_frequency = "daily_3d"      # 72, 48, 24
        self.event.save()
        # the cadence was set 5h before the start: 72h and 48h are long gone, 24h too (6h late)
        at = self.start - datetime.timedelta(hours=5)
        self.assertEqual(dr.send_due_reminders(now=at, send=self.fake_send), {"sent": 0, "skipped": 3})
        self.assertEqual(self.sent, [])
        plan = self._read().json()["plan"]
        self.assertEqual([p["state"] for p in plan], ["skipped", "skipped", "skipped"])

    def test_a_moment_inside_the_grace_still_goes_out(self):
        self.event.discord_reminder_frequency = "once_24h"
        self.event.save()
        at = self.start - datetime.timedelta(hours=24) + datetime.timedelta(minutes=40)
        self.assertEqual(dr.send_due_reminders(now=at, send=self.fake_send), {"sent": 1, "skipped": 0})

    def test_off_cancelled_and_started_events_send_nothing(self):
        self.event.discord_reminder_frequency = "once_24h"
        self.event.event_status = "cancelled"
        self.event.save()
        at = self.start - datetime.timedelta(hours=24)
        self.assertEqual(dr.send_due_reminders(now=at, send=self.fake_send), {"sent": 0, "skipped": 0})
        self.event.event_status = "upcoming"
        self.event.save()
        self.assertEqual(dr.send_due_reminders(now=self.start + datetime.timedelta(minutes=1), send=self.fake_send),
                         {"sent": 0, "skipped": 0})
        self.event.discord_reminder_frequency = "off"
        self.event.save()
        self.assertEqual(dr.send_due_reminders(now=at, send=self.fake_send), {"sent": 0, "skipped": 0})

    def test_a_closed_dm_counts_as_not_delivered_and_a_solo_registrant_is_included(self):
        solo = _user("solo", discord="2001")
        RegisteredCompetitors.objects.create(event=self.event, user=solo, status="registered")
        self.event.discord_reminder_frequency = "once_24h"
        self.event.save()
        refusing = lambda discord_id, content: discord_id != "2001"
        at = self.start - datetime.timedelta(hours=24)
        dr.send_due_reminders(now=at, send=refusing)
        row = EventDiscordReminder.objects.get(event=self.event, offset_hours=24)
        self.assertEqual((row.recipients, row.delivered), (3, 2))

    def test_the_start_instant_is_in_the_events_own_timezone(self):
        # A Lagos event at 20:00 starts at 19:00 UTC: the 1h-before moment is 18:00 UTC.
        lagos_start = datetime.datetime(2030, 1, 10, 20, 0)
        event = _event(lagos_start, name="Lagos Night", tz="Africa/Lagos")
        start = dr.start_instant(event)
        self.assertEqual(start.astimezone(datetime.timezone.utc).hour, 19)
