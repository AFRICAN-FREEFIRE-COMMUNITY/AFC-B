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

    # ── the age rule, removed by the owner on 2026-08-30 ─────────────────────────────
    # These three used to assert that a minor, an unknown date of birth and a missing
    # profile row each cost the player their email claim. They passed for months while the
    # feature was broken, because each of them CREATED a date of birth first. Production
    # has none: 0 of 6,780 profile rows, so every player was treated as a minor and no
    # partner ever received an email address.
    #
    # THE REPLACEMENTS BELOW ARE WRITTEN FROM THE PRODUCTION SHAPE, which is the whole
    # lesson: no date of birth anywhere, because nothing collects one.

    def test_a_player_with_NO_date_of_birth_still_releases_their_email(self):
        """The reported bug, as one assertion. This is every player on the platform: the
        field is read by claims.py and written by nothing, so nobody has one."""
        UserProfile.objects.filter(user=self.user).update(date_of_birth=None)
        claims = build_claims(self.user, self.app, ALL_SCOPES)
        self.assertIn("email", claims)
        self.assertEqual(claims["email"], self.user.email)

    def test_a_player_with_NO_profile_row_still_releases_their_email(self):
        """UserProfile is a plain FK, not a OneToOne, so a player can have no row at all.
        That used to fail closed too."""
        UserProfile.objects.filter(user=self.user).delete()
        self.assertIn("email", build_claims(self.user, self.app, ALL_SCOPES))

    def test_a_minor_ALSO_releases_email_now_and_that_is_the_decision(self):
        """Recorded deliberately rather than left implicit. AFC holds no age signal, so it
        enforces no age rule; the player's own consent is what releases the address. If a
        date of birth is ever collected, put "email" back in CONTACT_SCOPES and this test
        is the one to invert."""
        profile = UserProfile.objects.get(user=self.user)
        profile.date_of_birth = timezone.now().date() - datetime.timedelta(days=365 * 15)
        profile.save()
        self.assertIn("email", build_claims(self.user, self.app, ALL_SCOPES))

    def test_the_age_machinery_is_still_wired_for_a_future_contact_scope(self):
        """CONTACT_SCOPES is empty, not deleted. Putting a scope back in it must withhold
        that scope again, so the mechanism cannot rot while it is unused."""
        from unittest.mock import patch

        UserProfile.objects.filter(user=self.user).update(date_of_birth=None)
        with patch("afc_sso.claims.CONTACT_SCOPES", frozenset({"email"})):
            self.assertNotIn("email", build_claims(self.user, self.app, ALL_SCOPES))
        self.assertIn("email", build_claims(self.user, self.app, ALL_SCOPES))

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

    def test_profile_releases_an_absolute_avatar_url(self):
        """Owner enabled avatar sharing 2026-08-03. It MUST be absolute: claims are built
        without a Django request, and a bare /media/ path would resolve against the
        partner's own domain and 404 for every player."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        profile = UserProfile.objects.get(user=self.user)
        profile.profile_pic = SimpleUploadedFile("a.jpg", b"x", content_type="image/jpeg")
        profile.save()

        picture = build_claims(self.user, self.app, ALL_SCOPES)["picture"]
        self.assertTrue(picture.startswith("http"), picture)
        self.assertIn("/media/", picture)

    def test_a_player_with_no_avatar_omits_the_claim_entirely(self):
        """Absent, not null: a partner must be able to treat every claim as optional."""
        self.assertNotIn("picture", build_claims(self.user, self.app, ALL_SCOPES))
