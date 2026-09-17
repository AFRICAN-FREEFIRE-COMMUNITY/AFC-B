"""
afc_support/tests_support_desk.py - the desk keeps what people send, and only the right people read it.

WHY EACH TEST IS HERE (owner 2026-09-14)
  The whole app exists because the old contact form stored NOTHING and emailed the words
  "Valid email." So the first test is the one that would have caught that: the message a person
  typed is readable back out of the database afterwards, whatever the mail server did.

  - A ticket gets a number and an opaque token, and the acknowledgement + Discord DM are attempted.
  - Files are stored, and a refused file is NAMED rather than silently dropped.
  - The requester reads their own thread by TOKEN, and their reply reopens a resolved ticket.
  - A stranger cannot read a ticket, the queue, or an attachment. Support staff can.
  - Only head admins can open the audit, and the audit shows every message with its attachments.
  - The staff reply emails and DMs the person, and records who wrote it.

Mail and Discord are stubbed at the SERVICE BOUNDARY (the names inside afc_support.views /
afc_support.notify), so nothing in this file touches Office365 or Discord.

Run: python manage.py test afc_support.tests_support_desk
"""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_support.models import SupportAttachment, SupportMessage, SupportTicket


class SupportDeskTests(TestCase):
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
                   side_effect=lambda discord_id, content: (
                       self.dms.append((discord_id, content)) or True))
        p2.start()
        self.addCleanup(p2.stop)

        self.player = User.objects.create(username="writer", email="writer@gmail.com",
                                          full_name="Writer Person", password="x",
                                          discord_id="99887766", language="fr")
        self.staff = self._user_with_role("deskhand", "desk@gmail.com", "support_admin")
        self.boss = self._user_with_role("bigboss", "boss@gmail.com", "head_admin")
        self.stranger = User.objects.create(username="nobody", email="nobody@gmail.com",
                                            full_name="No Body", password="x")

    # ── fixtures ─────────────────────────────────────────────────────────────────────────────
    def _user_with_role(self, username, email, role_name):
        user = User.objects.create(username=username, email=email, full_name=username.title(),
                                   password="x")
        row, _ = Roles.objects.get_or_create(role_name=role_name,
                                             defaults={"description": role_name})
        UserRoles.objects.create(user=user, role=row)
        return user

    def _token(self, user):
        return SessionToken.objects.create(
            user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME).token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token(user)}"}

    def _contact(self, **over):
        payload = {"name": "Writer Person", "email": "writer@gmail.com",
                   "message": "my account is locked and i cannot get in"}
        payload.update(over)
        files = payload.pop("files", None)
        if files:
            payload["files"] = files
        return self.client.post("/support/contact/", payload)

    # ── the fault this app exists to end ─────────────────────────────────────────────────────
    def test_the_message_is_readable_back_out_of_the_database(self):
        r = self._contact()
        self.assertEqual(r.status_code, 200, r.content[:300])
        number = r.json()["ticket_number"]
        self.assertTrue(number.startswith("AFC-"))

        ticket = SupportTicket.objects.get(ticket_number=number)
        first = ticket.messages.filter(direction="in").first()
        self.assertEqual(first.body, "my account is locked and i cannot get in")
        self.assertNotIn("Valid email", first.body)  # the 2026-09-14 bug, named
        self.assertEqual(ticket.email, "writer@gmail.com")
        self.assertTrue(ticket.public_token.startswith("t_"))

    def test_an_account_is_matched_so_the_language_and_discord_are_known(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        self.assertEqual(ticket.user_id, self.player.user_id)
        self.assertEqual(ticket.discord_id, "99887766")
        # The acknowledgement went in the account's language, and the DM went out.
        self.assertTrue(any("writer@gmail.com" == to for to, _s, _b in self.sent_email))
        self.assertEqual(self.dms[0][0], "99887766")
        self.assertIn(ticket.ticket_number, self.dms[0][1])

    def test_a_stranger_with_no_account_still_gets_a_ticket(self):
        r = self._contact(email="someone@some-company.co.uk", name="Someone Else")
        self.assertEqual(r.status_code, 200)
        ticket = SupportTicket.objects.get(ticket_number=r.json()["ticket_number"])
        self.assertIsNone(ticket.user_id)
        self.assertEqual(self.dms, [])  # nothing to DM

    def test_the_acknowledgement_is_recorded_as_an_outgoing_message(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        auto = ticket.messages.filter(channel="auto").first()
        self.assertIsNotNone(auto)
        self.assertEqual(auto.direction, "out")
        self.assertIn("Acknowledgement sent", auto.body)

    # ── files ────────────────────────────────────────────────────────────────────────────────
    def test_an_attached_picture_is_stored_with_its_real_name(self):
        upload = SimpleUploadedFile("proof of payment.png", b"\x89PNG fake bytes",
                                    content_type="image/png")
        r = self._contact(files=[upload])
        self.assertEqual(r.status_code, 200, r.content[:300])
        att = SupportAttachment.objects.get()
        self.assertEqual(att.original_name, "proof of payment.png")
        self.assertEqual(att.content_type, "image/png")
        self.assertGreater(att.size_bytes, 0)

    def test_a_refused_file_is_named_rather_than_dropped_in_silence(self):
        bad = SimpleUploadedFile("hack.exe", b"MZ", content_type="application/octet-stream")
        r = self._contact(files=[bad])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SupportAttachment.objects.count(), 0)
        self.assertEqual(r.json()["rejected_files"], [{"name": "hack.exe", "reason": "type"}])

    # ── the requester's own thread ───────────────────────────────────────────────────────────
    def test_the_thread_opens_by_token_and_hides_which_staff_member_answered(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        SupportMessage.objects.create(ticket=ticket, direction="out", channel="web",
                                      author=self.staff, author_name="AFC Support",
                                      body="we are on it")
        r = self.client.get(f"/support/t/{ticket.public_token}/")
        self.assertEqual(r.status_code, 200)
        bodies = [m["body"] for m in r.json()["messages"]]
        self.assertIn("we are on it", bodies)
        for m in r.json()["messages"]:
            self.assertNotIn("author_username", m)  # never leaked to the requester

    def test_a_wrong_token_is_a_404(self):
        self._contact()
        self.assertEqual(self.client.get("/support/t/t_notarealtoken/").status_code, 404)

    def test_their_reply_reopens_a_resolved_ticket(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        ticket.status = SupportTicket.STATUS_RESOLVED
        ticket.save(update_fields=["status"])
        r = self.client.post(f"/support/t/{ticket.public_token}/reply/",
                             {"message": "it is still broken"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        self.assertEqual(ticket.messages.filter(direction="in").count(), 2)

    def test_an_empty_reply_is_refused(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        r = self.client.post(f"/support/t/{ticket.public_token}/reply/", {"message": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "reply_empty")

    # ── who may work the desk ────────────────────────────────────────────────────────────────
    def test_the_queue_is_closed_to_strangers_and_open_to_support(self):
        self._contact()
        self.assertEqual(self.client.get("/support/tickets/").status_code, 401)
        self.assertEqual(self.client.get("/support/tickets/",
                                         **self._auth(self.stranger)).status_code, 403)
        r = self.client.get("/support/tickets/", **self._auth(self.staff))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_count"], 1)
        self.assertEqual(r.json()["open_count"], 1)

    def test_support_can_search_the_queue_by_what_was_written(self):
        self._contact()
        self._contact(message="where is my prize money", email="other@gmail.com", name="Other")
        r = self.client.get("/support/tickets/?q=prize", **self._auth(self.staff))
        self.assertEqual(r.json()["total_count"], 1)
        self.assertEqual(r.json()["results"][0]["email"], "other@gmail.com")

    # ── answering ────────────────────────────────────────────────────────────────────────────
    def test_a_staff_reply_is_stored_emailed_and_dmed(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        self.sent_email.clear()
        self.dms.clear()
        r = self.client.post(f"/support/tickets/{ticket.ticket_number}/reply/",
                             {"message": "unlocked it for you"}, **self._auth(self.staff))
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(r.json()["emailed"])
        self.assertTrue(r.json()["discord_dm"])
        out = ticket.messages.filter(direction="out", channel="web").last()
        self.assertEqual(out.body, "unlocked it for you")
        self.assertEqual(out.author_id, self.staff.user_id)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.STATUS_WAITING)
        self.assertEqual(ticket.assigned_to_id, self.staff.user_id)

    def test_a_stranger_cannot_reply_as_afc(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        r = self.client.post(f"/support/tickets/{ticket.ticket_number}/reply/",
                             {"message": "hi"}, **self._auth(self.stranger))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(ticket.messages.filter(body="hi").exists())

    def test_status_can_be_set_and_a_nonsense_status_is_refused(self):
        self._contact()
        ticket = SupportTicket.objects.first()
        ok = self.client.post(f"/support/tickets/{ticket.ticket_number}/status/",
                              {"status": "resolved"}, **self._auth(self.staff))
        self.assertEqual(ok.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")
        bad = self.client.post(f"/support/tickets/{ticket.ticket_number}/status/",
                               {"status": "banana"}, **self._auth(self.staff))
        self.assertEqual(bad.status_code, 400)

    # ── attachments are private ──────────────────────────────────────────────────────────────
    def test_an_attachment_needs_staff_or_the_tickets_own_token(self):
        upload = SimpleUploadedFile("id card.png", b"\x89PNG bytes", content_type="image/png")
        self._contact(files=[upload])
        att = SupportAttachment.objects.get()
        ticket = SupportTicket.objects.first()

        self.assertEqual(self.client.get(f"/support/attachments/{att.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/support/attachments/{att.id}/",
                                         **self._auth(self.stranger)).status_code, 404)
        self.assertEqual(self.client.get(f"/support/attachments/{att.id}/",
                                         **self._auth(self.staff)).status_code, 200)
        self.assertEqual(self.client.get(
            f"/support/attachments/{att.id}/?t={ticket.public_token}").status_code, 200)

    # ── the audit ────────────────────────────────────────────────────────────────────────────
    def test_only_head_admins_open_the_audit(self):
        self._contact()
        self.assertEqual(self.client.get("/support/audit/").status_code, 401)
        self.assertEqual(self.client.get("/support/audit/",
                                         **self._auth(self.stranger)).status_code, 403)
        # Support staff work tickets but do NOT see the audit: that was the owner's line.
        self.assertEqual(self.client.get("/support/audit/",
                                         **self._auth(self.staff)).status_code, 403)
        self.assertEqual(self.client.get("/support/audit/",
                                         **self._auth(self.boss)).status_code, 200)

    def test_the_audit_shows_every_message_with_its_files_and_times(self):
        upload = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4", content_type="application/pdf")
        self._contact(files=[upload])
        ticket = SupportTicket.objects.first()
        self.client.post(f"/support/tickets/{ticket.ticket_number}/reply/",
                         {"message": "sorted"}, **self._auth(self.staff))

        r = self.client.get("/support/audit/", **self._auth(self.boss))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # the visitor's message, the automatic acknowledgement, and the staff reply
        self.assertEqual(body["total_count"], 3)
        self.assertEqual(body["attachment_count"], 1)
        rows = {row["direction"] + ":" + row["channel"]: row for row in body["results"]}
        self.assertIn("in:web", rows)
        self.assertIn("out:auto", rows)
        self.assertIn("out:web", rows)
        self.assertEqual(rows["out:web"]["author_username"], "deskhand")
        self.assertEqual(rows["in:web"]["attachments"][0]["name"], "receipt.pdf")
        for row in body["results"]:
            self.assertTrue(row["created_at"])  # full timestamp on every row

    def test_the_audit_can_be_searched_and_filtered(self):
        self._contact(message="where is my prize money")
        r = self.client.get("/support/audit/?q=prize&direction=in", **self._auth(self.boss))
        self.assertEqual(r.json()["total_count"], 1)

    # ── what the frontend asks before drawing anything ───────────────────────────────────────
    def test_access_tells_the_frontend_what_to_draw(self):
        anon = self.client.get("/support/access/").json()
        self.assertEqual(anon, {"can_work_tickets": False, "can_read_audit": False})
        desk = self.client.get("/support/access/", **self._auth(self.staff)).json()
        self.assertEqual(desk, {"can_work_tickets": True, "can_read_audit": False})
        boss = self.client.get("/support/access/", **self._auth(self.boss)).json()
        self.assertEqual(boss, {"can_work_tickets": True, "can_read_audit": True})

    # ── the legacy endpoint still stores ─────────────────────────────────────────────────────
    def test_the_old_contact_endpoint_opens_a_ticket_too(self):
        with patch("afc_auth.views.send_email", return_value=True):
            r = self.client.post("/auth/contact-us/",
                                 {"name": "Old Client", "email": "old@gmail.com",
                                  "message": "posted to the old address"},
                                 content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(r.json()["ticket_number"].startswith("AFC-"))
        self.assertTrue(SupportMessage.objects.filter(
            body="posted to the old address").exists())
