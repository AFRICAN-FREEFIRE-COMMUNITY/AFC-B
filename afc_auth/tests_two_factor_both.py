"""
afc_auth/tests_two_factor_both.py - email code AND authenticator app on together, picked at sign-in.

WHY (owner 2026-09-27, inbox #61, approved mockup mockups/account-and-2fa): "users should be able to
have both active and choose which they want to use to sign during login each time". Before this an
account had ONE way: setting up the app replaced email.

What is pinned here
  - Both on: login answers with a chooser (choose_method, methods, last_method) and SENDS NOTHING.
  - Picking email (two-factor/switch-method/) sends exactly one code; picking the app back works.
  - The app challenge never eats the email send budget or passes for "the email already sent".
  - The way used last is remembered and marked.
  - Remove the app (two-factor/totp/remove/) needs proof, keeps two-step sign-in on with email.
  - One way on (email only): today's flow exactly, no chooser.

Drives the real endpoints through TotpTestBase (tests_two_factor_totp.py), email delivery captured.
Run: python manage.py test afc_auth.tests_two_factor_both --keepdb
"""
from afc_auth import two_factor
from afc_auth.models import TwoFactorChallenge, TwoFactorSettings
from afc_auth.tests_two_factor_totp import TotpTestBase


class BothOnTests(TotpTestBase):
    def setUp(self):
        super().setUp()
        self.session, self.secret, _codes = self.enrol_totp()
        self.sent_codes.clear()
        self.forget_spent_step()

    def switch(self, token, method):
        return self.post("/auth/two-factor/switch-method/", {"challenge_token": token, "method": method})

    def test_login_offers_the_choice_and_sends_nothing(self):
        body = self.do_login().json()
        self.assertTrue(body["two_factor_required"])
        self.assertTrue(body["choose_method"])
        self.assertEqual(body["methods"], ["email", "totp"])
        self.assertEqual(body["last_method"], "totp")
        self.assertEqual(body["method"], "totp")
        self.assertEqual(self.sent_codes, [])
        self.assertTrue(body["email_destination"].endswith("@gmail.com"))
        self.assertNotIn("player1@", body["email_destination"])

    def test_picking_email_sends_one_code_and_it_signs_in(self):
        first = self.do_login().json()
        resp = self.switch(first["challenge_token"], "email")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["method"], "email")
        self.assertTrue(body["code_sent"])
        self.assertEqual(len(self.sent_codes), 1)
        self.assertNotEqual(body["challenge_token"], first["challenge_token"])
        # The old (app) token is burned: one answerable challenge at a time.
        dead = self.post("/auth/two-factor/verify/", {"challenge_token": first["challenge_token"],
                                                      "code": self.app_code(self.secret)})
        self.assertEqual(dead.status_code, 400)

        ok = self.post("/auth/two-factor/verify/", {"challenge_token": body["challenge_token"],
                                                    "code": self.sent_codes[-1]})
        self.assertEqual(ok.status_code, 200, ok.content)
        # Remembered: next time email is marked as used last.
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).method, "email")
        self.assertEqual(self.do_login().json()["last_method"], "email")

    def test_the_app_code_works_on_the_first_token(self):
        token = self.do_login().json()["challenge_token"]
        ok = self.post("/auth/two-factor/verify/", {"challenge_token": token, "code": self.app_code(self.secret)})
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_switching_back_to_the_app_from_email(self):
        token = self.do_login().json()["challenge_token"]
        email = self.switch(token, "email").json()
        back = self.switch(email["challenge_token"], "totp")
        self.assertEqual(back.status_code, 200, back.content)
        self.assertEqual(back.json()["method"], "totp")
        ok = self.post("/auth/two-factor/verify/", {"challenge_token": back.json()["challenge_token"],
                                                    "code": self.app_code(self.secret)})
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_an_unknown_way_or_token_is_refused(self):
        token = self.do_login().json()["challenge_token"]
        for bad in ({"challenge_token": token, "method": "whatsapp"},
                    {"challenge_token": "nope", "method": "email"}):
            resp = self.post("/auth/two-factor/switch-method/", bad)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["code"], "two_factor_switch_refused")
        self.assertEqual(self.sent_codes, [])

    def test_email_twice_reuses_the_email_code_not_the_app_challenge(self):
        token = self.do_login().json()["challenge_token"]
        first = self.switch(token, "email").json()
        again = self.switch(first["challenge_token"], "email").json()
        self.assertEqual(again["method"], "email")
        self.assertFalse(again["code_sent"])                    # inside the 60 s cooldown
        self.assertEqual(again["challenge_token"], first["challenge_token"])
        self.assertEqual(len(self.sent_codes), 1)
        self.assertEqual(TwoFactorChallenge.objects.get(token=again["challenge_token"]).method, "email")

    def test_app_challenges_do_not_spend_the_email_budget(self):
        for _ in range(TwoFactorChallenge.MAX_SENDS_PER_HOUR + 2):
            self.do_login()
        token = self.do_login().json()["challenge_token"]
        resp = self.switch(token, "email").json()
        self.assertTrue(resp["code_sent"])

    def test_status_lists_both_ways(self):
        body = self.client.get("/auth/two-factor/status/", HTTP_AUTHORIZATION=f"Bearer {self.session}").json()
        self.assertEqual(body["methods_on"], ["email", "totp"])
        self.assertTrue(body["enabled"])

    # ── Remove the app, keep email ──
    def test_removing_the_app_needs_proof(self):
        resp = self.post("/auth/two-factor/totp/remove/", {}, token=self.session)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(two_factor.methods_on(self.user), ["email", "totp"])

    def test_removing_the_app_with_an_email_proof_keeps_email_on(self):
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "disable", "method": "email"},
                         token=self.session)
        self.assertEqual(sent.status_code, 200, sent.content)
        self.assertEqual(sent.json()["method"], "email")
        resp = self.post("/auth/two-factor/totp/remove/",
                         {"challenge_token": sent.json()["challenge_token"], "code": self.sent_codes[-1]},
                         token=self.session)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["methods_on"], ["email"])
        self.assertTrue(two_factor.is_enabled_for(self.user))
        body = self.do_login().json()
        self.assertFalse(body["choose_method"])
        self.assertEqual(body["method"], "email")

    def test_a_proof_by_a_way_that_is_not_on_is_refused(self):
        resp = self.post("/auth/two-factor/send-code/", {"purpose": "disable", "method": "whatsapp"},
                         token=self.session)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "two_factor_method_not_on")


class EmailOnlyUnchangedTests(TotpTestBase):
    def test_email_only_login_is_todays_flow(self):
        self.enable_email_2fa()
        self.sent_codes.clear()
        body = self.do_login().json()
        self.assertFalse(body["choose_method"])
        self.assertEqual(body["methods"], ["email"])
        self.assertEqual(body["method"], "email")
        self.assertTrue(body["code_sent"])
        self.assertEqual(len(self.sent_codes), 1)
