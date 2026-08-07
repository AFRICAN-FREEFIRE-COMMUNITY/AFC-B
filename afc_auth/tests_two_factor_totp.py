"""
Tests for the AUTHENTICATOR APP second factor (TOTP, RFC 6238) - owner 2026-08-07.

The email method and everything it shares with this one is covered in tests_two_factor.py. This
file covers only what the authenticator app added, and one thing it must NOT have changed.

  ENROLMENT (the anti-lockout property)
    - setup hands out a secret and an otpauth:// URI and changes NOTHING about the account.
    - The flag does not flip, and the method does not change, until a real code proves the app.
    - A wrong app code, a stale enrolment and a missing proof all refuse.
    - Enrolling requires fresh proof of the account as it stands, so a stolen session alone cannot
      bolt an attacker's authenticator onto an account.

  SIGNING IN
    - Login returns a challenge with method "totp", no session token, and nothing "sent".
    - A code from the app completes it, through the SAME verify endpoint as email.
    - Drift: the previous and next 30-second step are accepted; two steps out is refused.
    - Replay: a code that worked cannot work again inside its own window.
    - The hourly guessing budget stops someone with the password walking the code space.

  SWITCHING AND TURNING OFF
    - Switching from email to the app requires proof of the EMAIL factor first.
    - Switching keeps the recovery codes that already exist (no second parallel set).
    - Turning off requires a code from the app, and wipes the secret.

  RECOVERY CODES
    - Still work at the login step when the method is the app, and can still be regenerated.

  STORAGE
    - The secret in the database is NOT the base32 string the user scanned.

  REGRESSION
    - A user with NO 2FA, and a user on the EMAIL method, are both completely unaffected.

WHY THIS FILE COMPUTES ITS OWN TOTP CODES:
  The application uses pyotp. If the tests also used pyotp, they would prove only that pyotp agrees
  with itself, which is worth nothing. `_reference_totp` below is a from-scratch RFC 6238
  implementation over hmac + struct from the standard library, written against the spec rather than
  the library. When a test signs in with a code from it, that is a real interoperability check
  against exactly the arithmetic Google Authenticator performs. The RFC 6238 Appendix B test vectors
  are checked against it first, so the oracle itself is verified before anything leans on it.

Run: python manage.py test afc_auth.tests_two_factor_totp
"""
import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth import two_factor
from afc_auth.models import (
    SessionToken,
    TwoFactorBackupCode,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
)

PASSWORD = "CorrectHorse!9"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The independent oracle. Standard library only, no pyotp.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _reference_hotp(secret_bytes: bytes, counter: int, digits: int = 6,
                    digest=hashlib.sha1) -> str:
    """RFC 4226 HOTP, written from the spec.

    HMAC the 8-byte big-endian counter, then dynamic truncation: the low nibble of the last byte
    picks a 4-byte offset, the high bit of that word is masked off, and the result is taken modulo
    10^digits."""
    mac = hmac.new(secret_bytes, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def _reference_totp(base32_secret: str, at=None, step_offset: int = 0,
                    period: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP: HOTP with the counter set to the number of `period`-second steps since the
    Unix epoch. `step_offset` moves whole steps, which is how the drift tests below stand a code
    exactly one or two steps away from now."""
    padding = "=" * (-len(base32_secret) % 8)
    secret_bytes = base64.b32decode(base32_secret + padding, casefold=True)
    counter = int(at if at is not None else time.time()) // period + step_offset
    return _reference_hotp(secret_bytes, counter, digits=digits)


class ReferenceImplementationTests(TestCase):
    """Verify the ORACLE before anything relies on it, using RFC 6238 Appendix B.

    Those published vectors use the ASCII secret "12345678901234567890"; base32 of that is the
    string below. If this class is red, every other assertion in this file is meaningless."""

    RFC_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode()

    def test_rfc6238_appendix_b_vectors(self):
        # (unix time, expected 8-digit SHA1 code) straight out of the RFC's table.
        for at, expected in [
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
            (20000000000, "65353130"),
        ]:
            self.assertEqual(
                _reference_totp(self.RFC_SECRET_B32, at=at, digits=8), expected,
                f"RFC 6238 vector failed at t={at}")

    def test_the_app_and_the_oracle_agree(self):
        """The interoperability assertion: pyotp (what AFC runs) and the from-scratch
        implementation (what an authenticator app runs) produce the same digits for the same secret
        and the same instant."""
        secret = two_factor.pyotp.random_base32(length=two_factor.TOTP_SECRET_LENGTH)
        now = int(time.time())
        for offset in (-2, -1, 0, 1, 2):
            step = now // two_factor.TOTP_PERIOD + offset
            self.assertEqual(
                two_factor.pyotp.TOTP(secret).generate_otp(step),
                _reference_totp(secret, at=now, step_offset=offset),
            )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpTestBase(TestCase):
    """A real user, email delivery captured instead of sent, and helpers that drive the actual
    endpoints rather than writing rows - so every test downstream is exercising a genuinely enrolled
    account and not a hand-built one."""

    def setUp(self):
        self.client = Client()
        self.sent_codes = []

        geo_patcher = patch("afc_auth.views.geo_for_ip", return_value={})
        geo_patcher.start()
        self.addCleanup(geo_patcher.stop)

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

    def do_login(self):
        return self.post("/auth/login/",
                         {"ign_or_uid": self.user.username, "password": PASSWORD})

    def session_without_2fa(self):
        return self.do_login().json()["session_token"]

    def app_code(self, secret, step_offset=0):
        """The digits the user's authenticator app would be showing, from the oracle."""
        return _reference_totp(secret, step_offset=step_offset)

    def enrol_totp(self, session=None):
        """Turn the authenticator on the way a real user does: setup -> email proof -> confirm.
        Returns (session_token, secret, backup_codes)."""
        session = session or self.session_without_2fa()

        setup = self.post("/auth/two-factor/totp/setup/", {}, token=session)
        self.assertEqual(setup.status_code, 200, setup.content)
        secret = setup.json()["secret"]

        proof = self.post("/auth/two-factor/send-code/",
                          {"purpose": setup.json()["proof_purpose"]}, token=session)
        self.assertEqual(proof.status_code, 200, proof.content)

        confirm = self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(secret),
            "proof_challenge_token": proof.json()["challenge_token"],
            "proof_code": self.sent_codes[-1],
        }, token=session)
        self.assertEqual(confirm.status_code, 200, confirm.content)
        return session, secret, confirm.json()["backup_codes"]

    def forget_spent_step(self):
        """Clear the replay floor.

        Confirming an enrolment legitimately SPENDS the step it was proved with (that is the point -
        see test_the_enrolment_code_cannot_then_be_used_to_sign_in). A real user's next sign-in is
        minutes later and lands in a fresh step, but a test runs in the same millisecond, so without
        this every test that enrols and then signs in would be asserting the replay rule again
        instead of the thing it is actually about."""
        TwoFactorSettings.objects.filter(user=self.user).update(totp_last_step=None)

    def sign_in_with_app(self, secret):
        """Complete BOTH login steps with the authenticator and return the session token."""
        self.forget_spent_step()
        challenge_token = self.do_login().json()["challenge_token"]
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": self.app_code(secret)})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["session_token"]

    def enable_email_2fa(self):
        """The already-shipped email flow, used by the switching and regression tests."""
        session = self.session_without_2fa()
        sent = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)
        resp = self.post("/auth/two-factor/enable/",
                         {"challenge_token": sent.json()["challenge_token"],
                          "code": self.sent_codes[-1]}, token=session)
        self.assertEqual(resp.status_code, 200, resp.content)
        return session, resp.json()["backup_codes"]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ENROLMENT
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpEnrolmentTests(TotpTestBase):

    def test_setup_returns_a_scannable_uri_and_a_typeable_secret(self):
        session = self.session_without_2fa()

        body = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()

        self.assertEqual(len(body["secret"]), two_factor.TOTP_SECRET_LENGTH)
        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/"))
        # The URI carries the same secret the user can type by hand, and names AFC so the app entry
        # is recognisable in a list of twenty.
        self.assertIn(f"secret={body['secret']}", body["otpauth_uri"])
        self.assertIn("issuer=AFC", body["otpauth_uri"])
        self.assertIn("player1", body["otpauth_uri"])
        self.assertEqual(body["digits"], 6)
        self.assertEqual(body["period"], 30)

    def test_setup_alone_changes_nothing(self):
        """The anti-lockout property. Someone who opens the dialog, sees the QR and closes the tab
        must be exactly where they started."""
        session = self.session_without_2fa()

        self.post("/auth/two-factor/totp/setup/", {}, token=session)

        self.assertFalse(two_factor.is_enabled_for(self.user))
        row = TwoFactorSettings.objects.get(user=self.user)
        self.assertFalse(row.is_enabled)
        self.assertEqual(row.method, "email")       # not switched
        self.assertEqual(row.totp_secret, "")       # nothing ACTIVE
        self.assertIsNone(row.totp_confirmed_at)
        # And signing in is still one step.
        self.assertIn("session_token", self.do_login().json())

    def test_setup_requires_a_session(self):
        self.assertEqual(self.post("/auth/two-factor/totp/setup/", {}).status_code, 400)
        self.assertEqual(
            self.post("/auth/two-factor/totp/setup/", {}, token="nope").status_code, 401)

    def test_confirm_turns_it_on_and_returns_recovery_codes_once(self):
        _session, _secret, codes = self.enrol_totp()

        row = TwoFactorSettings.objects.get(user=self.user)
        self.assertTrue(row.is_enabled)
        self.assertEqual(row.method, "totp")
        self.assertIsNotNone(row.totp_confirmed_at)
        self.assertIsNotNone(row.enabled_at)
        # The pending slot is emptied, so an abandoned half-enrolment cannot linger.
        self.assertEqual(row.totp_pending_secret, "")
        self.assertIsNone(row.totp_pending_at)
        self.assertEqual(len(codes), two_factor.BACKUP_CODE_COUNT)

    def test_a_wrong_app_code_does_not_flip_the_flag(self):
        session = self.session_without_2fa()
        self.post("/auth/two-factor/totp/setup/", {}, token=session)
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)

        resp = self.post("/auth/two-factor/totp/confirm/", {
            "code": "000000",
            "proof_challenge_token": proof.json()["challenge_token"],
            "proof_code": self.sent_codes[-1],
        }, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(two_factor.is_enabled_for(self.user))
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).totp_secret, "")

    def test_confirm_without_proof_of_the_account_is_refused(self):
        """A stolen session token must not be enough to attach an authenticator: whoever holds the
        browser still has to prove the factor the account has right now (here, the mailbox)."""
        session = self.session_without_2fa()
        secret = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()["secret"]

        resp = self.post("/auth/two-factor/totp/confirm/",
                         {"code": self.app_code(secret)}, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_confirm_with_a_forged_proof_token_is_refused(self):
        session = self.session_without_2fa()
        secret = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()["secret"]

        resp = self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(secret),
            "proof_challenge_token": "made-up",
            "proof_code": "123456",
        }, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_a_login_challenge_cannot_be_spent_as_enrolment_proof(self):
        """A challenge minted before a session exists must never be usable on an authenticated
        surface. Here the user already has email 2FA on, so signing in produces a login challenge."""
        session, _codes = self.enable_email_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        login_challenge = self.do_login().json()["challenge_token"]
        session = self.post("/auth/two-factor/verify/",
                            {"challenge_token": login_challenge,
                             "code": self.sent_codes[-1]}).json()["session_token"]
        # A fresh login challenge, deliberately left unspent.
        SessionToken.objects.filter(user=self.user).exists()
        stray = self.do_login().json()["challenge_token"]
        secret = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()["secret"]

        resp = self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(secret),
            "proof_challenge_token": stray,
            "proof_code": self.sent_codes[-1],
        }, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).method, "email")

    def test_a_stale_enrolment_cannot_be_confirmed(self):
        """A QR left on a shared screen must not still be claimable half an hour later."""
        session = self.session_without_2fa()
        secret = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()["secret"]
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)
        TwoFactorSettings.objects.filter(user=self.user).update(
            totp_pending_at=timezone.now() - two_factor.TOTP_ENROLMENT_LIFETIME
            - timedelta(seconds=1))

        resp = self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(secret),
            "proof_challenge_token": proof.json()["challenge_token"],
            "proof_code": self.sent_codes[-1],
        }, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_restarting_setup_does_not_break_a_working_authenticator(self):
        """The reason there are two secret columns. Someone who presses "set up" again and walks
        away must still be able to sign in with the app they already have."""
        _session, secret, _codes = self.enrol_totp()
        session2 = self.sign_in_with_app(secret)

        self.post("/auth/two-factor/totp/setup/", {}, token=session2)  # abandoned

        row = TwoFactorSettings.objects.get(user=self.user)
        self.assertNotEqual(row.totp_pending_secret, "")     # a new enrolment is waiting
        self.assertEqual(two_factor.active_totp_secret(self.user), secret)  # the old one still rules

    def test_the_enrolment_code_cannot_then_be_used_to_sign_in(self):
        """Replay protection spans the two flows: the six digits typed into the setup screen are
        SPENT by the setup screen, so they cannot be turned around into a sign-in thirty seconds
        later. A real user is minutes away and lands in a fresh step, so this costs them nothing."""
        _session, secret, _codes = self.enrol_totp()
        SessionToken.objects.filter(user=self.user).delete()

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "code": self.app_code(secret)})

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_status_reports_the_authenticator_method(self):
        _session, secret, _codes = self.enrol_totp()
        session = self.sign_in_with_app(secret)

        body = self.client.get("/auth/two-factor/status/",
                               HTTP_AUTHORIZATION=f"Bearer {session}").json()

        self.assertTrue(body["enabled"])
        self.assertEqual(body["method"], "totp")
        # There is no address to show, and saying "we send codes to..." would be a lie.
        self.assertEqual(body["destination"], "")
        self.assertIn("totp", body["available_methods"])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SIGNING IN WITH THE AUTHENTICATOR
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpLoginTests(TotpTestBase):

    def setUp(self):
        super().setUp()
        _session, self.secret, self.codes = self.enrol_totp()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        self.forget_spent_step()

    def test_login_returns_a_totp_challenge_and_sends_nothing(self):
        body = self.do_login().json()

        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("session_token", body)
        self.assertEqual(body["method"], "totp")
        self.assertEqual(body["destination"], "")
        # Nothing was sent, nothing FAILED to send, and there is no cooldown to wait out.
        self.assertFalse(body["code_sent"])
        self.assertFalse(body["delivery_failed"])
        self.assertEqual(body["retry_after"], 0)
        self.assertEqual(self.sent_codes, [])
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_a_code_from_the_app_completes_the_same_verify_endpoint(self):
        challenge_token = self.do_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": self.app_code(self.secret)})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Byte-identical to an email-method login and to a no-2FA login.
        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo"})
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=body["session_token"]).exists())

    def test_a_wrong_code_mints_nothing(self):
        challenge_token = self.do_login().json()["challenge_token"]

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token, "code": "000000"})

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_a_code_with_the_space_the_app_displays_is_accepted(self):
        """Authenticator apps show "123 456" and people paste what they see."""
        challenge_token = self.do_login().json()["challenge_token"]
        code = self.app_code(self.secret)

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": challenge_token,
                          "code": f"{code[:3]} {code[3:]}"})

        self.assertEqual(resp.status_code, 200)

    # ── DRIFT ────────────────────────────────────────────────────────────────────────────────
    def test_one_step_of_drift_either_side_is_accepted(self):
        for offset in (-1, 1):
            with self.subTest(offset=offset):
                SessionToken.objects.filter(user=self.user).delete()
                # Each pass must start from a clean replay floor: the point here is the drift
                # window, and the previous pass has already spent a step.
                TwoFactorSettings.objects.filter(user=self.user).update(totp_last_step=None)
                challenge_token = self.do_login().json()["challenge_token"]

                resp = self.post("/auth/two-factor/verify/", {
                    "challenge_token": challenge_token,
                    "code": self.app_code(self.secret, step_offset=offset),
                })

                self.assertEqual(resp.status_code, 200, resp.content)

    def test_two_steps_out_is_refused(self):
        """The window is one step either side (90 seconds total). A phone more than a minute out
        gets told to fix its clock rather than quietly widening the guessing surface."""
        for offset in (-2, 2):
            with self.subTest(offset=offset):
                SessionToken.objects.filter(user=self.user).delete()
                TwoFactorSettings.objects.filter(user=self.user).update(totp_last_step=None)
                challenge_token = self.do_login().json()["challenge_token"]

                resp = self.post("/auth/two-factor/verify/", {
                    "challenge_token": challenge_token,
                    "code": self.app_code(self.secret, step_offset=offset),
                })

                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    # ── REPLAY ───────────────────────────────────────────────────────────────────────────────
    def test_a_code_cannot_be_replayed_inside_its_own_window(self):
        """Without this the same six digits keep working for up to 90 seconds, which is plenty for
        anyone who watched them being typed."""
        code = self.app_code(self.secret)
        first = self.post("/auth/two-factor/verify/",
                          {"challenge_token": self.do_login().json()["challenge_token"],
                           "code": code})
        self.assertEqual(first.status_code, 200)
        SessionToken.objects.filter(user=self.user).delete()

        replay = self.post("/auth/two-factor/verify/",
                           {"challenge_token": self.do_login().json()["challenge_token"],
                            "code": code})

        self.assertEqual(replay.status_code, 400)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_spending_a_step_also_kills_the_older_code_still_in_the_window(self):
        """The previous step is inside the drift window, so it has to die with the step that was
        spent - otherwise the replay guard would only cover the exact code that was used."""
        self.post("/auth/two-factor/verify/",
                  {"challenge_token": self.do_login().json()["challenge_token"],
                   "code": self.app_code(self.secret)})
        SessionToken.objects.filter(user=self.user).delete()

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "code": self.app_code(self.secret, step_offset=-1)})

        self.assertEqual(resp.status_code, 400)

    def test_the_next_step_still_works_after_one_is_spent(self):
        """The other half of the replay rule: spending a step must not lock the user out of their
        own next code."""
        self.post("/auth/two-factor/verify/",
                  {"challenge_token": self.do_login().json()["challenge_token"],
                   "code": self.app_code(self.secret)})
        SessionToken.objects.filter(user=self.user).delete()

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "code": self.app_code(self.secret, step_offset=1)})

        self.assertEqual(resp.status_code, 200, resp.content)

    # ── THROTTLING ───────────────────────────────────────────────────────────────────────────
    def test_each_login_gets_a_fresh_challenge_with_no_cooldown(self):
        """The email path caps SENDS. Nothing is sent here, so a sixth sign-in in an hour must not
        be refused the way a sixth emailed code would be."""
        tokens = set()
        for _ in range(TwoFactorChallenge.MAX_SENDS_PER_HOUR + 3):
            body = self.do_login().json()
            self.assertTrue(body["two_factor_required"])
            self.assertTrue(body["challenge_token"])
            tokens.add(body["challenge_token"])

        self.assertEqual(len(tokens), TwoFactorChallenge.MAX_SENDS_PER_HOUR + 3)
        # And the last one still works, so nobody was locked out by a limit that means nothing here.
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": body["challenge_token"],
                          "code": self.app_code(self.secret)})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_guessing_is_capped_across_challenges(self):
        """A per-challenge cap is not a throttle when fresh challenges are free. This is what stops
        someone who already has the password from walking the six-digit space."""
        spent = 0
        while spent < two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR:
            token = self.do_login().json()["challenge_token"]
            for _ in range(TwoFactorChallenge.MAX_ATTEMPTS):
                if spent >= two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR:
                    break
                self.post("/auth/two-factor/verify/",
                          {"challenge_token": token, "code": "000000"})
                spent += 1

        # Budget spent: even the RIGHT code is now refused, on a brand new challenge.
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "code": self.app_code(self.secret)})

        self.assertEqual(resp.status_code, 429, resp.content)
        self.assertFalse(SessionToken.objects.filter(user=self.user).exists())

    def test_a_recovery_code_still_works_when_the_guessing_budget_is_spent(self):
        """Being throttled must not become a lockout for the real owner."""
        spent = 0
        while spent < two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR:
            token = self.do_login().json()["challenge_token"]
            for _ in range(TwoFactorChallenge.MAX_ATTEMPTS):
                if spent >= two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR:
                    break
                self.post("/auth/two-factor/verify/",
                          {"challenge_token": token, "code": "000000"})
                spent += 1

        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "backup_code": self.codes[0]})

        self.assertEqual(resp.status_code, 200, resp.content)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# RECOVERY CODES WITH THE AUTHENTICATOR (method-agnostic, and it has to stay that way)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpBackupCodeTests(TotpTestBase):

    def setUp(self):
        super().setUp()
        _session, self.secret, self.codes = self.enrol_totp()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        self.forget_spent_step()

    def test_a_recovery_code_signs_you_in_and_is_then_spent(self):
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": self.do_login().json()["challenge_token"],
                          "backup_code": self.codes[0]})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("session_token", resp.json())
        self.assertEqual(
            TwoFactorBackupCode.objects.filter(user=self.user, used_at__isnull=True).count(),
            two_factor.BACKUP_CODE_COUNT - 1)

    def test_recovery_codes_can_be_regenerated_with_an_app_code(self):
        session = self.sign_in_with_app(self.secret)
        # "send-code" for an authenticator account sends nothing; it just raises the challenge.
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)
        self.assertEqual(proof.status_code, 200, proof.content)
        self.assertFalse(proof.json()["code_sent"])
        self.assertEqual(proof.json()["method"], "totp")
        self.assertEqual(self.sent_codes, [])

        resp = self.post("/auth/two-factor/backup-codes/",
                         {"challenge_token": proof.json()["challenge_token"],
                          # A later step than the one the login just spent, per the replay rule.
                          "code": self.app_code(self.secret, step_offset=1)},
                         token=session)

        self.assertEqual(resp.status_code, 200, resp.content)
        new_codes = resp.json()["backup_codes"]
        self.assertEqual(len(new_codes), two_factor.BACKUP_CODE_COUNT)
        self.assertEqual(set(new_codes) & set(self.codes), set())


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SWITCHING METHODS AND TURNING IT OFF
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpSwitchAndDisableTests(TotpTestBase):

    def test_switching_from_email_needs_proof_of_the_email_factor(self):
        """Swapping the owner's factor for somebody else's is a takeover, so it costs the same as
        turning 2FA off."""
        session, _codes = self.enable_email_2fa()
        secret = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()["secret"]

        without_proof = self.post("/auth/two-factor/totp/confirm/",
                                  {"code": self.app_code(secret)}, token=session)

        self.assertEqual(without_proof.status_code, 400)
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).method, "email")

    def test_switching_from_email_keeps_the_existing_recovery_codes(self):
        """Minting a second parallel set would quietly invalidate the piece of paper in the user's
        drawer, which is the one thing they reach for when everything else has failed."""
        session, original_codes = self.enable_email_2fa()
        setup = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()
        self.assertEqual(setup["proof_purpose"], "disable")
        self.assertEqual(setup["proof_method"], "email")
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)

        resp = self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(setup["secret"]),
            "proof_challenge_token": proof.json()["challenge_token"],
            "proof_code": self.sent_codes[-1],
        }, token=session)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["method"], "totp")
        # No second set was minted...
        self.assertEqual(resp.json()["backup_codes"], [])
        self.assertEqual(
            TwoFactorBackupCode.objects.filter(user=self.user, used_at__isnull=True).count(),
            two_factor.BACKUP_CODE_COUNT)
        # ...and the codes the user saved BEFORE the switch still sign them in.
        SessionToken.objects.filter(user=self.user).delete()
        signed_in = self.post("/auth/two-factor/verify/",
                              {"challenge_token": self.do_login().json()["challenge_token"],
                               "backup_code": original_codes[0]})
        self.assertEqual(signed_in.status_code, 200, signed_in.content)

    def test_switching_keeps_the_original_enabled_at(self):
        session, _codes = self.enable_email_2fa()
        was_enabled_at = TwoFactorSettings.objects.get(user=self.user).enabled_at
        setup = self.post("/auth/two-factor/totp/setup/", {}, token=session).json()
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)

        self.post("/auth/two-factor/totp/confirm/", {
            "code": self.app_code(setup["secret"]),
            "proof_challenge_token": proof.json()["challenge_token"],
            "proof_code": self.sent_codes[-1],
        }, token=session)

        # The user changed HOW they get the code, not whether two-step sign-in is on.
        self.assertEqual(TwoFactorSettings.objects.get(user=self.user).enabled_at, was_enabled_at)

    def test_a_session_alone_cannot_turn_the_authenticator_off(self):
        _session, secret, _codes = self.enrol_totp()
        session = self.sign_in_with_app(secret)

        resp = self.post("/auth/two-factor/disable/", {}, token=session)

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(two_factor.is_enabled_for(self.user))

    def test_disable_with_an_app_code_wipes_the_secret(self):
        _session, secret, _codes = self.enrol_totp()
        session = self.sign_in_with_app(secret)
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)

        resp = self.post("/auth/two-factor/disable/",
                         {"challenge_token": proof.json()["challenge_token"],
                          "code": self.app_code(secret, step_offset=1)}, token=session)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["enabled"])
        row = TwoFactorSettings.objects.get(user=self.user)
        # An entry left in somebody's authenticator must not still open a re-enabled account.
        self.assertEqual(row.totp_secret, "")
        self.assertIsNone(row.totp_confirmed_at)
        self.assertEqual(TwoFactorBackupCode.objects.filter(user=self.user).count(), 0)
        # And sign-in is one step again.
        self.assertIn("session_token", self.do_login().json())

    def test_turning_it_back_on_by_email_works_after_an_authenticator(self):
        """The leftover-preference case: the row still says method "totp" after a disable, and
        asking for a code from an authenticator that no longer exists would refuse the request."""
        _session, secret, _codes = self.enrol_totp()
        session = self.sign_in_with_app(secret)
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)
        self.post("/auth/two-factor/disable/",
                  {"challenge_token": proof.json()["challenge_token"],
                   "code": self.app_code(secret, step_offset=1)}, token=session)
        self.sent_codes.clear()

        sent = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)

        self.assertEqual(sent.status_code, 200, sent.content)
        self.assertEqual(sent.json()["method"], "email")
        self.assertTrue(sent.json()["code_sent"])
        enabled = self.post("/auth/two-factor/enable/",
                            {"challenge_token": sent.json()["challenge_token"],
                             "code": self.sent_codes[-1]}, token=session)
        self.assertEqual(enabled.status_code, 200, enabled.content)
        self.assertEqual(enabled.json()["method"], "email")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# STORAGE AND THE LOGIC LAYER
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TotpStorageTests(TotpTestBase):

    def test_the_secret_is_not_sitting_in_the_database_in_plaintext(self):
        _session, secret, _codes = self.enrol_totp()

        row = TwoFactorSettings.objects.get(user=self.user)

        # What the user scanned is NOT what is stored...
        self.assertNotEqual(row.totp_secret, secret)
        self.assertNotIn(secret, row.totp_secret)
        # ...but it is recoverable, because a TOTP secret has to be. That is exactly why it is
        # encrypted rather than hashed.
        self.assertEqual(two_factor.decrypt_totp_secret(row.totp_secret), secret)

    def test_a_secret_that_cannot_be_decrypted_fails_soft(self):
        """A rotated Django secret key must not lock every authenticator user out permanently. It
        degrades to a one-step sign-in they can re-enrol from."""
        self.enrol_totp()
        TwoFactorSettings.objects.filter(user=self.user).update(totp_secret="not-a-fernet-token")

        self.assertEqual(two_factor.active_totp_secret(self.user), "")
        self.assertFalse(two_factor.is_enabled_for(self.user))
        self.assertIn("session_token", self.do_login().json())

    def test_encrypt_decrypt_round_trips_and_is_not_deterministic(self):
        secret = two_factor.pyotp.random_base32()

        sealed_once = two_factor.encrypt_totp_secret(secret)
        sealed_twice = two_factor.encrypt_totp_secret(secret)

        self.assertEqual(two_factor.decrypt_totp_secret(sealed_once), secret)
        self.assertEqual(two_factor.decrypt_totp_secret(sealed_twice), secret)
        # Fernet is randomised, so two rows holding the same secret do not look identical - a dump
        # cannot be scanned for accounts that share one.
        self.assertNotEqual(sealed_once, sealed_twice)
        self.assertEqual(two_factor.decrypt_totp_secret("garbage"), "")
        self.assertEqual(two_factor.decrypt_totp_secret(""), "")

    def test_an_unconfirmed_enrolment_never_satisfies_a_login(self):
        session = self.session_without_2fa()
        self.post("/auth/two-factor/totp/setup/", {}, token=session)
        # Force the flag on WITHOUT confirming, the state a bug would have to produce.
        TwoFactorSettings.objects.filter(user=self.user).update(is_enabled=True, method="totp")

        # is_available is False, so the account is not stranded behind a factor it never proved.
        self.assertEqual(two_factor.active_totp_secret(self.user), "")
        self.assertFalse(two_factor.is_enabled_for(self.user))

    def test_provisioning_uri_names_the_issuer_and_the_account(self):
        secret = two_factor.pyotp.random_base32()

        uri = two_factor.totp_provisioning_uri(self.user, secret)

        self.assertTrue(uri.startswith("otpauth://totp/AFC:player1?"))
        self.assertIn(f"secret={secret}", uri)
        self.assertIn("issuer=AFC", uri)

    def test_match_totp_step_enforces_drift_and_the_replay_floor(self):
        secret = two_factor.pyotp.random_base32()
        now_step = two_factor.current_totp_step()

        # In the window.
        self.assertEqual(
            two_factor.match_totp_step(secret, _reference_totp(secret)), now_step)
        self.assertEqual(
            two_factor.match_totp_step(secret, _reference_totp(secret, step_offset=-1)),
            now_step - 1)
        # Outside it.
        self.assertIsNone(
            two_factor.match_totp_step(secret, _reference_totp(secret, step_offset=2)))
        # Inside the window but at or below the floor: refused without being compared.
        self.assertIsNone(
            two_factor.match_totp_step(secret, _reference_totp(secret), after_step=now_step))
        # Junk input never reaches the maths.
        self.assertIsNone(two_factor.match_totp_step(secret, "12345"))
        self.assertIsNone(two_factor.match_totp_step(secret, "abcdef"))
        self.assertIsNone(two_factor.match_totp_step(secret, None))
        self.assertIsNone(two_factor.match_totp_step("", "123456"))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE REGRESSION. Everything above is new; this class is about what must NOT have moved.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class NothingElseChangedTests(TotpTestBase):
    """Two populations must not notice this feature exists at all: the ~6,790 accounts with no 2FA,
    and everyone already using the email method."""

    def test_a_user_without_2fa_logs_in_exactly_as_before(self):
        resp = self.do_login()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Exactly the keys login() has always returned - no more, no fewer.
        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo"})
        self.assertEqual(body["message"], "Login successful")
        self.assertNotIn("two_factor_required", body)
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=body["session_token"]).exists())
        # No settings row is created just by signing in, and no code is sent anywhere.
        self.assertFalse(TwoFactorSettings.objects.filter(user=self.user).exists())
        self.assertEqual(self.sent_codes, [])

    def test_an_email_2fa_user_is_completely_unaffected(self):
        session, codes = self.enable_email_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()

        body = self.do_login().json()

        # Still challenged, still by email, still with a masked destination and a live cooldown.
        self.assertTrue(body["two_factor_required"])
        self.assertEqual(body["method"], "email")
        self.assertEqual(body["destination"], "pl*****@gmail.com")
        self.assertTrue(body["code_sent"])
        self.assertEqual(len(self.sent_codes), 1)
        # And the emailed code still completes the sign-in.
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": body["challenge_token"], "code": self.sent_codes[-1]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(codes), two_factor.BACKUP_CODE_COUNT)

    def test_the_email_send_cooldown_still_bites(self):
        """The codeless short path must not have loosened the email rate limits."""
        self.enable_email_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()
        token = self.do_login().json()["challenge_token"]
        self.assertEqual(len(self.sent_codes), 1)

        resp = self.post("/auth/two-factor/resend/", {"challenge_token": token})

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["code_sent"])
        self.assertGreater(resp.json()["retry_after"], 0)
        self.assertEqual(len(self.sent_codes), 1)

    def test_email_2fa_is_not_throttled_by_the_codeless_attempt_budget(self):
        """MAX_CODELESS_ATTEMPTS_PER_HOUR is scoped to methods with no send ceiling. Widening it to
        email would be a behaviour change to a flow thousands of accounts already use: they would
        start being locked out of their own inbox codes by a limit that was written for a different
        method entirely.

        The failed guesses are banked on a spent challenge rather than driven through 20 real login
        rounds, because doing that collides with the email HOURLY SEND cap - a different limit, and
        the one thing this test is deliberately not about."""
        self.enable_email_2fa()
        SessionToken.objects.filter(user=self.user).delete()
        self.sent_codes.clear()

        # A burned challenge from earlier in the hour, carrying far more failed guesses than the
        # codeless budget allows. Already past MAX_ATTEMPTS, so it is not live and cannot itself be
        # answered; it exists only to make the hourly attempt count large.
        TwoFactorChallenge.objects.create(
            user=self.user, purpose="login", method="email",
            token="carries-old-failed-attempts", code_hash=make_password(None),
            attempts=two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR + 5,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.assertGreater(two_factor._attempts_last_hour(self.user),
                           two_factor.MAX_CODELESS_ATTEMPTS_PER_HOUR)

        # Well past the codeless budget, a correct emailed code still signs the user in.
        body = self.do_login().json()
        resp = self.post("/auth/two-factor/verify/",
                         {"challenge_token": body["challenge_token"], "code": self.sent_codes[-1]})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            SessionToken.objects.filter(user=self.user, token=resp.json()["session_token"]).exists())
