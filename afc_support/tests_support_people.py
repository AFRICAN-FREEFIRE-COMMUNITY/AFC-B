"""
afc_support/tests_support_people.py - the desk by PERSON (inbox #169 / #174).

Owner 2026-10-08: "view all messages from each user in a single place without having to scroll,
... filter requests by dates, time, country etc. ... reply all messages together or at least reply
one by one." Approved preview: WEBSITE/mockups/support-desk-v2/support-desk-preview.html.

Pins: grouping (an account's tickets are one person, a signed-out sender's tickets are one person by
email), every filter, the person view, one reply recorded on several requests with ONE email that
names the others, a reply to one request, the bulk reply, refusals with codes, the staff gate, and
that a person key carries neither an email address nor a database id.

Run: ../backend/.venv/Scripts/python.exe tools/run_tests.py -- afc_support.tests_support_people
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_support.models import SupportMessage, SupportTicket
from afc_support.tests_support_desk import _real_png
from afc_support.views import create_ticket_from_contact
from afc_support.views_people import person_key


class SupportPeopleTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.sent_email = []
        self.dms = []
        p1 = patch("afc_support.notify.send_email",
                   side_effect=lambda to, subject, body, language="en", prelocalized=False,
                                     reply_to=None, from_name=None: (
                       self.sent_email.append((to, subject, body)) or True))
        p1.start()
        self.addCleanup(p1.stop)
        p2 = patch("afc_support.notify.send_discord_dm",
                   side_effect=lambda discord_id, content: (self.dms.append((discord_id, content)) or True))
        p2.start()
        self.addCleanup(p2.stop)

        self.staff = self._user("deskhand", "desk@gmail.com", role_name="support_admin")
        self.stranger = self._user("nobody", "nobody@gmail.com")
        self.tunde = self._user("tunde_rush", "tunde@gmail.com", country="Nigeria", discord_id="111")
        self.ama = self._user("ama_sniper", "ama@gmail.com", country="Ghana")
        # Tunde: three requests (two open, one resolved), one with a picture.
        self.t1, _, _ = create_ticket_from_contact("Tunde Bakare", "tunde@gmail.com", "cannot join a team",
                                                   files=[SimpleUploadedFile("join.png", _real_png(), content_type="image/png")])
        self.t2, _, _ = create_ticket_from_contact("Tunde Bakare", "tunde@gmail.com", "still cannot join",
                                                   source=SupportTicket.SOURCE_HELP_BOT)
        self.t3, _, _ = create_ticket_from_contact("Tunde Bakare", "tunde@gmail.com", "uid already used")
        SupportTicket.objects.filter(pk=self.t3.pk).update(status=SupportTicket.STATUS_RESOLVED)
        # Ama: one open request.
        self.a1, _, _ = create_ticket_from_contact("Ama Mensah", "ama@gmail.com", "diamonds not received")
        # A signed-out sender, two requests from the same address (different case).
        self.k1, _, _ = create_ticket_from_contact("Kwame", "Kwame@Example.com", "locked out")
        self.k2, _, _ = create_ticket_from_contact("Kwame", "kwame@example.com", "still locked out")
        self.sent_email.clear()
        self.dms.clear()

    def _user(self, username, email, role_name=None, **extra):
        user = User.objects.create(username=username, email=email, full_name=username.title(), password="x", **extra)
        if role_name:
            row, _ = Roles.objects.get_or_create(role_name=role_name, defaults={"description": role_name})
            UserRoles.objects.create(user=user, role=row)
        return user

    def _auth(self, user):
        token = SessionToken.objects.create(user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
                                            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _people(self, **params):
        r = self.client.get("/support/people/", params, **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _names(self, **params):
        return [p["name"] for p in self._people(**params)["results"]]

    # ── grouping ─────────────────────────────────────────────────────────────────────────────
    def test_one_row_per_person(self):
        data = self._people()
        rows = {p["name"]: p for p in data["results"]}
        self.assertEqual(set(rows), {"Tunde Bakare", "Ama Mensah", "Kwame"})
        self.assertEqual(rows["Tunde Bakare"]["ticket_count"], 3)
        self.assertEqual(rows["Tunde Bakare"]["open_count"], 2)
        self.assertEqual(rows["Tunde Bakare"]["status"], "open")
        self.assertTrue(rows["Tunde Bakare"]["has_discord"])
        self.assertEqual(rows["Kwame"]["ticket_count"], 2)      # one address, two spellings of case
        self.assertTrue(rows["Ama Mensah"]["needs_reply"])
        self.assertEqual(data["status_counts"]["resolved"], 1)

    def test_a_person_key_hides_the_email_and_the_id(self):
        for p in self._people()["results"]:
            self.assertNotIn("@", p["key"])
            self.assertNotIn(str(self.tunde.user_id), p["key"][:2])
            self.assertEqual(len(p["key"]), 24)

    # ── filters ──────────────────────────────────────────────────────────────────────────────
    def test_status_country_source_files_and_search(self):
        self.assertEqual(self._names(status="resolved"), ["Tunde Bakare"])
        self.assertEqual(set(self._names(country="nigeria")), {"Tunde Bakare"})
        self.assertEqual(set(self._names(country="ghana")), {"Ama Mensah"})
        self.assertEqual(self._names(source="help_bot"), ["Tunde Bakare"])
        self.assertEqual(self._names(has_files="1"), ["Tunde Bakare"])
        self.assertEqual(self._names(q=self.a1.ticket_number), ["Ama Mensah"])
        self.assertEqual(self._names(q="locked out"), ["Kwame"])
        countries = {c["value"] for c in self._people()["countries"]}
        self.assertEqual(countries, {"nigeria", "ghana"})

    def test_date_and_time_window(self):
        old = timezone.now() - timedelta(days=20)
        SupportTicket.objects.filter(pk=self.a1.pk).update(created_at=old, last_message_at=old)
        since = (timezone.now() - timedelta(days=7)).isoformat()
        self.assertNotIn("Ama Mensah", self._names(date_from=since))
        until = (timezone.now() - timedelta(days=10)).isoformat()
        self.assertEqual(self._names(date_to=until), ["Ama Mensah"])

    def test_assigned_filter(self):
        SupportTicket.objects.filter(pk=self.a1.pk).update(assigned_to=self.staff)
        self.assertEqual(self._names(assigned="me"), ["Ama Mensah"])
        self.assertNotIn("Ama Mensah", self._names(assigned="none"))

    def test_bad_values_are_refused_with_codes(self):
        h = self._auth(self.staff)
        self.assertEqual(self.client.get("/support/people/", {"status": "lost"}, **h).json()["code"], "bad_status")
        self.assertEqual(self.client.get("/support/people/", {"date_from": "yesterday"}, **h).json()["code"], "bad_date")
        self.assertEqual(self.client.get("/support/people/", {"source": "fax"}, **h).json()["code"], "bad_source")
        self.assertEqual(self.client.get("/support/people/", {"q": "x" * 121}, **h).json()["code"], "query_too_long")

    def test_staff_only(self):
        self.assertEqual(self.client.get("/support/people/").status_code, 401)
        self.assertEqual(self.client.get("/support/people/", **self._auth(self.stranger)).status_code, 403)

    # ── the person ───────────────────────────────────────────────────────────────────────────
    def test_the_person_view_has_every_request_and_signed_files(self):
        r = self.client.get(f"/support/people/{person_key(self.t1)}/", **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content)
        numbers = [t["ticket_number"] for t in r.json()["tickets"]]
        self.assertEqual(set(numbers), {self.t1.ticket_number, self.t2.ticket_number, self.t3.ticket_number})
        files = [a for t in r.json()["tickets"] for m in t["messages"] for a in m["attachments"]]
        self.assertEqual(len(files), 1)
        self.assertIn("?s=", files[0]["url"])

    def test_a_ticket_number_opens_its_person(self):
        r = self.client.get("/support/people/by-ticket/", {"ticket": self.k2.ticket_number}, **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["person"]["ticket_count"], 2)
        self.assertEqual(self.client.get("/support/people/nope/", **self._auth(self.staff)).json()["code"],
                         "person_not_found")

    # ── replies ──────────────────────────────────────────────────────────────────────────────
    def test_reply_to_all_open_requests_sends_one_email(self):
        r = self.client.post(f"/support/people/{person_key(self.t1)}/reply/",
                             {"message": "The window opens on 1 January."}, **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(set(r.json()["answered"]), {self.t1.ticket_number, self.t2.ticket_number})
        for t in (self.t1, self.t2):
            t.refresh_from_db()
            self.assertEqual(t.status, SupportTicket.STATUS_WAITING)
            self.assertTrue(SupportMessage.objects.filter(ticket=t, direction="out", body__startswith="The window").exists())
        self.t3.refresh_from_db()
        self.assertEqual(self.t3.status, SupportTicket.STATUS_RESOLVED)      # untouched
        self.assertEqual(len(self.sent_email), 1)                             # ONE email
        newest, other = r.json()["answered"]
        self.assertIn(other, self.sent_email[0][2])                           # naming the other request
        self.assertEqual(len(self.dms), 1)

    def test_reply_to_one_request_and_resolve(self):
        r = self.client.post(f"/support/people/{person_key(self.t1)}/reply/",
                             {"message": "Fixed.", "ticket_numbers": self.t2.ticket_number, "resolve": "true"},
                             **self._auth(self.staff))
        self.assertEqual(r.json()["answered"], [self.t2.ticket_number])
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t2.status, SupportTicket.STATUS_RESOLVED)
        self.assertEqual(self.t1.status, SupportTicket.STATUS_OPEN)

    def test_reply_refusals(self):
        h = self._auth(self.staff)
        url = f"/support/people/{person_key(self.t1)}/reply/"
        self.assertEqual(self.client.post(url, {"message": " "}, **h).json()["code"], "reply_empty")
        self.assertEqual(self.client.post(url, {"message": "x", "ticket_numbers": self.a1.ticket_number}, **h).json()["code"],
                         "ticket_not_theirs")
        SupportTicket.objects.filter(email__iexact="ama@gmail.com").update(status=SupportTicket.STATUS_CLOSED)
        self.assertEqual(self.client.post(f"/support/people/{person_key(self.a1)}/reply/", {"message": "x"}, **h).json()["code"],
                         "no_open_requests")

    def test_bulk_reply_emails_each_person_once(self):
        keys = [person_key(self.t1), person_key(self.a1), person_key(self.k1)]
        SupportTicket.objects.filter(pk__in=[self.k1.pk, self.k2.pk]).update(status=SupportTicket.STATUS_CLOSED)
        r = self.client.post("/support/people/bulk-reply/", {"keys": keys, "message": "Maintenance tonight."},
                             content_type="application/json", **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["people_sent"], 2)
        self.assertEqual(r.json()["requests_answered"], 3)   # Tunde's two + Ama's one
        self.assertEqual(r.json()["skipped"], 1)             # Kwame has nothing open
        self.assertEqual(sorted(e[0] for e in self.sent_email), ["ama@gmail.com", "tunde@gmail.com"])
        self.assertEqual(self.client.post("/support/people/bulk-reply/", {"keys": [], "message": "x"},
                                          content_type="application/json", **self._auth(self.staff)).json()["code"],
                         "no_people")
