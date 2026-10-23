"""afc_referrals.notify - the bell notification when somebody earns a referral prize.

In the recipient's language (User.language, blank = English), written by hand in all three (the site's
rule for user-facing text). The link opens their profile's Referrals card, where the prize and its status
are listed. Mirrors afc_partner_apply/notify.py: it logs and never raises, because it runs inside an
award, which runs inside other features' saves.
Caller: engine.give.
"""
import logging

from afc_auth.models import Notifications

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "referral_reward"
PROFILE_LINK = "/profile#referrals"

TEXT = {
    "en": {"title": "You earned a referral prize",
           "body": "Your referrals in {program} earned you a prize: {prize}. See it in the Referrals section of your profile."},
    "fr": {"title": "Vous avez gagné une récompense de parrainage",
           "body": "Vos parrainages dans {program} vous ont rapporté une récompense : {prize}. Retrouvez-la dans la section Parrainages de votre profil."},
    "pt": {"title": "Ganhou um prémio de indicação",
           "body": "As suas indicações em {program} valeram-lhe um prémio: {prize}. Veja-o na secção Indicações do seu perfil."},
}
WELCOME_TEXT = {
    "en": {"title": "Your welcome prize is here",
           "body": "Thanks for joining through {program}. Your welcome prize: {prize}. See it in the Referrals section of your profile."},
    "fr": {"title": "Votre récompense de bienvenue est arrivée",
           "body": "Merci de nous avoir rejoints via {program}. Votre récompense de bienvenue : {prize}. Retrouvez-la dans la section Parrainages de votre profil."},
    "pt": {"title": "O seu prémio de boas-vindas chegou",
           "body": "Obrigado por se juntar através de {program}. O seu prémio de boas-vindas: {prize}. Veja-o na secção Indicações do seu perfil."},
}


def prize_label(prize):
    """A short, language-neutral description of what the prize is (names and numbers, not sentences)."""
    if prize.prize_type in ("shop_item", "diamonds") and prize.product_variant_id:
        variant = prize.product_variant
        return variant.title or variant.product.name
    if prize.prize_type == "coupon":
        value = prize.coupon_discount_value or 0
        return f"{value:g}%" if prize.coupon_discount_type == "percent" else f"${value:g}"
    if prize.prize_type == "cash" and prize.cash_amount is not None:
        return f"${prize.cash_amount:g}"
    return prize.custom_text or prize.prize_type


def notify_reward(reward):
    try:
        lang = (getattr(reward.user, "language", "") or "en")
        table = WELCOME_TEXT if reward.referral_id else TEXT
        text = table.get(lang, table["en"])
        fmt = {"program": reward.program.name, "prize": prize_label(reward.prize)}
        return Notifications.objects.create(
            user=reward.user, notification_type=NOTIFICATION_TYPE,
            title=text["title"], message=text["body"].format(**fmt),
            target_type="custom", target_id=PROFILE_LINK,
        )
    except Exception as exc:  # noqa: BLE001 - see the header
        logger.warning("referrals: notification for reward %s failed: %s", reward.pk, exc)
        return None
