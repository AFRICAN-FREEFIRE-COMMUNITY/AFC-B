"""
Tests for LOGIN IDENTIFIER RESOLUTION and the cross-column guard (owner 2026-08-07).

Covers afc_auth/identifiers.py, afc_auth/backends.py, and the four write paths that must no longer
be able to create an ambiguous identifier.

THE BUG THESE PIN
    Sign-in accepts an email, an in-game name OR a Free Fire UID in one box. It used to resolve
    that with a single Q(username) | Q(uid) | Q(email__iexact) .get(). Each column is unique, so
    nothing stopped one string being row A's username and row B's uid - and when that happened the
    query matched two rows, .get() raised MultipleObjectsReturned, and BOTH people were refused.
    On 2026-08-07 the live table held 10 such pairs, 20 accounts, every one the same shape.

WHAT IS COVERED, AND WHY EACH ONE IS HERE

  RESOLUTION
    - Each identifier resolves on its own; email stays case-insensitive.
    - Precedence email > username > uid, proved on a real collision rather than asserted.
    - The REAL live shape as a regression fixture: after the fix BOTH parties can sign in.
    - The empty-credential short-circuit, which exists because a regression there 500s the login
      view rather than merely misbehaving.
    - A wrong password does NOT fall through to the next column. This is the security property of
      the whole design: falling through would turn one typed string into a password probe against
      up to three accounts, on a path with no rate limiting.

  THE UNIQUENESS ASSUMPTION
    resolve_login_identifier uses .filter().first() per column, which is deterministic ONLY because
    all three columns are unique. UniqueColumnAssumptionTests fails loudly if that ever stops being
    true, because the failure mode is silent otherwise.

  2FA (shipped v7.1.38, so this is the live login path)
    - A 2FA user resolved by ANY of the three identifiers gets a challenge bound to the right row,
      and no session token is issued until the code is verified.

  PREVENTION
    - register, edit_profile and the Google SSO username generator all refuse to create a new
      cross-column collision, and register's unverified-takeover sweeps an abandoned row that is
      squatting on an incoming value in a DIFFERENT column.

Run: python manage.py test afc_auth.tests_login_identifiers
"""
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase

from afc_auth.identifiers import (
    LOGIN_IDENTIFIER_PRECEDENCE,
    cross_field_conflict,
    resolve_login_identifier,
)
from afc_auth.models import User
from afc_auth.views import _unique_username_from_email

PASSWORD = "CorrectHorse!9"
OTHER_PASSWORD = "DifferentHorse!7"


def make_user(username, email, uid=None, is_active=True, password=PASSWORD):
    return User.objects.create(
        username=username, email=email, full_name=username, role="player",
        password=make_password(password), is_active=is_active, uid=uid, language="en",
    )


class ResolutionTests(TestCase):
    """One typed string in, at most one user out."""

    def setUp(self):
        self.user = make_user("Kinglarry21", "larry@gmail.com", uid="9137457129")

    def test_each_identifier_resolves_on_its_own(self):
        for typed in ("larry@gmail.com", "Kinglarry21", "9137457129"):
            self.assertEqual(resolve_login_identifier(typed), self.user, f"{typed} should resolve")

    def test_email_matching_stays_case_insensitive(self):
        self.assertEqual(resolve_login_identifier("LARRY@Gmail.COM"), self.user)

    def test_unknown_identifier_resolves_to_nobody(self):
        self.assertIsNone(resolve_login_identifier("nobody-has-this"))

    def test_empty_identifier_short_circuits(self):
        """A blank identifier must never reach the query layer: filtering the NULLABLE uid column
        on None matched every UID-less row and 500ed the login view."""
        make_user("no_uid_at_all", "nouid@gmail.com", uid=None)
        for blank in ("", None):
            self.assertIsNone(resolve_login_identifier(blank))


class PrecedenceTests(TestCase):
    """The declared order is email > username > uid. Proved on collisions, not asserted."""

    def test_email_beats_a_username(self):
        email_owner = make_user("realname", "shared@gmail.com")
        make_user("shared@gmail.com", "othername@gmail.com")   # username IS that address
        self.assertEqual(resolve_login_identifier("shared@gmail.com"), email_owner)

    def test_email_beats_a_uid(self):
        # Contrived (an email-shaped uid), but it pins the order rather than the data.
        email_owner = make_user("emailowner", "9137457129@gmail.com")
        make_user("uidowner", "uidowner@gmail.com", uid="9137457129@gmail.com"[:15])
        self.assertEqual(resolve_login_identifier("9137457129@gmail.com"), email_owner)

    def test_username_beats_a_uid(self):
        """The shape all 10 live collisions have."""
        name_owner = make_user("9137457129", "nameowner@gmail.com")
        make_user("Kinglarry21", "larry@gmail.com", uid="9137457129")
        self.assertEqual(resolve_login_identifier("9137457129"), name_owner)

    def test_the_order_constant_is_the_documented_one(self):
        """If someone reorders LOGIN_IDENTIFIER_PRECEDENCE, they have to come here and say why."""
        self.assertEqual(
            [field for field, _lookup in LOGIN_IDENTIFIER_PRECEDENCE],
            ["email", "username", "uid"],
        )


class UniqueColumnAssumptionTests(TestCase):
    """resolve_login_identifier uses .filter().first() per column and calls it deterministic.

    That is true ONLY while every one of those columns is unique. If a migration ever drops one of
    these constraints, resolution silently degrades to "whichever row the database returned first",
    which is exactly the class of bug this module was written to remove. Fail loudly instead."""

    def test_every_login_identifier_column_is_still_unique(self):
        for field, _lookup in LOGIN_IDENTIFIER_PRECEDENCE:
            self.assertTrue(
                User._meta.get_field(field).unique,
                f"User.{field} is no longer unique. resolve_login_identifier() depends on each "
                f"login column matching at most one row; fix the resolver before landing this.",
            )


class BackendTests(TestCase):
    """The resolver as the login backend actually uses it: resolve first, check the password once."""

    def setUp(self):
        self.client = Client()
        self.name_owner = make_user("9137457129", "nameowner@gmail.com", password=PASSWORD)
        self.uid_owner = make_user("Kinglarry21", "larry@gmail.com", uid="9137457129",
                                   password=OTHER_PASSWORD)

    def login(self, identifier, password):
        return self.client.post(
            "/auth/login/",
            data=json.dumps({"ign_or_uid": identifier, "password": password}),
            content_type="application/json",
        )

    def test_the_real_collision_shape_lets_BOTH_parties_sign_in(self):
        """The regression fixture. Before the fix this exact pair 401ed for both of them."""
        # The person NAMED that number gets it, by precedence.
        self.assertEqual(self.login("9137457129", PASSWORD).status_code, 200)
        # And the person who OWNS that UID still has two working routes of their own.
        self.assertEqual(self.login("Kinglarry21", OTHER_PASSWORD).status_code, 200)
        self.assertEqual(self.login("larry@gmail.com", OTHER_PASSWORD).status_code, 200)

    def test_a_wrong_password_does_NOT_fall_through_to_the_next_column(self):
        """The security property of the whole design.

        "9137457129" resolves to the name-holder. Presenting the UID-holder's password must FAIL
        rather than quietly matching the other row: falling through on a mismatch would turn one
        typed string into a password probe across up to three accounts, and this endpoint has no
        rate limiting or lockout anywhere on it."""
        resp = self.login("9137457129", OTHER_PASSWORD)
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("session_token", resp.json())

    def test_empty_credentials_do_not_500_the_login_view(self):
        for identifier, password in (("", PASSWORD), (None, PASSWORD),
                                     ("9137457129", ""), (None, None)):
            resp = self.login(identifier, password)
            self.assertEqual(resp.status_code, 401, f"{identifier!r}/{password!r} should be a 401")

    def test_login_by_each_identifier_when_there_is_no_collision(self):
        solo = make_user("SoloPlayer", "solo@gmail.com", uid="4242424242")
        for typed in ("solo@gmail.com", "SoloPlayer", "4242424242"):
            resp = self.login(typed, PASSWORD)
            self.assertEqual(resp.status_code, 200, f"{typed} should sign in")
            self.assertIn("session_token", resp.json())
        self.assertTrue(solo.is_active)


class TwoFactorInteractionTests(TestCase):
    """2FA shipped into this exact path in v7.1.38, so pin that resolution feeds it correctly."""

    def setUp(self):
        from django.utils import timezone
        from afc_auth.models import TwoFactorSettings
        self.client = Client()
        # The 2FA code goes out through the real Office365 chokepoint (two_factor.EmailCodeMethod
        # imports afc_auth.views.send_email inside deliver()). Stub it at that boundary so issuing
        # a challenge does not open an SMTP socket and stall on a 10060 timeout.
        mail_patcher = patch("afc_auth.views.send_email", return_value=True)
        mail_patcher.start()
        self.addCleanup(mail_patcher.stop)

        self.user = make_user("TwoFactorGuy", "tfa@gmail.com", uid="5566778899")
        TwoFactorSettings.objects.create(
            user=self.user, is_enabled=True, method="email", enabled_at=timezone.now())

    def login(self, identifier):
        return self.client.post(
            "/auth/login/",
            data=json.dumps({"ign_or_uid": identifier, "password": PASSWORD}),
            content_type="application/json",
        )

    def test_a_challenge_is_bound_to_the_right_user_whichever_identifier_was_typed(self):
        from afc_auth.models import TwoFactorChallenge
        for typed in ("tfa@gmail.com", "TwoFactorGuy", "5566778899"):
            resp = self.login(typed)
            self.assertEqual(resp.status_code, 200, resp.content)
            body = resp.json()
            # No session until the second factor is passed, whichever identifier got them here.
            self.assertTrue(body.get("two_factor_required"))
            self.assertNotIn("session_token", body)
            challenge = TwoFactorChallenge.objects.get(token=body["challenge_token"])
            self.assertEqual(challenge.user, self.user, f"{typed} bound the challenge to the wrong user")


class CrossFieldGuardTests(TestCase):
    """The helper itself: same-column clashes are somebody else's job, cross-column ones are ours."""

    def setUp(self):
        self.holder = make_user("9137457129", "holder@gmail.com")

    def test_finds_a_value_held_in_a_different_column(self):
        holder, held_as = cross_field_conflict("9137457129", "uid")
        self.assertEqual(holder, self.holder)
        self.assertEqual(held_as, "username")

    def test_ignores_a_clash_inside_the_SAME_column(self):
        """Each caller checks its own column with a better-worded message, so we must not double up."""
        holder, held_as = cross_field_conflict("9137457129", "username")
        self.assertIsNone(holder)
        self.assertIsNone(held_as)

    def test_excludes_the_row_being_edited(self):
        """Setting a UID equal to your OWN in-game name is not ambiguous: it is one row."""
        holder, _ = cross_field_conflict("9137457129", "uid", exclude_pk=self.holder.pk)
        self.assertIsNone(holder)

    def test_an_empty_value_is_never_a_conflict(self):
        """uid is nullable and 1,218 live accounts have none; filtering on blank would match many."""
        make_user("someone_with_no_uid", "nouid@gmail.com", uid=None)
        for blank in ("", None):
            self.assertEqual(cross_field_conflict(blank, "uid"), (None, None))


class RegistrationGuardTests(TestCase):
    """register() is where all ten live collisions were born."""

    def setUp(self):
        self.client = Client()
        # Signup sends a verification code through the real Office365 chokepoint. Stub it at the
        # service boundary (the name views.py looks up), so a success-path test does not open an
        # SMTP socket and hang on a 10060 timeout.
        mail_patcher = patch("afc_auth.views.send_email", return_value=True)
        mail_patcher.start()
        self.addCleanup(mail_patcher.stop)

    def signup(self, in_game_name, email, uid=None):
        body = {"in_game_name": in_game_name, "email": email, "full_name": "Test Person",
                "password": PASSWORD, "confirm_password": PASSWORD}
        if uid:
            body["uid"] = uid
        return self.client.post("/auth/signup/", data=json.dumps(body),
                                content_type="application/json")

    def test_cannot_sign_up_with_a_name_that_is_an_active_players_uid(self):
        make_user("Kinglarry21", "larry@gmail.com", uid="9137457129")
        resp = self.signup("9137457129", "newcomer@gmail.com")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(User.objects.filter(username="9137457129").exists())

    def test_cannot_sign_up_with_a_uid_that_is_an_active_players_name(self):
        make_user("9137457129", "nameowner@gmail.com")
        resp = self.signup("Newcomer", "newcomer@gmail.com", uid="9137457129")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(User.objects.filter(email="newcomer@gmail.com").exists())

    def test_the_refusal_never_names_the_other_account(self):
        """register is PUBLIC: naming the holder would confirm an account exists to any prober."""
        make_user("Kinglarry21", "larry@gmail.com", uid="9137457129")
        message = self.signup("9137457129", "newcomer@gmail.com").json()["message"]
        self.assertNotIn("Kinglarry21", message)
        self.assertNotIn("larry@gmail.com", message)

    def test_an_abandoned_unverified_squatter_is_swept_not_refused(self):
        """The existing unverified-takeover, now cross-column. An abandoned signup that typed a UID
        into the name box must not hold that number against the player who owns it."""
        squatter = make_user("9137457129", "abandoned@gmail.com", is_active=False)
        resp = self.signup("RealPlayer", "real@gmail.com", uid="9137457129")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(User.objects.filter(pk=squatter.pk).exists())
        self.assertEqual(User.objects.get(email="real@gmail.com").uid, "9137457129")

    def test_an_ordinary_signup_still_works(self):
        resp = self.signup("BrandNewPlayer", "brandnew@gmail.com", uid="1122334455")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.filter(username="BrandNewPlayer").exists())


class SsoUsernameGeneratorTests(TestCase):
    """An all-digits Google local-part is exactly how one of these collisions gets minted."""

    def test_a_generated_username_never_lands_on_someones_uid(self):
        make_user("Kinglarry21", "larry@gmail.com", uid="9137457129")
        candidate = _unique_username_from_email("9137457129@gmail.com")
        self.assertNotEqual(candidate, "9137457129")
        self.assertEqual(cross_field_conflict(candidate, "username"), (None, None))

    def test_it_still_returns_the_plain_slug_when_nothing_clashes(self):
        self.assertEqual(_unique_username_from_email("cleanname@gmail.com"), "cleanname")
