"""
afc_mail/content.py - a campaign turned into the email one person receives.

TWO WAYS AN EMAIL IS MADE (Campaign.mode):
  "write"   the composer's document per language (afc_mail.document renders it through an allowlist)
            inside AFC's marketing shell, coloured and set in the campaign's style (afc_mail.style).
  "design"  an uploaded design per language, already cleaned when it was stored (afc_mail.designs).
Either way the server adds, every time and whatever the content says: the footer sentence with the
Unsubscribe link (in the reader's language), the open pixel, and tracking on every web link.

A reader gets their own language when the campaign has that version (subject AND content), English
otherwise. No machine translation of marketing copy.

TRACKING (the tracker the owner asked for, 2026-10-16): every web link is rewritten to
/mail/c/<recipient token>/<link index>/ (afc_mail.views.click); a 1x1 pixel /mail/o/<token>.gif counts
the open. The test send and the preview carry the real links and no pixel, so testing never moves the
tracker.

CONNECTS TO: afc_mail.tasks (sending), afc_mail.views (preview, test, send), afc_mail.document,
afc_mail.style, afc_mail.assets, afc_mail.addresses.
"""
import html
import re

from bs4 import BeautifulSoup

from . import assets as asset_store
from .addresses import frontend_base, pixel, tracked_link, unsubscribe_page
from .document import Renderer, attr, doc_is_empty, doc_tokens
from .models import LANGS
from .style import FONTS, font_tokens, mix, parse_style

# The footer, per language. Hand-written (owner rule: no machine-translated product copy).
FOOTER = {
    "en": ("You get this email because you chose to receive news from African Free Fire Community.",
           "Unsubscribe"),
    "fr": ("Vous recevez cet e-mail parce que vous avez choisi de recevoir les nouvelles d'African Free Fire Community.",
           "Se désabonner"),
    "pt": ("Recebe este e-mail porque escolheu receber as novidades da African Free Fire Community.",
           "Cancelar a subscrição"),
}


def _lang(language):
    lang = (language or "en").split("-")[0].lower()
    return lang if lang in LANGS else "en"


def has_version(campaign, lang):
    """Does the campaign carry a complete version in this language (subject and content)?"""
    if not (getattr(campaign, f"subject_{lang}", "") or "").strip():
        return False
    if campaign.mode == "design":
        return bool((campaign.designs or {}).get(lang))
    return not doc_is_empty((campaign.docs or {}).get(lang))


def version_for(campaign, language):
    """The language a reader in `language` gets: theirs when the campaign has it, English otherwise."""
    lang = _lang(language)
    return lang if lang != "en" and has_version(campaign, lang) else "en"


def style_of(campaign):
    """The campaign's style, re-checked (an uploaded font deleted since is dropped, not sent)."""
    raw = campaign.style or {}
    return parse_style(raw, font_assets=asset_store.load(font_tokens(raw), kind="font"))


def _fonts_css(style):
    fonts = asset_store.load([f["asset"] for f in style.get("fonts", [])], kind="font")
    rules = []
    for f in style.get("fonts", []):
        asset = fonts.get(f["asset"])
        if not asset:
            continue
        fmt = {"font/woff2": "woff2", "font/woff": "woff", "font/otf": "opentype"}.get(asset.content_type, "truetype")
        # One uploaded file is one face. Declaring it for every weight makes headings (bold) use the
        # face as drawn, instead of the app smearing it into a fake bold (seen with Impact, 17 Oct).
        rules.append(f'@font-face{{font-family:"{f["family"]}";src:url("{asset_store.public_url(asset)}") format("{fmt}");'
                     f'font-weight:100 900;font-display:swap;}}')
    return f"<style>{''.join(rules)}</style>" if rules else ""


def _asset_url(doc):
    found = asset_store.load(doc_tokens(doc), kind="image")
    return lambda token, opacity: asset_store.baked_url(found.get(token), opacity)


def _footer(lang, style, unsub_href):
    note, label = FOOTER.get(lang, FOOTER["en"])
    colour = mix(style["text"], style["mail"], 0.35)
    link = (f'<a href="{html.escape(unsub_href, quote=True)}" style="color:{colour};text-decoration:underline;">{html.escape(label)}</a>'
            if unsub_href else f'<u>{html.escape(label)}</u>')
    return (f'<p style="margin:22px 0 0;font-size:12px;line-height:1.6;color:{colour};">{html.escape(note)} {link}</p>',
            f"{note} {label}" + (f": {unsub_href}" if unsub_href else ""))


def _shell(*, lang, subject, preheader, style, inner, footer_html, pixel_url):
    from .style import font_stack
    body = font_stack(style["bodyFont"], style)
    hidden = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{style["page"]};">'
              f'{html.escape(preheader)}</div>' if preheader else "")
    pix = (f'<img src="{html.escape(pixel_url, quote=True)}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px;">'
           if pixel_url else "")
    return f"""<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{html.escape(subject)}</title>{_fonts_css(style)}</head>
<body style="margin:0;padding:0;background-color:{style["page"]};">{hidden}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{style["page"]}" style="background-color:{style["page"]};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="{style["mail"]}" style="width:100%;max-width:600px;background-color:{style["mail"]};border-radius:8px;">
<tr><td style="padding:28px 24px;font-family:{attr(body)};font-size:{style["size"]}px;line-height:1.6;color:{style["text"]};">
{inner}
{footer_html}
{pix}
</td></tr></table>
</td></tr></table>
</body></html>"""


def _design_email(campaign, lang, *, link_for, unsub_href, pixel_url, links_seen):
    """An uploaded design, with tracked links, the hidden preview line, the footer and the pixel."""
    design = (campaign.designs or {}).get(lang) or (campaign.designs or {}).get("en") or {}
    soup = BeautifulSoup(design.get("html") or "<html><body></body></html>", "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.match(r"^https?://", href, re.I):
            if href not in links_seen:
                links_seen.append(href)
            a["href"] = link_for(href)
    body = soup.body or soup
    preheader = (getattr(campaign, f"preheader_{lang}", "") or "").strip()
    if preheader:
        hidden = soup.new_tag("div", style="display:none;max-height:0;overflow:hidden;mso-hide:all;")
        hidden.string = preheader
        body.insert(0, hidden)
    note, label = FOOTER.get(lang, FOOTER["en"])
    foot = BeautifulSoup(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td align="center" style="padding:18px 12px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.6;color:#8a948d;">'
        f'{html.escape(note)} '
        + (f'<a href="{html.escape(unsub_href, quote=True)}" style="color:#8a948d;text-decoration:underline;">{html.escape(label)}</a>' if unsub_href else f"<u>{html.escape(label)}</u>")
        + (f'<img src="{html.escape(pixel_url, quote=True)}" width="1" height="1" alt="" style="display:block;border:0;">' if pixel_url else "")
        + "</td></tr></table>", "lxml")
    for node in list((foot.body or foot).children):
        body.append(node)
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    text += f"\n\n--\n{note} {label}" + (f": {unsub_href}" if unsub_href else "") + "\n"
    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out, text


def render(campaign, language, *, link_for=None, recipient_token=None, preview=False):
    """(subject, html, text, links) for a reader in `language`.

    recipient_token: a real recipient (tracked links via link_for, pixel, their unsubscribe page).
    preview: the admin's preview (real links, no pixel, the Unsubscribe word shown, not linked).
    Neither: the test send (real links, no pixel, the footer pointing at the site)."""
    lang = version_for(campaign, language)
    subject = (getattr(campaign, f"subject_{lang}", "") or "").strip()
    preheader = (getattr(campaign, f"preheader_{lang}", "") or "").strip()
    link_for = link_for or (lambda url: url)
    if recipient_token:
        unsub_href, pixel_url = unsubscribe_page(recipient_token), pixel(recipient_token)
    elif preview:
        unsub_href, pixel_url = "", ""
    else:
        unsub_href, pixel_url = frontend_base(), ""
    links = []
    if campaign.mode == "design":
        out, text = _design_email(campaign, lang, link_for=link_for, unsub_href=unsub_href, pixel_url=pixel_url, links_seen=links)
        return subject, out, text, links
    style = style_of(campaign)
    doc = (campaign.docs or {}).get(lang) or {}
    renderer = Renderer(style, _asset_url(doc), link_for)
    inner = renderer.html(doc)
    text = renderer.text(doc)
    footer_html, footer_text = _footer(lang, style, unsub_href)
    out = _shell(lang=lang, subject=subject, preheader=preheader, style=style, inner=inner,
                 footer_html=footer_html, pixel_url=pixel_url)
    return subject, out, f"{text}\n--\n{footer_text}\n", renderer.links


def campaign_links(campaign):
    """Every distinct web link across the campaign's versions, in order. Stored on the campaign when
    it is sent; the click redirect only ever goes to an entry of this list (R85, no open redirect)."""
    seen = []
    for lang in LANGS:
        if lang != "en" and not has_version(campaign, lang):
            continue
        _s, _h, _t, links = render(campaign, lang, preview=True)
        for url in links:
            if url not in seen:
                seen.append(url)
    return seen


def render_for_recipient(campaign, recipient):
    """(subject, html, text) for one recipient, every link tracked, pixel and footer added."""
    subject, out, text, _links = render(campaign, recipient.language,
                                        link_for=tracked_link(recipient.token, campaign.links or []),
                                        recipient_token=recipient.token)
    return subject, out, text


def render_preview(campaign, language="en"):
    """(subject, html) exactly as a reader in `language` would get it, without tracking."""
    subject, out, _text, _links = render(campaign, language, preview=True)
    return subject, out


def render_test(campaign, language="en"):
    """(subject, html, text) for the admin's test send: real links, no tracking, no pixel."""
    subject, out, text, _links = render(campaign, language)
    return subject, out, text


__all__ = ["FOOTER", "FONTS", "has_version", "version_for", "render", "render_for_recipient", "render_preview",
           "render_test", "campaign_links", "style_of"]
