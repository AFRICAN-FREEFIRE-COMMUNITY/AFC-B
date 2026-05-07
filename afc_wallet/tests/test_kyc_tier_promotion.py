"""KYC tier promotion tests — TIER_LITE iff both whatsapp + discord verified."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_wallet.models import KYCTier, KYCTierLevel
from afc_wallet.services import recompute_kyc_tier


User = get_user_model()


class KYCTierPromotionTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="kyctest",
            email="k@example.com",
            password="x",
            full_name="K",
            country="NG",
        )

    def test_default_tier_is_tier_0(self):
        kyc = recompute_kyc_tier(self.user)
        self.assertEqual(kyc.tier, KYCTierLevel.TIER_0)

    def test_only_whatsapp_verified_stays_tier_0(self):
        KYCTier.objects.create(
            user=self.user,
            whatsapp_number="+2348012345678",
            whatsapp_verified_at=timezone.now(),
        )
        kyc = recompute_kyc_tier(self.user)
        self.assertEqual(kyc.tier, KYCTierLevel.TIER_0)

    def test_only_discord_verified_stays_tier_0(self):
        KYCTier.objects.create(
            user=self.user,
            discord_user_id="999",
            discord_linked_at=timezone.now(),
        )
        kyc = recompute_kyc_tier(self.user)
        self.assertEqual(kyc.tier, KYCTierLevel.TIER_0)

    def test_both_verified_promotes_to_tier_lite(self):
        KYCTier.objects.create(
            user=self.user,
            whatsapp_number="+2348012345678",
            whatsapp_verified_at=timezone.now(),
            discord_user_id="999",
            discord_linked_at=timezone.now(),
        )
        kyc = recompute_kyc_tier(self.user)
        self.assertEqual(kyc.tier, KYCTierLevel.TIER_LITE)

    def test_recompute_is_idempotent(self):
        KYCTier.objects.create(
            user=self.user,
            whatsapp_verified_at=timezone.now(),
            discord_linked_at=timezone.now(),
        )
        a = recompute_kyc_tier(self.user)
        b = recompute_kyc_tier(self.user)
        self.assertEqual(a.tier, KYCTierLevel.TIER_LITE)
        self.assertEqual(b.tier, KYCTierLevel.TIER_LITE)

    def test_revoking_one_drops_back_to_tier_0(self):
        kyc = KYCTier.objects.create(
            user=self.user,
            whatsapp_verified_at=timezone.now(),
            discord_linked_at=timezone.now(),
        )
        recompute_kyc_tier(self.user)
        # Revoke whatsapp.
        kyc.whatsapp_verified_at = None
        kyc.save(update_fields=["whatsapp_verified_at"])
        kyc = recompute_kyc_tier(self.user)
        self.assertEqual(kyc.tier, KYCTierLevel.TIER_0)
