"""
afc_tournament_and_scrims/test_event_rename_and_long_names.py
================================================================================
Inbox #171 (owner, 8 Oct 2026): "an admin duplicated an event and edited somethings and it said
Server error: unexpected response format ... and then when they rename an event they duplicated, it
says no event matches the given query."

Both were real and both are pinned here:

1. A NAME LONGER THAN THE COLUMN. Event.event_name was 40 characters. The admin renamed the copy
   "THE DEVELOPMENT LEAGUE (NG) DAY 17 (Copy" to something longer; MySQL raised "Data too long for
   column 'event_name'" inside save() (production log, 11:42 to 11:44 UTC, four times), the endpoint
   answered a 500 page and the edit page could only say "unexpected response format". The column is
   100 now, and anything longer than ANY text column is refused before saving with a coded 400 that
   names the field and the limit.

2. A RENAME KILLED THE ADDRESS. edit_event re-slugged a renamed event without recording the old slug,
   and the three detail readers looked the event up by exact slug. The edit page re-read the event by
   the address it was opened on and got 404 "No Event matches the given query." (18:30:51, 18:31:00,
   18:31:25 UTC). Event.save now goes through afc_auth.slugs.sync_slug (history recorded), the readers
   resolve a retired slug, and edit_event answers the new slug so the page can move to it.

Also: duplicating no longer cuts " (Copy)" in half.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_event_rename_and_long_names --keepdb
"""
import json
from datetime import date, time, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, SlugHistory, User, UserProfile
from afc_tournament_and_scrims.models import Event


def _admin(username):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="admin", password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    return u, SessionToken.objects.create(user=u, token=f"tok_{username}").token


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class EventRenameAndLongNameTests(TestCase):
    def setUp(self):
        self.admin, self.token = _admin("renameadmin")
        self.event = Event.objects.create(
            event_name="THE DEVELOPMENT LEAGUE (NG) DAY 17 (Copy",
            competition_type="scrims", participant_type="squad", event_type="online",
            event_mode="single", max_teams_or_players=12, number_of_stages=1,
            start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=3),
            registration_open_date=date.today(), registration_end_date=date.today() + timedelta(days=2),
            event_start_time=time(22, 0), event_end_time=time(23, 59),
            prizepool="0", event_rules="Rules.", event_description="Scrim day.", creator=self.admin,
        )
        self.old_slug = self.event.slug

    def _post(self, path, payload):
        return Client().post(path, data=json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _edit(self, **payload):
        return self._post("/events/edit-event/", {"event_id": self.event.event_id, **payload})

    # ── 2. the rename ────────────────────────────────────────────────────────────────────────
    def test_a_rename_answers_the_new_address_and_keeps_the_old_one(self):
        resp = self._edit(event_name="THE DEVELOPMENT LEAGUE (NG) DAY 17 10PM")
        self.assertEqual(resp.status_code, 200, resp.content)
        new_slug = resp.json()["slug"]
        self.assertEqual(new_slug, "the-development-league-ng-day-17-10pm")
        self.assertNotEqual(new_slug, self.old_slug)
        self.assertTrue(SlugHistory.objects.filter(model="event", old_slug=self.old_slug,
                                                   object_pk=str(self.event.event_id)).exists())

        # The edit page's re-read, by the address it was opened on, finds the event.
        for path in ("/events/get-event-details-for-admin/", "/events/get-event-details/"):
            resp = self._post(path, {"slug": self.old_slug})
            self.assertEqual(resp.status_code, 200, f"{path}: {resp.content[:200]}")
        resp = Client().post("/events/get-event-details-not-logged-in/",
                             data=json.dumps({"slug": self.old_slug}), content_type="application/json")
        self.assertNotEqual(resp.status_code, 404, resp.content[:200])

    # ── inbox #209: an old address answers where the event lives now ────────────────────────
    def _read_all(self, address):
        """The three readers for one address: {path: response}."""
        out = {path: self._post(path, {"slug": address})
               for path in ("/events/get-event-details-for-admin/", "/events/get-event-details/")}
        out["/events/get-event-details-not-logged-in/"] = Client().post(
            "/events/get-event-details-not-logged-in/", data=json.dumps({"slug": address}),
            content_type="application/json")
        return out

    def test_a_retired_address_answers_moved_to_the_current_one(self):
        resp = self._edit(event_name="THE DEVELOPMENT LEAGUE (NG) DAY 17 10PM")
        new_slug = resp.json()["slug"]
        for address in (self.old_slug, str(self.event.event_id)):
            for path, resp in self._read_all(address).items():
                self.assertEqual(resp.status_code, 200, f"{path} {address}: {resp.content[:200]}")
                self.assertEqual(resp.json().get("moved_to"), f"/tournaments/{new_slug}", f"{path} {address}")

    def test_the_current_address_answers_no_move(self):
        for path, resp in self._read_all(self.old_slug).items():
            self.assertEqual(resp.status_code, 200, f"{path}: {resp.content[:200]}")
            self.assertNotIn("moved_to", resp.json(), path)

    def test_an_unknown_address_is_a_coded_404(self):
        resp = self._post("/events/get-event-details/", {"slug": "no-such-event-anywhere"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "event_not_found")

    def test_a_save_that_does_not_rename_keeps_the_address(self):
        resp = self._edit(event_description="New words.")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["slug"], self.old_slug)
        self.assertFalse(SlugHistory.objects.filter(model="event").exists())

    # ── 1. the long name ─────────────────────────────────────────────────────────────────────
    def test_a_name_past_forty_characters_now_saves(self):
        name = "THE DEVELOPMENT LEAGUE (NG) DAY 18 10PM FINAL QUALIFIER"   # 55
        resp = self._edit(event_name=name)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertEqual(self.event.event_name, name)

    def test_a_name_past_the_column_is_a_coded_400_and_nothing_changes(self):
        resp = self._edit(event_name="X" * 101, event_description="Should not land.")
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "event_name_too_long")
        self.assertEqual(body["field"], "event_name")
        self.assertEqual(body["limit"], 100)
        self.assertIn("100", body["message"])
        self.event.refresh_from_db()
        self.assertEqual(self.event.event_name, "THE DEVELOPMENT LEAGUE (NG) DAY 17 (Copy")
        self.assertEqual(self.event.event_description, "Scrim day.")

    # ── duplicating ──────────────────────────────────────────────────────────────────────────
    def test_a_copy_keeps_its_whole_suffix(self):
        self.event.event_name = "A" * 98
        self.event.save()
        resp = self._post(f"/events/{self.event.event_id}/duplicate-event/", {})
        self.assertIn(resp.status_code, (200, 201), resp.content)
        copy = Event.objects.exclude(event_id=self.event.event_id).order_by("-event_id").first()
        self.assertTrue(copy.event_name.endswith(" (Copy)"), copy.event_name)
        self.assertLessEqual(len(copy.event_name), 100)
        self.assertNotEqual(copy.slug, self.event.slug)
