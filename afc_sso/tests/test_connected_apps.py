"""The Connected apps page is the player's only way to take a partner org back out of
their account, so the two things it has to get right are: show me MY connections and
nobody else's, and when I press Remove, actually remove everything the partner holds.

Covers afc_sso/api.py, reached at /sso/me/connected-apps/.
"""
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_grant_model,
    get_id_token_model,
    get_refresh_token_model,
)

from afc_auth.models import SessionToken

AccessToken = get_access_token_model()
Application = get_application_model()
Grant = get_grant_model()
IDToken = get_id_token_model()
RefreshToken = get_refresh_token_model()
User = get_user_model()

LIST_URL = "/sso/me/connected-apps/"


class ConnectedAppsTests(TestCase):
    def setUp(self):
        self.player = User.objects.create_user(
            username="listplayer", email="list@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.player, token="tok-player")

        # A second, entirely separate player. Every isolation assertion in this file is
        # about keeping these two apart.
        self.other = User.objects.create_user(
            username="otherplayer", email="other@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.other, token="tok-other")

        self.app = self._application("Partner One", share_profile=True)
        self.second_app = self._application("Partner Two", share_freefire_uid=True)

    # ── helpers ──

    def _application(self, name, **toggles):
        return Application.objects.create(
            name=name, user=self.player,
            display_name=f"{name} Display",
            logo_url="https://cdn.partner.test/logo.png",
            homepage_url="https://partner.test",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            **toggles,
        )

    def _access_token(self, user, application, token, scope="openid profile", hours=1):
        """hours < 0 produces an already-expired token, which is how the expiry rule is tested."""
        return AccessToken.objects.create(
            user=user, application=application, token=token, scope=scope,
            expires=timezone.now() + timezone.timedelta(hours=hours),
        )

    def _refresh_token(self, user, application, token, access_token=None, revoked=None):
        return RefreshToken.objects.create(
            user=user, application=application, token=token,
            access_token=access_token, revoked=revoked,
        )

    def _grant(self, user, application, code):
        return Grant.objects.create(
            user=user, application=application, code=code,
            expires=timezone.now() + timezone.timedelta(minutes=5),
            redirect_uri="https://partner.test/cb", scope="openid profile",
        )

    def _id_token(self, user, application):
        return IDToken.objects.create(
            user=user, application=application, scope="openid profile",
            expires=timezone.now() + timezone.timedelta(hours=1),
        )

    def _list(self, token="tok-player"):
        return self.client.get(LIST_URL, headers={"authorization": f"Bearer {token}"})

    def _revoke(self, application_id, token="tok-player"):
        return self.client.delete(
            f"{LIST_URL}{application_id}/", headers={"authorization": f"Bearer {token}"}
        )

    def _apps(self, response):
        return json.loads(response.content)["apps"]

    # ── the list: whose connections, and how many rows ──

    def test_the_list_shows_only_the_calling_players_apps(self):
        """The failure this guards against is the worst one available here: showing, or
        later letting a player revoke, somebody else's connection."""
        self._access_token(self.player, self.app, "mine")
        self._access_token(self.other, self.second_app, "theirs")

        apps = self._apps(self._list())
        self.assertEqual([a["application_id"] for a in apps], [self.app.pk])

        other_apps = self._apps(self._list(token="tok-other"))
        self.assertEqual([a["application_id"] for a in other_apps], [self.second_app.pk])

    def test_an_app_with_several_tokens_appears_once(self):
        """A partner refreshing all day accumulates token rows. The page is a list of ORGS."""
        self._access_token(self.player, self.app, "a1")
        self._access_token(self.player, self.app, "a2")
        self._refresh_token(self.player, self.app, "r1")

        apps = self._apps(self._list())
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["application_id"], self.app.pk)

    def test_a_player_with_no_connections_gets_an_empty_list(self):
        self.assertEqual(self._apps(self._list()), [])

    # ── the expiry decision, stated in api._connection_rows ──

    def test_an_expired_only_connection_is_not_listed(self):
        """DECISION: expired access token, no live refresh token, means not connected. The
        partner cannot read anything with it, so the player has nothing to remove."""
        self._access_token(self.player, self.app, "stale", hours=-1)
        self.assertEqual(self._apps(self._list()), [])

    def test_an_expired_access_token_still_counts_when_a_refresh_token_is_live(self):
        """The other half of the same decision. AFC sets no REFRESH_TOKEN_EXPIRE_SECONDS, so
        an unrevoked refresh token means the partner mints a new access token whenever it
        likes. Hiding this org would tell the player they are safe while the partner is not
        cut off, and would leave them no Remove button."""
        expired = self._access_token(
            self.player, self.app, "stale", scope="openid profile", hours=-1
        )
        self._refresh_token(self.player, self.app, "still-good", access_token=expired)

        apps = self._apps(self._list())
        self.assertEqual([a["application_id"] for a in apps], [self.app.pk])
        # Scope falls back to the last access token, because RefreshToken has no scope column
        # and a refresh re-mints exactly those scopes.
        self.assertEqual(apps[0]["scope_codes"], ["profile"])
        self.assertIsNone(apps[0]["expires_at"], "no live access token left to expire")

    def test_a_revoked_refresh_token_does_not_keep_a_dead_connection_alive(self):
        self._access_token(self.player, self.app, "stale", hours=-1)
        self._refresh_token(self.player, self.app, "dead", revoked=timezone.now())
        self.assertEqual(self._apps(self._list()), [])

    # ── the payload the frontend is being built against ──

    def test_the_row_carries_the_org_identity_and_the_timestamps(self):
        self._access_token(self.player, self.app, "a1")
        row = self._apps(self._list())[0]
        self.assertEqual(row["name"], "Partner One Display")
        self.assertEqual(row["logo_url"], "https://cdn.partner.test/logo.png")
        self.assertEqual(row["homepage_url"], "https://partner.test")
        for field in ("granted_at", "last_used_at", "expires_at"):
            self.assertIsNotNone(row[field], field)

    def test_the_scope_lines_are_the_consent_screens_own_words(self):
        """The point of routing through claims.describe_scopes: the page cannot promise
        something different from what the player was shown when they clicked Allow."""
        self._access_token(
            self.player, self.app, "a1", scope="openid profile afc.freefire"
        )
        row = self._apps(self._list())[0]
        self.assertEqual(row["scope_codes"], ["afc.freefire", "profile"])
        self.assertEqual(row["scopes"], [
            "Your Free Fire UID",
            "Your in-game name, avatar, country and language",
        ])
        self.assertNotIn("openid", row["scope_codes"], "openid releases no player data")

    def test_scopes_and_scope_codes_stay_parallel(self):
        """The frontend zips the two lists, so they must line up index for index."""
        self._access_token(
            self.player, self.app, "a1", scope="openid profile email afc.team"
        )
        row = self._apps(self._list())[0]
        self.assertEqual(len(row["scopes"]), len(row["scope_codes"]))
        self.assertEqual(row["scope_codes"], ["afc.team", "email", "profile"])

    # ── revoke: all four tables, asserted one at a time ──

    def _connect_everything(self, user, application, suffix):
        access = self._access_token(user, application, f"access-{suffix}")
        self._refresh_token(user, application, f"refresh-{suffix}", access_token=access)
        self._grant(user, application, f"code-{suffix}")
        self._id_token(user, application)

    def test_revoke_deletes_the_access_token(self):
        self._connect_everything(self.player, self.app, "x")
        self.assertEqual(self._revoke(self.app.pk).status_code, 200)
        self.assertFalse(
            AccessToken.objects.filter(user=self.player, application=self.app).exists()
        )

    def test_revoke_deletes_the_refresh_token(self):
        """Miss this one and the partner quietly mints a new access token on its next
        refresh, so the player's Remove click would have changed nothing."""
        self._connect_everything(self.player, self.app, "x")
        self._revoke(self.app.pk)
        self.assertFalse(
            RefreshToken.objects.filter(user=self.player, application=self.app).exists()
        )

    def test_revoke_deletes_the_outstanding_grant(self):
        """An unexchanged authorization code is still worth a whole new token pair."""
        self._connect_everything(self.player, self.app, "x")
        self._revoke(self.app.pk)
        self.assertFalse(
            Grant.objects.filter(user=self.player, application=self.app).exists()
        )

    def test_revoke_deletes_the_id_token(self):
        self._connect_everything(self.player, self.app, "x")
        self._revoke(self.app.pk)
        self.assertFalse(
            IDToken.objects.filter(user=self.player, application=self.app).exists()
        )

    def test_revoke_reports_what_it_removed(self):
        self._connect_everything(self.player, self.app, "x")
        body = json.loads(self._revoke(self.app.pk).content)
        self.assertEqual(body["application_id"], self.app.pk)
        self.assertEqual(body["revoked"], {
            "access_tokens": 1, "refresh_tokens": 1, "grants": 1, "id_tokens": 1,
        })

    def test_the_revoked_app_disappears_from_the_list(self):
        self._connect_everything(self.player, self.app, "x")
        self._revoke(self.app.pk)
        self.assertEqual(self._apps(self._list()), [])

    def test_revoke_leaves_the_players_other_connections_alone(self):
        self._connect_everything(self.player, self.app, "one")
        self._connect_everything(self.player, self.second_app, "two")
        self._revoke(self.app.pk)
        remaining = [a["application_id"] for a in self._apps(self._list())]
        self.assertEqual(remaining, [self.second_app.pk])

    # ── revoke: idempotency and the security boundary ──

    def test_revoking_an_app_the_player_never_connected_is_a_clean_no_op(self):
        resp = self._revoke(self.second_app.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["revoked"], {
            "access_tokens": 0, "refresh_tokens": 0, "grants": 0, "id_tokens": 0,
        })

    def test_revoking_twice_succeeds_rather_than_erroring(self):
        self._connect_everything(self.player, self.app, "x")
        self.assertEqual(self._revoke(self.app.pk).status_code, 200)
        self.assertEqual(self._revoke(self.app.pk).status_code, 200)

    def test_revoking_an_unknown_application_id_does_not_500(self):
        self.assertEqual(self._revoke(999999).status_code, 200)

    def test_a_player_cannot_revoke_another_players_connection(self):
        """SECURITY. `application_id` comes off the URL and is attacker controlled; the USER
        comes off the session token and is not. Every queryset in revoke_connected_app is
        filtered by both, so naming somebody else's org removes nothing of theirs."""
        self._connect_everything(self.other, self.app, "victim")

        resp = self._revoke(self.app.pk, token="tok-player")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["revoked"], {
            "access_tokens": 0, "refresh_tokens": 0, "grants": 0, "id_tokens": 0,
        })

        self.assertTrue(
            AccessToken.objects.filter(user=self.other, application=self.app).exists())
        self.assertTrue(
            RefreshToken.objects.filter(user=self.other, application=self.app).exists())
        self.assertTrue(
            Grant.objects.filter(user=self.other, application=self.app).exists())
        self.assertTrue(
            IDToken.objects.filter(user=self.other, application=self.app).exists())
        # And the victim still sees the connection they never asked to lose.
        self.assertEqual(
            [a["application_id"] for a in self._apps(self._list(token="tok-other"))],
            [self.app.pk],
        )

    # ── auth, the house preamble ──

    def test_the_list_rejects_a_request_with_no_authorization_header(self):
        self.assertEqual(self.client.get(LIST_URL).status_code, 400)

    def test_the_list_rejects_a_non_bearer_authorization_header(self):
        resp = self.client.get(LIST_URL, headers={"authorization": "Token tok-player"})
        self.assertEqual(resp.status_code, 400)

    def test_the_list_rejects_an_invalid_token(self):
        self.assertEqual(self._list(token="not-a-real-token").status_code, 401)

    def test_revoke_rejects_a_request_with_no_authorization_header(self):
        self._connect_everything(self.player, self.app, "x")
        self.assertEqual(self.client.delete(f"{LIST_URL}{self.app.pk}/").status_code, 400)
        self.assertTrue(AccessToken.objects.filter(application=self.app).exists())

    def test_revoke_rejects_a_non_bearer_authorization_header(self):
        resp = self.client.delete(
            f"{LIST_URL}{self.app.pk}/", headers={"authorization": "tok-player"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_revoke_rejects_an_invalid_token(self):
        self.assertEqual(self._revoke(self.app.pk, token="not-a-real-token").status_code, 401)

    def test_revoke_still_works_when_the_sso_cookie_is_also_present(self):
        """Regression guard for the reason these views set authentication_classes([]).

        SSOSessionTokenMiddleware populates request.user for every /sso/ path from the
        auth_token cookie. DRF's default SessionAuthentication sees that populated user and
        runs a CSRF check, which turns this DELETE into a 403 for any browser that also holds
        the cookie, and it does whenever the frontend and the API share a host, as in local
        dev. Removing @authentication_classes([]) from revoke_connected_app makes this test
        fail with 403 CSRF Failed, which is exactly the point of it.

        enforce_csrf_checks=True is load bearing: the default test client sets
        _dont_enforce_csrf_checks on every request, so a plain self.client would pass here
        whether the bug was present or not.
        """
        self._connect_everything(self.player, self.app, "x")
        strict = Client(enforce_csrf_checks=True)
        strict.cookies["auth_token"] = "tok-player"
        resp = strict.delete(
            f"{LIST_URL}{self.app.pk}/", headers={"authorization": "Bearer tok-player"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(AccessToken.objects.filter(application=self.app).exists())
