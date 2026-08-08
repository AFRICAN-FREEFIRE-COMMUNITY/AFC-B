"""
Tests for TRUSTED DEVICES, "remember this device" (owner 2026-08-08).

WHAT THIS FEATURE IS FOR, because it shapes what has to be tested: the owner's complaint about
two-factor authentication was that typing a code on every single sign-in is stressful. Trusting a
device removes the second step on ONE browser for 30 days. So the tests have to prove two opposite
things equally hard - that a remembered device really does skip the challenge, and that everything
which is NOT that remembered device still gets challenged, every time.

  GRANTING TRUST
    - Trust is only ever granted by asking. No tick, no row, and the response is unchanged.
    - Ticking it returns a device token, and the row stores a HASH, never the token.
    - A recovery-code sign-in can also ask to be remembered.
    - A device token cannot be minted by any other endpoint.

  SPENDING TRUST
    - The remembered device signs in with no challenge and gets a real session.
    - A device that was never remembered is still challenged.
    - Garbage, a half-token and an unknown selector are all refused.
    - EXPIRY: past the 30 day window the same token is refused.
    - CROSS-USER: account A's valid token does not skip account B's factor, even with B's password.

  REVOKING TRUST
    - The user can list their devices and revoke one, and it takes effect on the NEXT sign-in.
    - Revoking all clears the lot; revoking somebody else's id removes nothing.
    - Turning 2FA off revokes every device.
    - Changing the password revokes every device.
    - Resetting the password revokes every device.
    - An admin moving the account's email revokes every device.
    - SPENDING A RECOVERY CODE revokes every device, and does so at the chokepoint so all three
      places a code can be spent inherit it. If the revocation fails, the code is NOT spent.

  WHATSAPP IS NOT A SIGN-IN FACTOR
    - It is registered (recovery uses it by name) and it is NOT in ENABLED_METHODS, so it can never
      appear on the security page as something a user could switch on.

  SESSIONS (the neighbouring control on the same page, and NOT the same thing)
    - The session list marks the caller's own session and counts the others.
    - Signing out elsewhere ends the others and leaves the caller signed in.
    - It deliberately does NOT touch trusted devices.

  THE REGRESSION THAT MATTERS MOST
    - A user with NO two-factor authentication signs in exactly as before: one step, a session
      token, and not one byte of trusted-device machinery in the response.

Run: python manage.py test afc_auth.tests_trusted_devices
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth import trusted_devices, two_factor
from afc_auth.models import (
    Roles,
    SessionToken,
    TrustedDevice,
    TwoFactorSettings,
    User,
    UserRoles,
)

PASSWORD = "CorrectHorse!9"
OTHER_PASSWORD = "AnotherHorse!7"

# A recognisable user agent, so the label assertions are about real strings rather than a fixture
# invented to make them pass. This is Chrome on a Pixel.
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Mobile Safari/537.36")


class TrustedDeviceTestBase(TestCase):
    """A real 2FA user, email delivery captured instead of sent, and helpers that drive the ACTUAL
    endpoints rather than writing rows - so every test downstream exercises a genuinely enabled
    account, not a hand-built one. Same shape as tests_two_factor_totp.TotpTestBase."""

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

        # send_email is called by change_password / reset_password / the admin email move. Patched
        # so those tests exercise the revocation without going near SMTP.
        mail_patcher = patch("afc_auth.views.send_email", return_value=True)
        mail_patcher.start()
        self.addCleanup(mail_patcher.stop)

        self.user = User.objects.create(
            username="player1",
            email="player1@gmail.com",
            full_name="Player One",
            role="player",
            password=make_password(PASSWORD),
            is_active=True,
        )

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def post(self, path, body=None, token=None, ua=ANDROID_UA):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        if ua:
            headers["HTTP_USER_AGENT"] = ua
        return self.client.post(
            path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def get(self, path, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(path, **headers)

    def do_login(self, user=None, password=PASSWORD, device_token=None, ua=ANDROID_UA):
        body = {"ign_or_uid": (user or self.user).username, "password": password}
        if device_token:
            body["device_token"] = device_token
        return self.post("/auth/login/", body, ua=ua)

    def enable_email_2fa(self):
        """Turn 2FA on the way a real user does: sign in, send the proof code, enter it.
        Returns (session_token, backup_codes)."""
        session = self.do_login().json()["session_token"]
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=session)
        self.assertEqual(proof.status_code, 200, proof.content)
        enabled = self.post("/auth/two-factor/enable/", {
            "challenge_token": proof.json()["challenge_token"],
            "code": self.sent_codes[-1],
        }, token=session)
        self.assertEqual(enabled.status_code, 200, enabled.content)
        return session, enabled.json()["backup_codes"]

    def sign_in_and_remember(self, ua=ANDROID_UA):
        """A full two-step sign-in that ticks "remember this device".
        Returns (device_token, session_token)."""
        challenge = self.do_login(ua=ua).json()
        self.assertTrue(challenge.get("two_factor_required"), challenge)
        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "code": self.sent_codes[-1],
            "remember_device": True,
        }, ua=ua)
        self.assertEqual(verified.status_code, 200, verified.content)
        body = verified.json()
        return body["device_token"], body["session_token"]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# GRANTING TRUST
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class GrantingTrustTests(TrustedDeviceTestBase):

    def test_trust_is_never_granted_without_being_asked_for(self):
        """The default is OFF. A normal second step must leave no trusted device behind and must
        return the same body it returned yesterday."""
        self.enable_email_2fa()
        challenge = self.do_login().json()

        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "code": self.sent_codes[-1],
        })

        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertNotIn("device_token", verified.json())
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_asking_for_it_returns_a_token_and_creates_exactly_one_row(self):
        self.enable_email_2fa()

        device_token, session_token = self.sign_in_and_remember()

        self.assertTrue(device_token)
        self.assertTrue(session_token)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)

    def test_the_token_is_stored_hashed_not_in_plaintext(self):
        """The single most important storage assertion: a database dump must not be a pile of
        working device tokens. Only the SELECTOR (the public half) is in the row as itself."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()
        selector, _, verifier = device_token.partition(".")

        row = TrustedDevice.objects.get(user=self.user)

        self.assertEqual(row.selector, selector)
        self.assertNotIn(verifier, row.verifier_hash)
        self.assertNotEqual(row.verifier_hash, verifier)
        # Django's hasher format: algorithm$iterations$salt$hash. Anything else means it went in raw.
        self.assertIn("$", row.verifier_hash)

    def test_the_row_records_a_recognisable_label(self):
        """A device list nobody can read is a device list nobody can act on."""
        self.enable_email_2fa()
        self.sign_in_and_remember()

        self.assertEqual(TrustedDevice.objects.get(user=self.user).label, "Chrome on Android")

    def test_a_recovery_code_sign_in_can_also_be_remembered(self):
        """Somebody signing in with a recovery code is exactly the person who least wants to do
        this again next week, so the tick has to work on that path too."""
        _session, backup_codes = self.enable_email_2fa()
        challenge = self.do_login().json()

        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "backup_code": backup_codes[0],
            "remember_device": True,
        })

        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertIn("device_token", verified.json())

    def test_a_wrong_code_never_mints_a_device_token(self):
        """Trust costs a passed factor. Asking nicely while failing it must buy nothing."""
        self.enable_email_2fa()
        challenge = self.do_login().json()

        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "code": "000000",
            "remember_device": True,
        })

        self.assertEqual(verified.status_code, 400, verified.content)
        self.assertNotIn("device_token", verified.json())
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SPENDING TRUST
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class SpendingTrustTests(TrustedDeviceTestBase):

    def test_a_remembered_device_skips_the_challenge(self):
        """THE FEATURE. Same password, same endpoint, and this time a session comes straight back."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()

        again = self.do_login(device_token=device_token)

        self.assertEqual(again.status_code, 200, again.content)
        body = again.json()
        self.assertNotIn("two_factor_required", body)
        self.assertIn("session_token", body)
        # A real, usable session, not a stub: it validates like any other.
        self.assertTrue(SessionToken.objects.filter(token=body["session_token"]).exists())

    def test_a_device_that_was_never_remembered_is_still_challenged(self):
        """The other half of the feature, and the one that makes it a second factor at all."""
        self.enable_email_2fa()
        self.sign_in_and_remember()

        fresh_browser = self.do_login()

        self.assertTrue(fresh_browser.json().get("two_factor_required"), fresh_browser.content)
        self.assertNotIn("session_token", fresh_browser.json())

    def test_junk_tokens_are_refused(self):
        """Nothing shaped wrong, and nothing merely plausible, gets through."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()
        selector, _, verifier = device_token.partition(".")

        for label, bad in [
            ("empty", ""),
            ("no separator", "justonelongstringwithnodot"),
            ("selector only", selector),
            ("selector with empty verifier", f"{selector}."),
            ("right selector, wrong verifier", f"{selector}.{'x' * len(verifier)}"),
            ("unknown selector, real verifier", f"nosuchselector.{verifier}"),
            ("halves swapped", f"{verifier}.{selector}"),
        ]:
            with self.subTest(token=label):
                res = self.do_login(device_token=bad)
                self.assertTrue(res.json().get("two_factor_required"),
                                f"{label} was accepted: {res.content}")

    def test_trust_expires(self):
        """The 30 day window is real, and it is enforced at read time rather than by a sweep, so a
        cleanup job that never ran cannot quietly extend somebody's trust."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()

        # Age the row past the window. One second past, so this asserts the boundary and not a
        # comfortable margin around it.
        TrustedDevice.objects.filter(user=self.user).update(
            expires_at=timezone.now() - timedelta(seconds=1))

        res = self.do_login(device_token=device_token)

        self.assertTrue(res.json().get("two_factor_required"), res.content)

    def test_trust_lasts_the_advertised_window(self):
        """The complement of the test above: 29 days in, it still works. Without this, "it expires"
        would also pass if trust never worked at all."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()
        row = TrustedDevice.objects.get(user=self.user)

        self.assertEqual(
            (row.expires_at - row.created_at).days, TrustedDevice.TRUST_LIFETIME.days)

        TrustedDevice.objects.filter(user=self.user).update(
            expires_at=timezone.now() + timedelta(days=1))
        res = self.do_login(device_token=device_token)

        self.assertIn("session_token", res.json(), res.content)

    def test_another_users_device_token_does_not_skip_this_users_factor(self):
        """A STOLEN TOKEN BOUND TO SOMEONE ELSE. Player two hands over a perfectly valid token of
        their own while signing in as player one, with player one's real password. It must be worth
        nothing: trust is bound to the account it was granted to."""
        self.enable_email_2fa()

        other = User.objects.create(
            username="player2", email="player2@gmail.com", full_name="Player Two",
            role="player", password=make_password(OTHER_PASSWORD), is_active=True)
        # Give player two 2FA and a genuine trusted device of their own.
        other_session = self.post(
            "/auth/login/", {"ign_or_uid": other.username, "password": OTHER_PASSWORD}
        ).json()["session_token"]
        proof = self.post("/auth/two-factor/send-code/", {"purpose": "enable"}, token=other_session)
        self.post("/auth/two-factor/enable/", {
            "challenge_token": proof.json()["challenge_token"], "code": self.sent_codes[-1],
        }, token=other_session)
        other_challenge = self.post(
            "/auth/login/", {"ign_or_uid": other.username, "password": OTHER_PASSWORD}).json()
        other_device = self.post("/auth/two-factor/verify/", {
            "challenge_token": other_challenge["challenge_token"],
            "code": self.sent_codes[-1],
            "remember_device": True,
        }).json()["device_token"]

        # Player two's token really does work for player two, so the refusal below is about the
        # BINDING and not about the token being broken.
        self.assertIn("session_token", self.post(
            "/auth/login/", {"ign_or_uid": other.username, "password": OTHER_PASSWORD,
                             "device_token": other_device}).json())

        stolen = self.do_login(device_token=other_device)

        self.assertTrue(stolen.json().get("two_factor_required"), stolen.content)
        self.assertNotIn("session_token", stolen.json())

    def test_last_used_is_recorded(self):
        """So a user can tell a live device from one they stopped using months ago."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()
        # Backdate past TOUCH_THROTTLE so the slide is not suppressed as a duplicate write.
        old = timezone.now() - timedelta(days=2)
        TrustedDevice.objects.filter(user=self.user).update(last_used_at=old)

        self.do_login(device_token=device_token)

        self.assertGreater(TrustedDevice.objects.get(user=self.user).last_used_at, old)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# REVOKING TRUST
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class RevokingTrustTests(TrustedDeviceTestBase):

    def test_the_user_can_see_their_devices(self):
        self.enable_email_2fa()
        _device_token, session = self.sign_in_and_remember()

        listed = self.get("/auth/devices/trusted/", token=session)

        self.assertEqual(listed.status_code, 200, listed.content)
        body = listed.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["devices"][0]["label"], "Chrome on Android")
        self.assertEqual(body["trust_days"], TrustedDevice.TRUST_LIFETIME.days)

    def test_the_list_never_returns_a_token(self):
        """Not the verifier, not the selector, not a fragment. The id is all revoking needs."""
        self.enable_email_2fa()
        device_token, session = self.sign_in_and_remember()
        selector, _, verifier = device_token.partition(".")

        raw = self.get("/auth/devices/trusted/", token=session).content.decode()

        self.assertNotIn(verifier, raw)
        self.assertNotIn(selector, raw)

    def test_revoking_one_takes_effect_on_the_very_next_sign_in(self):
        """"Immediately" is the promise, so this asserts the behaviour and not just the row count."""
        self.enable_email_2fa()
        device_token, session = self.sign_in_and_remember()
        device_id = self.get("/auth/devices/trusted/", token=session).json()["devices"][0]["id"]

        revoked = self.post("/auth/devices/trusted/revoke/", {"device_id": device_id}, token=session)

        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertEqual(revoked.json()["revoked"], 1)
        self.assertTrue(self.do_login(device_token=device_token).json().get("two_factor_required"))

    def test_revoking_all_clears_every_device(self):
        self.enable_email_2fa()
        first_token, session = self.sign_in_and_remember()
        second_token, _ = self.sign_in_and_remember(
            ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Safari/605.1.15")
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 2)

        revoked = self.post("/auth/devices/trusted/revoke/", {"all": True}, token=session)

        self.assertEqual(revoked.json()["revoked"], 2)
        for token in (first_token, second_token):
            self.assertTrue(self.do_login(device_token=token).json().get("two_factor_required"))

    def test_you_cannot_revoke_somebody_elses_device(self):
        """The delete is scoped to the caller in the query, so guessing an id removes nothing."""
        self.enable_email_2fa()
        _victim_token, victim_session = self.sign_in_and_remember()
        victim_device_id = self.get(
            "/auth/devices/trusted/", token=victim_session).json()["devices"][0]["id"]

        attacker = User.objects.create(
            username="player2", email="player2@gmail.com", full_name="Player Two",
            role="player", password=make_password(OTHER_PASSWORD), is_active=True)
        attacker_session = self.post(
            "/auth/login/", {"ign_or_uid": attacker.username, "password": OTHER_PASSWORD}
        ).json()["session_token"]

        res = self.post("/auth/devices/trusted/revoke/",
                        {"device_id": victim_device_id}, token=attacker_session)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["revoked"], 0)
        self.assertTrue(TrustedDevice.objects.filter(id=victim_device_id).exists())

    def test_turning_two_factor_off_revokes_every_device(self):
        """Trust is permission to skip a factor. With the factor gone the permission is meaningless,
        and leaving it would mean re-enabling 2FA later silently honoured an old browser."""
        session, _codes = self.enable_email_2fa()
        self.sign_in_and_remember()
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)

        proof = self.post("/auth/two-factor/send-code/", {"purpose": "disable"}, token=session)
        off = self.post("/auth/two-factor/disable/", {
            "challenge_token": proof.json()["challenge_token"], "code": self.sent_codes[-1],
        }, token=session)

        self.assertEqual(off.status_code, 200, off.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_changing_the_password_revokes_every_device(self):
        """The usual reason to change a password is believing it leaked. If the devices survived,
        an attacker who had ticked the box would keep a standing pass around the second factor."""
        session, _codes = self.enable_email_2fa()
        self.sign_in_and_remember()

        changed = self.post("/auth/change-password/", {
            "old_password": PASSWORD, "new_password": "BrandNewHorse!3",
        }, token=session)

        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_resetting_the_password_revokes_every_device(self):
        """Same rule, and it matters more: a reset is what somebody who has actually lost control
        of their account reaches for."""
        from afc_auth.models import PasswordResetToken

        self.enable_email_2fa()
        self.sign_in_and_remember()
        PasswordResetToken.objects.create(user=self.user, token="123456")

        reset = self.post("/auth/reset-password/", {
            "email": self.user.email, "token": "123456", "new_password": "BrandNewHorse!3",
        })

        self.assertEqual(reset.status_code, 200, reset.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_an_admin_moving_the_email_revokes_every_device(self):
        """That endpoint exists to RESCUE a stolen account. It already ends every session and takes
        2FA down; leaving trusted devices behind would make the rescue tool the thing that kept the
        attacker's way in open."""
        self.enable_email_2fa()
        self.sign_in_and_remember()

        admin = User.objects.create(
            username="boss", email="boss@afc.com", full_name="Boss", role="admin",
            password=make_password(OTHER_PASSWORD), is_active=True)
        # `role_name`, not `name` - and Roles.description is non-null, so the default is required.
        # Same fixture shape as tests_admin_identity.py.
        role, _ = Roles.objects.get_or_create(
            role_name="head_admin", defaults={"description": "head_admin"})
        UserRoles.objects.create(user=admin, role=role)
        admin_session = self.post(
            "/auth/login/", {"ign_or_uid": admin.username, "password": OTHER_PASSWORD}
        ).json()["session_token"]

        moved = self.post("/auth/admin/set-user-email/", {
            "user_id": self.user.user_id,
            "new_email": "rescued@gmail.com",
            "reason": "Player lost access to the old inbox and opened a ticket.",
            "disable_two_factor": True,
        }, token=admin_session)

        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SPENDING A RECOVERY CODE REVOKES EVERYTHING (owner 2026-08-08)
#
# The sharpest of the revocation rules, and the one worth its own class. Somebody typing a recovery
# code has lost their normal factor: the inbox is gone, the phone is gone, or the account is not in
# their hands any more. Every browser holding permission to skip the second step is suspect at that
# moment, so all of them go. The rule lives at the chokepoint (two_factor.consume_backup_code), so
# the login second step, the disable flow and the switch-to-authenticator flow all inherit it.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class RecoveryCodeRevokesTrustTests(TrustedDeviceTestBase):

    def test_signing_in_with_a_recovery_code_forgets_every_remembered_device(self):
        """THE RULE. Two remembered browsers, a recovery code typed on a third, and both of the
        first two are asked for a code again."""
        _session, backup_codes = self.enable_email_2fa()
        phone_token, _ = self.sign_in_and_remember()
        laptop_token, _ = self.sign_in_and_remember(
            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Safari/537.36")
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 2)

        challenge = self.do_login().json()
        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "backup_code": backup_codes[0],
        })

        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)
        # Not just the row count: the tokens themselves are worthless now.
        for token in (phone_token, laptop_token):
            self.assertTrue(self.do_login(device_token=token).json().get("two_factor_required"))

    def test_the_browser_using_the_recovery_code_can_still_be_remembered(self):
        """Revoke-then-mint, in that order. Somebody who just used a recovery code is exactly the
        person who least wants to be back here next week, so ticking the box on THAT screen leaves
        them with exactly one remembered device: the browser they are sitting at."""
        _session, backup_codes = self.enable_email_2fa()
        old_token, _ = self.sign_in_and_remember()

        challenge = self.do_login().json()
        verified = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "backup_code": backup_codes[0],
            "remember_device": True,
        })

        new_token = verified.json()["device_token"]
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)
        self.assertTrue(self.do_login(device_token=old_token).json().get("two_factor_required"))
        self.assertIn("session_token", self.do_login(device_token=new_token).json())

    def test_a_wrong_recovery_code_forgets_nothing(self):
        """Revocation is a consequence of SPENDING a code, not of typing one. Otherwise anyone who
        knew a username could clear somebody's remembered devices by guessing at the recovery box."""
        self.enable_email_2fa()
        device_token, _ = self.sign_in_and_remember()

        challenge = self.do_login().json()
        refused = self.post("/auth/two-factor/verify/", {
            "challenge_token": challenge["challenge_token"],
            "backup_code": "ZZZZZ-ZZZZZ",
        })

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)
        self.assertIn("session_token", self.do_login(device_token=device_token).json())

    def test_the_rule_lives_at_the_chokepoint_so_every_caller_inherits_it(self):
        """consume_backup_code is called from three places (the login second step, disabling 2FA,
        and switching to an authenticator app). Asserting the revocation on the FUNCTION rather than
        on one endpoint is what makes the other two, and any fourth added later, correct by
        construction."""
        _session, backup_codes = self.enable_email_2fa()
        self.sign_in_and_remember()

        spent = two_factor.consume_backup_code(self.user, backup_codes[0])

        self.assertTrue(spent)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_a_failed_revocation_does_not_spend_the_code(self):
        """FAILS CLOSED, and this is the one trusted-device operation allowed to fail a request.
        The revocation shares a transaction with marking the code used, so a database error rolls
        both back: no session is handed out AND the code survives, so the user can try the same one
        again rather than losing it to an outage."""
        _session, backup_codes = self.enable_email_2fa()
        self.sign_in_and_remember()

        with patch("afc_auth.trusted_devices.revoke_all", side_effect=Exception("db down")):
            with self.assertRaises(Exception):
                two_factor.consume_backup_code(self.user, backup_codes[0])

        # Still unused, and still the device we had: neither half of the transaction landed.
        self.assertEqual(two_factor.backup_codes_remaining(self.user), 10)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)
        # And it works normally once the database is healthy again.
        self.assertTrue(two_factor.consume_backup_code(self.user, backup_codes[0]))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# WHATSAPP IS NOT A SIGN-IN FACTOR (owner decision, 2026-08-08)
#
# WhatsApp sign-in was proposed and TURNED DOWN. The implementation still exists, because account
# RECOVERY (afc_auth/views_recovery.py) sends its forgot-password code through it by name. So the
# method is REGISTERED and deliberately held out of ENABLED_METHODS, and these tests pin that gap
# open: a well-meaning edit that adds "whatsapp" to the offerable list fails here instead of quietly
# shipping a second factor 98% of accounts cannot use, on the same channel as the way back in.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class WhatsAppIsNotASignInFactorTests(TrustedDeviceTestBase):

    def test_whatsapp_is_registered_but_not_enabled(self):
        # Registered, because recovery asks for it by name.
        self.assertIn("whatsapp", two_factor.METHODS)
        self.assertEqual(two_factor.get_method("whatsapp").code, "whatsapp")
        # Not offerable. Pinned as an exact tuple rather than a "not in": that way ADDING a method
        # is a deliberate edit to this line, not something that slips past on a partial assertion.
        self.assertNotIn("whatsapp", two_factor.ENABLED_METHODS)
        self.assertEqual(two_factor.ENABLED_METHODS, ("email", "totp"))

    def test_the_security_page_is_never_offered_whatsapp(self):
        """available_methods is what the security page renders one card per. There is deliberately
        no endpoint that takes a method NAME from the client (enabling always means email, and the
        only other way in is the authenticator enrolment pair), so this response is the whole
        attack surface for "can a user pick WhatsApp", and it never contains it."""
        session, _codes = self.enable_email_2fa()

        status_body = self.get("/auth/two-factor/status/", token=session).json()

        self.assertNotIn("whatsapp", status_body["available_methods"])
        self.assertEqual(status_body["available_methods"], ["email", "totp"])
        self.assertEqual(status_body["method"], "email")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SESSIONS - the neighbouring control, and deliberately NOT the same thing
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class SessionControlTests(TrustedDeviceTestBase):

    def test_the_list_marks_the_callers_own_session(self):
        """So the page can say "this device" and never invite somebody to sign themselves out."""
        first = self.do_login().json()["session_token"]
        self.do_login()  # a second browser

        listed = self.get("/auth/devices/sessions/", token=first)

        self.assertEqual(listed.status_code, 200, listed.content)
        body = listed.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["others"], 1)
        self.assertEqual(sum(1 for s in body["sessions"] if s["current"]), 1)

    def test_signing_out_elsewhere_leaves_the_caller_signed_in(self):
        """The property that makes this safe as a single tap: nobody can lock themselves out."""
        first = self.do_login().json()["session_token"]
        second = self.do_login().json()["session_token"]

        res = self.post("/auth/devices/sessions/sign-out-others/", {}, token=first)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["signed_out"], 1)
        self.assertTrue(SessionToken.objects.filter(token=first).exists())
        self.assertFalse(SessionToken.objects.filter(token=second).exists())

    def test_signing_out_elsewhere_does_not_forget_the_device(self):
        """Two different questions, two different controls. A user may well want their own phone
        signed out and still remembered, and quietly doing both would be a surprise."""
        self.enable_email_2fa()
        _device_token, session = self.sign_in_and_remember()

        self.post("/auth/devices/sessions/sign-out-others/", {}, token=session)

        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 1)

    def test_signing_out_twice_is_harmless(self):
        first = self.do_login().json()["session_token"]
        self.do_login()
        self.post("/auth/devices/sessions/sign-out-others/", {}, token=first)

        again = self.post("/auth/devices/sessions/sign-out-others/", {}, token=first)

        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["signed_out"], 0)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE REGRESSION THAT MATTERS MOST
#
# ~6,790 accounts have no two-factor authentication at all. Everything above must be invisible to
# every one of them.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class NoTwoFactorIsUntouchedTests(TrustedDeviceTestBase):

    def test_a_user_without_two_factor_signs_in_in_one_step(self):
        res = self.do_login()

        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertIn("session_token", body)
        self.assertNotIn("two_factor_required", body)
        self.assertNotIn("device_token", body)

    def test_the_login_body_has_exactly_the_keys_it_always_had(self):
        """Not "contains a session token" but "is the same shape", because an added key is a change
        to a response 6,790 accounts and every client depend on."""
        body = self.do_login().json()

        self.assertEqual(set(body.keys()), {"message", "session_token", "user", "geo"})
        self.assertEqual(set(body["user"].keys()), {"id", "username", "language"})

    def test_a_stray_device_token_changes_nothing_for_them(self):
        """A leftover cookie from an account that later switched 2FA off must not confuse the gate."""
        res = self.do_login(device_token="anything.at.all")

        self.assertIn("session_token", res.json(), res.content)
        self.assertNotIn("two_factor_required", res.json())

    def test_no_trusted_device_row_is_ever_created_for_them(self):
        self.do_login()
        self.do_login()

        self.assertEqual(TrustedDevice.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The pure helpers, tested directly. Cheap, and they are what the list a user reads is built from.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class DeviceLabelTests(TestCase):

    def test_labels_the_common_browsers_and_platforms(self):
        cases = [
            (ANDROID_UA, "Chrome on Android"),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1", "Safari on iPhone"),
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "Edge on Windows"),
            ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/121.0",
             "Firefox on Mac"),
            ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 SamsungBrowser/23.0 "
             "Chrome/115.0.0.0 Mobile Safari/537.36", "Samsung Internet on Android"),
            ("", "Unknown device"),
            ("some-random-http-client/1.0", "Unknown device"),
        ]
        for ua, expected in cases:
            with self.subTest(ua=ua[:40]):
                self.assertEqual(trusted_devices.device_label(ua), expected)

    def test_a_chromium_browser_is_not_mislabelled_as_chrome_or_safari(self):
        """Every Chromium browser also claims "Chrome", and Chrome itself also claims "Safari", so
        the order the table is walked in is load-bearing rather than cosmetic."""
        opera = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0")

        self.assertEqual(trusted_devices.device_label(opera), "Opera on Windows")


class IsTrustedUnitTests(TrustedDeviceTestBase):
    """is_trusted() called directly, for the branches the HTTP tests cannot reach cleanly."""

    def test_no_user_and_no_token_are_both_refused(self):
        self.assertFalse(trusted_devices.is_trusted(self.user, None))
        self.assertFalse(trusted_devices.is_trusted(self.user, ""))
        self.assertFalse(trusted_devices.is_trusted(None, "sel.ver"))

    def test_a_database_failure_fails_closed(self):
        """Migrations are generated on the server in this repo, so code can land before the table
        exists. That must mean "everybody is challenged" (today's behaviour), never "2FA is off"."""
        with patch("afc_auth.trusted_devices.TrustedDevice.objects") as broken:
            broken.filter.side_effect = Exception("no such table")

            self.assertFalse(trusted_devices.is_trusted(self.user, "selector.verifier"))
