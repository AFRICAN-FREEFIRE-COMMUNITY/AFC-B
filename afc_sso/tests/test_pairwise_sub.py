"""The P0 spike proved the library gives every app the SAME sub (the raw user id).
That lets two partners join their databases on AFC identity behind the player's back.
Each app must see a different, stable, opaque identifier."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from oauth2_provider.models import get_application_model

from afc_sso.validators import pairwise_sub

Application = get_application_model()
User = get_user_model()


class PairwiseSubTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="pairwise", email="pairwise@afc.test", password="x"
        )
        self.a = self._app("Org A")
        self.b = self._app("Org B")

    def _app(self, name):
        return Application.objects.create(
            name=name, user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
        )

    def test_two_apps_see_different_subs(self):
        self.assertNotEqual(pairwise_sub(self.user, self.a), pairwise_sub(self.user, self.b))

    def test_sub_is_stable_across_calls(self):
        self.assertEqual(pairwise_sub(self.user, self.a), pairwise_sub(self.user, self.a))

    def test_sub_does_not_leak_the_user_id(self):
        self.assertNotIn(str(self.user.user_id), pairwise_sub(self.user, self.a))

    def test_sub_survives_a_display_name_change(self):
        before = pairwise_sub(self.user, self.a)
        self.a.display_name = "Renamed Org"
        self.a.save()
        self.assertEqual(before, pairwise_sub(self.user, self.a))

    def test_two_players_at_the_same_org_see_different_subs(self):
        """The other half of the property: pairwise must not collapse distinct players
        into one identifier at a single partner."""
        other = User.objects.create_user(
            username="pairwise2", email="pairwise2@afc.test", password="x"
        )
        self.assertNotEqual(pairwise_sub(self.user, self.a), pairwise_sub(other, self.a))
