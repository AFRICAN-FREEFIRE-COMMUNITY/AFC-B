"""
google_auth stores the Google `sub` and prefers it over the email match.

THE DEFECT THIS CLOSES: google_auth found the AFC user BY EMAIL and stored nothing about the Google
account. A player who changed their Gmail address therefore became a different person to AFC, and a
new AFC account created under the new address would absorb their Google sign-in. `sub` is stable for
the life of the Google account.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections_google
"""
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from afc_auth.models import ConnectedAccount, User


def _claims(sub="google-sub-1", email="gplayer@gmail.com", name="Player One"):
    return {"sub": sub, "email": email, "email_verified": True, "name": name, "picture": ""}


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
class GoogleLinkOnSignInTests(TestCase):
    def _sign_in(self, claims):
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
            return Client().post(
                "/auth/google/", {"credential": "fake"}, content_type="application/json",
            )

    def test_sign_in_records_the_google_link(self):
        resp = self._sign_in(_claims())
        self.assertIn(resp.status_code, (200, 201), resp.content)
        row = ConnectedAccount.objects.get(provider="google", provider_user_id="google-sub-1")
        self.assertEqual(row.email, "gplayer@gmail.com")

    def test_a_changed_email_still_resolves_to_the_same_afc_account(self):
        self._sign_in(_claims())
        first_user_id = ConnectedAccount.objects.get(provider="google").user_id
        before = User.objects.count()

        self._sign_in(_claims(email="gplayer-new@gmail.com"))

        self.assertEqual(User.objects.count(), before, "no second account may be created")
        self.assertEqual(ConnectedAccount.objects.get(provider="google").user_id, first_user_id)

    def test_an_existing_email_matched_account_gains_a_link_on_next_sign_in(self):
        """The ~thousands of accounts that predate this change keep working, and pick up their row
        the first time they sign in again."""
        existing = User.objects.create(
            username="legacygoogle", email="legacy@gmail.com", full_name="Legacy",
            role="player", country="Nigeria", is_active=True, status="active",
        )
        existing.set_unusable_password()
        existing.save()

        self._sign_in(_claims(sub="legacy-sub", email="legacy@gmail.com"))

        row = ConnectedAccount.objects.get(provider="google", provider_user_id="legacy-sub")
        self.assertEqual(row.user_id, existing.user_id)

    def test_a_link_write_failure_does_not_break_the_sign_in(self):
        """Best-effort by design: a player must never be locked out because a bookkeeping row
        could not be written."""
        with patch(
            "afc_auth.connections.links.link_account", side_effect=RuntimeError("db hiccup")
        ):
            resp = self._sign_in(_claims(sub="resilient-sub", email="resilient@gmail.com"))
        self.assertIn(resp.status_code, (200, 201), resp.content)
