"""
afc_partner_apply.emails - the four emails an applicant receives, in their own language.

WHY A MODULE RATHER THAN INLINE SENDS
    Four transitions each need the same three things: the branded shell, the applicant's language,
    and a link back into their own status page. Doing that inline in two view modules would mean
    four near-copies of the same twelve lines, and the first one to drift would be the one that
    forgot the language.

THE LANGUAGE
    Every send here uses HAND-AUTHORED copy from afc_auth.email_i18n (templates "partner_apply_*")
    in PartnerApplication.locale, the language the organisation actually filled the form in, and
    passes prelocalized=True so afc_auth.views.send_email does NOT machine-translate it. Same
    decision, same reason, as every other fixed transactional email on AFC: natural sentences that
    do not depend on a translation engine being up.

THE ONE RULE THAT MATTERS MOST HERE
    NO EMAIL EVER CARRIES A CLIENT SECRET OR AN API KEY. The approval email carries a single-use
    claim LINK; the credential itself is minted when that link is opened and shown once, on one
    page. An inbox is forever and gets forwarded; a link that expires in 72 hours and dies on first
    use is not. See afc_partner_apply/views_public.py claim_credentials.

BEST EFFORT, ALWAYS
    Every send runs on a daemon thread and swallows its own failures, exactly like
    afc_sponsors/engagements.py _notify_rejection. send_email talks to Office365 SMTP
    synchronously, and a slow or unreachable server must never hold up an owner's decision or an
    applicant's submission. The status page is the guaranteed channel; the email is the courtesy.
"""
import logging
import threading

from django.conf import settings

from .models import CLAIM_WINDOW_HOURS

logger = logging.getLogger(__name__)


def _frontend_origin():
    """Where the applicant's own pages live.

    The status and credentials pages are Next.js routes, not Django ones, so every link in these
    emails points at the FRONTEND origin, never at the API. Mirrors how afc_sso/views.py picks
    between the two configured origins, with the production value as the default because an email
    is far more likely to be read somewhere other than a developer's machine.
    """
    return (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")


def status_url(application, token):
    """The applicant's link to their own application.

    `token` is the PLAINTEXT access token, held only in memory at the moment an email is composed
    (the row stores its hash). Consumed by frontend app/(root)/partners/apply/status/page.tsx.
    """
    return f"{_frontend_origin()}/partners/apply/status?ref={application.reference}&token={token}"


def claim_url(application, token):
    """The single-use credential collection link. Consumed by
    frontend app/(root)/partners/apply/credentials/page.tsx."""
    return (
        f"{_frontend_origin()}/partners/apply/credentials"
        f"?ref={application.reference}&token={token}"
    )


def _send(application, template, subject_key, body_keys, **fmt):
    """Compose one email from the catalog and send it on a daemon thread.

    `body_keys` is the ordered list of copy keys to render as paragraphs, so each caller below
    reads as "which sentences, in which order" rather than as HTML. Every {placeholder} in those
    sentences is filled from **fmt, and the values are injected AS-IS: an organisation name or an
    owner's free-text reason is user-generated content and is never translated, exactly like a
    username elsewhere in this catalog.
    """
    from afc_auth.email_i18n import copy_for, subject_for
    from afc_auth.views import _email_shell, send_email

    lang = application.locale or "en"
    copy = copy_for(template, lang)
    subject = subject_for(subject_key, lang, reference=application.reference,
                          organisation=application.organisation_name)

    paragraphs = []
    for key in body_keys:
        sentence = copy.get(key)
        if not sentence:
            continue
        try:
            paragraphs.append(sentence.format(**fmt))
        except Exception:  # noqa: BLE001 - a stray brace must never cost the applicant the email
            paragraphs.append(sentence)

    heading = copy.get("heading", subject)
    body_html = "".join(
        f'<tr><td style="padding:0 44px 14px;font-size:15px;line-height:1.6;color:#aab5ae;">'
        f"{p}</td></tr>"
        for p in paragraphs
    )
    inner = (
        f'<tr><td style="padding:38px 44px 14px;">'
        f'<div style="font-size:21px;font-weight:700;color:#ffffff;">{heading}</div></td></tr>'
        f"{body_html}"
    )

    def _deliver():
        try:
            send_email(
                application.contact_email,
                subject,
                _email_shell(inner, "green"),
                language=lang,
                prelocalized=True,
            )
        except Exception as exc:  # noqa: BLE001 - see the module header: never blocks a decision
            logger.warning(
                "partner apply: could not email %s about %s: %s",
                application.contact_email, application.reference, exc,
            )

    threading.Thread(target=_deliver, daemon=True).start()


# ── link markup ───────────────────────────────────────────────────────────────────────────────
# One helper so every link in every one of these emails looks the same, and so a caller cannot
# accidentally drop a raw URL into a sentence where the catalog expected an anchor.
def _link(url, label):
    return f'<a href="{url}" style="color:#34d27b;text-decoration:none;font-weight:600;">{label}</a>'


# ── the four transitions ──────────────────────────────────────────────────────────────────────

def guide_url():
    """The public integration guide download (owner 2026-08-05).

    Points at the BACKEND, not the frontend: the PDF is served by
    afc_partner_apply/views_public.py integration_guide, which is the ungated twin of the admin
    route. An applicant clicking this in their inbox is not signed in to anything, which is why
    that route has no auth on it.
    """
    api = (getattr(settings, "AFC_API_BASE_URL", "") or "").rstrip("/")
    return f"{api}/partner-apply/integration-guide/"


def send_received(application, access_token):
    """Submitted. Confirms AFC has it, explains what they applied for, and links the guide.

    CALLED BY afc_partner_apply/views_public.py submit_application, once, at creation. This is the
    email that carries the access token, so it is the one an applicant must keep.

    WHY IT SAYS MORE THAN "WE GOT IT" (owner 2026-08-05). An organisation that has just applied is
    at the exact moment it wants to know what it signed up for and how long it will wait. Telling
    them here, with the guide attached as a link, is what turns the waiting period into time they
    can spend building rather than time they spend emailing to ask.

    THE GUIDE IS LINKED, NOT ATTACHED. It is close to 2 MB, and a 2 MB attachment on a
    transactional email is what gets a sender's domain treated as spam, or gets stripped by a
    corporate mail gateway before it arrives. A link also always serves the CURRENT build.
    """
    _send(
        application,
        template="partner_apply_received",
        subject_key="partner_apply_received",
        body_keys=("intro", "next_steps", "what_it_is", "guide", "keep_link"),
        organisation=application.organisation_name,
        reference=application.reference,
        link=_link(status_url(application, access_token), application.reference),
        guide=_link(guide_url(), "Sign in with AFC integration guide (PDF)"),
    )


def send_changes_requested(application, access_token):
    """The owner wants something fixed. Carries their note and the link to edit in place.

    CALLED BY afc_partner_apply/views_admin.py decide_application, action "request_changes".
    """
    _send(
        application,
        template="partner_apply_changes",
        subject_key="partner_apply_changes",
        body_keys=("intro", "note", "how_to_fix"),
        organisation=application.organisation_name,
        reference=application.reference,
        note=application.decision_note,
        link=_link(status_url(application, access_token), application.reference),
    )


def send_approved(application, access_token, claim_token):
    """Approved and provisioned. Carries the single-use credentials link.

    CALLED BY afc_partner_apply/views_admin.py decide_application, action "approve", and again by
    resend_credentials when the owner mints a fresh link.

    NOTE WHAT IS NOT IN HERE: no client secret, no API key. `claim_link` is single use and expires
    (PartnerApplication.CLAIM_WINDOW_HOURS), which an emailed secret never does.
    """
    _send(
        application,
        template="partner_apply_approved",
        subject_key="partner_apply_approved",
        body_keys=("intro", "credentials", "expiry", "guide"),
        organisation=application.organisation_name,
        reference=application.reference,
        hours=CLAIM_WINDOW_HOURS,
        claim_link=_link(claim_url(application, claim_token), "Collect your credentials"),
        link=_link(status_url(application, access_token), application.reference),
    )


def send_rejected(application):
    """Declined, with the owner's reason. No link: there is nothing left to act on, and the status
    page would only invite a refresh.

    CALLED BY afc_partner_apply/views_admin.py decide_application, action "reject".
    """
    _send(
        application,
        template="partner_apply_rejected",
        subject_key="partner_apply_rejected",
        body_keys=("intro", "note", "reapply"),
        organisation=application.organisation_name,
        reference=application.reference,
        note=application.decision_note,
    )


# ── the internal notification ─────────────────────────────────────────────────────────────────
# Where AFC's own copy goes. Read from settings so a deployment can point it somewhere else
# without a code change, defaulting to the address the rest of the site already publishes as its
# contact (afc_auth/views.py send_email's from_address).
AFC_NOTIFY_ADDRESS = getattr(
    settings, "PARTNER_APPLY_NOTIFY_EMAIL", "info@africanfreefirecommunity.com")


def send_internal_new_application(application):
    """Tell AFC that an application arrived (owner 2026-08-05).

    CALLED BY afc_partner_apply/views_public.py submit_application, once, immediately after
    send_received, so the applicant's own confirmation is never delayed by this one.

    WHY IT EXISTS. The review queue is a page somebody has to remember to open. Until now the only
    signal that an organisation was waiting was the queue's own unread count, which nobody sees
    unless they are already in the admin. An organisation that waits a week because nobody looked
    is an organisation that goes and builds on something else.

    ENGLISH, DELIBERATELY, and NOT through the localized catalog the four applicant emails use.
    This goes to AFC staff at one fixed address, not to a person with a language preference on
    their account, so translating it would be work with no reader. It is also written to be
    scanned rather than read: the facts first, the link last.

    NEVER LOCALIZED, NEVER BLOCKING, and it carries NO credential: there is nothing to leak here
    that the review screen does not already show, and the daemon-thread pattern is the same one
    the module header describes for every other send.
    """
    from afc_auth.views import _email_shell, send_email

    subject = f"New AFC partner application: {application.organisation_name} ({application.reference})"

    admin_link = f"{_frontend_origin()}/a/partners?tab=applications"
    rows = [
        ("Organisation", application.organisation_name),
        ("Country", application.country or "not given"),
        ("Contact", f"{application.contact_name} ({application.contact_email})"),
        ("WhatsApp", application.contact_whatsapp or "not given"),
        ("Website", application.homepage_url),
        ("Reference", application.reference),
    ]
    facts = "".join(
        f'<tr><td style="padding:0 44px 6px;font-size:14px;line-height:1.6;color:#aab5ae;">'
        f'<span style="color:#7d8a83;">{label}:</span> {value}</td></tr>'
        for label, value in rows
    )
    inner = (
        f'<tr><td style="padding:38px 44px 14px;">'
        f'<div style="font-size:21px;font-weight:700;color:#ffffff;">'
        f"An organisation has applied to be an AFC partner</div></td></tr>"
        f"{facts}"
        f'<tr><td style="padding:16px 44px 14px;font-size:15px;line-height:1.6;color:#aab5ae;">'
        f"They applied for Sign in with AFC. What they are building and what they need is on the "
        f"review screen: {_link(admin_link, 'open the application queue')}.</td></tr>"
    )

    def _deliver():
        try:
            send_email(
                AFC_NOTIFY_ADDRESS,
                subject,
                _email_shell(inner, "green"),
                language="en",
                prelocalized=True,
            )
        except Exception as exc:  # noqa: BLE001 - see the module header: never blocks a submission
            logger.warning(
                "partner apply: could not notify %s about %s: %s",
                AFC_NOTIFY_ADDRESS, application.reference, exc,
            )

    threading.Thread(target=_deliver, daemon=True).start()
