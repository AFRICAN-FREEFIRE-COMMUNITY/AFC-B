"""The deletion signal: telling a partner to delete its copy of a player's data.

AFC cutting a partner off (afc_sso/tokens.py) only stops FUTURE reads. The data the
partner already holds is beyond AFC's reach, so a player's "Remove" is only half true
until the partner is told. This is that other half.

Four things have to hold, and each has a test below:
  1. it FIRES, on revoke and on account deletion;
  2. it is SIGNED with AFC's OIDC key and carries the PAIRWISE sub, never the user id,
     because pairwise is the only identifier that partner has ever seen;
  3. it RETRIES a partner that is briefly down, and gives up on one that says no;
  4. it can NEVER break the player's revoke, which must succeed locally whatever the
     partner's server does.
"""
import json
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model

from afc_auth.models import SessionToken
from afc_sso.validators import pairwise_sub
from afc_sso.webhooks import (
    EVENT_TYPE,
    REASON_ACCOUNT_DELETED,
    REASON_PLAYER_REVOKED,
    build_signal,
)

Application = get_application_model()
AccessToken = get_access_token_model()
User = get_user_model()

WEBHOOK_URL = "https://partner.test/afc/disconnected"


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _b64_segment(segment):
    import base64

    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _decode_unverified(token):
    """Read the claims of a compact JWS without verifying, for assertions only.

    The signature IS verified in test_the_signal_verifies_against_afcs_published_jwks,
    which is the check a partner actually performs.
    """
    return _b64_segment(token.split(".")[1])


def _decode_header(token):
    """The JOSE header, where `kid` tells a partner which JWKS key to verify with."""
    return _b64_segment(token.split(".")[0])


# SSO_WEBHOOKS_SYNC runs the delivery inline instead of handing it to Celery, so these
# tests need no broker and no worker. It defaults to DEBUG, which the test runner turns
# off, so it is set explicitly rather than relied on.
@override_settings(SSO_WEBHOOKS_SYNC=True)
class DeletionSignalTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.player = User.objects.create_user(
            username="signalplayer", email="signal@afc.test", password="x")
        SessionToken.objects.create(user=self.player, token="tok-signal")

        self.partner = self._application("Signal Partner", WEBHOOK_URL)
        # A second partner with no webhook URL: it must simply be skipped, never error.
        self.quiet_partner = self._application("Quiet Partner", "")

    def _application(self, name, webhook_url):
        return Application.objects.create(
            name=name,
            display_name=name,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            algorithm=Application.RS256_ALGORITHM,
            redirect_uris="https://partner.test/cb",
            deletion_webhook_url=webhook_url,
            client_secret=f"{name}-secret",
        )

    def _connect(self, application):
        """Give the player a live token for this partner, which is what "connected" means
        (there is no connection table; see afc_sso/api.py _connection_rows)."""
        return AccessToken.objects.create(
            user=self.player, application=application, token=f"tok-{application.pk}",
            expires=timezone.now() + timezone.timedelta(hours=1), scope="openid",
        )

    def _revoke(self, application):
        return self.client.delete(
            f"/sso/me/connected-apps/{application.pk}/",
            HTTP_AUTHORIZATION="Bearer tok-signal",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 1) It fires
    # ──────────────────────────────────────────────────────────────────────────
    def test_revoking_sends_the_signal(self):
        self._connect(self.partner)

        with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(202)) as post:
            resp = self._revoke(self.partner)

        self.assertEqual(resp.status_code, 200)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], WEBHOOK_URL)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Content-Type"], "application/secevent+jwt")

        claims = _decode_unverified(post.call_args.kwargs["data"].decode())
        self.assertEqual(
            claims["events"][EVENT_TYPE]["reason"], REASON_PLAYER_REVOKED)

    def test_a_partner_with_no_webhook_url_is_skipped_silently(self):
        self._connect(self.quiet_partner)

        with patch("afc_sso.tasks.requests.post") as post:
            resp = self._revoke(self.quiet_partner)

        self.assertEqual(resp.status_code, 200)
        post.assert_not_called()

    def test_revoking_a_partner_that_was_never_connected_sends_nothing(self):
        """A repeat click, or an id for an org the player never used, revokes nothing and
        must not tell that partner anything happened."""
        with patch("afc_sso.tasks.requests.post") as post:
            resp = self._revoke(self.partner)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["revoked"]["access_tokens"], 0)
        post.assert_not_called()

    def test_deleting_the_account_signals_every_connected_partner(self):
        self._connect(self.partner)
        other = self._application("Second Partner", "https://second.test/hook")
        self._connect(other)
        self._connect(self.quiet_partner)  # no URL, must be skipped

        with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(200)) as post:
            self.player.delete()

        self.assertEqual(post.call_count, 2)
        urls = {call.args[0] for call in post.call_args_list}
        self.assertEqual(urls, {WEBHOOK_URL, "https://second.test/hook"})
        for call in post.call_args_list:
            claims = _decode_unverified(call.kwargs["data"].decode())
            self.assertEqual(
                claims["events"][EVENT_TYPE]["reason"], REASON_ACCOUNT_DELETED)

    def test_deleting_an_account_with_no_connections_sends_nothing(self):
        lonely = User.objects.create_user(
            username="lonely", email="lonely@afc.test", password="x")

        with patch("afc_sso.tasks.requests.post") as post:
            lonely.delete()

        post.assert_not_called()

    # ──────────────────────────────────────────────────────────────────────────
    # 2) It is signed, and it names the player the way the partner knows them
    # ──────────────────────────────────────────────────────────────────────────
    def test_the_subject_is_the_pairwise_sub_never_the_afc_user_id(self):
        """Pairwise is the only identifier this partner has ever seen. Sending the raw pk
        would also hand every partner a shared key they could join their databases on,
        which is the whole thing pairwise exists to prevent."""
        token = build_signal(self.partner, self.player, REASON_PLAYER_REVOKED)
        claims = _decode_unverified(token)

        expected = pairwise_sub(self.player, self.partner)
        self.assertEqual(claims["sub"], expected)
        self.assertEqual(claims["events"][EVENT_TYPE]["subject"]["id"], expected)
        self.assertNotIn(str(self.player.pk), claims["sub"])

    def test_two_partners_receive_different_subjects_for_one_player(self):
        first = _decode_unverified(
            build_signal(self.partner, self.player, REASON_PLAYER_REVOKED))
        second = _decode_unverified(
            build_signal(self.quiet_partner, self.player, REASON_PLAYER_REVOKED))
        self.assertNotEqual(first["sub"], second["sub"])

    def test_the_signal_verifies_against_afcs_published_jwks(self):
        """The verification a partner actually performs: fetch the JWKS they already use
        for ID tokens, match on `kid`, check the signature, then check iss and aud. If
        this passes, the documented procedure in the integration guide works."""
        from jwcrypto import jwk, jwt as jwcrypto_jwt

        token = build_signal(self.partner, self.player, REASON_PLAYER_REVOKED)

        jwks = self.client.get("/sso/.well-known/jwks.json").json()
        key_set = jwk.JWKSet.from_json(json.dumps(jwks))

        header = _decode_header(token)
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "secevent+jwt")
        self.assertTrue(any(k.get("kid") == header["kid"] for k in jwks["keys"]))

        verified = jwcrypto_jwt.JWT(key=key_set, jwt=token)
        claims = json.loads(verified.claims)
        self.assertEqual(claims["aud"], self.partner.client_id)
        self.assertTrue(claims["iss"].endswith("/sso"))
        self.assertIn("jti", claims)
        self.assertIn("iat", claims)

    def test_a_tampered_signal_fails_verification(self):
        """The signature has to be worth checking."""
        from jwcrypto import jwk, jwt as jwcrypto_jwt
        from jwcrypto.common import JWException

        token = build_signal(self.partner, self.player, REASON_PLAYER_REVOKED)
        head, payload, signature = token.split(".")
        forged = f"{head}.{payload[:-4]}AAAA.{signature}"

        key_set = jwk.JWKSet.from_json(
            json.dumps(self.client.get("/sso/.well-known/jwks.json").json()))
        # JWException is the jwcrypto base: verifying against a key SET reports the
        # failure as "no working key found" rather than "bad signature", and either way
        # the partner's verify() call raises and the payload is rejected.
        with self.assertRaises(JWException):
            jwcrypto_jwt.JWT(key=key_set, jwt=forged)

    # ──────────────────────────────────────────────────────────────────────────
    # 3) It retries the partner that is down, and gives up on the one saying no
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_5xx_is_retried(self):
        from celery.exceptions import Retry

        from afc_sso.tasks import deliver_disconnect_signal

        with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(503)):
            with self.assertRaises(Retry):
                deliver_disconnect_signal(self.partner.pk, WEBHOOK_URL, "signed.token.here")

    def test_a_connection_error_is_retried(self):
        from celery.exceptions import Retry

        from afc_sso.tasks import deliver_disconnect_signal

        with patch("afc_sso.tasks.requests.post",
                   side_effect=requests.ConnectionError("partner is down")):
            with self.assertRaises(Retry):
                deliver_disconnect_signal(self.partner.pk, WEBHOOK_URL, "signed.token.here")

    def test_a_4xx_is_not_retried(self):
        """A wrong URL or a rejected signature will still be wrong in five minutes, and
        hammering the partner is rude and pointless."""
        from afc_sso.tasks import deliver_disconnect_signal

        with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(404)):
            result = deliver_disconnect_signal(self.partner.pk, WEBHOOK_URL, "signed.token.here")

        self.assertFalse(result)

    def test_a_2xx_is_accepted(self):
        from afc_sso.tasks import deliver_disconnect_signal

        for code in (200, 202, 204):
            with self.subTest(code=code):
                with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(code)):
                    self.assertTrue(deliver_disconnect_signal(
                        self.partner.pk, WEBHOOK_URL, "signed.token.here"))

    def test_the_same_token_is_redelivered_on_retry(self):
        """A partner deduping on `jti` needs the identifier to survive a redelivery, so
        the token is signed once and carried through the retries unchanged."""
        from afc_sso.tasks import deliver_disconnect_signal

        # Patch the task's own retry so the arguments it would be re-queued with are
        # readable; the Retry exception itself does not carry them.
        with patch("afc_sso.tasks.requests.post", return_value=FakeResponse(500)):
            with patch.object(deliver_disconnect_signal, "retry",
                              side_effect=RuntimeError("retried")) as retry:
                with self.assertRaises(RuntimeError):
                    deliver_disconnect_signal(
                        self.partner.pk, WEBHOOK_URL, "signed.token.here")

        requeued = retry.call_args.kwargs["kwargs"]
        self.assertEqual(requeued["token"], "signed.token.here")
        self.assertEqual(requeued["url"], WEBHOOK_URL)
        # Backoff, not an immediate hammer.
        self.assertGreaterEqual(retry.call_args.kwargs["countdown"], 20)

    # ──────────────────────────────────────────────────────────────────────────
    # 4) It can never cost the player their revoke
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_failing_webhook_still_leaves_the_revoke_successful(self):
        """The revoke has already committed by the time the signal is attempted. A
        partner being down does not get to undo it, or to turn it into a 500."""
        self._connect(self.partner)

        with patch("afc_sso.tasks.requests.post",
                   side_effect=requests.ConnectionError("partner is down")):
            resp = self._revoke(self.partner)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["revoked"]["access_tokens"], 1)
        self.assertFalse(
            AccessToken.objects.filter(user=self.player, application=self.partner).exists())

    def test_an_exploding_dispatch_still_leaves_the_revoke_successful(self):
        """Not just a network failure: anything at all going wrong on the way out."""
        self._connect(self.partner)

        with patch("afc_sso.webhooks.build_signal", side_effect=RuntimeError("boom")):
            resp = self._revoke(self.partner)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            AccessToken.objects.filter(user=self.player, application=self.partner).exists())

    def test_a_broken_account_deletion_signal_still_deletes_the_account(self):
        self._connect(self.partner)

        with patch("afc_sso.webhooks.notify_disconnected", side_effect=RuntimeError("boom")):
            self.player.delete()

        self.assertFalse(User.objects.filter(username="signalplayer").exists())
