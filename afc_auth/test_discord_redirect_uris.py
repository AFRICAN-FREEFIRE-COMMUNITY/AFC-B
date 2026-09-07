"""Every redirect_uri AFC can send to discord.com, pinned.

WHY THIS FILE EXISTS (owner 2026-09-07)
    Screenshot from a phone at 01:00: pressing Connect on Discord under Connected accounts on
    /profile landed on Discord's page "Invalid OAuth2 redirect_uri".

    Discord answers with that page when the redirect_uri in the authorize request is not, character
    for character, one of the strings listed under OAuth2 -> Redirects on the application. The
    portal listed three:

        https://api.africanfreefirecommunity.com/auth/connect-discord/callback/
        https://api.africanfreefirecommunity.com/auth/discord/sso/callback/
        http://localhost:8000/auth/discord/sso/callback/

    AFC sends a FOURTH, from the connected-accounts flow added on 2026-08-26:

        https://api.africanfreefirecommunity.com/auth/connections/discord/callback/

    Nothing in the codebase named the full set, so adding a flow that sends a new callback looked
    like an ordinary change. The two older flows kept working, which is why it read as "Discord is
    broken" rather than "one of three Discord flows is broken".

    Second defect found while looking: discord_sso_start and discord_sso_callback COMPOSED their
    redirect_uri out of the incoming Host header (request.build_absolute_uri). ALLOWED_HOSTS
    defaults to "*", so a request arriving under any other name produced a string that cannot be in
    the portal, and sign-in died the same way. Both now read a declared setting.

WHAT IS COVERED
    That each flow sends the exact registered string, that a hostile or unexpected Host header
    cannot change it, and that the SET of URIs AFC can send is the set a human has registered. The
    last one fails on purpose when a new Discord flow appears: register the string in the portal,
    then add it here.

Run: AFC_TEST_DB_NAME=test_afc_discord python manage.py test afc_auth.test_discord_redirect_uris
"""
from urllib.parse import parse_qs, urlparse

from django.test import RequestFactory, TestCase, override_settings

from afc_auth import views as auth_views
from afc_auth.connections.views import _callback_uri

# The exact strings that must be listed under OAuth2 -> Redirects on the AFC Discord application
# (client id 1482738934407630930). Production first, then the local-dev entries.
REGISTERED = {
    "https://api.africanfreefirecommunity.com/auth/connect-discord/callback/",
    "https://api.africanfreefirecommunity.com/auth/discord/sso/callback/",
    "https://api.africanfreefirecommunity.com/auth/connections/discord/callback/",
    "http://localhost:8000/auth/discord/sso/callback/",
}

PROD = dict(
    DISCORD_REDIRECT_URI="https://api.africanfreefirecommunity.com/auth/connect-discord/callback/",
    DISCORD_SSO_REDIRECT_URI="https://api.africanfreefirecommunity.com/auth/discord/sso/callback/",
    DISCORD_SSO_REDIRECT_URI_LOCAL="http://localhost:8000/auth/discord/sso/callback/",
    AFC_API_BASE_URL="https://api.africanfreefirecommunity.com",
)


@override_settings(**PROD)
class DiscordRedirectUriTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    # ── the SSO pair: declared, never composed from the request ──────────────
    def test_sso_redirect_uri_is_the_registered_production_string(self):
        request = self.factory.get("/auth/discord/sso/start/", HTTP_HOST="api.africanfreefirecommunity.com")
        self.assertEqual(
            auth_views._discord_sso_redirect_uri(request),
            "https://api.africanfreefirecommunity.com/auth/discord/sso/callback/",
        )

    def test_an_unexpected_host_cannot_invent_a_redirect_uri(self):
        # ALLOWED_HOSTS defaults to "*", so these DO reach the view in production. Before the fix
        # each of them produced a different, unregistered redirect_uri and Discord refused it.
        for host in ("africanfreefirecommunity.com", "1.2.3.4", "afc-env.eba-x.us-east-1.elasticbeanstalk.com"):
            with self.subTest(host=host):
                request = self.factory.get("/auth/discord/sso/start/", HTTP_HOST=host)
                self.assertEqual(
                    auth_views._discord_sso_redirect_uri(request),
                    "https://api.africanfreefirecommunity.com/auth/discord/sso/callback/",
                )

    def test_local_dev_still_gets_the_localhost_callback(self):
        for host in ("localhost:8000", "127.0.0.1:8000"):
            with self.subTest(host=host):
                request = self.factory.get("/auth/discord/sso/start/", HTTP_HOST=host)
                self.assertEqual(
                    auth_views._discord_sso_redirect_uri(request),
                    "http://localhost:8000/auth/discord/sso/callback/",
                )

    # ── connected accounts: the flow that actually broke ─────────────────────
    def test_connections_callback_is_the_string_that_must_be_registered(self):
        request = self.factory.get("/auth/connections/discord/start/", HTTP_HOST="1.2.3.4")
        self.assertEqual(
            _callback_uri(request, "discord"),
            "https://api.africanfreefirecommunity.com/auth/connections/discord/callback/",
        )

    # ── the whole set, so a new flow cannot be added quietly ─────────────────
    def test_every_redirect_uri_afc_can_send_is_one_a_human_registered(self):
        """Fails when a new Discord flow appears. Register the string in the Discord Developer
        Portal FIRST, then add it to REGISTERED above. Do not just edit the list."""
        prod = self.factory.get("/", HTTP_HOST="api.africanfreefirecommunity.com")
        local = self.factory.get("/", HTTP_HOST="localhost:8000")
        from django.conf import settings

        sent = {
            settings.DISCORD_REDIRECT_URI,
            auth_views._discord_sso_redirect_uri(prod),
            auth_views._discord_sso_redirect_uri(local),
            _callback_uri(prod, "discord"),
        }
        self.assertEqual(sent - REGISTERED, set(), "sent to Discord but not registered in the portal")

    # ── the consent URL itself, end to end ───────────────────────────────────
    @override_settings(DISCORD_CLIENT_ID="1482738934407630930", **PROD)
    def test_sso_start_puts_the_registered_uri_in_the_consent_url(self):
        response = self.client.get(
            "/auth/discord/sso/start/?next=/home", HTTP_HOST="africanfreefirecommunity.com",
        )
        self.assertEqual(response.status_code, 302)
        sent = parse_qs(urlparse(response["Location"]).query)["redirect_uri"][0]
        self.assertIn(sent, REGISTERED)
