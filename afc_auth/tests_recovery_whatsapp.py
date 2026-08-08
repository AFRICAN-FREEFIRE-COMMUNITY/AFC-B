"""
Tests for ACCOUNT RECOVERY BY WHATSAPP (owner 2026-08-08).

Somebody who cannot get into their account, and whose emailed reset token would go to an inbox they
cannot read, proves the WhatsApp number already saved on the account. ONE proof, TWO endings:
    A. reset the password (the priority, and the ordinary case), or
    B. move the account onto an email address they can actually read.
Endpoints and rules: afc_auth/views_recovery.py.

WHAT IS COVERED, and why each one is here:

  THE HAPPY PATH
    - A code is issued and delivered to the number on file, verified, and the password changes.
    - The new password is readable back out of the database (it signs the user in), the old one no
      longer works, and every session is ended.

  THE CODE ITSELF (all of it borrowed from two_factor.py, so these prove the WIRING, not the maths)
    - A wrong code refuses and costs an attempt. An expired one refuses. A used one cannot be
      reused. The attempt cap burns the challenge.
    - The hourly send ceiling refuses a sixth code, and is scoped to purpose "recovery" so it
      cannot burn a user's sign-in code budget.

  NOTHING LEAKS WHETHER AN ACCOUNT EXISTS
    - An unknown identifier, an account with no number, and an opted-out account all produce a
      response byte-identical to a real one, and their decoy tokens fail exactly like a wrong code.

  THIS IS NOT A WAY AROUND TWO-STEP SIGN-IN (the property the whole design turns on)
    The module claims four things make it safe. There is one test per claim, deliberately, so a
    change that breaks the reasoning breaks a named test rather than passing quietly:
      1. the reset hands back no session of any kind;
      2. a 2FA account is still challenged at the very next sign-in, and its settings are untouched;
      3. every trusted device is forgotten, so no remembered browser can skip the factor afterwards;
      4. a code minted for recovery cannot be spent on the login second step.

  THE NEW PASSWORD
    - The strength rule is enforced SERVER-SIDE, and a rejection does not cost the grant.
    - A grant is single use, expires, and is replaced by a later one.

  THE SECOND ENDING - MOVING THE ACCOUNT'S EMAIL (EmailChangeEndingTests)
    - The address moves, is readable back out of the database, and both the old and the new address
      are told. The code proving the new address goes to the NEW one and nowhere else, and nothing
      is written until it comes back.
    - Sessions, trusted devices and any pending emailed reset token all go with it, and a
      never-verified signup is activated.
    - ONE GRANT BUYS ONE ENDING: after the move, the same grant cannot also set a password.
    - The address guards are the admin tool's guards: case-insensitive duplicate refusal, the
      cross-column "that is somebody's in-game name" collision, and a re-check at commit time.
    - The code is capped, expires, cannot be reused, and the cap burns the grant.

  THE TWO-STEP SIGN-IN RULE FOR THE EMAIL MOVE (EmailChangeTwoFactorRuleTests)
    STRICTER than the admin path on purpose: a flat refusal, no override flag, whatever the method.
    One test per limb - the refusal itself, that no flag switches the factor off, that the confirm
    call refuses too, that an authenticator account is refused as well (the deliberate cost), that
    the PASSWORD reset stays open to the same account, and that the refusal explains itself.

  THE NUMBER ITSELF
    - A number without a country code is refused SERVER-SIDE, at signup and at profile edit, and
      an accepted one is stored normalised.
    - Saving a number DATES it, clearing one clears the date, and a signup without one stores none.

  A NUMBER IS ONLY EVIDENCE WHILE IT IS STILL THEIRS (StaleNumberTests)
    Recycled SIMs are the hole a recovery-by-phone-number feature opens. A number nobody has
    confirmed for over a year sends no code, is indistinguishable from a real account, and its decoy
    token fails like a wrong code; one confirmed inside the window still works; re-saving it in
    profile settings restarts the clock; and a missing date counts as fresh (the judgement call in
    _number_too_stale, pinned so it cannot be flipped by accident).

Delivery is stubbed at the SERVICE BOUNDARY (afc_whatsapp.tasks.queue_template), so the suite
captures what would have gone to Meta without touching graph.facebook.com. There are NO WhatsApp
credentials on a dev machine, so this is also the only way the flow can be exercised locally.

Run: python manage.py test afc_auth.tests_recovery_whatsapp
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from afc_auth import trusted_devices, two_factor
from afc_auth.models import (
    AccountRecoveryGrant,
    EmailChangeRequest,
    PasswordResetToken,
    SessionToken,
    TrustedDevice,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
    UserProfile,
)

PASSWORD = "CorrectHorse!9"
NEW_PASSWORD = "FreshHorse!42"
NUMBER = "+2348051234567"

START = "/auth/recovery/whatsapp/start/"
VERIFY = "/auth/recovery/whatsapp/verify/"
RESET = "/auth/recovery/whatsapp/reset-password/"
REQUEST_EMAIL = "/auth/recovery/whatsapp/request-email-change/"
CONFIRM_EMAIL = "/auth/recovery/whatsapp/confirm-email-change/"
LOGIN = "/auth/login/"
NEW_EMAIL = "reachable@gmail.com"


@override_settings(WHATSAPP_LOGIN_CODE_TEMPLATE="login_code", WHATSAPP_LOGIN_CODE_LANG="en")
class RecoveryTestBase(TestCase):
    """Shared fixtures: an account with a WhatsApp number, and a capture of every send that would
    have gone to Meta."""

    def setUp(self):
        self.client = Client()
        # The per-IP throttle counts in the shared cache; a leftover count from another test would
        # make this suite order-dependent.
        cache.clear()

        # Every queue_template call the flow makes, as (number, template, language, body_params),
        # with the full kwargs kept alongside so the redaction flag can be asserted too.
        self.sends = []
        self.send_kwargs = []

        def _capture(to, template_name, language, **kwargs):
            self.sends.append((to, template_name, language, kwargs.get("body_params") or []))
            self.send_kwargs.append(kwargs)
            # queue_template returns a truthy id/True when the send was taken. Returning True is
            # the production shape (a worker took it), which is the one worth exercising.
            return True

        patcher = patch("afc_whatsapp.tasks.queue_template", side_effect=_capture)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The reset notice goes out over real SMTP (send_email opens a socket), so it is stubbed at
        # the same boundary. Captured rather than discarded, because "the account's address is told"
        # is one of the properties this feature has to have, and NotifiesTheAccountTests asserts it
        # off this list.
        self.emails = []
        email_patcher = patch("afc_auth.views_recovery.send_email",
                              side_effect=lambda to, subject, body, **kw: self.emails.append(
                                  (to, subject, kw.get("language"))) or True)
        email_patcher.start()
        self.addCleanup(email_patcher.stop)

        self.user = User.objects.create(
            username="player1", email="old@gmail.com", is_active=True, country="Nigeria")
        self.user.set_password(PASSWORD)
        self.user.save()
        UserProfile.objects.create(user=self.user, whatsapp_number=NUMBER, whatsapp_opt_in=True)

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def start(self, identifier="player1"):
        """Step 1. Returns (response, recovery_token)."""
        response = self.client.post(START, {"identifier": identifier},
                                    content_type="application/json")
        return response, response.json().get("recovery_token")

    def last_code(self):
        """The six digits that would have reached the phone on the most recent send."""
        return self.sends[-1][3][0]

    def verified_grant(self):
        """Walk steps 1 and 2 and hand back a live grant token."""
        _response, token = self.start()
        verify = self.client.post(VERIFY, {"recovery_token": token, "code": self.last_code()},
                                  content_type="application/json")
        self.assertEqual(verify.status_code, 200, verify.content)
        return verify.json()["grant_token"]

    def reset(self, grant_token, new_password=NEW_PASSWORD):
        return self.client.post(RESET, {"grant_token": grant_token, "new_password": new_password},
                                content_type="application/json")

    def sign_in(self, password=NEW_PASSWORD):
        return self.client.post(LOGIN, {"ign_or_uid": "player1", "password": password},
                                content_type="application/json")

    # ── the OTHER ending: move the account onto an address they can read ─────────────────────────
    def request_email(self, grant_token, new_email=NEW_EMAIL):
        return self.client.post(REQUEST_EMAIL,
                                {"grant_token": grant_token, "new_email": new_email},
                                content_type="application/json")

    def confirm_email(self, grant_token, code):
        return self.client.post(CONFIRM_EMAIL, {"grant_token": grant_token, "code": code},
                                content_type="application/json")

    def emailed_change_code(self):
        """The six digits parked for the pending email change. Read from the row rather than from
        the captured send, because send_email is stubbed at the boundary and the body never renders.
        """
        return EmailChangeRequest.objects.get(user=self.user).token


class HappyPathTests(RecoveryTestBase):
    """The whole flow, end to end, and what it leaves behind in the database."""

    def test_a_code_goes_to_the_number_on_file_and_the_password_changes(self):
        # Arrange: a live session that the reset must kill.
        SessionToken.objects.create(user=self.user, token="s" * 40,
                                    expires_at=timezone.now() + timezone.timedelta(days=7))

        # Act
        response = self.reset(self.verified_grant())

        # Assert: the response, then the database, then the message that went out.
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["sessions_ended"], 1)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.check_password(PASSWORD), "the old password must stop working")
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

        number, template, language, params = self.sends[0]
        self.assertEqual(number, NUMBER)
        self.assertEqual(template, "login_code")
        self.assertEqual(language, "en")
        self.assertEqual(len(params[0]), two_factor.CODE_LENGTH)

    def test_the_new_password_actually_signs_the_user_in(self):
        # The end the user cares about. Asserting check_password proves the hash; this proves the
        # whole journey, through the real login endpoint.
        self.reset(self.verified_grant())

        response = self.sign_in()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("session_token", response.json())

    def test_the_code_is_never_stored_in_plaintext(self):
        self.start()
        challenge = TwoFactorChallenge.objects.get(purpose="recovery")
        self.assertNotIn(self.last_code(), challenge.code_hash)
        self.assertTrue(challenge.code_hash.startswith("pbkdf2_"))

    def test_the_code_is_kept_out_of_the_whatsapp_message_log(self):
        # The log normally records the template's variable VALUES so a message can be
        # reconstructed as the recipient saw it. That is right for a room ID and wrong for a
        # one-time code: it would leave a live code readable from a database dump, a read replica
        # or a Django admin session. Everything else about the send is still recorded.
        self.start()

        self.assertTrue(self.send_kwargs[-1]["redact_variables"],
                        "the recovery code send must ask for its variables to be redacted")

    def test_a_pending_emailed_reset_token_is_dropped(self):
        # Somebody who tried the email route first and then came here. That token was sent to an
        # address they may not control, so it must not survive a reset done by someone else's proof.
        PasswordResetToken.objects.create(user=self.user, token="123456")

        self.reset(self.verified_grant())

        self.assertFalse(PasswordResetToken.objects.filter(user=self.user).exists())

    def test_an_identifier_can_be_the_username_the_email_or_the_uid(self):
        self.user.uid = "9137457129"
        self.user.save(update_fields=["uid"])

        for identifier in ("player1", "OLD@gmail.com", "9137457129"):
            with self.subTest(identifier=identifier):
                before = len(self.sends)
                # Each start() burns a send, so the cooldown is stepped over deliberately: this is
                # about the RESOLVER, not the rate limit, which has its own test below.
                TwoFactorChallenge.objects.filter(user=self.user).update(
                    created_at=timezone.now() - timezone.timedelta(minutes=5),
                    consumed_at=timezone.now())
                self.start(identifier)
                self.assertEqual(len(self.sends), before + 1)


class NotifiesTheAccountTests(RecoveryTestBase):
    """The tripwire. If the real owner did not do this, that message is the only warning they get."""

    def test_the_account_address_is_told(self):
        self.reset(self.verified_grant())

        recipients = [to for to, _subject, _lang in self.emails]
        self.assertEqual(recipients, ["old@gmail.com"])

    def test_the_notice_names_whatsapp_so_the_reader_knows_which_door_was_used(self):
        # "Your password was reset" alone would not tell the owner anything they could act on. The
        # channel is the whole content of the warning.
        self.reset(self.verified_grant())

        _to, subject, _lang = self.emails[0]
        self.assertIn("WhatsApp", subject)

    def test_the_notice_goes_out_in_the_recipients_language(self):
        self.user.language = "fr"
        self.user.save(update_fields=["language"])

        self.reset(self.verified_grant())

        _to, subject, language = self.emails[0]
        self.assertEqual(language, "fr")
        self.assertIn("réinitialisé", subject)

    def test_a_mail_failure_does_not_undo_the_reset(self):
        # A dead address is the NORMAL case for this flow, so a bounce or an SMTP outage must never
        # roll back a password that has already been proved and written.
        with patch("afc_auth.views_recovery.send_email", side_effect=RuntimeError("smtp down")):
            response = self.reset(self.verified_grant())

        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))


class CodeRulesTests(RecoveryTestBase):
    """The code is two_factor.py's, so these prove this flow is WIRED to those rules rather than
    quietly reimplementing looser ones."""

    def test_a_wrong_code_is_refused_and_costs_an_attempt(self):
        _response, token = self.start()

        response = self.client.post(VERIFY, {"recovery_token": token, "code": "000000"},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["attempts_left"], TwoFactorChallenge.MAX_ATTEMPTS - 1)

    def test_an_expired_code_is_refused(self):
        _response, token = self.start()
        TwoFactorChallenge.objects.filter(token=token).update(
            expires_at=timezone.now() - timezone.timedelta(seconds=1))

        response = self.client.post(VERIFY, {"recovery_token": token, "code": self.last_code()},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_a_code_cannot_be_used_twice(self):
        _response, token = self.start()
        code = self.last_code()
        self.client.post(VERIFY, {"recovery_token": token, "code": code},
                         content_type="application/json")

        again = self.client.post(VERIFY, {"recovery_token": token, "code": code},
                                 content_type="application/json")

        self.assertEqual(again.status_code, 400)

    def test_the_attempt_cap_burns_the_challenge(self):
        _response, token = self.start()

        for _ in range(TwoFactorChallenge.MAX_ATTEMPTS):
            last = self.client.post(VERIFY, {"recovery_token": token, "code": "000000"},
                                    content_type="application/json")

        self.assertEqual(last.status_code, 429)
        # And the real code no longer works, so guessing cannot be resumed.
        real = self.client.post(VERIFY, {"recovery_token": token, "code": self.last_code()},
                                content_type="application/json")
        self.assertEqual(real.status_code, 400)

    def test_the_hourly_send_ceiling_stops_a_sixth_code(self):
        for _ in range(TwoFactorChallenge.MAX_SENDS_PER_HOUR):
            self.start()
            # Step over the 60 second resend cooldown so this test measures the HOURLY ceiling.
            TwoFactorChallenge.objects.filter(user=self.user).update(
                created_at=timezone.now() - timezone.timedelta(minutes=5))
        sends_before = len(self.sends)

        response, token = self.start()

        # Still a 200 with a token, because a refusal here would leak that the account exists.
        self.assertEqual(response.status_code, 200)
        self.assertTrue(token)
        self.assertEqual(len(self.sends), sends_before, "no sixth message may go out")

    def test_the_recovery_ceiling_does_not_touch_the_sign_in_code_budget(self):
        # Spend the whole recovery budget.
        for _ in range(TwoFactorChallenge.MAX_SENDS_PER_HOUR + 2):
            self.start()
            TwoFactorChallenge.objects.filter(user=self.user, purpose="recovery").update(
                created_at=timezone.now() - timezone.timedelta(minutes=5))

        # A login challenge is a DIFFERENT purpose and must still be issuable.
        issued = two_factor.issue_challenge(self.user, purpose="login")

        self.assertIsNotNone(issued["challenge"])


class NoAccountLeakTests(RecoveryTestBase):
    """A request for an unknown identifier must look identical to one for a known account. These
    compare the actual bodies rather than trusting the code path."""

    def _shape(self, response):
        """What a caller can observe: the status, the keys, the message, and the token's length."""
        body = response.json()
        return (response.status_code, sorted(body.keys()), body["message"],
                len(body["recovery_token"]))

    def test_an_unknown_identifier_is_indistinguishable_from_a_real_one(self):
        real, _token = self.start("player1")
        unknown, _token = self.start("nobody-has-this-name")

        self.assertEqual(self._shape(real), self._shape(unknown))

    def test_an_account_with_no_number_is_indistinguishable(self):
        stranger = User.objects.create(username="nonumber", email="nn@gmail.com", is_active=True)
        UserProfile.objects.create(user=stranger, whatsapp_number="")

        real, _token = self.start("player1")
        no_number, _token = self.start("nonumber")

        self.assertEqual(self._shape(real), self._shape(no_number))

    def test_an_opted_out_account_is_indistinguishable_and_is_not_messaged(self):
        stranger = User.objects.create(username="optedout", email="oo@gmail.com", is_active=True,
                                       country="Nigeria")
        UserProfile.objects.create(user=stranger, whatsapp_number=NUMBER, whatsapp_opt_in=False)
        sends_before = len(self.sends)

        real, _token = self.start("player1")
        opted_out, _token = self.start("optedout")

        self.assertEqual(self._shape(real), self._shape(opted_out))
        # Exactly ONE message went out across both calls: the real one. Meta policy is that an
        # opt-out is honoured, and it is honoured here rather than only at the send boundary.
        self.assertEqual(len(self.sends), sends_before + 1)

    def test_a_decoy_token_fails_exactly_like_a_wrong_code(self):
        _response, decoy = self.start("nobody-has-this-name")
        _response, real = self.start("player1")

        on_decoy = self.client.post(VERIFY, {"recovery_token": decoy, "code": "000000"},
                                    content_type="application/json")
        on_real = self.client.post(VERIFY, {"recovery_token": real, "code": "000000"},
                                   content_type="application/json")

        self.assertEqual(on_decoy.status_code, on_real.status_code)
        self.assertEqual(on_decoy.json()["message"], on_real.json()["message"])

    def test_step_one_never_reveals_the_number_it_sent_to(self):
        response, _token = self.start()

        self.assertNotIn("destination", response.json())
        self.assertNotIn("4567", response.content.decode())

    def test_the_account_is_only_named_once_the_code_has_been_proved(self):
        # The username and the masked address are real facts about a real account, so they may only
        # appear behind the code. Step 1 must carry neither.
        step_one, token = self.start()
        self.assertNotIn("player1", step_one.content.decode())

        verify = self.client.post(VERIFY, {"recovery_token": token, "code": self.last_code()},
                                  content_type="application/json")

        self.assertEqual(verify.json()["username"], "player1")
        # Masked, never in full.
        self.assertNotIn("old@gmail.com", verify.content.decode())
        self.assertIn("gmail.com", verify.json()["current_email"])


class NotAWayAroundTwoFactorTests(RecoveryTestBase):
    """THE property this feature turns on, one test per claim in views_recovery.py's header.

    THE RULE: a WhatsApp-proved reset sets the password and nothing else. Two-step sign-in is never
    disabled, never reset, never stepped around, and is still demanded at the next sign-in.

    Contrast with the ADMIN email tool, which takes 2FA down to do its job: that is defensible there
    because a human has checked who they are talking to. Nothing here has a human in it, so nothing
    here is allowed to weaken the factor.
    """

    def _enable_email_2fa(self):
        TwoFactorSettings.objects.update_or_create(
            user=self.user, defaults={"is_enabled": True, "method": "email"})

    def _enrol_authenticator(self):
        secret = two_factor.start_totp_enrolment(self.user)
        two_factor.promote_totp_enrolment(self.user, -1)
        TwoFactorSettings.objects.update_or_create(
            user=self.user, defaults={"is_enabled": True, "method": "totp"})
        return secret

    # ── claim 1: a reset is not a sign-in ──────────────────────────────────────────────────────
    def test_the_reset_hands_back_no_session_of_any_kind(self):
        response = self.reset(self.verified_grant())

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertNotIn("session_token", body)
        self.assertNotIn("token", body)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists(),
                         "recovery must never mint a session; the user has to sign in")

    # ── claim 2: the factor is untouched and still demanded ────────────────────────────────────
    def test_a_two_factor_account_is_still_challenged_at_the_very_next_sign_in(self):
        # The single most important test in this file. If a WhatsApp number could get somebody past
        # the second factor, it would be here that it showed.
        self._enable_email_2fa()

        self.reset(self.verified_grant())
        response = self.sign_in()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["two_factor_required"])
        self.assertNotIn("session_token", response.json(),
                         "the correct new password must still not be enough on a 2FA account")

    def test_the_reset_does_not_touch_the_two_factor_settings(self):
        self._enrol_authenticator()
        codes_before = two_factor.backup_codes_remaining(self.user)

        self.reset(self.verified_grant())

        self.assertTrue(two_factor.is_enabled_for(self.user))
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).method, "totp")
        # Nor does it quietly spend a recovery code on the user's behalf.
        self.assertEqual(two_factor.backup_codes_remaining(self.user), codes_before)

    def test_an_email_method_account_is_recoverable_rather_than_stranded(self):
        # The design decision, stated as a test: we do NOT demand the second factor here, precisely
        # so that the user whose factor goes to a lost inbox can still get their password back. They
        # will meet the factor at sign-in, where they can use a recovery code.
        self._enable_email_2fa()

        response = self.reset(self.verified_grant())

        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertTrue(two_factor.is_enabled_for(self.user))

    # ── claim 3: trusted devices die with the password ─────────────────────────────────────────
    def test_every_trusted_device_is_forgotten(self):
        # THE LOAD-BEARING ONE. A remembered browser skips the factor, so a device left standing
        # would turn claim 2 into a lie: the attacker would reset the password and walk in.
        self._enable_email_2fa()
        request = self.client.request().wsgi_request
        device_token = trusted_devices.remember_device(self.user, request)
        self.assertTrue(trusted_devices.is_trusted(self.user, device_token))

        response = self.reset(self.verified_grant())

        self.assertEqual(response.json()["devices_forgotten"], 1)
        self.assertFalse(TrustedDevice.objects.filter(user=self.user).exists())
        self.assertFalse(trusted_devices.is_trusted(self.user, device_token))

    def test_the_forgotten_device_can_no_longer_skip_the_second_factor(self):
        # The same fact proved through the front door rather than the helper, because that is where
        # it would actually matter.
        self._enable_email_2fa()
        request = self.client.request().wsgi_request
        device_token = trusted_devices.remember_device(self.user, request)

        self.reset(self.verified_grant())
        response = self.client.post(
            LOGIN,
            {"ign_or_uid": "player1", "password": NEW_PASSWORD, "device_token": device_token},
            content_type="application/json")

        self.assertTrue(response.json()["two_factor_required"])
        self.assertNotIn("session_token", response.json())

    # ── claim 4: a recovery code is not a login code ───────────────────────────────────────────
    def test_a_recovery_code_cannot_be_spent_on_the_login_second_step(self):
        # Both are six digits from the same generator; only the challenge's PURPOSE keeps them
        # apart. If get_challenge ever stopped filtering on it, the WhatsApp code would become a
        # second factor and this flow really would be a bypass.
        self._enable_email_2fa()
        _response, recovery_token = self.start()
        recovery_code = self.last_code()

        response = self.client.post(
            "/auth/two-factor/verify/",
            {"challenge_token": recovery_token, "code": recovery_code},
            content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("session_token", response.json())


class NewPasswordRulesTests(RecoveryTestBase):
    """The strength rule, and the grant's life cycle."""

    def test_a_short_password_is_refused_server_side(self):
        # A client-side rule is not a rule: this posts straight past the form.
        response = self.reset(self.verified_grant(), new_password="Ab1!")

        self.assertEqual(response.status_code, 400)
        self.assertIn("8 characters", response.json()["message"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD), "the old password must still stand")

    def test_a_password_missing_a_character_class_is_refused(self):
        for weak, wanted in (
            ("alllowercase1!", "uppercase"),
            ("ALLUPPERCASE1!", "lowercase"),
            ("NoDigitsHere!!", "number"),
            ("NoSpecials1234", "special"),
        ):
            with self.subTest(password=weak):
                response = self.reset(self.verified_grant(), new_password=weak)
                self.assertEqual(response.status_code, 400)
                self.assertIn(wanted, response.json()["message"])
                # Each loop needs a fresh challenge: the previous start() is inside the cooldown.
                TwoFactorChallenge.objects.filter(user=self.user).update(
                    created_at=timezone.now() - timezone.timedelta(minutes=5),
                    consumed_at=timezone.now())

    def test_a_missing_password_is_refused(self):
        response = self.client.post(RESET, {"grant_token": self.verified_grant()},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_a_refused_password_does_not_cost_the_grant(self):
        # Somebody who is already locked out should not have to redo the WhatsApp code because they
        # forgot a capital letter.
        grant = self.verified_grant()

        weak = self.reset(grant, new_password="weak")
        self.assertEqual(weak.status_code, 400)

        good = self.reset(grant)
        self.assertEqual(good.status_code, 200, good.content)

    def test_a_spent_grant_cannot_be_spent_again(self):
        grant = self.verified_grant()
        self.reset(grant)

        again = self.reset(grant, new_password="SecondTry!77")

        self.assertEqual(again.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD),
                        "the second reset must not have taken effect")

    def test_an_expired_grant_is_refused(self):
        grant = self.verified_grant()
        AccountRecoveryGrant.objects.filter(token=grant).update(
            expires_at=timezone.now() - timezone.timedelta(seconds=1))

        response = self.reset(grant)

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_an_unknown_grant_is_refused(self):
        response = self.reset("not-a-real-grant-token")

        self.assertEqual(response.status_code, 400)

    def test_verifying_again_invalidates_the_earlier_grant(self):
        first = self.verified_grant()
        TwoFactorChallenge.objects.filter(user=self.user).update(
            created_at=timezone.now() - timezone.timedelta(minutes=5))
        second = self.verified_grant()

        self.assertEqual(self.reset(first).status_code, 400)
        self.assertEqual(self.reset(second).status_code, 200)


class MessageLogRedactionTests(TestCase):
    """The half of the redaction that the flag alone does not prove: what actually lands on the
    WhatsAppMessage row. Calls the REAL task with only the HTTP client stubbed, because the row is
    written by the task and asserting the flag would just be asserting my own mock."""

    def _send(self, redact):
        from afc_whatsapp.models import WhatsAppMessage
        from afc_whatsapp.tasks import send_whatsapp_message

        with patch("afc_whatsapp.client.send_template",
                   return_value={"ok": True, "wamid": f"wamid.{redact}", "raw": None,
                                 "status_code": 200, "error_code": None, "error_title": None,
                                 "error_detail": None, "retryable": False}):
            message_id = send_whatsapp_message(
                to=NUMBER, template_name="login_code", language="en",
                body_params=["481902"], context="account_recovery_code",
                redact_variables=redact,
            )
        return WhatsAppMessage.objects.get(id=message_id)

    def test_a_redacted_send_keeps_the_code_off_the_row(self):
        message = self._send(redact=True)

        self.assertEqual(message.variables["body"], ["redacted"])
        self.assertNotIn("481902", str(message.variables))
        # Everything that makes the log USEFUL is still there, so "did my code go out?" is still
        # answerable: only the digits are gone.
        self.assertEqual(message.status, "sent")
        self.assertEqual(message.phone, NUMBER)
        self.assertEqual(message.template_name, "login_code")
        self.assertEqual(message.context, "account_recovery_code")

    def test_an_ordinary_send_still_records_its_variables(self):
        # The redaction must be opt-in: a room ID or an order number in the log is the whole point
        # of having one, and this is the regression that would notice if the default flipped.
        message = self._send(redact=False)

        self.assertEqual(message.variables["body"], ["481902"])


class PerIpThrottleTests(RecoveryTestBase):
    """Step 1 is unauthenticated, in-game names are public, and every WhatsApp message is billed."""

    def test_an_ip_is_cut_off_after_the_hourly_allowance(self):
        from afc_auth.views_recovery import RECOVERY_START_PER_IP_PER_HOUR

        for _ in range(RECOVERY_START_PER_IP_PER_HOUR):
            self.assertEqual(self.start("someone-else")[0].status_code, 200)

        blocked, _token = self.start("someone-else")

        self.assertEqual(blocked.status_code, 429)


class WhatsAppNumberCaptureTests(TestCase):
    """The number is OPTIONAL everywhere it is offered, and the country code is COMPULSORY when one
    is given. A client-side rule is not a rule, so both write paths are exercised over HTTP.

    This half matters as much as the recovery half: only 116 of 6,809 accounts have a number saved,
    so until capture improves the flow above helps almost nobody."""

    def setUp(self):
        self.client = Client()
        geo_patcher = patch("afc_auth.views.geo_for_ip", return_value={"country": "Nigeria"})
        geo_patcher.start()
        self.addCleanup(geo_patcher.stop)
        # A successful signup mails a verification code, and views.send_email opens a real SMTP
        # socket. Stubbed at the same boundary the rest of this file uses, so the suite neither waits
        # on a network timeout nor prints a stack trace for work that succeeded. Nothing here asserts
        # on mail; these tests are about what lands in UserProfile.
        email_patcher = patch("afc_auth.views.send_email", return_value=True)
        email_patcher.start()
        self.addCleanup(email_patcher.stop)

    def _signup(self, **extra):
        body = {
            "in_game_name": "newplayer",
            "email": "newplayer@gmail.com",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "full_name": "New Player",
        }
        body.update(extra)
        return self.client.post("/auth/signup/", body, content_type="application/json")

    def test_signup_works_with_no_number_at_all(self):
        response = self._signup()

        self.assertEqual(response.status_code, 201, response.content)
        profile = UserProfile.objects.get(user__username="newplayer")
        self.assertEqual(profile.whatsapp_number, "")

    def test_signup_stores_an_international_number_normalised(self):
        response = self._signup(whatsapp_number="+234 805 123 4567")

        self.assertEqual(response.status_code, 201, response.content)
        profile = UserProfile.objects.get(user__username="newplayer")
        self.assertEqual(profile.whatsapp_number, NUMBER)

    def test_signup_refuses_a_number_with_no_country_code(self):
        # The exact shape 34 of 133 stored numbers are in: Nigerian national form. It would have
        # resolved at send time by guessing the country, and that guess is what this refuses.
        response = self._signup(whatsapp_number="08051234567")

        self.assertEqual(response.status_code, 400)
        self.assertIn("country code", response.json()["error"])
        self.assertFalse(User.objects.filter(username="newplayer").exists(),
                         "a bad number must not leave a half-made account behind")

    def _edit_profile(self, username, whatsapp_number, number_on_file=""):
        """POST the profile form the way the real page does. edit_profile is a FULL-FORM save and
        rejects a body missing full_name / in_game_name / email, so those ride along."""
        user = User.objects.create(username=username, email=f"{username}@gmail.com",
                                   is_active=True, country="Nigeria", full_name="Edit Or")
        user.set_password(PASSWORD)
        user.save()
        UserProfile.objects.create(user=user, whatsapp_number=number_on_file)
        session = SessionToken.objects.create(
            user=user, token=username.ljust(40, "x"),
            expires_at=timezone.now() + timezone.timedelta(days=7))

        response = self.client.post(
            "/auth/edit-profile/",
            {
                "full_name": user.full_name,
                "in_game_name": user.username,
                "email": user.email,
                "whatsapp_number": whatsapp_number,
            },
            HTTP_AUTHORIZATION=f"Bearer {session.token}",
        )
        return user, response

    def test_profile_edit_refuses_a_number_with_no_country_code(self):
        user, response = self._edit_profile("editor", "08051234567")

        self.assertEqual(response.status_code, 400)
        self.assertIn("country code", response.json()["message"])
        self.assertEqual(UserProfile.objects.get(user=user).whatsapp_number, "")

    def test_profile_edit_stores_an_international_number_normalised(self):
        user, response = self._edit_profile("editortwo", "+234 805 123 4567")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserProfile.objects.get(user=user).whatsapp_number, NUMBER)

    def test_clearing_the_number_is_still_allowed(self):
        user, response = self._edit_profile("editorthree", "", number_on_file=NUMBER)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserProfile.objects.get(user=user).whatsapp_number, "")

    # ── the date the recovery staleness rule reads (owner 2026-08-08) ───────────────────────────
    def test_saving_a_number_dates_it(self):
        user, response = self._edit_profile("editorfour", "+234 805 123 4567")

        self.assertEqual(response.status_code, 200, response.content)
        profile = UserProfile.objects.get(user=user)
        self.assertIsNotNone(profile.whatsapp_number_updated_at,
                             "views_recovery reads this to decide whether the number is still proof")

    def test_clearing_the_number_clears_its_date(self):
        # An empty profile must not carry a stale date that some later path could misread as a
        # fresh claim about a number typed by somebody else.
        user, response = self._edit_profile("editorfive", "", number_on_file=NUMBER)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(UserProfile.objects.get(user=user).whatsapp_number_updated_at)

    def test_signup_dates_the_number_it_stores(self):
        response = self._signup(whatsapp_number="+234 805 123 4567")

        self.assertEqual(response.status_code, 201, response.content)
        profile = UserProfile.objects.get(user__username="newplayer")
        self.assertIsNotNone(profile.whatsapp_number_updated_at)

    def test_a_signup_with_no_number_carries_no_date(self):
        response = self._signup()

        self.assertEqual(response.status_code, 201, response.content)
        profile = UserProfile.objects.get(user__username="newplayer")
        self.assertIsNone(profile.whatsapp_number_updated_at)


class EmailChangeEndingTests(RecoveryTestBase):
    """THE SECOND ENDING (owner 2026-08-08): move the account onto an address its owner can read.

    A password reset gets somebody back IN; it does not fix the dead inbox that locked them out. This
    is the self-serve version of what a head admin does with admin_set_user_email, for the subset of
    users who can prove a WhatsApp number.

    The new address is PROVEN with a second code before anything is written, which the admin path
    deliberately does not do. Here it is free (a person typing an address is claiming they can read
    it) and it buys the thing this feature most needs to avoid: a typo that moves the account onto an
    inbox that does not exist, locking it out permanently with no way back.
    """

    def move_email(self, new_email=NEW_EMAIL):
        """The whole ending: prove the number, name the address, prove the address."""
        grant = self.verified_grant()
        request = self.request_email(grant, new_email)
        self.assertEqual(request.status_code, 200, request.content)
        return self.confirm_email(grant, self.emailed_change_code())

    # ── the happy path ─────────────────────────────────────────────────────────────────────────
    def test_the_address_moves_and_is_readable_back_out_of_the_database(self):
        response = self.move_email()

        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW_EMAIL)
        self.assertEqual(response.json()["previous_email"], "old@gmail.com")

    def test_the_code_goes_to_the_new_address_and_nowhere_else(self):
        # THE proof of ownership. If it were sent to the old address it would prove nothing, since
        # the premise is that the old address is unreachable.
        grant = self.verified_grant()
        self.emails.clear()

        self.request_email(grant)

        self.assertEqual([to for to, _subject, _lang in self.emails], [NEW_EMAIL])

    def test_nothing_is_written_until_the_new_address_is_proved(self):
        grant = self.verified_grant()

        self.request_email(grant)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com",
                         "asking is not the same as proving; the request call must change nothing")

    def test_both_addresses_are_told_after_the_move(self):
        # The OLD one is the tripwire, and it is the last message AFC can ever send that inbox about
        # this account: from here on every password reset goes to the new address.
        self.move_email()

        told = [to for to, _subject, _lang in self.emails]
        self.assertIn("old@gmail.com", told)
        self.assertIn(NEW_EMAIL, told)

    def test_the_notice_names_whatsapp_so_the_reader_knows_which_door_was_used(self):
        self.move_email()

        subjects = [subject for to, subject, _lang in self.emails if to == "old@gmail.com"]
        self.assertTrue(any("WhatsApp" in s for s in subjects), subjects)

    def test_a_never_verified_signup_is_activated_by_the_move(self):
        # is_active on this model IS the email-verified flag, and False means "abandoned signup",
        # not "banned" (bans live on BannedPlayer). A mistyped signup address is exactly the case
        # this ending exists for.
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self.move_email()

        self.assertTrue(response.json()["reactivated"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_every_session_ends_and_every_trusted_device_is_forgotten(self):
        # Sharper here than for the password reset: an address that has been moved cannot be moved
        # back by whoever lost it, so a surviving cookie or remembered browser is how a takeover
        # keeps its foothold.
        SessionToken.objects.create(user=self.user, token="livesession".ljust(40, "x"),
                                    expires_at=timezone.now() + timezone.timedelta(days=7))
        request = self.client.request().wsgi_request
        device_token = trusted_devices.remember_device(self.user, request)

        response = self.move_email()

        self.assertEqual(response.json()["sessions_ended"], 1)
        self.assertEqual(response.json()["devices_forgotten"], 1)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())
        self.assertFalse(trusted_devices.is_trusted(self.user, device_token))

    def test_a_pending_emailed_reset_token_is_dropped(self):
        # A token mailed to the OLD address must not be spendable once the address has moved.
        PasswordResetToken.objects.create(user=self.user, token="123456")

        self.move_email()

        self.assertFalse(PasswordResetToken.objects.filter(user=self.user).exists())

    def test_the_move_hands_back_no_session(self):
        response = self.move_email()

        self.assertNotIn("session_token", response.json())
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    # ── one grant, one ending ──────────────────────────────────────────────────────────────────
    def test_the_grant_is_spent_by_the_move(self):
        grant = self.verified_grant()
        self.request_email(grant)
        self.confirm_email(grant, self.emailed_change_code())

        again = self.request_email(grant, "third@gmail.com")

        self.assertEqual(again.status_code, 400)
        self.assertFalse(AccountRecoveryGrant.objects.filter(
            token=grant, consumed_at__isnull=True).exists())

    def test_one_grant_cannot_both_move_the_address_and_set_the_password(self):
        # The rule in the module header, pinned: a single WhatsApp code buys ONE ending. Somebody who
        # wants both does the proof twice, which costs an extra code and takes a compounding step
        # away from an attacker.
        grant = self.verified_grant()
        self.request_email(grant)
        self.confirm_email(grant, self.emailed_change_code())

        response = self.reset(grant)

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD), "the old password must still stand")

    # ── the trail: the grant row IS the audit log for this feature ─────────────────────────────
    def test_the_spent_grant_records_that_it_moved_the_email_and_which_address_it_left(self):
        # AuditLogMiddleware skips unauthenticated mutations, and every endpoint here is
        # unauthenticated by definition, so there is no AuditLog row and never will be. This row is
        # the only durable record of the one irreversible thing the feature does - and the notice
        # email cannot stand in for it, because it goes to an inbox that is dead by assumption.
        self.move_email()

        grant = AccountRecoveryGrant.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(grant.outcome, AccountRecoveryGrant.OUTCOME_EMAIL)
        self.assertIn("old@gmail.com", grant.outcome_detail)
        self.assertIn(NEW_EMAIL, grant.outcome_detail)

    def test_a_password_reset_records_its_outcome_and_no_password(self):
        self.reset(self.verified_grant())

        grant = AccountRecoveryGrant.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(grant.outcome, AccountRecoveryGrant.OUTCOME_PASSWORD)
        self.assertEqual(grant.outcome_detail, "",
                         "nothing about a password may be recorded, not even its shape")

    def test_a_grant_burned_without_being_spent_records_no_outcome(self):
        # The attempt cap burns the grant without it having bought anything. An outcome there would
        # be a lie in the trail, and the trail is only worth having if it cannot lie.
        grant = self.verified_grant()
        self.request_email(grant)
        for _attempt in range(TwoFactorChallenge.MAX_ATTEMPTS):
            self.confirm_email(grant, "000000")

        row = AccountRecoveryGrant.objects.get(token=grant)
        self.assertIsNotNone(row.consumed_at)
        self.assertEqual(row.outcome, "")

    def test_a_dead_grant_cannot_start_an_email_change(self):
        response = self.request_email("not-a-real-grant")

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com")

    # ── the address guards, the same ones the admin tool runs ──────────────────────────────────
    def test_an_address_registered_to_another_account_is_refused(self):
        User.objects.create(username="someoneelse", email="taken@gmail.com", is_active=True)

        response = self.request_email(self.verified_grant(), "taken@gmail.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already registered", response.json()["message"])

    def test_the_duplicate_check_is_case_insensitive(self):
        User.objects.create(username="someoneelse", email="taken@gmail.com", is_active=True)

        response = self.request_email(self.verified_grant(), "TAKEN@Gmail.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already registered", response.json()["message"])

    def test_an_address_that_is_another_players_in_game_name_is_refused(self):
        # The cross-column trap uniqueness cannot see: 106 accounts have a username that IS a
        # well-formed email address, and sign-in resolves one typed string against all three
        # columns. This endpoint ends lockouts, so it must not create one.
        User.objects.create(username="collide@gmail.com", email="other@gmail.com", is_active=True)

        response = self.request_email(self.verified_grant(), "collide@gmail.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("in-game name", response.json()["message"])

    def test_the_address_has_to_actually_change(self):
        response = self.request_email(self.verified_grant(), "old@gmail.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already the address", response.json()["message"])

    def test_a_malformed_address_is_refused(self):
        response = self.request_email(self.verified_grant(), "not-an-address")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(EmailChangeRequest.objects.filter(user=self.user).exists())

    def test_the_address_is_re_checked_at_commit_time(self):
        # Minutes pass between the two calls, and somebody else can register the address in between.
        grant = self.verified_grant()
        self.request_email(grant)
        code = self.emailed_change_code()
        User.objects.create(username="racer", email=NEW_EMAIL, is_active=True)

        response = self.confirm_email(grant, code)

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com")

    # ── the code sent to the new address ───────────────────────────────────────────────────────
    def test_a_wrong_code_is_refused_and_costs_an_attempt(self):
        grant = self.verified_grant()
        self.request_email(grant)

        response = self.confirm_email(grant, "000000")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["attempts_left"], TwoFactorChallenge.MAX_ATTEMPTS - 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com")

    def test_the_attempt_cap_burns_the_grant(self):
        # EmailChangeRequest has no attempt counter of its own (the signed-in flow that owns it does
        # not need one). Without this cap, six digits behind a 15 minute grant is a guessable
        # takeover. Enforced by burning the grant in the DATABASE, so a flushed cache cannot hand
        # back an unlimited retry.
        grant = self.verified_grant()
        self.request_email(grant)

        for _attempt in range(TwoFactorChallenge.MAX_ATTEMPTS):
            response = self.confirm_email(grant, "000000")

        self.assertEqual(response.status_code, 429)
        self.assertFalse(AccountRecoveryGrant.objects.filter(
            token=grant, consumed_at__isnull=True).exists())
        # And the real code is worthless now, because the grant it belonged to is gone.
        self.assertEqual(self.confirm_email(grant, "000000").status_code, 400)

    def test_an_expired_code_is_refused(self):
        grant = self.verified_grant()
        self.request_email(grant)
        pending = EmailChangeRequest.objects.get(user=self.user)
        code = pending.token
        pending.created_at = timezone.now() - timezone.timedelta(minutes=11)
        pending.save(update_fields=["created_at"])

        response = self.confirm_email(grant, code)

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com")

    def test_a_code_cannot_be_used_twice(self):
        grant = self.verified_grant()
        self.request_email(grant)
        code = self.emailed_change_code()
        self.confirm_email(grant, code)

        response = self.confirm_email(grant, code)

        self.assertEqual(response.status_code, 400)

    def test_a_second_code_inside_a_minute_is_refused(self):
        grant = self.verified_grant()
        self.request_email(grant)

        response = self.request_email(grant, "another@gmail.com")

        self.assertEqual(response.status_code, 429)

    def test_a_send_that_fails_is_reported_rather_than_swallowed(self):
        # views.send_email NEVER raises: it catches everything and answers False. An earlier draft
        # of this endpoint wrapped it in try/except, which is a check that can never fire, and the
        # user would have been parked on a code screen waiting for something that was never sent.
        grant = self.verified_grant()

        with patch("afc_auth.views_recovery.send_email", return_value=False):
            response = self.request_email(grant)

        self.assertEqual(response.status_code, 400)
        self.assertIn("could not send", response.json()["message"])

    def test_a_failed_send_does_not_cost_the_retry(self):
        # The 60 second cooldown reads the pending row's timestamp, so leaving that row behind after
        # OUR failure would make AFC's own broken send lock the user out of trying again.
        grant = self.verified_grant()
        with patch("afc_auth.views_recovery.send_email", return_value=False):
            self.request_email(grant)

        response = self.request_email(grant)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(EmailChangeRequest.objects.get(user=self.user).new_email, NEW_EMAIL)

    def test_asking_again_later_replaces_the_pending_address(self):
        grant = self.verified_grant()
        self.request_email(grant)
        pending = EmailChangeRequest.objects.get(user=self.user)
        pending.created_at = timezone.now() - timezone.timedelta(minutes=2)
        pending.save(update_fields=["created_at"])

        response = self.request_email(grant, "changedmymind@gmail.com")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(EmailChangeRequest.objects.get(user=self.user).new_email,
                         "changedmymind@gmail.com")


class EmailChangeTwoFactorRuleTests(RecoveryTestBase):
    """THE RULE (views_recovery.py §4): the email move REFUSES on any account with two-step sign-in
    switched on. No acknowledgement flag, no override, whatever the method.

    This is STRICTER than what a head admin may do (admin_set_user_email accepts
    disable_two_factor: true and proceeds), and the difference is the point: an admin has checked
    identity out of band and lands on an audit row, while a WhatsApp code proves possession of a
    phone number and nothing more.

    One test per limb of the argument, so a change that breaks the reasoning breaks a named test.
    """

    def _enable_email_2fa(self):
        TwoFactorSettings.objects.update_or_create(
            user=self.user, defaults={"is_enabled": True, "method": "email"})

    def _enrol_authenticator(self):
        two_factor.start_totp_enrolment(self.user)
        two_factor.promote_totp_enrolment(self.user, -1)
        TwoFactorSettings.objects.update_or_create(
            user=self.user, defaults={"is_enabled": True, "method": "totp"})

    def test_an_account_with_two_step_sign_in_cannot_move_its_email(self):
        self._enable_email_2fa()

        response = self.request_email(self.verified_grant())

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["two_factor_enabled"])
        self.assertFalse(EmailChangeRequest.objects.filter(user=self.user).exists())

    def test_there_is_no_flag_that_switches_the_factor_off(self):
        # The admin tool has exactly such a flag. Sending it here must achieve nothing: if this ever
        # starts working, the module's whole safety argument is gone (number, then email, then the
        # ordinary emailed reset, then in with no factor at all).
        self._enable_email_2fa()
        grant = self.verified_grant()

        response = self.client.post(
            REQUEST_EMAIL,
            {"grant_token": grant, "new_email": NEW_EMAIL, "disable_two_factor": True},
            content_type="application/json")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(two_factor.is_enabled_for(self.user))
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).method, "email")

    def test_the_confirm_call_refuses_too_when_the_factor_arrives_late(self):
        # The two calls are minutes apart and another session could switch 2FA on in between, so the
        # rule is checked on both rather than only at the door.
        grant = self.verified_grant()
        self.request_email(grant)
        code = self.emailed_change_code()
        self._enable_email_2fa()

        response = self.confirm_email(grant, code)

        self.assertEqual(response.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@gmail.com")

    def test_an_authenticator_account_is_refused_as_well(self):
        # The deliberate cost of one rule that cannot rot, written down as a test. A narrower rule
        # (refuse only an EMAIL-delivered factor) was available and rejected: it would make this
        # endpoint's safety depend on another module's method registry, which two_factor.py says is
        # one line away from listing "whatsapp".
        self._enrol_authenticator()

        response = self.request_email(self.verified_grant())

        self.assertEqual(response.status_code, 409)

    def test_the_same_account_can_still_reset_its_password(self):
        # Only the email move is closed to a 2FA account. Closing the password reset too would strand
        # exactly the users this feature exists for, and it is unnecessary: the factor still stands
        # after a reset.
        self._enable_email_2fa()

        response = self.reset(self.verified_grant())

        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertTrue(two_factor.is_enabled_for(self.user))

    def test_the_refusal_explains_itself_and_offers_a_way_forward(self):
        # A dead end with no explanation sends a locked-out person to nobody. The message has to name
        # both things they can still do.
        self._enable_email_2fa()

        message = self.request_email(self.verified_grant()).json()["message"]

        self.assertIn("two-step sign-in", message)
        self.assertIn("reset your password", message)
        self.assertIn("support", message)


class StaleNumberTests(RecoveryTestBase):
    """A NUMBER IS ONLY EVIDENCE WHILE IT IS STILL THEIRS (owner 2026-08-08).

    Mobile lines that go dead get reissued, so a number saved and forgotten years ago may now belong
    to a stranger who would inherit the whole account. views_recovery.RECOVERY_NUMBER_MAX_AGE caps
    how long a saved number counts as proof; UserProfile.whatsapp_number_updated_at is the date it
    is measured from.

    The ordinary tripwire cannot cover this case: the premise of the entire flow is that the
    account's inbox may be dead, so the warning email may reach nobody. That is why the guard exists
    rather than relying on the notice.
    """

    def _age_the_number(self, days):
        """Backdate the saved number's confirmation date by `days`."""
        profile = UserProfile.objects.get(user=self.user)
        profile.whatsapp_number_updated_at = timezone.now() - timezone.timedelta(days=days)
        profile.save(update_fields=["whatsapp_number_updated_at"])

    def test_a_number_nobody_has_confirmed_for_over_a_year_sends_no_code(self):
        self._age_the_number(400)

        response, _token = self.start()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.sends, [], "a stale number must not be messaged at all")

    def test_a_stale_account_is_indistinguishable_from_a_real_one(self):
        # The refusal must not become an oracle for "this account has an OLD number", which would be
        # a map of exactly which accounts are worth attacking through a recycled SIM.
        fresh_response, _fresh_token = self.start()
        self.sends.clear()
        self._age_the_number(400)

        stale_response, stale_token = self.start()

        self.assertEqual(stale_response.status_code, fresh_response.status_code)
        self.assertEqual(stale_response.json()["message"], fresh_response.json()["message"])
        self.assertEqual(len(stale_token), len(fresh_response.json()["recovery_token"]))

    def test_the_stale_accounts_decoy_token_fails_like_a_wrong_code(self):
        self._age_the_number(400)
        _response, token = self.start()

        verify = self.client.post(VERIFY, {"recovery_token": token, "code": "123456"},
                                  content_type="application/json")

        self.assertEqual(verify.status_code, 400)
        self.assertNotIn("grant_token", verify.json())

    def test_a_number_confirmed_inside_the_window_still_works(self):
        # The boundary from the usable side: 364 days is still proof, so the guard is a ceiling on
        # abandonment rather than a slow expiry of everybody's access.
        self._age_the_number(364)

        response, _token = self.start()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.sends), 1)

    def test_re_saving_the_number_restarts_the_clock(self):
        # The only self-serve way back for somebody whose number went stale, and the reason
        # edit_profile stamps the field even when the digits do not change.
        self._age_the_number(400)
        session = SessionToken.objects.create(
            user=self.user, token="restartclock".ljust(40, "x"),
            expires_at=timezone.now() + timezone.timedelta(days=7))
        self.user.full_name = "Player One"
        self.user.save(update_fields=["full_name"])

        edit = self.client.post(
            "/auth/edit-profile/",
            {
                "full_name": self.user.full_name,
                "in_game_name": self.user.username,
                "email": self.user.email,
                "whatsapp_number": NUMBER,   # the SAME number, re-confirmed
            },
            HTTP_AUTHORIZATION=f"Bearer {session.token}")
        self.assertEqual(edit.status_code, 200, edit.content)

        response, _token = self.start()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.sends), 1, "re-confirming the number must make it usable again")

    def test_a_number_with_no_recorded_date_is_treated_as_fresh(self):
        # The judgement call in _number_too_stale, pinned so it cannot be flipped by accident.
        # Migration 0039 dated every number that already existed, so NULL now means "no number", and
        # an account in that state is refused earlier by issue_challenge. Treating an unexpected
        # NULL as STALE would silently disable recovery for anybody a future code path creates a
        # number for without stamping it.
        profile = UserProfile.objects.get(user=self.user)
        self.assertIsNone(profile.whatsapp_number_updated_at)

        response, _token = self.start()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.sends), 1)
