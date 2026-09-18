"""
afc_partner_apply.notify - the two channels beside email that tell an applicant they were decided.

WHY (owner 2026-09-18): "immediately a partner is approved, they should get a email and
notification ... Once denied, they get a reply and with a reason also that states why they were
denied." The email (emails.py) is the guaranteed channel and carries the credentials link and the
reason. This module adds the two AFC has for a person it can identify:

    1. THE IN-APP BELL. An organisation applies with a contact email, and on the day this was
       written two of the three real applicants had an AFC account under exactly that address.
       If an active account matches contact_email, it gets a Notifications row, in the language
       of the ACCOUNT (the person reads the bell where they read the site), carrying the decision
       and, for a rejection or a change request, the owner's reason.

    2. WHATSAPP. The form collects a WhatsApp number in E.164 precisely so that "someone could get
       messaged on it" (the model's own words, owner 2026-08-04). One message per decision through
       the approved broadcast template ({{1}} the contact name, {{2}} the sentence), which is the
       only approved template that carries free text. Off when WHATSAPP_BROADCAST_TEMPLATE is
       blank, exactly like the broadcast sender. It costs one Meta conversation per decision and
       never carries a credential: it says what happened and where to look.

NEITHER CAN BLOCK OR FAIL A DECISION. Both are wrapped so an exception here is logged and
forgotten: the decision has already been saved when this runs, and the email is the channel the
applicant was promised. Same rule as emails.py.

CALLED BY afc_partner_apply/views_admin.py decide_application, right after the matching emails.*
send, for approve, reject and request_changes.
"""
import logging

from django.conf import settings

from afc_auth.models import Notifications, User

logger = logging.getLogger(__name__)

# The decision in a sentence, per language. {organisation} and {reference} identify the
# application; {email} says where the details went; {note} is the owner's reason, written to be
# read by them (never the internal note, which no channel here ever carries).
_COPY = {
    "approved": {
        "en": {
            "title": "Your AFC partner application is approved",
            "body": "The application for {organisation} ({reference}) is approved. We have emailed {email} a one-time link to collect your credentials, plus everything you need to integrate.",
        },
        "fr": {
            "title": "Votre demande de partenariat AFC est approuvée",
            "body": "La demande de {organisation} ({reference}) est approuvée. Nous avons envoyé à {email} un lien à usage unique pour récupérer vos identifiants, ainsi que tout ce qu'il faut pour l'intégration.",
        },
        "pt": {
            "title": "A sua candidatura a parceiro AFC foi aprovada",
            "body": "A candidatura de {organisation} ({reference}) foi aprovada. Enviámos para {email} uma ligação de utilização única para recolher as suas credenciais, e tudo o que precisa para a integração.",
        },
    },
    "rejected": {
        "en": {
            "title": "About your AFC partner application",
            "body": "We are not able to approve the application for {organisation} ({reference}). Here is why: {note}. You are welcome to apply again if what we raised changes; the full reply is in your email at {email}.",
        },
        "fr": {
            "title": "Au sujet de votre demande de partenariat AFC",
            "body": "Nous ne sommes pas en mesure d'approuver la demande de {organisation} ({reference}). En voici la raison : {note}. Vous pouvez déposer une nouvelle demande si le point soulevé évolue ; la réponse complète est dans votre e-mail à {email}.",
        },
        "pt": {
            "title": "Sobre a sua candidatura a parceiro AFC",
            "body": "Não nos é possível aprovar a candidatura de {organisation} ({reference}). O motivo: {note}. Pode voltar a candidatar-se se o ponto que levantámos mudar; a resposta completa está no seu e-mail em {email}.",
        },
    },
    "changes_requested": {
        "en": {
            "title": "Action needed on your AFC partner application",
            "body": "We need one thing changed on the application for {organisation} ({reference}) before we can decide: {note}. The link to edit it is in your email at {email}.",
        },
        "fr": {
            "title": "Action requise sur votre demande de partenariat AFC",
            "body": "Un point doit être modifié sur la demande de {organisation} ({reference}) avant que nous puissions décider : {note}. Le lien pour la modifier est dans votre e-mail à {email}.",
        },
        "pt": {
            "title": "Ação necessária na sua candidatura a parceiro AFC",
            "body": "É preciso alterar um ponto na candidatura de {organisation} ({reference}) antes de podermos decidir: {note}. A ligação para a editar está no seu e-mail em {email}.",
        },
    },
}

NOTIFICATION_TYPE = "partner_application"


def _lang(value):
    value = (value or "en").lower()[:2]
    return value if value in ("en", "fr", "pt") else "en"


def _copy(decision, lang, application):
    text = _COPY[decision][_lang(lang)]
    fmt = dict(
        organisation=application.organisation_name,
        reference=application.reference,
        email=application.contact_email,
        note=(application.decision_note or "").strip(),
    )
    return text["title"].format(**fmt), text["body"].format(**fmt)


def matching_account(application):
    """The active AFC account whose email is the application's contact email, or None.

    `iexact` because email case is not identity anywhere else on AFC either. A deleted or
    suspended account is not notified: its bell is not read.
    """
    if not application.contact_email:
        return None
    return (
        User.objects.filter(email__iexact=application.contact_email, status="active")
        .order_by("pk")
        .first()
    )


def notify_in_app(application, decision):
    """A Notifications row for the matching account. Returns the row, or None when nobody matched
    or the write failed (logged)."""
    try:
        user = matching_account(application)
        if user is None:
            return None
        title, body = _copy(decision, getattr(user, "language", "") or application.locale,
                            application)
        return Notifications.objects.create(
            user=user,
            notification_type=NOTIFICATION_TYPE,
            title=title,
            message=body,
            target_type="none",
        )
    except Exception as exc:  # noqa: BLE001 - see the module header: never blocks a decision
        logger.warning("partner apply: in-app notification for %s failed: %s",
                       application.reference, exc)
        return None


def notify_whatsapp(application, decision):
    """One template message to the applicant's WhatsApp number, when they gave one and the
    broadcast template is configured. Returns whatever queue_template returns (None = not sent)."""
    try:
        number = (application.contact_whatsapp or "").strip()
        template = (getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE", "") or "").strip()
        if not number or not template:
            return None
        from afc_whatsapp.tasks import queue_template

        _title, body = _copy(decision, application.locale, application)
        # Meta refuses an empty parameter and one carrying a newline; one running line each.
        name = " ".join(str(application.contact_name or "").split()) or "-"
        body = " ".join(body.split())
        return queue_template(
            number,
            template,
            getattr(settings, "WHATSAPP_BROADCAST_TEMPLATE_LANG", "en") or "en",
            body_params=[name, body],
            context=f"partner_{decision}",
        )
    except Exception as exc:  # noqa: BLE001 - see the module header: never blocks a decision
        logger.warning("partner apply: WhatsApp notice for %s failed: %s",
                       application.reference, exc)
        return None


def notify_decision(application, decision):
    """Both channels for one decision. `decision` is "approved", "rejected" or
    "changes_requested" (PartnerApplication's own status values)."""
    if decision not in _COPY:
        return
    notify_in_app(application, decision)
    notify_whatsapp(application, decision)
