"""
Tests for TWO-FACTOR AUTHENTICATION (owner 2026-08-06).

Opt-in email codes as a second sign-in step. What is covered, and why each one is here:

  REGRESSION (the one that matters most)
    - A user WITHOUT 2FA logs in exactly as before: same status, same keys, same session token,
      same LoginHistory row. 6,790 accounts depend on this branch being untouched.

  THE LOGIN GATE
    - With 2FA on, step one returns a challenge and NEVER a session token, and creates no session.
    - Step two exchanges challenge + code for the identical login payload.
    - Wrong code, expired code, reused code, and the attempt cap all refuse.
    - Issuing a new code invalidates the previous one.

  ENABLE / DISABLE / RECOVERY
    - Enabling requires proving the method works first; the flag does not flip on a bad code.
    - Backup codes are single-use, work at the login step, and can be regenerated.
    - Disabling requires fresh proof and clears the recovery codes.

  SECURITY PROPERTIES
    - The plaintext code is never stored (only a hash), and failures never leak which part failed.

Delivery is stubbed at the SERVICE BOUNDARY (afc_auth.two_factor.EmailCodeMethod.deliver), so the
suite captures the code that would have been emailed without touching SMTP. Same idea as mocking an
SDK client: we test our flow, not Office365.

Run: python manage.py test afc_auth.tests_two_factor
"""
import json
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from afc_auth import two_factor
from afc_auth.models import (
    LoginHistory,
    SessionToken,
    TwoFactorBackupCode,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
)

PASSWORD = "CorrectHorse!9"


class TwoFactorTestBase(TestCase):
    """Shared fixtures: a user with a real (hashed) password, plus helpers that capture the code
    that WOULD have been emailed so the tests can submit it."""

    def setUp(self):
        self.client = Client()
        self.sent_codes = []

        # geo_for_ip hits ipinfo.io on a login that is the day's first. Stubbed so the suite never
        # makes a billed external call (mock at the boundary, per the project testing rules).
        geo_patcher = patch("afc_auth.views.geo_for_ip", return_value={})
        geo_patcher.start()
        self.addCleanup(geo_patcher.stop)

        # Capture instead of send. Returns True so the flow believes delivery succeeded.
        def _capture(_self, _user, code):
            self.sent_codes.append(code)
            return True

        deliver_patcher = patch.object(
            two_factor.EmailCodeMethod, "deliver", _capture, create=False)
        deliver_patcher.start()
        self.addCleanup(deliver_patcher.stop)

        self.user = User.objects.create(
            username="player1",
            email="player1@gmail.com",
            full_name="Player One",
            role="player",
            password=make_password(PASSWORD),
            is_active=True,
        )

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def post(self, path, body=None, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def do_login(self, password=PASSWORD, username=None):
        return self.post("/auth/login/",
                         {"ign_or_uid": username or self.user.username, "password": password})

    def enable_2fa(self):
        """Switch 2FA on the way a real user does (send-code -> enable) and return the session
        token plus the recovery codes. Using the real endpoints rather than writing the rows
        directly means every test downstream is exercising a genuinely enabled account."""
        session = self.do_login().json()["session_token"]
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)
        self.assertEqual(sent.status_code, 200, sent.content)
        resp = self.post(
            "/auth/two-factor/enable/",
            {"challenge_token": sent.json()["challenge_token"], "code": self.sent_codes[-1]},
            token=session,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return session, resp.json()["backup_codes"]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE REGRESSION. If this class ever goes red, 6,790 people cannot sign in.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class LoginWithoutTwoFactorUnchangedTests(TwoFactorTestBase):
    """A user who has never touched 2FA must see the byte-identical login they saw before."""

    def test_login_response_shape_is_unchanged(self):
        resp = self.do_login()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Exactly the keys login() has always returned - no more, no fewer.
        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo"})
        self.assertEqual(body["message"], "Login successful")
        self.assertEqual(set(body["user"].keys()), {"id", "username", "language"})
        self.assertEqual(body["user"]["username"], "player1")
        self.assertEqual(body["user"]["language"], "en")
        self.assertNotIn("two_factor_required", body)

    def test_login_still_creates_a_usable_session(self):
        token = self.do_login().json()["session_token"]

        # Read it back out of the DATABASE, not just off the response.
        self.assertTrue(SessionToken.objects.filter(user=self.user, token=token).exists())
        profile = self.client.get("/auth/get-user-profile/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(profile.status_code, 200)

    def test_login_still_records_login_history(self):
        self.do_login()
        self.assertEqual(LoginHistory.objects.filter(user=self.user).count(), 1)

    def test_bad_password_still_401s(self):
        resp = self.do_login(password="wrong")
        self.assertEqual(resp.status_code, 401)

    def test_inactive_account_still_403s(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.do_login().status_code, 403)

    def test_no_code_is_emailed_for_a_user_without_2fa(self):
        self.do_login()
        self.assertEqual(self.sent_codes, [])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ENABLING
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class EnableFlowTests(TwoFactorTestBase):

    def test_enable_requires_proving_the_method_first(self):
        session = self.do_login().json()["session_token"]

        resp = self.post("/auth/two-factor/enable/",
                         {"challenge_token": "made-up", "code": "123456"}, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TwoFactorSettings.objects.filter(user=self.user, is_enabled=True).exists())

    def test_wrong_code_does_not_flip_the_flag(self):
        session = self.do_login().json()["session_token"]
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)

        resp = self.post("/auth/two-factor/enable/",
                         {"challenge_token": sent.json()["challenge_token"], "code": "000000"},
                         token=session)

        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_enable_turns_it_on_and_returns_backup_codes_once(self):
        _session, codes = self.enable_2fa()

        # State read back from the DB, not from the response.
        row = TwoFactorSettings.objects.get(user=self.user)
        self.assertTrue(row.is_enabled)
        self.assertEqual(row.method, "email")
        self.assertIsNotNone(row.enabled_at)
        # The codes exist, are the promised count, and are stored ONLY as hashes.
        self.assertEqual(len(codes), two_factor.BACKUP_CODE_COUNT)
        stored = list(TwoFactorBackupCode.objects.filter(user=self.user)
                      .values_list("code_hash", flat=True))
        self.assertEqual(len(stored), two_factor.BACKUP_CODE_COUNT)
        for plain in codes:
            self.assertNotIn(plain, stored)

    def test_enabling_twice_is_refused(self):
        session, _ = self.enable_2fa()
        resp = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)
        self.assertEqual(resp.status_code, 409)

    def test_status_reflects_the_change(self):
        session, _ = self.enable_2fa()
        body = self.client.get("/auth/two-factor/status/",
                               HTTP_AUTHORIZATION=f"Bearer {session}").json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["method"], "email")
        self.assertEqual(body["backup_codes_remaining"], two_factor.BACKUP_CODE_COUNT)
        # The destination is masked even on an authenticated surface.
        self.assertEqual(body["destination"], "pl*****@gmail.com")

    def test_status_requires_a_session(self):
        self.assertEqual(self.client.get("/auth/two-factor/status/").status_code, 400)
        self.assertEqual(
            self.client.get("/auth/two-factor/status/", HTTP_AUTHORIZATION="Bearer nope")
            .status_code, 401)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# LOGGING IN WITH 2FA ON
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class LoginWithTwoFactorTests(TwoFactorTestBase):

    def setUp(self):
        super().setUp()
        self.enable_2fa()
        # Forget the sessions and codes created during setup so each test starts clean.
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()

    def test_step_one_returns_a_challenge_and_no_session_token(self):
        resp = self.do_login()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("session_token", body)
        self.assertTrue(body["challenge_token"])
        self.assertEqual(body["destination"], "pl*****@gmail.com")
        # The decisive assertion: nothing that can call the API was minted.
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_step_one_emails_exactly_one_code_and_never_returns_it(self):
        body = self.do_login().json()
        self.assertEqual(len(self.sent_codes), 1)
        self.assertNotIn(self.sent_codes[0], json.dumps(body))

    def test_step_two_exchanges_the_code_for_a_normal_session(self):
        challenge_token = self.do_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": self.sent_codes[-1]})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Identical to a no-2FA login response.
        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo"})
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=body["session_token"]).exists())

    def test_wrong_code_is_refused_and_mints_nothing(self):
        challenge_token = self.do_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": "000000"})

        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("session_token", resp.json())
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_expired_code_is_refused(self):
        challenge_token = self.do_login().json()["challenge_token"]
        code = self.sent_codes[-1]
        # Age the challenge past its 10-minute window.
        TwoFactorChallenge.objects.filter(token=challenge_token).update(
            expires_at=timezone.now() - timedelta(seconds=1))

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": code})

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_a_code_cannot_be_used_twice(self):
        challenge_token = self.do_login().json()["challenge_token"]
        code = self.sent_codes[-1]
        first = self.post("/auth/two-factor/verify/",
                          {"challenge_token": challenge_token, "code": code})
        self.assertEqual(first.status_code, 200)

        replay = self.post("/auth/two-factor/verify/",
                           {"challenge_token": challenge_token, "code": code})

        self.assertEqual(replay.status_code, 400)
        # Still exactly the ONE session from the first, legitimate exchange.
        self.assertEqual(SessionToken.objects.filter(user=self.user).count(), 1)

    def test_attempt_cap_burns_the_challenge(self):
        challenge_token = self.do_login().json()["challenge_token"]
        real_code = self.sent_codes[-1]

        for _ in range(TwoFactorChallenge.MAX_ATTEMPTS):
            self.post("/auth/two-factor/verify/",
                      {"challenge_token": challenge_token, "code": "000000"})

        # Even the CORRECT code no longer works once the cap is spent.
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": real_code})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_attempts_left_counts_down(self):
        challenge_token = self.do_login().json()["challenge_token"]
        first = self.post("/auth/two-factor/verify/",
                          {"challenge_token": challenge_token, "code": "000000"})
        self.assertEqual(first.json()["attempts_left"], TwoFactorChallenge.MAX_ATTEMPTS - 1)

    def test_issuing_a_new_code_invalidates_the_old_one(self):
        first_token = self.do_login().json()["challenge_token"]
        first_code = self.sent_codes[-1]
        # Step past the resend cooldown so a genuinely new code is issued.
        TwoFactorChallenge.objects.filter(token=first_token).update(
            created_at=timezone.now() - TwoFactorChallenge.RESEND_COOLDOWN - timedelta(seconds=1))

        resend = self.post("/auth/two-factor/resend/", {"challenge_token": first_token})
        self.assertEqual(resend.status_code, 200)
        self.assertTrue(resend.json()["code_sent"])
        new_token = resend.json()["challenge_token"]
        self.assertNotEqual(new_token, first_token)

        # The OLD code is dead...
        stale = self.post("/auth/two-factor/verify/",
                          {"challenge_token": first_token, "code": first_code})
        self.assertEqual(stale.status_code, 400)
        # ...and the NEW one works.
        fresh = self.post("/auth/two-factor/verify/",
                          {"challenge_token": new_token, "code": self.sent_codes[-1]})
        self.assertEqual(fresh.status_code, 200)

    def test_resend_inside_the_cooldown_does_not_send_a_second_code(self):
        challenge_token = self.do_login().json()["challenge_token"]
        self.assertEqual(len(self.sent_codes), 1)

        resp = self.post("/auth/two-factor/resend/", {"challenge_token": challenge_token})

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["code_sent"])
        self.assertGreater(resp.json()["retry_after"], 0)
        self.assertEqual(len(self.sent_codes), 1)  # still just the one email

    def test_unknown_challenge_token_gives_the_same_generic_error(self):
        wrong_code = self.post("/auth/two-factor/verify/",
                               {"challenge_token": self.do_login().json()["challenge_token"],
                                "code": "000000"})
        unknown = self.post("/auth/two-factor/verify/",
                            {"challenge_token": "does-not-exist", "code": "000000"})
        # Identical wording, so a caller cannot probe which accounts have 2FA on.
        self.assertEqual(wrong_code.json()["message"], unknown.json()["message"])

    def test_send_rate_limit_never_locks_the_real_user_out(self):
        """Past the hourly ceiling we stop sending, but the user still gets a usable challenge -
        otherwise anyone with the password could deny the owner their own factor."""
        for _ in range(TwoFactorChallenge.MAX_SENDS_PER_HOUR + 2):
            body = self.do_login().json()
            # Age each challenge past the cooldown so the HOURLY cap is what bites, not the 60s gap.
            TwoFactorChallenge.objects.filter(token=body["challenge_token"]).update(
                created_at=timezone.now() - TwoFactorChallenge.RESEND_COOLDOWN
                - timedelta(seconds=1))

        self.assertEqual(len(self.sent_codes), TwoFactorChallenge.MAX_SENDS_PER_HOUR)
        self.assertTrue(body["challenge_token"])
        self.assertFalse(body["code_sent"])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# RECOVERY CODES
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class BackupCodeTests(TwoFactorTestBase):

    def setUp(self):
        super().setUp()
        _session, self.codes = self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()

    def test_backup_code_signs_you_in_and_is_then_spent(self):
        challenge_token = self.do_login().json()["challenge_token"]

        first = self.post("/auth/two-factor/verify/",
                          {"challenge_token": challenge_token, "backup_code": self.codes[0]})

        self.assertEqual(first.status_code, 200)
        self.assertIn("session_token", first.json())
        self.assertEqual(
            TwoFactorBackupCode.objects.filter(user=self.user, used_at__isnull=True).count(),
            two_factor.BACKUP_CODE_COUNT - 1)

    def test_the_same_backup_code_cannot_be_used_twice(self):
        self.post("/auth/two-factor/verify/",
                  {"challenge_token": self.do_login().json()["challenge_token"],
                   "backup_code": self.codes[0]})
        SessionToken.objects.filter(user=self.user).delete()

        second = self.post("/auth/two-factor/verify/",
                           {"challenge_token": self.do_login().json()["challenge_token"],
                            "backup_code": self.codes[0]})

        self.assertEqual(second.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_backup_codes_are_case_and_hyphen_insensitive(self):
        typed = self.codes[1].lower().replace("-", "")
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "backup_code": typed})
        self.assertEqual(resp.status_code, 200)

    def test_regenerate_invalidates_the_previous_set(self):
        session = self.post("/auth/two-factor/verify/",
                            {"challenge_token": self.do_login().json()["challenge_token"],
                             "code": self.sent_codes[-1]}).json()["session_token"]
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)

        resp = self.post("/auth/two-factor/backup-codes/",
                         {"challenge_token": sent.json()["challenge_token"],
                          "code": self.sent_codes[-1]}, token=session)

        self.assertEqual(resp.status_code, 200)
        new_codes = resp.json()["backup_codes"]
        self.assertEqual(len(new_codes), two_factor.BACKUP_CODE_COUNT)
        self.assertEqual(set(new_codes) & set(self.codes), set())
        # An OLD code no longer signs anyone in.
        SessionToken.objects.filter(user=self.user).delete()
        stale = self.post("/auth/two-factor/verify/",
                          {"challenge_token": self.do_login().json()["challenge_token"],
                           "backup_code": self.codes[0]})
        self.assertEqual(stale.status_code, 400)

    def test_regenerate_needs_fresh_proof(self):
        session = self.post("/auth/two-factor/verify/",
                            {"challenge_token": self.do_login().json()["challenge_token"],
                             "code": self.sent_codes[-1]}).json()["session_token"]

        resp = self.post("/auth/two-factor/backup-codes/",
                         {"challenge_token": "made-up", "code": "123456"}, token=session)

        self.assertEqual(resp.status_code, 400)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# DISABLING
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class DisableFlowTests(TwoFactorTestBase):

    def setUp(self):
        super().setUp()
        _session, self.codes = self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        # Sign in properly through both steps so we hold a real post-2FA session.
        challenge = self.do_login().json()["challenge_token"]
        self.session = self.post("/auth/two-factor/verify/",
                                 {"challenge_token": challenge,
                                  "code": self.sent_codes[-1]}).json()["session_token"]

    def test_a_session_alone_cannot_disable_it(self):
        resp = self.post("/auth/two-factor/disable/", {}, token=self.session)

        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(two_factor.is_enabled_for(self.user))

    def test_disable_with_a_fresh_code(self):
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=self.session)

        resp = self.post("/auth/two-factor/disable/",
                         {"challenge_token": sent.json()["challenge_token"],
                          "code": self.sent_codes[-1]}, token=self.session)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])
        self.user.refresh_from_db()
        self.assertFalse(two_factor.is_enabled_for(self.user))
        # Recovery codes go with it - a printed code must not survive to a later re-enable.
        self.assertEqual(TwoFactorBackupCode.objects.filter(user=self.user).count(), 0)

    def test_disable_with_a_backup_code(self):
        resp = self.post("/auth/two-factor/disable/",
                         {"backup_code": self.codes[0]}, token=self.session)

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_login_returns_to_one_step_after_disabling(self):
        self.post("/auth/two-factor/disable/", {"backup_code": self.codes[0]}, token=self.session)

        body = self.do_login().json()

        self.assertNotIn("two_factor_required", body)
        self.assertIn("session_token", body)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE LOGIC LAYER, TESTED DIRECTLY
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TwoFactorModuleTests(TwoFactorTestBase):

    def test_code_is_stored_hashed_not_plaintext(self):
        issued = two_factor.issue_challenge(self.user, purpose="login")
        code = self.sent_codes[-1]

        self.assertNotEqual(issued["challenge"].code_hash, code)
        self.assertNotIn(code, issued["challenge"].code_hash)

    def test_is_enabled_for_is_false_without_a_settings_row(self):
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_is_enabled_for_is_false_when_the_method_cannot_reach_the_user(self):
        TwoFactorSettings.objects.create(user=self.user, is_enabled=True, method="email")
        self.user.email = ""
        self.user.save(update_fields=["email"])
        # Never strand someone behind a factor that can no longer arrive.
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_mask_email_hides_the_address(self):
        self.assertEqual(two_factor.mask_email("jonathan@gmail.com"), "jo******@gmail.com")
        # A very short local part reveals only ONE character - showing two would show the whole
        # thing, which is the opposite of what masking is for.
        self.assertEqual(two_factor.mask_email("ab@x.com"), "a***@x.com")
        self.assertEqual(two_factor.mask_email("a@x.com"), "a***@x.com")
        self.assertEqual(two_factor.mask_email("not-an-email"), "")

    def test_get_method_never_returns_none(self):
        self.assertEqual(two_factor.get_method("email").code, "email")
        # An unknown or future value falls back rather than crashing a login.
        self.assertEqual(two_factor.get_method("totp").code, "email")
        self.assertEqual(two_factor.get_method(None).code, "email")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SSO: GOOGLE AND DISCORD GO THROUGH THE SAME GATE (owner 2026-08-06)
#
# A second factor that a linked social account walks straight past is not a second factor, and the
# accounts most likely to switch 2FA on (admins, organizers) are exactly the ones with Discord
# linked. These tests exist to make that regression loud if anyone re-adds a provider-local login.
#
# Both providers are mocked at the SERVICE BOUNDARY - Google's ID-token verifier, Discord's two
# HTTP calls - so nothing here touches a real OAuth server and no real client id/secret is needed.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id.apps.googleusercontent.com")
class GoogleSsoTwoFactorTests(TwoFactorTestBase):
    """POST /auth/google/ runs login_or_challenge, exactly like the password login."""

    def _google_login(self):
        # The claims Google would have signed for this account.
        claims = {"email": self.user.email, "email_verified": True, "name": "Player One"}
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
            return self.post("/auth/google/", {"credential": "a-google-id-token"})

    def test_without_2fa_google_signs_in_exactly_as_before(self):
        resp = self._google_login()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # The shape this endpoint has always returned: the login body plus is_new.
        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo", "is_new"})
        self.assertFalse(body["is_new"])
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=body["session_token"]).exists())

    def test_with_2fa_google_is_challenged_and_gets_no_session(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()

        resp = self._google_login()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("session_token", body)
        # is_new must NOT ride along on a challenge: it would tell someone who has not yet passed
        # the factor whether an account had just been created.
        self.assertNotIn("is_new", body)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_google_challenge_is_completed_by_the_shared_verify_endpoint(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        challenge_token = self._google_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": self.sent_codes[-1]})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("session_token", resp.json())

    def test_google_wrong_code_is_refused_and_mints_nothing(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        challenge_token = self._google_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": "000000"})

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_google_challenge_is_single_use(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        challenge_token = self._google_login().json()["challenge_token"]
        code = self.sent_codes[-1]
        self.assertEqual(
            self.post("/auth/two-factor/verify/",
                      {"challenge_token": challenge_token, "code": code}).status_code, 200)

        replay = self.post("/auth/two-factor/verify/",
                           {"challenge_token": challenge_token, "code": code})

        self.assertEqual(replay.status_code, 400)
        self.assertEqual(SessionToken.objects.filter(user=self.user).count(), 1)


# LocMemCache, not the project's Redis: the Discord handoff lives in the cache, and these tests
# must neither depend on Redis being up nor write into the cache the dev server shares.
@override_settings(
    DISCORD_CLIENT_ID="discord-client",
    DISCORD_CLIENT_SECRET="discord-secret",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class DiscordSsoTwoFactorTests(TwoFactorTestBase):
    """The Discord redirect flow: the callback stashes the gate's result, the exchange hands it
    over. The challenge has to survive the redirect WITHOUT appearing in the URL."""

    def _callback(self):
        """Drive start -> callback with Discord's two HTTP calls mocked. Returns the redirect."""
        start = self.client.get("/auth/discord/sso/start/?next=/home")
        self.assertEqual(start.status_code, 302)
        # Pull the CSRF state nonce back out of the consent URL we were sent to.
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]

        token_response = Mock(status_code=200)
        token_response.json.return_value = {"access_token": "an-access-token"}
        me_response = Mock()
        me_response.json.return_value = {
            "id": "42", "email": self.user.email, "verified": True,
            "username": "player1", "global_name": "Player One",
        }
        with patch("afc_auth.views.requests.post", return_value=token_response), \
             patch("afc_auth.views.requests.get", return_value=me_response):
            return self.client.get(
                f"/auth/discord/sso/callback/?code=discord-code&state={state}")

    def _handoff_from(self, redirect_response):
        self.assertEqual(redirect_response.status_code, 302)
        return parse_qs(urlparse(redirect_response["Location"]).query)["code"][0]

    def test_without_2fa_discord_signs_in_exactly_as_before(self):
        handoff = self._handoff_from(self._callback())

        resp = self.post("/auth/discord/sso/exchange/", {"code": handoff})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("session_token", body)
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=body["session_token"]).exists())

    def test_with_2fa_discord_is_challenged_and_gets_no_session(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()

        handoff = self._handoff_from(self._callback())
        resp = self.post("/auth/discord/sso/exchange/", {"code": handoff})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("session_token", body)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_the_challenge_token_never_appears_in_the_redirect_url(self):
        """The whole reason the handoff exists: this URL lands in browser history and can leak
        through Referer, so nothing usable may be in it."""
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()

        redirect_response = self._callback()
        location = redirect_response["Location"]
        handoff = self._handoff_from(redirect_response)
        challenge = self.post("/auth/discord/sso/exchange/",
                              {"code": handoff}).json()["challenge_token"]

        self.assertNotIn(challenge, location)
        # And the opaque handoff is single use, so the URL itself cannot be replayed.
        self.assertEqual(
            self.post("/auth/discord/sso/exchange/", {"code": handoff}).status_code, 400)

    def test_discord_challenge_is_completed_by_the_shared_verify_endpoint(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        handoff = self._handoff_from(self._callback())
        challenge_token = self.post("/auth/discord/sso/exchange/",
                                    {"code": handoff}).json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": self.sent_codes[-1]})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("session_token", resp.json())

    def test_discord_wrong_code_is_refused_and_mints_nothing(self):
        self.enable_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        handoff = self._handoff_from(self._callback())
        challenge_token = self.post("/auth/discord/sso/exchange/",
                                    {"code": handoff}).json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": "000000"})

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_exchange_still_accepts_a_pre_deploy_handoff(self):
        """Handoffs minted by the previous version stored a bare session-token STRING. They live
        90 seconds, so a deploy can straddle one and it must not 500."""
        SessionToken.objects.create(user=self.user, token="legacy_token_1")
        cache.set("discord_sso_handoff:legacy", "legacy_token_1", 90)

        resp = self.post("/auth/discord/sso/exchange/", {"code": "legacy"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["session_token"], "legacy_token_1")
