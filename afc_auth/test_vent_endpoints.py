"""The v-ent.co adapter must point at v-ent.co's real endpoints.

WHY THIS FILE EXISTS (2026-08-27)
    The adapter was written DARK, against the usual OIDC path convention:

        {issuer}/oauth2/authorize   {issuer}/oauth2/token   {issuer}/oauth2/userinfo

    All three were wrong, and nothing could have noticed: the provider was disabled, so no code
    path ever built a URL, and no test asserted what the URLs should be. It would have failed on
    the first real player, with an error pointing at v-ent.co rather than at AFC.

    The correct values come from v-ent.co's own metadata document, quoted in providers/vent.py.
    The trap it contains is worth stating twice: the AUTHORIZE endpoint is on a different host
    (v-ent.co, the browser) from the token and userinfo endpoints (api.v-ent.co, server to server).
    An adapter that derives everything from one issuer cannot be right.

WHAT IS COVERED
    The endpoint builder, the scopes, and the tolerance in normalize(). All offline: the live
    metadata check is a separate opt-in test, because a unit suite must not depend on somebody
    else's uptime.

Run: AFC_TEST_DB_NAME=test_afc_vent python manage.py test afc_auth.test_vent_endpoints
"""
import json
import os
import urllib.request

from django.test import SimpleTestCase, override_settings

from afc_auth.connections.providers import vent
from afc_auth.connections.registry import get_provider

# What v-ent.co published on 2026-08-27. Kept here as data so a drift shows up as a diff.
PUBLISHED = {
    "issuer": "https://api.v-ent.co",
    "authorization_endpoint": "https://v-ent.co/partners/authorize",
    "token_endpoint": "https://api.v-ent.co/partners/sso/token/",
    "userinfo_endpoint": "https://api.v-ent.co/partners/sso/userinfo/",
    "scopes_supported": ["identity", "identity:email", "identity:teams"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["client_secret_post"],
}


class VentEndpointsTests(SimpleTestCase):
    def test_the_defaults_match_what_v_ent_publishes(self):
        """THE REGRESSION TEST. Before the fix these were /oauth2/authorize, /oauth2/token and
        /oauth2/userinfo, all under the API host."""
        got = vent.endpoints()
        self.assertEqual(got["authorize_url"], PUBLISHED["authorization_endpoint"])
        self.assertEqual(got["token_url"], PUBLISHED["token_endpoint"])
        self.assertEqual(got["userinfo_url"], PUBLISHED["userinfo_endpoint"])

    def test_the_browser_host_and_the_api_host_are_DIFFERENT(self):
        """The trap. Stated as its own test so a future refactor that folds them back into one
        issuer fails here with a name that explains why."""
        got = vent.endpoints()
        self.assertTrue(got["authorize_url"].startswith("https://v-ent.co/"))
        self.assertTrue(got["token_url"].startswith("https://api.v-ent.co/"))

    def test_the_token_and_userinfo_paths_keep_their_trailing_slash(self):
        """Django would redirect without it, and a 302 on a POST loses the body."""
        got = vent.endpoints()
        self.assertTrue(got["token_url"].endswith("/"))
        self.assertTrue(got["userinfo_url"].endswith("/"))

    @override_settings(
        VENT_ISSUER="https://staging-api.v-ent.co/",
        VENT_AUTHORIZE_BASE="https://staging.v-ent.co/",
    )
    def test_both_hosts_can_be_overridden_and_trailing_slashes_are_stripped(self):
        got = vent.endpoints()
        self.assertEqual(got["authorize_url"], "https://staging.v-ent.co/partners/authorize")
        self.assertEqual(got["token_url"], "https://staging-api.v-ent.co/partners/sso/token/")

    def test_the_requested_scopes_are_ones_v_ent_actually_supports(self):
        """It used to ask for `openid profile email`, none of which v-ent.co publishes."""
        provider = get_provider("vent")
        for scope in provider.scopes:
            self.assertIn(scope, PUBLISHED["scopes_supported"], f"v-ent.co does not support {scope}")

    def test_the_teams_scope_is_NOT_requested(self):
        """AFC only needs to know who the player is. Asking for their team list would collect
        something no AFC surface reads."""
        self.assertNotIn("identity:teams", get_provider("vent").scopes)


# The EXACT body v-ent.co returns, from vent_partners/views_sso.py::sso_userinfo (read 2026-08-28),
# including the envelope its error responses also use. Kept as data so a drift shows up as a diff.
REAL_USERINFO = {
    "status": "success",
    "message": "Profile",
    "data": {
        "sub": "4821",
        "username": "Layott",
        "name": "Layo Tunde",
        "country": "Nigeria",
        "city": "Lagos",
        "picture": "https://api.v-ent.co/media/profiles/layott.png",
        "is_founding_member": True,
        "email": "layott@example.com",
        "email_verified": True,
    },
}


class VentRealUserinfoTests(SimpleTestCase):
    """The shape v-ent.co actually sends, rather than a shape I hoped for."""

    def test_the_real_response_maps_onto_the_house_shape(self):
        got = vent.normalize(REAL_USERINFO)
        self.assertEqual(got["provider_user_id"], "4821")
        self.assertEqual(got["username"], "Layott")
        self.assertEqual(got["email"], "layott@example.com")
        self.assertEqual(got["avatar_url"], "https://api.v-ent.co/media/profiles/layott.png")

    def test_a_player_with_no_avatar_still_links(self):
        """`picture` is NULL when the player has never uploaded one, which is common."""
        body = {**REAL_USERINFO, "data": {**REAL_USERINFO["data"], "picture": None}}
        got = vent.normalize(body)
        self.assertEqual(got["avatar_url"], "")
        self.assertEqual(got["provider_user_id"], "4821")

    def test_without_the_email_scope_the_link_still_works(self):
        """email and email_verified are only present when identity:email was granted. AFC does ask
        for it, but a player can decline a scope on the consent screen."""
        data = {k: v for k, v in REAL_USERINFO["data"].items()
                if k not in ("email", "email_verified")}
        got = vent.normalize({**REAL_USERINFO, "data": data})
        self.assertEqual(got["email"], "")
        self.assertEqual(got["provider_user_id"], "4821")

    def test_the_subject_is_the_v_ent_user_id_and_not_the_username(self):
        """A username can be changed; the row keyed on it would then point at nobody. `sub` is the
        stable id, and it is what ConnectedAccount must store."""
        self.assertEqual(vent.normalize(REAL_USERINFO)["provider_user_id"], "4821")


# The EXACT token body, from vent_partners/views_sso.py::sso_token (`_ok({...})`, read 2026-08-28).
REAL_TOKEN_RESPONSE = {
    "status": "success",
    "message": "Token",
    "data": {"access_token": "vent_at_abc123", "token_type": "Bearer", "expires_in": 3600},
}


class VentAccessTokenTests(SimpleTestCase):
    """v-ent.co wraps the access token, and reading it flat breaks every connection.

    This is the nastiest of the three defects in this integration, because it fails LATE: the
    player has already approved AFC on v-ent.co's consent screen by the time it goes wrong, and
    the error surfaces as a 401 from v-ent.co, so it reads as their fault rather than ours.
    """

    def test_the_wrapped_token_is_found(self):
        from afc_auth.connections import oauth
        provider = get_provider("vent")
        self.assertEqual(oauth.access_token(provider, REAL_TOKEN_RESPONSE), "vent_at_abc123")

    def test_the_FLAT_read_would_have_returned_nothing(self):
        """Pins the bug itself. The old call site was tokens.get("access_token"), which is None
        here, so AFC sent "Bearer None" and v-ent.co answered 401 BAD_TOKEN."""
        self.assertIsNone(REAL_TOKEN_RESPONSE.get("access_token"))

    def test_a_flat_body_still_works_if_v_ent_ever_changes(self):
        from afc_auth.connections import oauth
        provider = get_provider("vent")
        self.assertEqual(oauth.access_token(provider, {"access_token": "flat"}), "flat")

    def test_other_providers_keep_the_plain_top_level_read(self):
        """Discord and Google answer the flat OAuth 2 body. Unwrapping them would break both."""
        from afc_auth.connections import oauth
        for slug in ("discord", "google"):
            self.assertEqual(
                oauth.access_token(get_provider(slug), {"access_token": "flat"}), "flat"
            )

    def test_a_junk_body_yields_an_empty_token_rather_than_None(self):
        """An empty string fails the profile fetch cleanly; None would be formatted into the
        Authorization header as the text "None"."""
        from afc_auth.connections import oauth
        provider = get_provider("vent")
        self.assertEqual(oauth.access_token(provider, None), "")
        self.assertEqual(oauth.access_token(provider, {"status": "error", "data": None}), "")


class VentNormalizeTests(SimpleTestCase):
    """The tolerance around that shape, kept because renaming a key must not hard-fail a link."""

    def test_an_oidc_shaped_profile_is_read(self):
        got = vent.normalize(
            {"sub": "abc", "preferred_username": "Layott", "email": "A@B.COM", "picture": "u"}
        )
        self.assertEqual(got["provider_user_id"], "abc")
        self.assertEqual(got["username"], "Layott")
        self.assertEqual(got["email"], "a@b.com")
        self.assertEqual(got["avatar_url"], "u")

    def test_a_plain_profile_is_read_too(self):
        got = vent.normalize({"id": 42, "username": "Layott", "avatar_url": "u"})
        self.assertEqual(got["provider_user_id"], "42")
        self.assertEqual(got["username"], "Layott")
        self.assertEqual(got["avatar_url"], "u")

    def test_a_status_data_envelope_is_unwrapped(self):
        """v-ent.co's other endpoints answer {"status": ..., "data": {...}}."""
        got = vent.normalize({"status": "success", "data": {"id": "7", "username": "N"}})
        self.assertEqual(got["provider_user_id"], "7")
        self.assertEqual(got["username"], "N")

    def test_a_missing_optional_does_not_break_the_link(self):
        got = vent.normalize({"id": "7"})
        self.assertEqual(got["provider_user_id"], "7")
        self.assertEqual(got["email"], "")
        self.assertEqual(got["avatar_url"], "")

    def test_no_id_yields_an_empty_subject_rather_than_an_invented_one(self):
        """Without a stable subject there is nothing to key ConnectedAccount on, so the caller must
        refuse the link. Inventing an id here would attach the wrong player."""
        self.assertEqual(vent.normalize({"username": "N"})["provider_user_id"], "")

    def test_a_non_dict_is_survived(self):
        self.assertEqual(vent.normalize(None)["provider_user_id"], "")
        self.assertEqual(vent.normalize("nope")["provider_user_id"], "")


class VentLiveMetadataTests(SimpleTestCase):
    """OPT-IN. Set AFC_CHECK_VENT_LIVE=1 to compare the adapter against the real document.

    Skipped by default on purpose: a unit suite that fails when somebody else's server is down is a
    suite people learn to ignore. Run it deliberately when the integration is being touched.
    """

    def test_the_live_metadata_still_matches_this_adapter(self):
        if os.getenv("AFC_CHECK_VENT_LIVE") != "1":
            self.skipTest("set AFC_CHECK_VENT_LIVE=1 to hit v-ent.co")
        with urllib.request.urlopen(vent.metadata_url(), timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
        data = body.get("data", body)
        got = vent.endpoints()
        self.assertEqual(got["authorize_url"], data["authorization_endpoint"])
        self.assertEqual(got["token_url"], data["token_endpoint"])
        self.assertEqual(got["userinfo_url"], data["userinfo_endpoint"])
