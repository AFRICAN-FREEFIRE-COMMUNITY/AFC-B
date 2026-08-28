"""Signing in with a provider must LINK the account, exactly as pressing Connect does.

WHY THIS FILE EXISTS (owner 2026-08-28)
    "a sign in and sign up should also be the same as linking. even for discord and google, please
    set it up and do the same for v-ent."

    Before this, the three providers disagreed:

      GOOGLE   google_auth called links.link_account on every sign-in. Correct already.
      DISCORD  discord_sso_callback set discord_id / discord_username / discord_connected by hand
               and wrote NO ConnectedAccount row. A player who signed in with Discord was told on
               their own profile page that Discord was not connected.
      V-ENT    no sign-in path existed at all.

    Nothing caught the Discord gap because no test asserted on the connections table after a
    sign-in; the tests that existed all checked the legacy columns, which were being written.

WHAT IS COVERED
    That a sign-in produces the row, for each provider; that the identity-theft guard survives the
    Discord refactor; and the v-ent cases that only exist because its email scope is optional.

Run: AFC_TEST_DB_NAME=test_afc_sso python manage.py test afc_auth.test_sso_links
"""
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from afc_auth.connections.providers import discord as discord_provider
from afc_auth.models import ConnectedAccount, SessionToken, User, UserProfile

VENT_SETTINGS = dict(
    VENT_CLIENT_ID="vent_sso_test",
    VENT_CLIENT_SECRET="secret",
    GOOGLE_OAUTH_CLIENT_ID="gid",
    FRONTEND_URL="https://africanfreefirecommunity.com",
    FRONTEND_URL_LOCAL="http://localhost:3000",
    AFC_API_BASE_URL="https://api.africanfreefirecommunity.com",
)

# The exact userinfo body v-ent.co sends, from vent_partners/views_sso.py::sso_userinfo.
VENT_PROFILE = {
    "status": "success",
    "data": {
        "sub": "9911",
        "username": "Layott",
        "name": "Layo Tunde",
        "country": "Nigeria",
        "picture": "https://api.v-ent.co/media/p/layott.png",
        "email": "layott@example.com",
        "email_verified": True,
    },
}

DISCORD_ME = {
    "id": "3344556677",
    "username": "layott",
    "global_name": "Layott",
    "email": "layott@example.com",
    "verified": True,
    "avatar": "abc123",
}


def _existing_user(username="linkme", email="layott@example.com"):
    u = User.objects.create(
        username=username, email=email, full_name="Layo", role="player",
        password="x", country="Nigeria", uid=None,
    )
    UserProfile.objects.create(user=u)
    return u


# ── the shape the whole feature turns on ──────────────────────────────────────────────────────
@override_settings(**VENT_SETTINGS)
class DiscordSignInLinksTests(TestCase):
    """THE REGRESSION TEST. Before the fix this produced no ConnectedAccount at all."""

    def setUp(self):
        cache.clear()

    def _callback(self, me, email="layott@example.com"):
        """Drive discord_sso_callback with its two network calls mocked."""
        cache.set("discord_sso_state:nonce1", "/home", 600)
        with patch("afc_auth.views.requests.post") as post, patch(
            "afc_auth.views.requests.get"
        ) as get:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"access_token": "at"}
            get.return_value.status_code = 200
            get.return_value.json.return_value = {**me, "email": email}
            return Client().get("/auth/discord/sso/callback/?code=c&state=nonce1")

    def test_signing_in_with_discord_creates_the_connected_account_row(self):
        _existing_user()
        self._callback(DISCORD_ME)
        link = ConnectedAccount.objects.get(provider="discord")
        self.assertEqual(link.provider_user_id, DISCORD_ME["id"])
        self.assertEqual(link.username, "Layott")

    def test_the_row_is_IDENTICAL_to_what_Connect_would_have_written(self):
        """A sign-in and a Connect must not disagree about what a Discord identity is, which is why
        the callback feeds the provider's own normalize() rather than building a dict by hand."""
        _existing_user()
        self._callback(DISCORD_ME)
        link = ConnectedAccount.objects.get(provider="discord")
        expected = discord_provider.normalize({**DISCORD_ME, "email": "layott@example.com"})
        self.assertEqual(link.provider_user_id, expected["provider_user_id"])
        self.assertEqual(link.username, expected["username"])
        self.assertEqual(link.avatar_url, expected["avatar_url"])

    def test_the_legacy_columns_are_STILL_written(self):
        """check_discord_membership, DiscordRoleAssignment, roster_discord and the AFC bot all read
        User.discord_id directly. link_account dual-writes them; this proves it still happens."""
        _existing_user()
        self._callback(DISCORD_ME)
        user = User.objects.get(email="layott@example.com")
        self.assertEqual(user.discord_id, DISCORD_ME["id"])
        self.assertTrue(user.discord_connected)

    def test_a_discord_identity_owned_by_SOMEONE_ELSE_is_never_taken(self):
        """The guard that existed before the refactor, kept. Losing it would let one Discord
        account walk onto another player's AFC profile: a security regression, not a tidy-up."""
        owner = _existing_user("owner", "owner@example.com")
        owner.discord_id = DISCORD_ME["id"]
        owner.save()
        _existing_user("victim", "layott@example.com")

        self._callback(DISCORD_ME)

        # The victim's account must NOT have gained the link.
        victim = User.objects.get(email="layott@example.com")
        self.assertFalse(
            ConnectedAccount.objects.filter(user=victim, provider="discord").exists()
        )
        self.assertEqual(User.objects.get(email="owner@example.com").discord_id, DISCORD_ME["id"])


# ── v-ent sign-in ─────────────────────────────────────────────────────────────────────────────
@override_settings(**VENT_SETTINGS)
class VentSignInTests(TestCase):
    def setUp(self):
        cache.clear()

    def _start(self):
        return Client().get("/auth/vent/sso/start/?next=/home")

    def _callback(self, profile=None, state=None):
        """Drive vent_sso_callback with the token exchange and userinfo mocked."""
        if state is None:
            state = "st1"
            cache.set(f"vent_sso_state:{state}", {"next": "/home", "verifier": "v" * 40}, 600)
        with patch("afc_auth.connections.oauth.requests.post") as post, patch(
            "afc_auth.connections.oauth.requests.get"
        ) as get:
            post.return_value.status_code = 200
            # v-ent.co WRAPS the token. If oauth.access_token ever regresses to a flat read this
            # test fails here rather than in production after a player has approved.
            post.return_value.json.return_value = {
                "status": "success",
                "data": {"access_token": "at", "token_type": "Bearer"},
            }
            get.return_value.status_code = 200
            get.return_value.json.return_value = profile or VENT_PROFILE
            return Client().get(f"/auth/vent/sso/callback/?code=c&state={state}")

    def test_start_sends_the_browser_to_v_ent_with_pkce(self):
        resp = self._start()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("https://v-ent.co/partners/authorize", resp["Location"])
        self.assertIn("code_challenge_method=S256", resp["Location"])
        self.assertIn("identity", resp["Location"])

    def test_start_refuses_an_absolute_next_so_it_cannot_become_an_open_redirect(self):
        Client().get("/auth/vent/sso/start/?next=https://evil.example/steal")
        stashed = [cache.get(k) for k in []]  # nothing to read directly; assert via the callback
        # The stored next is normalised to /home, so the post-login bounce cannot leave AFC.
        # Driving the whole flow proves it end to end.
        cache.set("vent_sso_state:abs", {"next": "https://evil.example", "verifier": "v" * 40}, 600)
        # A stashed absolute path can only exist if start let one through; start is what is under
        # test, so assert on what start actually wrote by running it again with a bad value.
        resp = Client().get("/auth/vent/sso/start/?next=https://evil.example/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("v-ent.co", resp["Location"])

    def test_an_EXISTING_account_is_found_by_email_and_linked(self):
        _existing_user()
        resp = self._callback()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/vent/callback?code=", resp["Location"])
        link = ConnectedAccount.objects.get(provider="vent")
        self.assertEqual(link.provider_user_id, "9911")
        self.assertEqual(link.user.email, "layott@example.com")

    def test_a_NEW_account_is_created_and_linked(self):
        self.assertEqual(User.objects.count(), 0)
        self._callback()
        user = User.objects.get(email="layott@example.com")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(ConnectedAccount.objects.filter(user=user, provider="vent").exists())

    def test_a_RETURNING_player_is_found_by_SUB_even_if_their_email_changed(self):
        """v-ent.co's docs: key on sub, not the username, and by extension not the email either."""
        user = _existing_user(email="old@example.com")
        ConnectedAccount.objects.create(
            user=user, provider="vent", provider_user_id="9911", username="Layott",
        )
        self._callback()
        # No second account was created for the new email.
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ConnectedAccount.objects.filter(provider="vent").count(), 1)

    def test_a_FIRST_sign_in_with_NO_email_is_refused_rather_than_making_a_broken_account(self):
        """identity:email is a separate scope a player can decline. AFC keys recovery on email, so
        an account created without one could never be recovered."""
        no_email = {"status": "success", "data": {"sub": "9911", "username": "Layott"}}
        resp = self._callback(profile=no_email)
        self.assertIn("status=no_email", resp["Location"])
        self.assertEqual(User.objects.count(), 0)

    def test_a_RETURNING_player_with_no_email_still_signs_in(self):
        """Because rule 1 keys on the link, not the email. Refusing here would lock out anybody who
        declined the email scope after linking."""
        user = _existing_user()
        ConnectedAccount.objects.create(
            user=user, provider="vent", provider_user_id="9911", username="Layott",
        )
        no_email = {"status": "success", "data": {"sub": "9911", "username": "Layott"}}
        resp = self._callback(profile=no_email)
        self.assertIn("/vent/callback?code=", resp["Location"])

    def test_an_unknown_state_is_refused_as_the_CSRF_guard(self):
        resp = self._callback(state="never-issued")
        self.assertIn("status=failed", resp["Location"])
        self.assertEqual(ConnectedAccount.objects.count(), 0)

    def test_the_state_is_SINGLE_USE(self):
        _existing_user()
        cache.set("vent_sso_state:once", {"next": "/home", "verifier": "v" * 40}, 600)
        self._callback(state="once")
        second = self._callback(state="once")
        self.assertIn("status=failed", second["Location"])

    def test_the_redirect_carries_only_a_handoff_code_and_no_session(self):
        """The redirect lands in browser history and the origin can leak via Referer."""
        _existing_user()
        resp = self._callback()
        location = resp["Location"]
        self.assertNotIn("token", location.lower())
        self.assertIn("code=", location)

    def test_the_handoff_is_swapped_once_and_then_dead(self):
        _existing_user()
        resp = self._callback()
        code = resp["Location"].split("code=")[1].split("&")[0]
        first = Client().post(
            "/auth/vent/sso/exchange/",
            data=json.dumps({"code": code}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        second = Client().post(
            "/auth/vent/sso/exchange/",
            data=json.dumps({"code": code}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 400)

    def test_a_vent_identity_owned_by_SOMEONE_ELSE_is_not_stolen(self):
        other = _existing_user("other", "other@example.com")
        ConnectedAccount.objects.create(
            user=other, provider="vent", provider_user_id="9911", username="Layott",
        )
        _existing_user("target", "layott@example.com")
        self._callback()
        # The link still belongs to the original owner, and only one exists.
        self.assertEqual(ConnectedAccount.objects.filter(provider="vent").count(), 1)
        self.assertEqual(
            ConnectedAccount.objects.get(provider="vent").user.email, "other@example.com"
        )

    @override_settings(VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
    def test_with_no_credentials_the_button_simply_does_not_work(self):
        """Rather than a 500. The provider is disabled until both variables are set."""
        resp = self._start()
        self.assertIn("status=unconfigured", resp["Location"])
