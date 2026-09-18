"""
afc_wager.notify - telling the player what happened to their money, through the channels AFC
already has: the bell (afc_auth.Notifications), email (afc_auth.views.send_email with the
hand-written catalogue), WhatsApp (the approved broadcast template through
afc_whatsapp.tasks.queue_template) and a Discord DM (afc_support.notify.send_discord_dm).

WHICH EVENT GOES WHERE
    bell      every event below
    email     money: won, refunded, withdrawal paid / rejected, adjustment, frozen
    whatsapp  won, withdrawal paid (the two a player wants on their phone at once)
    discord   won, withdrawal paid

BEST EFFORT, ALWAYS: every send runs on a daemon thread and swallows its own failures. The ledger
line is the truth; a notification that did not go out never undoes a payout. Same rule as
afc_partner_apply/emails.py, and the same reason: send_email is synchronous SMTP.

Amounts arrive in kobo and are written as naira here (no coins, inbox #33).
"""
import logging
import threading

from django.conf import settings

from afc_auth.models import Notifications

logger = logging.getLogger(__name__)


def naira(kobo):
    """₦12,345.67 from kobo, for a sentence."""
    kobo = int(kobo or 0)
    return f"₦{kobo // 100:,}.{kobo % 100:02d}"


def _lang(user):
    return getattr(user, "language", None) or "en"


def _frontend():
    return (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")


def _bg(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - never blocks a money write
            logger.warning("wager notify: %s failed: %s", getattr(fn, "__name__", fn), exc)
    threading.Thread(target=run, daemon=True).start()


# ── the bell ────────────────────────────────────────────────────────────────────────────────────
_BELL = {
    "wager_active": {
        "en": ("Your stake is in", "Your {amount} stake on {market} is confirmed and in the pool."),
        "fr": ("Votre mise est enregistrée", "Votre mise de {amount} sur {market} est confirmée et dans la cagnotte."),
        "pt": ("A sua aposta está registada", "A sua aposta de {amount} em {market} está confirmada e na bolsa."),
    },
    "wager_cancelled": {
        "en": ("Wager cancelled", "You cancelled your stake on {market}. {amount} is in your Winnings."),
        "fr": ("Pari annulé", "Vous avez annulé votre mise sur {market}. {amount} est dans vos Gains."),
        "pt": ("Aposta cancelada", "Cancelou a sua aposta em {market}. {amount} está nos seus Ganhos."),
    },
    "market_locked": {
        "en": ("Market locked", "{market} has locked. No more stakes; the result settles it."),
        "fr": ("Marché verrouillé", "{market} est verrouillé. Plus de mises ; le résultat tranchera."),
        "pt": ("Mercado bloqueado", "{market} bloqueou. Sem mais apostas; o resultado decide."),
    },
    "wager_won": {
        "en": ("You won", "{market}: you won {amount}. It is in your Winnings."),
        "fr": ("Vous avez gagné", "{market} : vous avez gagné {amount}. C'est dans vos Gains."),
        "pt": ("Ganhou", "{market}: ganhou {amount}. Está nos seus Ganhos."),
    },
    "wager_lost": {
        "en": ("Not this time", "{market} settled and your stake did not win."),
        "fr": ("Pas cette fois", "{market} est réglé et votre mise n'a pas gagné."),
        "pt": ("Não foi desta", "{market} foi liquidado e a sua aposta não ganhou."),
    },
    "wager_refunded": {
        "en": ("Stake returned", "{market}: {reason} Your {amount} is back in your Winnings."),
        "fr": ("Mise remboursée", "{market} : {reason} Vos {amount} sont de retour dans vos Gains."),
        "pt": ("Aposta devolvida", "{market}: {reason} Os seus {amount} voltaram aos seus Ganhos."),
    },
    "withdrawal_requested": {
        "en": ("Withdrawal requested", "Your withdrawal of {amount} is with AFC for approval."),
        "fr": ("Retrait demandé", "Votre retrait de {amount} attend l'approbation d'AFC."),
        "pt": ("Levantamento pedido", "O seu levantamento de {amount} aguarda a aprovação da AFC."),
    },
    "withdrawal_approved": {
        "en": ("Withdrawal approved", "Your withdrawal of {amount} is on its way to your bank."),
        "fr": ("Retrait approuvé", "Votre retrait de {amount} est en route vers votre banque."),
        "pt": ("Levantamento aprovado", "O seu levantamento de {amount} está a caminho do seu banco."),
    },
    "withdrawal_paid": {
        "en": ("Paid out", "{amount} has been sent to your bank account."),
        "fr": ("Versé", "{amount} ont été envoyés sur votre compte bancaire."),
        "pt": ("Pago", "{amount} foram enviados para a sua conta bancária."),
    },
    "withdrawal_rejected": {
        "en": ("Withdrawal not approved", "Your withdrawal of {amount} was not approved: {reason} The money is back in your Winnings."),
        "fr": ("Retrait refusé", "Votre retrait de {amount} n'a pas été approuvé : {reason} L'argent est de retour dans vos Gains."),
        "pt": ("Levantamento recusado", "O seu levantamento de {amount} não foi aprovado: {reason} O dinheiro voltou aos seus Ganhos."),
    },
    "adjustment": {
        "en": ("Winnings adjusted", "AFC adjusted your Winnings by {amount}: {reason}"),
        "fr": ("Gains ajustés", "AFC a ajusté vos Gains de {amount} : {reason}"),
        "pt": ("Ganhos ajustados", "A AFC ajustou os seus Ganhos em {amount}: {reason}"),
    },
    "frozen": {
        "en": ("Winnings frozen", "Your Winnings are frozen: {reason} Contact support."),
        "fr": ("Gains gelés", "Vos Gains sont gelés : {reason} Contactez le support."),
        "pt": ("Ganhos congelados", "Os seus Ganhos estão congelados: {reason} Contacte o suporte."),
    },
    "unfrozen": {
        "en": ("Winnings unfrozen", "Your Winnings are available again."),
        "fr": ("Gains dégelés", "Vos Gains sont de nouveau disponibles."),
        "pt": ("Ganhos descongelados", "Os seus Ganhos estão de novo disponíveis."),
    },
}


def bell(user, key, *, target="/winnings", **fmt):
    """One Notifications row in the user's language, with a deep link."""
    lang = _lang(user)[:2]
    title, body = _BELL[key].get(lang) or _BELL[key]["en"]
    try:
        Notifications.objects.create(
            user=user, notification_type="wager", title=title.format(**fmt), message=body.format(**fmt),
            target_type="custom", target_id=target,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("wager notify: bell %s for %s failed: %s", key, user.pk, exc)


# ── email ───────────────────────────────────────────────────────────────────────────────────────
def _email(user, template, subject_key, body_keys, **fmt):
    from afc_auth.email_i18n import copy_for, subject_for
    from afc_auth.views import _email_shell, send_email

    lang = _lang(user)
    copy = copy_for(template, lang)
    subject = subject_for(subject_key, lang, **fmt)
    paragraphs = []
    for key in body_keys:
        sentence = copy.get(key)
        if not sentence:
            continue
        try:
            paragraphs.append(sentence.format(**fmt))
        except Exception:  # noqa: BLE001
            paragraphs.append(sentence)
    heading = copy.get("heading", subject)
    cta = copy.get("cta", "")
    body_html = "".join(
        f'<tr><td style="padding:0 44px 14px;font-size:15px;line-height:1.6;color:#aab5ae;">{p}</td></tr>'
        for p in paragraphs
    )
    cta_html = (
        f'<tr><td style="padding:6px 44px 30px;"><a href="{_frontend()}/winnings" '
        f'style="display:inline-block;padding:12px 22px;border-radius:10px;background:#34d27b;'
        f'color:#0b1410;font-weight:700;text-decoration:none;">{cta}</a></td></tr>'
        if cta else ""
    )
    inner = (
        f'<tr><td style="padding:38px 44px 14px;"><div style="font-size:21px;font-weight:700;'
        f'color:#ffffff;">{heading}</div></td></tr>{body_html}{cta_html}'
    )

    def deliver():
        send_email(user.email, subject, _email_shell(inner, "green"), language=lang, prelocalized=True)

    _bg(deliver)


# ── WhatsApp and Discord ────────────────────────────────────────────────────────────────────────
def _whatsapp(user, sentence):
    from afc_auth.models import canonical_profile
    from afc_whatsapp.tasks import queue_template

    template = (getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE", "") or "").strip()
    profile = canonical_profile(user)
    number = (getattr(profile, "whatsapp_number", "") or "") if profile else ""
    if not template or not number:
        return
    name = " ".join(str(user.username or "").split()) or "-"
    queue_template(number, template, getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE_LANG", "en") or "en",
                   body_params=[name, " ".join(sentence.split())], user=user, context="wager")


def _discord(user, sentence):
    from afc_support.notify import send_discord_dm

    if getattr(user, "discord_id", None):
        send_discord_dm(user.discord_id, sentence)


# ── the events ──────────────────────────────────────────────────────────────────────────────────
def wager_activated(wager):
    bell(wager.user, "wager_active", target=f"/wagers/{wager.market.slug}",
         amount=naira(wager.total_stake_kobo), market=wager.market.title)


def wager_refunded_late(wager):
    bell(wager.user, "wager_refunded", amount=naira(wager.refund_kobo), market=wager.market.title,
         reason="Your payment arrived after the market closed.")
    _email(wager.user, "wager_refunded", "wager_refunded", ("intro", "next"),
           market=wager.market.title, amount=naira(wager.refund_kobo),
           reason="your payment arrived after the market had closed")


def wager_cancelled(wager):
    bell(wager.user, "wager_cancelled", amount=naira(wager.refund_kobo), market=wager.market.title)


def market_locked(market):
    from .models import Wager
    users = {w.user for w in Wager.objects.filter(market=market, status=Wager.ACTIVE).select_related("user")}
    for user in users:
        bell(user, "market_locked", target=f"/wagers/{market.slug}", market=market.title)


def market_settled(market, settlement):
    from .models import Wager
    wagers = Wager.objects.filter(market=market).select_related("user")
    for w in wagers:
        if w.status == Wager.WON:
            amount = naira(w.payout_kobo)
            bell(w.user, "wager_won", amount=amount, market=market.title)
            _email(w.user, "wager_won", "wager_won", ("intro", "next"), market=market.title, amount=amount)
            sentence = f"AFC: you won {amount} on {market.title}. It is in your Winnings."
            _bg(_whatsapp, w.user, sentence)
            _bg(_discord, w.user, sentence)
        elif w.status == Wager.LOST:
            bell(w.user, "wager_lost", target=f"/wagers/{market.slug}", market=market.title)
        elif w.status == Wager.REFUNDED:
            reason = ("Nobody staked on the winning option, so every stake goes back."
                      if settlement.resolution == "VOID_NO_WINNER"
                      else "Everyone was on the same side, so every stake goes back.")
            bell(w.user, "wager_refunded", amount=naira(w.refund_kobo), market=market.title, reason=reason)
            _email(w.user, "wager_refunded", "wager_refunded", ("intro", "next"),
                   market=market.title, amount=naira(w.refund_kobo), reason=reason.lower().rstrip("."))


def market_voided(market, settlement):
    from .models import Wager
    for w in Wager.objects.filter(market=market, status=Wager.REFUNDED).select_related("user"):
        reason = f"The market was voided ({market.void_reason})."
        bell(w.user, "wager_refunded", amount=naira(w.refund_kobo), market=market.title, reason=reason)
        _email(w.user, "wager_refunded", "wager_refunded", ("intro", "next"),
               market=market.title, amount=naira(w.refund_kobo), reason=f"the market was voided: {market.void_reason}")


def withdrawal_requested(wd):
    bell(wd.user, "withdrawal_requested", amount=naira(wd.amount_kobo))


def withdrawal_approved(wd):
    if wd.status == "PAID":
        withdrawal_paid(wd)
        return
    bell(wd.user, "withdrawal_approved", amount=naira(wd.amount_kobo))


def withdrawal_paid(wd):
    amount = naira(wd.amount_kobo)
    bell(wd.user, "withdrawal_paid", amount=amount)
    _email(wd.user, "wager_withdrawal_paid", "wager_withdrawal_paid", ("intro", "next"), amount=amount)
    sentence = f"AFC: {amount} has been sent to your bank account."
    _bg(_whatsapp, wd.user, sentence)
    _bg(_discord, wd.user, sentence)


def withdrawal_rejected(wd):
    bell(wd.user, "withdrawal_rejected", amount=naira(wd.amount_kobo), reason=wd.reject_reason)
    _email(wd.user, "wager_withdrawal_rejected", "wager_withdrawal_rejected", ("intro", "next"),
           amount=naira(wd.amount_kobo), reason=wd.reject_reason)


def adjustment_made(adj):
    sign = "+" if adj.direction == "CREDIT" else "-"
    amount = f"{sign}{naira(adj.amount_kobo)}"
    bell(adj.user, "adjustment", amount=amount, reason=adj.reason)
    _email(adj.user, "wager_adjustment", "wager_adjustment", ("intro", "next"), amount=amount, reason=adj.reason)


def winnings_frozen(account):
    bell(account.user, "frozen", reason=account.frozen_reason)
    _email(account.user, "wager_frozen", "wager_frozen", ("intro", "next"), reason=account.frozen_reason)


def winnings_unfrozen(account):
    bell(account.user, "unfrozen")
