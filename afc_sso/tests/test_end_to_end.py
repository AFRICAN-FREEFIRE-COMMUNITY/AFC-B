"""The whole flow as a partner experiences it: authorize, exchange, read userinfo,
refresh, revoke. If this passes, "Sign in with AFC" works."""
import base64
import hashlib
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from oauth2_provider.models import get_application_model, Grant

from afc_auth.models import UserProfile

Application = get_application_model()
User = get_user_model()

VERIFIER = "b" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).decode().rstrip("=")


class EndToEndTests(TestCase):
    def setUp(self):
        import datetime
        self.user = User.objects.create_user(
            username="e2eplayer", email="e2e@afc.test", password="x",
            country="NG", uid="8390224792",
        )
        UserProfile.objects.create(user=self.user, date_of_birth=datetime.date(1994, 1, 1))
        self.secret = "e2e-secret-value"
        self.app = Application.objects.create(
            name="E2E Org", user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            client_secret=self.secret,
            share_profile=True, share_freefire_uid=True,
        )

    def _exchange(self, code="e2e-code"):
        Grant.objects.create(
            user=self.user, code=code, application=self.app,
            expires=timezone.now() + timezone.timedelta(minutes=5),
            redirect_uri="https://partner.test/cb",
            scope="openid profile afc.freefire",
            code_challenge=CHALLENGE, code_challenge_method="S256", nonce="n",
        )
        resp = self.client.post("/sso/token/", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://partner.test/cb",
            "client_id": self.app.client_id,
            "client_secret": self.secret,
            "code_verifier": VERIFIER,
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        return json.loads(resp.content)

    def test_partner_can_complete_the_whole_flow(self):
        tokens = self._exchange()
        self.assertIn("id_token", tokens)
        self.assertIn("refresh_token", tokens)

        info = json.loads(self.client.get(
            "/sso/userinfo/",
            headers={"authorization": f"Bearer {tokens['access_token']}"},
        ).content)
        self.assertEqual(info["ff_uid"], "8390224792")
        self.assertEqual(info["country"], "NG")
        self.assertNotIn("email", info, "email was never toggled on for this org")

        refreshed = self.client.post("/sso/token/", data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.app.client_id,
            "client_secret": self.secret,
        })
        self.assertEqual(refreshed.status_code, 200, refreshed.content)

    def test_userinfo_carries_the_pairwise_sub_not_the_user_id(self):
        """The privacy property has to hold on the real wire format, not just in the
        helper's unit test."""
        tokens = self._exchange()
        info = json.loads(self.client.get(
            "/sso/userinfo/",
            headers={"authorization": f"Bearer {tokens['access_token']}"},
        ).content)
        self.assertNotEqual(str(info["sub"]), str(self.user.pk))
        self.assertEqual(len(info["sub"]), 64, "expected a sha256 hex digest")

    def test_authorization_code_cannot_be_replayed(self):
        self._exchange()
        replay = self.client.post("/sso/token/", data={
            "grant_type": "authorization_code",
            "code": "e2e-code",
            "redirect_uri": "https://partner.test/cb",
            "client_id": self.app.client_id,
            "client_secret": self.secret,
            "code_verifier": VERIFIER,
        })
        self.assertEqual(replay.status_code, 400)

    def test_revoking_kills_the_refresh_token(self):
        tokens = self._exchange()
        self.client.post("/sso/revoke_token/", data={
            "token": tokens["refresh_token"],
            "client_id": self.app.client_id,
            "client_secret": self.secret,
        })
        resp = self.client.post("/sso/token/", data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.app.client_id,
            "client_secret": self.secret,
        })
        self.assertEqual(resp.status_code, 400)

    def test_turning_a_toggle_off_stops_the_data_at_the_next_userinfo_call(self):
        """Revocation of a FIELD, not of the whole grant. AFC withdrawing a permission
        has to take effect on a token that was already issued."""
        tokens = self._exchange()
        self.app.share_freefire_uid = False
        self.app.save()
        info = json.loads(self.client.get(
            "/sso/userinfo/",
            headers={"authorization": f"Bearer {tokens['access_token']}"},
        ).content)
        self.assertNotIn("ff_uid", info)
