"""
afc_polls.tests_awards_release - the KOL awards production pieces (owner, 11 Nov 2026, inbox #537 #543).

  1. HIDDEN UNTIL RELEASE (#543): a hidden edition, its ballots and its pages answer 404 to a stranger and to a player,
     are left out of every list, and take no vote from anybody; an AFC poll admin previews it. A themed edition is
     created hidden; switching it off releases it.
  2. ONE WHATSAPP NUMBER, ONE BALLOT (#449): an edition with whatsapp_required refuses a ballot from an account that
     has not proved a number; the voter proves it by SENDING "AFC <code>" (the webhook's inbound handler) or by typing
     back a code AFC sent; a number backs one account; codes are hashed, expire, are used once, and sending is capped.
  3. THE HALL OF FAME (#445): every published winner of every released edition, most decorated counted across seasons
     by the linked account or the name; nothing from a hidden edition.

Run: AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls.tests_awards_release
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import AwardsEdition, PollOption, PollQuestion, VoterWhatsApp, VoterWhatsAppCode
from .tests import make_user, token_for
from .tests_kol_awards import kol_edition, make_admin
from . import whatsapp_votes


class HiddenUntilReleaseTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.q1, self.q2 = kol_edition()
        AwardsEdition.objects.filter(pk=self.edition.pk).update(hidden=True)
        self.player = make_user("hidfan")
        self.admin = make_admin("hidboss")
        self.client = APIClient()

    def test_strangers_and_players_get_404_everywhere(self):
        for who in ({}, token_for(self.player)):
            self.assertEqual(self.client.get(f"/polls/editions/{self.edition.slug}/", **who).status_code, 404)
            self.assertEqual(self.client.get(f"/polls/{self.poll.slug}/", **who).status_code, 404)
            listed = [e["slug"] for e in self.client.get("/polls/editions/", **who).json()["results"]]
            self.assertNotIn(self.edition.slug, listed)
            polls = [p["slug"] for p in self.client.get("/polls/", **who).json()["results"]]
            self.assertNotIn(self.poll.slug, polls)

    def test_an_admin_previews_it(self):
        detail = self.client.get(f"/polls/editions/{self.edition.slug}/", **token_for(self.admin))
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["edition"]["hidden"])
        listed = [e["slug"] for e in self.client.get("/polls/editions/", **token_for(self.admin)).json()["results"]]
        self.assertIn(self.edition.slug, listed)

    def test_nobody_votes_in_a_hidden_edition(self):
        option = PollOption.objects.filter(question=self.q1).first().option_id
        for user in (self.player, self.admin):
            r = self.client.post(f"/polls/{self.poll.slug}/responses/",
                                 {"answers": [{"question_id": self.q1.question_id, "option_ids": [option]}], "merge": True},
                                 format="json", **token_for(user))
            self.assertEqual(r.status_code, 404)

    def test_a_themed_edition_is_created_hidden_and_released_by_a_switch(self):
        made = self.client.post("/polls/admin/editions/", {"title": "KOL 2027", "theme": "detty-royale"},
                                format="json", **token_for(self.admin)).json()["slug"]
        self.assertTrue(AwardsEdition.objects.get(slug=made).hidden)
        plain = self.client.post("/polls/admin/editions/", {"title": "Plain 2027"}, format="json",
                                 **token_for(self.admin)).json()["slug"]
        self.assertFalse(AwardsEdition.objects.get(slug=plain).hidden)
        self.client.patch(f"/polls/admin/editions/{made}/", {"hidden": False}, format="json", **token_for(self.admin))
        self.assertFalse(AwardsEdition.objects.get(slug=made).hidden)
        self.assertEqual(self.client.get(f"/polls/editions/{made}/").status_code, 200)

    def test_a_player_cannot_release_it(self):
        r = self.client.patch(f"/polls/admin/editions/{self.edition.slug}/", {"hidden": False}, format="json",
                              **token_for(self.player))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(AwardsEdition.objects.get(pk=self.edition.pk).hidden)


@override_settings(WHATSAPP_BUSINESS_NUMBER="+234 801 000 0000", WHATSAPP_LOGIN_CODE_TEMPLATE="afc_login_code")
class WhatsAppCheckTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.q1, self.q2 = kol_edition("kol-wa")
        AwardsEdition.objects.filter(pk=self.edition.pk).update(whatsapp_required=True)
        self.voter = make_user("wafan")
        self.other = make_user("wafan2")
        self.client = APIClient()

    def vote(self, user):
        option = PollOption.objects.filter(question=self.q1).first().option_id
        return self.client.post(f"/polls/{self.poll.slug}/responses/",
                                {"answers": [{"question_id": self.q1.question_id, "option_ids": [option]}], "merge": True},
                                format="json", **token_for(user))

    def inbound(self, text, sender="2348012345678"):
        whatsapp_votes.handle_inbound_message({"from": sender, "type": "text", "text": {"body": text}})

    def test_no_number_no_ballot(self):
        r = self.vote(self.voter)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "whatsapp_check_required"))

    def test_sending_the_code_proves_the_number_and_then_the_vote_counts(self):
        start = self.client.post("/polls/whatsapp/start/", **token_for(self.voter)).json()
        self.assertTrue(start["wa_link"].startswith("https://wa.me/2348010000000?text=AFC%20"))
        self.inbound("hi, my code is afc " + start["code"].lower())
        status = self.client.get("/polls/whatsapp/", **token_for(self.voter)).json()
        self.assertEqual((status["verified"], status["last4"], status["method"]), (True, "5678", "inbound"))
        self.assertEqual(self.vote(self.voter).status_code, 201)
        # used once: the same code from another number proves nothing for anybody
        self.inbound("AFC " + start["code"], sender="2348099999999")
        self.assertEqual(VoterWhatsApp.objects.count(), 1)

    def test_a_number_backs_one_account(self):
        first = self.client.post("/polls/whatsapp/start/", **token_for(self.voter)).json()["code"]
        self.inbound("AFC " + first)
        second = self.client.post("/polls/whatsapp/start/", **token_for(self.other)).json()["code"]
        self.inbound("AFC " + second)                         # same sender number
        self.assertFalse(VoterWhatsApp.objects.filter(user=self.other).exists())
        self.assertEqual(self.vote(self.other).json()["code"], "whatsapp_check_required")

    def test_an_expired_code_proves_nothing(self):
        code = self.client.post("/polls/whatsapp/start/", **token_for(self.voter)).json()["code"]
        VoterWhatsAppCode.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.inbound("AFC " + code)
        self.assertFalse(VoterWhatsApp.objects.exists())

    def test_codes_are_stored_hashed(self):
        code = self.client.post("/polls/whatsapp/start/", **token_for(self.voter)).json()["code"]
        row = VoterWhatsAppCode.objects.get()
        self.assertNotIn(code, row.code_hash)
        self.assertEqual(len(row.code_hash), 64)

    @mock.patch("afc_polls.whatsapp_votes._deliver", return_value=True)
    def test_send_me_a_code_then_type_it(self, deliver):
        sent = self.client.post("/polls/whatsapp/send-code/", {"number": "+234 803 111 2233"}, format="json",
                                **token_for(self.voter))
        self.assertEqual((sent.status_code, sent.json()["last4"]), (200, "2233"))
        code = deliver.call_args[0][2]
        wrong = self.client.post("/polls/whatsapp/verify/", {"code": "000000" if code != "000000" else "111111"},
                                 format="json", **token_for(self.voter))
        self.assertEqual(wrong.json()["code"], "whatsapp_code_wrong")
        ok = self.client.post("/polls/whatsapp/verify/", {"code": code}, format="json", **token_for(self.voter))
        self.assertEqual((ok.status_code, ok.json()["last4"]), (200, "2233"))
        self.assertEqual(self.vote(self.voter).status_code, 201)

    @mock.patch("afc_polls.whatsapp_votes._deliver", return_value=True)
    def test_three_codes_an_hour(self, _deliver):
        for _ in range(3):
            self.assertEqual(self.client.post("/polls/whatsapp/send-code/", {"number": "+2348031112233"}, format="json",
                                              **token_for(self.voter)).status_code, 200)
        r = self.client.post("/polls/whatsapp/send-code/", {"number": "+2348031112233"}, format="json",
                             **token_for(self.voter))
        self.assertEqual((r.status_code, r.json()["code"]), (429, "whatsapp_too_many_codes"))

    @mock.patch("afc_polls.whatsapp_votes._deliver", return_value=True)
    def test_too_many_wrong_codes_spend_it(self, _deliver):
        self.client.post("/polls/whatsapp/send-code/", {"number": "+2348031112233"}, format="json", **token_for(self.voter))
        VoterWhatsAppCode.objects.update(attempts=whatsapp_votes.MAX_TRIES)
        r = self.client.post("/polls/whatsapp/verify/", {"code": "123456"}, format="json", **token_for(self.voter))
        self.assertEqual((r.status_code, r.json()["code"]), (429, "whatsapp_too_many_tries"))

    def test_signed_out_is_refused(self):
        for path in ("/polls/whatsapp/start/", "/polls/whatsapp/send-code/", "/polls/whatsapp/verify/"):
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 401)
        self.assertEqual(self.client.get("/polls/whatsapp/").status_code, 401)

    @override_settings(WHATSAPP_BUSINESS_NUMBER="")
    def test_no_business_number_means_send_me_a_code_only(self):
        self.assertFalse(self.client.get("/polls/whatsapp/", **token_for(self.voter)).json()["inbound"])
        r = self.client.post("/polls/whatsapp/start/", **token_for(self.voter))
        self.assertEqual((r.status_code, r.json()["code"]), (409, "whatsapp_inbound_off"))


class PublishedWinnerIsPublicTests(TestCase):
    """An award ballot keeps results_visibility=admins_only by default. Once the edition's winners are out, the
    PUBLISHED winner is still named to everybody (the winners page, the award pages), and no vote count leaks."""

    def setUp(self):
        self.edition, self.poll, self.q1, self.q2 = kol_edition("kol-claims")
        self.winner = PollOption.objects.get(question=self.q1, label="ARMYKID")
        PollQuestion.objects.filter(pk=self.q1.pk).update(published_winner_option=self.winner, published_winner_votes=41)

    def questions(self, url):
        body = APIClient().get(url).json()
        polls = body.get("polls") or [body]
        return [q for p in polls for q in (p.get("questions") or [])]

    def test_named_once_the_winners_are_out_with_no_numbers(self):
        AwardsEdition.objects.filter(pk=self.edition.pk).update(status=AwardsEdition.WINNERS)
        for url in (f"/polls/editions/{self.edition.slug}/", f"/polls/{self.poll.slug}/"):
            q1 = next(q for q in self.questions(url) if q["question_id"] == self.q1.question_id)
            self.assertEqual(q1["published_winner_option_id"], self.winner.option_id)
            self.assertNotIn("published_winner_votes", q1)
            self.assertTrue(all("votes" not in o for o in q1["options"]))

    def test_not_named_before_the_announcement(self):
        for url in (f"/polls/editions/{self.edition.slug}/", f"/polls/{self.poll.slug}/"):
            q1 = next(q for q in self.questions(url) if q["question_id"] == self.q1.question_id)
            self.assertNotIn("published_winner_option_id", q1)


class HallOfFameTests(TestCase):
    def winners(self, slug, year, picks):
        edition, poll, q1, q2 = kol_edition(slug)
        AwardsEdition.objects.filter(pk=edition.pk).update(status=AwardsEdition.WINNERS, year=year)
        for q, label in zip((q1, q2), picks):
            PollQuestion.objects.filter(pk=q.pk).update(
                published_winner_option=PollOption.objects.get(question=q, label=label))
        return edition

    def test_seasons_and_most_decorated(self):
        self.winners("kol-2025", 2025, ("ARMYKID", "JOKKIE"))
        self.winners("kol-2026", 2026, ("ARMYKID", "ARMYKID"))
        body = APIClient().get("/polls/hall-of-fame/").json()
        self.assertEqual([s["year"] for s in body["seasons"]], [2026, 2025])
        self.assertEqual(body["most_decorated"][0]["name"], "ARMYKID")
        self.assertEqual((body["most_decorated"][0]["wins"], sorted(body["most_decorated"][0]["seasons"])), (3, [2025, 2026]))

    def test_a_hidden_edition_is_not_in_it(self):
        hidden = self.winners("kol-hid", 2027, ("JOKKIE", "JOKKIE"))
        AwardsEdition.objects.filter(pk=hidden.pk).update(hidden=True)
        body = APIClient().get("/polls/hall-of-fame/").json()
        self.assertEqual(body["seasons"], [])
