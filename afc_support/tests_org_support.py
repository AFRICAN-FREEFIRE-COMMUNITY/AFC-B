"""
afc_support/tests_org_support.py - questions asked of an ORGANIZER (inbox #167 / #175).

Owner 2026-10-08: "We want to give organizers their own support feature ... people will be able to
ask them questions and they should be able to answer and view things sent to them, including
attachments." Decisions: "Signed-in players only" may ask; "Only head admin and super admins can see
stuff of organizer".

Pins: who may ask (signed in only, active organization, the event must be the organization's own,
a cap per hour); who is told (the answerers, by notification and email, never AFC's support inbox);
who may read and answer (owner, a member with Answer support, AFC head / super admins; NOT ordinary
AFC support staff, NOT a plain sub-organizer, NOT another organization); that the AFC desk and the
old queue leave organizer tickets out and the organizer desk leaves AFC tickets out; that replies
are signed by the organization; the attachment door; support/access/ desks; My tickets labelling.

Run: ../backend/.venv/Scripts/python.exe tools/run_tests.py -- afc_support.tests_org_support
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Notifications, Roles, SessionToken, User, UserRoles
from afc_organizers.models import Organization, OrganizationMember
from afc_support.models import SupportAttachment, SupportTicket
from afc_support.tests_support_desk import _real_png
from afc_support.views import create_ticket_from_contact
from afc_support.views_org import QUESTIONS_PER_HOUR
from afc_support.views_people import person_key
from afc_tournament_and_scrims.models import Event


def _event(org, creator, name):
    return Event.objects.create(
        event_name=name, competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=16, event_mode="single",
        start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=4),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=2),
        number_of_stages=1, creator=creator, is_public=True, is_draft=False, organization=org,
    )


class OrganizerSupportTests(TestCase):
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

        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        self.other_org = Organization.objects.create(slug="rival", name="Rival Cups")
        self.owner = self._user("acme_owner", "owner@acme.test", language="fr")
        self.helper = self._user("acme_helper", "helper@acme.test")
        self.sub = self._user("acme_sub", "sub@acme.test")
        self.rival_owner = self._user("rival_owner", "owner@rival.test")
        OrganizationMember.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationMember.objects.create(organization=self.org, user=self.helper, can_answer_support=True)
        OrganizationMember.objects.create(organization=self.org, user=self.sub, can_edit_events=True)
        OrganizationMember.objects.create(organization=self.other_org, user=self.rival_owner, role="owner")

        self.support = self._user("deskhand", "desk@afc.test", role_name="support_admin")
        self.head = self._user("headadmin", "head@afc.test", role_name="head_admin")
        self.player = self._user("tunde_rush", "tunde@player.test", discord_id="111")
        self.event = _event(self.org, self.owner, "Acme Spring Cup")
        self.rival_event = _event(self.other_org, self.rival_owner, "Rival Night")

        # An ordinary question to AFC, which no organizer desk may show.
        self.afc_ticket, _, _ = create_ticket_from_contact("Ama", "ama@player.test", "diamonds not received")
        self.sent_email.clear()

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

    def _ask(self, user=None, slug="acme", **data):
        data.setdefault("message", "When does check-in open for the Spring Cup?")
        headers = self._auth(user) if user else {}
        return self.client.post(f"/support/organizations/{slug}/ask/", data, **headers)

    def _asked(self, **data):
        r = self._ask(self.player, **data)
        self.assertEqual(r.status_code, 200, r.content)
        return SupportTicket.objects.get(ticket_number=r.json()["ticket_number"])

    # ── asking ───────────────────────────────────────────────────────────────────────────────
    def test_signed_out_cannot_ask(self):
        r = self._ask()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "auth_required")
        self.assertFalse(SupportTicket.objects.filter(organization=self.org).exists())

    def test_a_question_is_addressed_to_the_organization(self):
        ticket = self._asked(event=self.event.slug,
                             files=[SimpleUploadedFile("bracket.png", _real_png(), content_type="image/png")])
        self.assertEqual(ticket.organization, self.org)
        self.assertEqual(ticket.event, self.event)
        self.assertEqual(ticket.source, SupportTicket.SOURCE_ORGANIZER)
        self.assertEqual(ticket.user, self.player)
        self.assertEqual(ticket.discord_id, "111")
        self.assertEqual(SupportAttachment.objects.filter(message__ticket=ticket).count(), 1)

    def test_the_answerers_are_told_and_nobody_else(self):
        ticket = self._asked()
        told = set(Notifications.objects.filter(notification_type="support").values_list("user__username", flat=True))
        self.assertEqual(told, {"acme_owner", "acme_helper"})
        note = Notifications.objects.get(user=self.owner, notification_type="support")
        # The owner reads French: the notification is in French and opens the organizer desk.
        self.assertIn("Nouvelle question pour Acme Esports", note.title)
        self.assertEqual(note.target_id, f"/organizer/support?ticket={ticket.ticket_number}")
        recipients = {to for to, _s, _b in self.sent_email}
        # Only the answerers: AFC's support inbox is never emailed about an organizer's question.
        self.assertEqual(recipients, {"owner@acme.test", "helper@acme.test"})
        french = next(s for to, s, _b in self.sent_email if to == "owner@acme.test")
        self.assertIn("Nouvelle question pour Acme Esports", french)

    def test_refusals_carry_codes(self):
        h = self._auth(self.player)
        cases = [
            ({"message": "  "}, 400, "question_empty"),
            ({"message": "x" * 4001}, 400, "question_too_long"),
            ({"event": self.rival_event.slug}, 400, "event_not_found"),
        ]
        for data, code, name in cases:
            data.setdefault("message", "hello")
            r = self.client.post("/support/organizations/acme/ask/", data, **h)
            self.assertEqual((r.status_code, r.json()["code"]), (code, name), data)
        r = self.client.post("/support/organizations/nobody-here/ask/", {"message": "hi"}, **h)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "organization_not_found"))

    def test_a_suspended_organization_takes_no_questions(self):
        Organization.objects.filter(pk=self.org.pk).update(status="suspended")
        r = self._ask(self.player)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "organization_not_found"))

    def test_questions_are_capped_per_hour(self):
        for _ in range(QUESTIONS_PER_HOUR):
            self._asked()
        r = self._ask(self.player)
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_many_questions"))
        # The cap is per organization: another organizer can still be asked.
        self.assertEqual(self._ask(self.player, slug="rival").status_code, 200)

    # ── who may read ─────────────────────────────────────────────────────────────────────────
    def test_the_afc_desk_and_queue_leave_organizer_questions_out(self):
        ticket = self._asked()
        h = self._auth(self.support)
        names = [p["name"] for p in self.client.get("/support/people/", **h).json()["results"]]
        self.assertEqual(names, ["Ama"])
        queue = [t["ticket_number"] for t in self.client.get("/support/tickets/", **h).json()["results"]]
        self.assertNotIn(ticket.ticket_number, queue)
        self.assertIn(self.afc_ticket.ticket_number, queue)

    def test_ordinary_support_staff_cannot_open_an_organizer_question(self):
        ticket = self._asked(files=[SimpleUploadedFile("proof.png", _real_png(), content_type="image/png")])
        h = self._auth(self.support)
        for url in (f"/support/tickets/{ticket.ticket_number}/",):
            self.assertEqual(self.client.get(url, **h).status_code, 404)
        self.assertEqual(self.client.post(f"/support/tickets/{ticket.ticket_number}/reply/",
                                          {"message": "hi"}, **h).status_code, 404)
        self.assertEqual(self.client.post(f"/support/tickets/{ticket.ticket_number}/status/",
                                          {"status": "resolved"}, **h).status_code, 404)
        r = self.client.get("/support/people/", {"organization": "acme"}, **h)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "org_support_forbidden"))
        att = SupportAttachment.objects.get(message__ticket=ticket)
        self.assertEqual(self.client.get(f"/support/attachments/{att.id}/", **h).status_code, 404)

    def test_the_organizer_desk_shows_only_its_own_questions(self):
        ticket = self._asked()
        self._ask(self.player, slug="rival")
        for user in (self.owner, self.helper, self.head):
            r = self.client.get("/support/people/", {"organization": "acme"}, **self._auth(user))
            self.assertEqual(r.status_code, 200, (user.username, r.content))
            people = r.json()["results"]
            self.assertEqual([p["key"] for p in people], [person_key(ticket)], user.username)
            self.assertEqual(people[0]["ticket_count"], 1, user.username)

    def test_who_may_not_open_the_organizer_desk(self):
        self._asked()
        for user in (self.sub, self.rival_owner, self.player):
            r = self.client.get("/support/people/", {"organization": "acme"}, **self._auth(user))
            self.assertEqual((r.status_code, r.json()["code"]), (403, "org_support_forbidden"), user.username)
        # An organization that does not exist answers the same as one you cannot open.
        r = self.client.get("/support/people/", {"organization": "no-such-org"}, **self._auth(self.owner))
        self.assertEqual((r.status_code, r.json()["code"]), (403, "org_support_forbidden"))
        # And an organizer is not AFC staff: without a slug they get the AFC refusal.
        r = self.client.get("/support/people/", **self._auth(self.owner))
        self.assertEqual((r.status_code, r.json()["code"]), (403, "support_forbidden"))

    def test_a_suspended_organization_closes_the_desk_for_members_not_oversight(self):
        self._asked()
        Organization.objects.filter(pk=self.org.pk).update(status="suspended")
        r = self.client.get("/support/people/", {"organization": "acme"}, **self._auth(self.owner))
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/support/people/", {"organization": "acme"}, **self._auth(self.head))
        self.assertEqual(r.status_code, 200)

    def test_another_organization_cannot_open_the_ticket(self):
        ticket = self._asked()
        h = self._auth(self.rival_owner)
        self.assertEqual(self.client.get(f"/support/tickets/{ticket.ticket_number}/", **h).status_code, 404)

    # ── answering ────────────────────────────────────────────────────────────────────────────
    def test_an_organizer_reply_is_signed_by_the_organization(self):
        ticket = self._asked()
        self.sent_email.clear()
        r = self.client.post(f"/support/people/{person_key(ticket)}/reply/",
                             {"message": "Check-in opens one hour before.", "organization": "acme"},
                             **self._auth(self.helper))
        self.assertEqual(r.status_code, 200, r.content)
        out = ticket.messages.filter(direction="out").last()
        self.assertEqual(out.author_name, "Acme Esports")
        self.assertEqual(len(self.sent_email), 1)
        to, subject, body = self.sent_email[0]
        self.assertEqual(to, "tunde@player.test")
        self.assertIn("Acme Esports replied to your question", subject)
        self.assertIn("not from the AFC team", body)
        self.assertTrue(self.dms and self.dms[0][1].startswith("Acme Esports replied"))
        # The player's own page names who they are talking to and who answered.
        thread = self.client.get(f"/support/t/{ticket.public_token}/").json()
        self.assertEqual(thread["organization"], {"name": "Acme Esports", "slug": "acme"})
        self.assertEqual(thread["messages"][-1]["author_name"], "Acme Esports")

    def test_the_per_ticket_endpoints_work_for_an_answerer(self):
        ticket = self._asked(files=[SimpleUploadedFile("proof.png", _real_png(), content_type="image/png")])
        h = self._auth(self.owner)
        detail = self.client.get(f"/support/tickets/{ticket.ticket_number}/", **h)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["organization"]["slug"], "acme")
        self.assertEqual(detail.json()["event"], None)
        r = self.client.post(f"/support/tickets/{ticket.ticket_number}/reply/", {"message": "Yes."}, **h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ticket.messages.filter(direction="out").last().author_name, "Acme Esports")
        r = self.client.post(f"/support/tickets/{ticket.ticket_number}/status/", {"status": "resolved"}, **h)
        self.assertEqual(r.status_code, 200)
        att = SupportAttachment.objects.get(message__ticket=ticket)
        self.assertEqual(self.client.get(f"/support/attachments/{att.id}/", **h).status_code, 200)

    def test_an_afc_reply_is_still_signed_afc(self):
        r = self.client.post(f"/support/tickets/{self.afc_ticket.ticket_number}/reply/", {"message": "On it."},
                             **self._auth(self.support))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.afc_ticket.messages.filter(direction="out").last().author_name, "AFC Support")
        self.assertIn("AFC replied to your message", self.sent_email[-1][1])

    # ── access and lists ─────────────────────────────────────────────────────────────────────
    def test_access_lists_the_desks_each_person_may_open(self):
        self._asked()
        def desks(user):
            return self.client.get("/support/access/", **self._auth(user)).json()["organizer_desks"]
        self.assertEqual(desks(self.owner), [{"name": "Acme Esports", "slug": "acme", "open_count": 1}])
        self.assertEqual([d["slug"] for d in desks(self.helper)], ["acme"])
        self.assertEqual(desks(self.sub), [])
        self.assertEqual(desks(self.support), [])
        # Head admins see every organization that has been asked something (Rival has not).
        self.assertEqual([d["slug"] for d in desks(self.head)], ["acme"])

    def test_my_tickets_names_the_organizer(self):
        self._asked()
        rows = self.client.get("/support/mine/", **self._auth(self.player)).json()["results"]
        self.assertEqual([r["organization_name"] for r in rows], ["Acme Esports"])
