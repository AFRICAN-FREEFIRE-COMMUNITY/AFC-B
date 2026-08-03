"""THE security test of this feature. A field is released only if ALL FOUR gates agree:
AFC's toggle, the requested scope, the player's consent, and AFC's own visibility rules.
Each test below removes exactly one gate and proves the field disappears."""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from oauth2_provider.models import get_application_model

from afc_auth.models import UserProfile
from afc_sso.claims import build_claims

Application = get_application_model()
User = get_user_model()

ALL_SCOPES = {
    "openid", "profile", "email", "afc.freefire",
    "afc.team", "afc.history", "afc.stats", "afc.ranking", "afc.standing",
}


class ClaimGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="claimplayer", email="claim@afc.test", password="x",
            country="NG", uid="8390224792",
        )
        self.user.stats_visible = True
        self.user.save()
        UserProfile.objects.create(
            user=self.user, date_of_birth=datetime.date(1995, 5, 5)
        )
        self.app = Application.objects.create(
            name="Partner", user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            share_profile=True, share_email=True, share_freefire_uid=True,
            share_stats=True,
        )

    def test_all_gates_open_releases_the_field(self):
        claims = build_claims(self.user, self.app, ALL_SCOPES)
        self.assertEqual(claims["ff_uid"], "8390224792")

    def test_gate_1_afc_toggle_off_removes_the_field(self):
        self.app.share_freefire_uid = False
        self.app.save()
        self.assertNotIn("ff_uid", build_claims(self.user, self.app, ALL_SCOPES))

    def test_gate_2_scope_not_requested_removes_the_field(self):
        claims = build_claims(self.user, self.app, {"openid", "profile"})
        self.assertNotIn("ff_uid", claims)

    def test_gate_3_scope_not_consented_removes_the_field(self):
        """granted_scopes IS the consent record: the token only ever carries scopes the
        player approved, so an absent scope here means absent consent."""
        claims = build_claims(self.user, self.app, {"openid"})
        self.assertNotIn("ff_uid", claims)
        self.assertNotIn("country", claims)

    def test_gate_4_stats_visible_false_removes_stats(self):
        self.user.stats_visible = False
        self.user.save()
        self.assertNotIn("afc_stats", build_claims(self.user, self.app, ALL_SCOPES))

    def test_gate_4_minors_never_get_email(self):
        """Hard rule from the spec: contact data for an under-18 is refused regardless
        of toggles and consent."""
        profile = UserProfile.objects.get(user=self.user)
        profile.date_of_birth = timezone.now().date() - datetime.timedelta(days=365 * 15)
        profile.save()
        claims = build_claims(self.user, self.app, ALL_SCOPES)
        self.assertNotIn("email", claims)
        self.assertIn("ff_uid", claims, "only contact data is withheld, not everything")

    def test_unknown_date_of_birth_is_treated_as_a_minor(self):
        """Fail closed: no DOB means we cannot prove they are an adult."""
        UserProfile.objects.filter(user=self.user).update(date_of_birth=None)
        self.assertNotIn("email", build_claims(self.user, self.app, ALL_SCOPES))

    def test_missing_profile_row_is_treated_as_a_minor(self):
        """UserProfile is a plain FK, not a OneToOne, so a player can have no profile row
        at all. That must fail closed the same way a null date of birth does."""
        UserProfile.objects.filter(user=self.user).delete()
        self.assertNotIn("email", build_claims(self.user, self.app, ALL_SCOPES))

    def test_suspended_account_releases_nothing_but_its_standing(self):
        """A suspended player is exactly the case a partner needs to be able to see, and
        exactly the case where nothing else should leave AFC."""
        self.user.status = "suspended"
        self.user.save()
        # Standing is still a gate-1 toggle: an org that was never granted it sees nothing
        # at all here, which is why this test has to grant it explicitly.
        self.app.share_standing = True
        self.app.save()
        claims = build_claims(self.user, self.app, ALL_SCOPES)
        self.assertNotIn("ff_uid", claims)
        self.assertNotIn("email", claims)
        self.assertFalse(claims["afc_standing"]["in_good_standing"])

    def test_denylisted_fields_never_appear(self):
        claims = build_claims(self.user, self.app, ALL_SCOPES)
        for forbidden in ("password", "ip_country", "role", "status", "whatsapp_number"):
            self.assertNotIn(forbidden, claims)
