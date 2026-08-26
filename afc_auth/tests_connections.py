"""
Tests for the CONNECTED ACCOUNTS layer (owner 2026-08-26).

WHY the feature exists: a player can link outside accounts (Discord, Google, v-ent.co) to their AFC
account, see them in one place, and cut any of them off. Before this, Discord lived in four columns
on User and Google was sign-in only with nothing stored at all.

WHAT THIS FILE COVERS: the model's two DB-level uniqueness rules, and the provider registry.

The uniqueness rules are load-bearing, so they are proven against the real (MySQL) test database
rather than assumed. "One outside account backs exactly one AFC account" is what stops a per-event
required-connection rule from being defeated by linking one Discord account to five AFC accounts.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from afc_auth.connections import enabled_providers, get_provider, is_enabled
from afc_auth.models import ConnectedAccount, User


def _user(username):
    return User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="player", password="x", country="Nigeria",
    )


class ConnectedAccountConstraintTests(TestCase):
    def setUp(self):
        self.a = _user("linka")
        self.b = _user("linkb")

    def test_one_outside_account_cannot_back_two_afc_accounts(self):
        ConnectedAccount.objects.create(user=self.a, provider="discord", provider_user_id="777")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedAccount.objects.create(
                    user=self.b, provider="discord", provider_user_id="777"
                )

    def test_one_provider_per_user(self):
        ConnectedAccount.objects.create(user=self.a, provider="discord", provider_user_id="777")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedAccount.objects.create(
                    user=self.a, provider="discord", provider_user_id="888"
                )

    def test_same_provider_user_id_on_a_different_provider_is_fine(self):
        ConnectedAccount.objects.create(user=self.a, provider="discord", provider_user_id="777")
        ConnectedAccount.objects.create(user=self.a, provider="google", provider_user_id="777")
        self.assertEqual(ConnectedAccount.objects.filter(user=self.a).count(), 2)


class ProviderRegistryTests(TestCase):
    """A provider with no credentials configured must be invisible EVERYWHERE: not on the profile
    page, not in the event-requirement picker, not enforceable by the registration gate. That is
    what lets v-ent.co ship dark today and light up the day credentials land in the server
    environment, with no code change and no second deploy."""

    @override_settings(VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
    def test_provider_without_credentials_is_not_enabled(self):
        self.assertFalse(is_enabled("vent"))
        self.assertNotIn("vent", [p.slug for p in enabled_providers()])

    @override_settings(
        VENT_CLIENT_ID="abc", VENT_CLIENT_SECRET="shh", VENT_ISSUER="https://v-ent.co"
    )
    def test_provider_with_credentials_is_enabled(self):
        self.assertTrue(is_enabled("vent"))
        self.assertIn("vent", [p.slug for p in enabled_providers()])

    def test_unknown_slug_returns_none_rather_than_raising(self):
        self.assertIsNone(get_provider("myspace"))

    @override_settings(DISCORD_CLIENT_ID="id", DISCORD_CLIENT_SECRET="secret")
    def test_discord_normalizes_a_profile_to_the_house_shape(self):
        provider = get_provider("discord")
        out = provider.normalize({"id": "777", "username": "ace", "avatar": "abc"})
        self.assertEqual(out["provider_user_id"], "777")
        self.assertEqual(out["username"], "ace")
        self.assertIn("777", out["avatar_url"])

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
    def test_google_normalizes_the_stable_subject_id(self):
        provider = get_provider("google")
        out = provider.normalize({"sub": "g-1", "email": "P@Gmail.com", "name": "Player"})
        self.assertEqual(out["provider_user_id"], "g-1")
        self.assertEqual(out["email"], "p@gmail.com", "email is normalised to lower case")
