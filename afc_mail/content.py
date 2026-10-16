"""
afc_mail/content.py - a campaign's text turned into the email one recipient receives.

THE FORMAT AN ADMIN WRITES (deliberately small, so no HTML written by a person is ever stored or
rendered, and nothing needs sanitising):
  a blank line           a new paragraph
  a single line break    a line break
  **words**              bold
  [words](https://...)   a link with its own text
  https://...            a bare link
Everything else is escaped. The admin preview (frontend, built after approval) shows the server's
own rendering of the same text, so what the admin sees is what goes out.

TRACKING (the tracker the owner asked for, 2026-10-16): every link is rewritten to
/mail/c/<recipient token>/<link index>/, which counts the click and sends the reader on
(afc_mail.views.click); a 1x1 pixel /mail/o/<recipient token>.gif counts the open. The unsubscribe
link opens the site's page /email/unsubscribe/<token>, and the List-Unsubscribe header (RFC 8058,
afc_mail.transport) lets a mail app unsubscribe in one click without opening anything.

CONNECTS TO: afc_mail.tasks (sending), afc_mail.views.campaign_preview, afc_auth.views._email_shell
(the shared AFC email surface, so marketing mail looks like every other AFC email).
"""
import html
import re

from django.conf import settings

LINK_MD = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,2000})\)")
BARE_URL = re.compile(r"(?<![\"'=(\]])\bhttps?://[^\s<>\"')\]]{1,2000}")
BOLD = re.compile(r"\*\*([^*\n]{1,300})\*\*")

# The footer, per language. Hand-written (owner rule: no machine-translated product copy).
FOOTER = {
    "en": ("You get this email because you chose to receive news from African Free Fire Community.",
           "Unsubscribe"),
    "fr": ("Vous recevez cet e-mail parce que vous avez choisi de recevoir les nouvelles d'African Free Fire Community.",
           "Se désabonner"),
    "pt": ("Recebe este e-mail porque escolheu receber as novidades da African Free Fire Community.",
           "Cancelar a subscrição"),
}


def api_base():
    """Where the tracking and one-click unsubscribe addresses live (the API host)."""
    return getattr(settings, "MAIL_PUBLIC_API_BASE", "") or "https://api.africanfreefirecommunity.com"


def frontend_base():
    return getattr(settings, "FRONTEND_URL", "") or "https://africanfreefirecommunity.com"


def extract_links(*bodies):
    """Every distinct http(s) link in the given bodies, in order of first appearance. Stored on
    the campaign at send time; the click redirect only ever goes to an entry of this list."""
    seen = []
    for body in bodies:
        if not body:
            continue
        taken = []
        for m in LINK_MD.finditer(body):
            taken.append((m.start(), m.group(2)))
        stripped = LINK_MD.sub(" ", body)
        for m in BARE_URL.finditer(stripped):
            taken.append((m.start(), m.group(0).rstrip(".,;:!?")))
        for _pos, url in sorted(taken):
            if url not in seen:
                seen.append(url)
    return seen


def _render_inline(text, link_for):
    """Escape a paragraph, then apply links and bold. `link_for(url)` gives the href to use."""
    out = []
    last = 0
    for m in LINK_MD.finditer(text):
        out.append(("text", text[last:m.start()]))
        out.append(("link", m.group(1), m.group(2)))
        last = m.end()
    out.append(("text", text[last:]))

    pieces = []
    for part in out:
        if part[0] == "link":
            label = html.escape(part[1])
            pieces.append(f'<a href="{html.escape(link_for(part[2]), quote=True)}" style="color:#4ade80;">{label}</a>')
            continue
        chunk = part[1]
        pos = 0
        for m in BARE_URL.finditer(chunk):
            url = m.group(0).rstrip(".,;:!?")
            pieces.append(html.escape(chunk[pos:m.start()]))
            pieces.append(f'<a href="{html.escape(link_for(url), quote=True)}" style="color:#4ade80;">{html.escape(url)}</a>')
            pos = m.start() + len(url)
        pieces.append(html.escape(chunk[pos:]))
    rendered = "".join(pieces)
    rendered = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", rendered)
    return rendered.replace("\n", "<br>")


def body_to_html(body, link_for=lambda url: url):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", (body or "").replace("\r\n", "\n")) if p.strip()]
    return "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.7;color:#cdd6cf;">{_render_inline(p, link_for)}</p>'
        for p in paragraphs
    )


def body_to_text(body, link_for=lambda url: url):
    """The plain-text part (multipart/alternative): mail apps and spam filters both read it."""
    text = (body or "").replace("\r\n", "\n")
    text = LINK_MD.sub(lambda m: f"{m.group(1)} ({link_for(m.group(2))})", text)
    text = BOLD.sub(lambda m: m.group(1), text)
    return text


def version_for(campaign, language):
    """(subject, body, language actually used): the reader's language when the campaign has it,
    English otherwise."""
    lang = (language or "en").split("-")[0].lower()
    if lang in ("fr", "pt"):
        subject = getattr(campaign, f"subject_{lang}", "") or ""
        body = getattr(campaign, f"body_{lang}", "") or ""
        if subject.strip() and body.strip():
            return subject.strip(), body, lang
    return campaign.subject_en.strip(), campaign.body_en, "en"


def tracked_link(token, links):
    def _for(url):
        try:
            i = links.index(url)
        except ValueError:
            return url  # cannot happen for a sent campaign: links were extracted from these bodies
        return f"{api_base()}/mail/c/{token}/{i}/"
    return _for


def unsubscribe_page(token):
    return f"{frontend_base()}/email/unsubscribe/{token}"


def one_click_unsubscribe(token):
    return f"{api_base()}/mail/unsubscribe/{token}/"


def render_for_recipient(campaign, recipient):
    """(subject, html, text) for one recipient, every link tracked, pixel and footer added."""
    from afc_auth.views import _email_shell

    subject, body, lang = version_for(campaign, recipient.language)
    link_for = tracked_link(recipient.token, campaign.links or [])
    note, unsub_label = FOOTER.get(lang, FOOTER["en"])
    unsub = unsubscribe_page(recipient.token)
    pixel = f"{api_base()}/mail/o/{recipient.token}.gif"
    inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{html.escape(subject)}</div>
  </td></tr>
  <tr><td style="padding:6px 44px 26px;">{body_to_html(body, link_for)}</td></tr>
  <tr><td style="padding:0 44px 34px;">
    <div style="font-size:12px;line-height:1.6;color:#8a948d;">{html.escape(note)}
      <a href="{html.escape(unsub, quote=True)}" style="color:#8a948d;">{html.escape(unsub_label)}</a></div>
    <img src="{html.escape(pixel, quote=True)}" width="1" height="1" alt="" style="display:block;border:0;">
  </td></tr>"""
    text = f"{body_to_text(body, link_for)}\n\n--\n{note} {unsub_label}: {unsub}\n"
    return subject, _email_shell(inner, "green"), text


def render_preview(campaign, language="en"):
    """(subject, html) exactly as a reader in `language` would get it, without tracking."""
    from afc_auth.views import _email_shell

    subject, body, lang = version_for(campaign, language)
    note, unsub_label = FOOTER.get(lang, FOOTER["en"])
    inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{html.escape(subject)}</div>
  </td></tr>
  <tr><td style="padding:6px 44px 26px;">{body_to_html(body)}</td></tr>
  <tr><td style="padding:0 44px 34px;">
    <div style="font-size:12px;line-height:1.6;color:#8a948d;">{html.escape(note)} <u>{html.escape(unsub_label)}</u></div>
  </td></tr>"""
    return subject, _email_shell(inner, "green")


def render_test(campaign, language="en"):
    """(subject, html, text) for the admin's test send: the reader's version with real links and
    no tracking, so testing a campaign never puts a click or an open in its tracker."""
    subject, html_body = render_preview(campaign, language)
    _subject, body, lang = version_for(campaign, language)
    note, unsub_label = FOOTER.get(lang, FOOTER["en"])
    return subject, html_body, f"{body_to_text(body)}\n\n--\n{note}\n"
