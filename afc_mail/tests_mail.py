"""afc_mail: consent, unsubscribe, audience, the composer's document and designs, sending, tracking,
SES events, the admin API (inbox #259, the approved composer #288 #290).

Nothing here reaches a real mailbox: the test runner forces settings.OUTBOUND_DELIVERY to "outbox"
(afc/settings.py) and afc_mail.transport asks afc_auth.outbox first. The SNS tests sign their own
messages with a throwaway certificate, served in place of Amazon's through a patched requests.get.
Files (pictures, fonts) go to a temporary MEDIA_ROOT that is removed afterwards.

Run: .venv\\Scripts\\python.exe manage.py test afc_mail
"""
import base64
import datetime
import io
import json
import shutil
import tempfile
import uuid
import zipfile
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth import outbox
from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_team.models import Team, TeamMembers

from . import assets as asset_store
from . import sns
from .content import campaign_links, render_for_recipient, render_preview
from .designs import DesignError, clean_design
from .document import DocError, Renderer, parse_doc
from .models import Campaign, CampaignClick, CampaignRecipient, EmailSubscription, EmailSuppression, EmailTemplate, MailAsset
from .recipients import audience_count, freeze_recipients, set_consent, suppress
from .style import THEMES, parse_style
from .tasks import RETRIES, send_campaign_chunk, send_chunk
from .transport import build_message

TOPIC = "arn:aws:sns:eu-north-1:123456789012:afc-ses-events"
CERT_URL = "https://sns.eu-north-1.amazonaws.com/SimpleNotificationService-test.pem"
RULES = "https://africanfreefirecommunity.com/rules"
TOURNAMENTS = "https://africanfreefirecommunity.com/tournaments"


def _user(name, *, email=None, opt_in=None, status_="active"):
    u = User.objects.create_user(username=name, email=email if email is not None else f"{name.lower()}@gmail.com",
                                 password="x", role="player", status=status_)
    if opt_in is not None:
        set_consent(u, opt_in, "settings")
    return u


def _auth(user):
    st = SessionToken.objects.create(user=user, token=f"tok-{user.username}-{uuid.uuid4().hex}",
                                     expires_at=timezone.now() + datetime.timedelta(days=1))
    return {"HTTP_AUTHORIZATION": f"Bearer {st.token}"}


def _head_admin(name="mailadmin"):
    u = User.objects.create_user(username=name, email=f"{name}@gmail.com", password="x", role="admin")
    role, _ = Roles.objects.get_or_create(role_name="head_admin")
    UserRoles.objects.create(user=u, role=role)
    return u


# ── documents as the editor sends them ──────────────────────────────────────────────────────────

def _t(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def _p(*inlines, **attrs):
    return {"type": "paragraph", "attrs": attrs, "content": list(inlines)}


def _doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def _link(href):
    return {"type": "link", "attrs": {"href": href}}


BOLD = {"type": "bold"}


def _doc_en(title="AFC news"):
    return _doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [_t(title)]},
        _p(_t("Hello "), _t("players", BOLD), _t(".")),
        _p(_t("Read "), _t("the rules", _link(RULES)), _t(" first.")),
        {"type": "emailButton", "attrs": {"label": "See tournaments", "href": TOURNAMENTS, "align": "center"}},
    )


def _campaign(admin=None, **kw):
    data = dict(name="October news", subject_en="AFC news", docs={"en": _doc_en()}, audience={"everyone": True},
                style=parse_style({}))
    data.update(kw)
    return Campaign.objects.create(created_by=admin, **data)


def _png(w=40, h=20, rgba=(220, 30, 30, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), rgba).save(buf, format="PNG")
    return buf.getvalue()


class _MediaMixin:
    """A throwaway MEDIA_ROOT, so stored pictures and fonts never land in the working tree."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp(prefix="afc-mail-test-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)


# ── consent and audience ────────────────────────────────────────────────────────────────────────

class ConsentTests(TestCase):
    def test_no_session_is_refused(self):
        self.assertEqual(self.client.get("/mail/preferences/").status_code, 401)

    def test_off_by_default_then_on_with_a_timestamp(self):
        u = _user("Ama")
        first = self.client.get("/mail/preferences/", **_auth(u)).json()
        self.assertFalse(first["marketing_opt_in"])
        self.assertFalse(first["answered"])
        res = self.client.post("/mail/preferences/", data=json.dumps({"marketing_opt_in": True}),
                               content_type="application/json", **_auth(u))
        self.assertTrue(res.json()["marketing_opt_in"])
        sub = EmailSubscription.objects.get(user=u)
        self.assertIsNotNone(sub.opted_in_at)
        self.assertEqual(sub.source, "settings")
        no = _user("SaidNo")
        res = self.client.post("/mail/preferences/", data=json.dumps({"marketing_opt_in": False, "source": "prompt"}),
                               content_type="application/json", **_auth(no))
        self.assertEqual((res.json()["marketing_opt_in"], res.json()["answered"]), (False, True))

    def test_a_non_boolean_is_refused_with_a_code(self):
        u = _user("Kofi")
        res = self.client.post("/mail/preferences/", data=json.dumps({"marketing_opt_in": "yes"}),
                               content_type="application/json", **_auth(u))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "mail_preference_invalid")


class AudienceTests(TestCase):
    def test_only_consenting_reachable_people_count(self):
        yes = _user("Yes", opt_in=True)
        _user("No", opt_in=False)
        _user("Never")                                   # never asked: no row, so no
        _user("NoMail", email="", opt_in=True)
        gone = _user("Gone", opt_in=True, status_="deleted")
        bounced = _user("Bounced", opt_in=True)
        suppress(bounced.email, "bounce")
        counts = audience_count({"everyone": True, "user_ids": [], "team_ids": [], "tiers": [], "countries": [],
                                 "roles": [], "languages": [], "event_ids": [], "team_roles": [], "rank_range": None,
                                 "season_tiers": None, "require_profile_fields": [], "include_suspended": False})
        self.assertEqual(counts["recipients"], 1)
        self.assertGreaterEqual(counts["selected"], 5)
        self.assertTrue(yes and gone)

    def test_an_empty_audience_selects_nobody(self):
        _user("Yes2", opt_in=True)
        c = _campaign(audience={})
        self.assertEqual(freeze_recipients(c), 0)

    def test_picked_people_and_teams_that_cannot_be_mailed_are_named_with_the_reason(self):
        admin = _head_admin()
        yes = _user("Mailable", opt_in=True)
        never = _user("NeverAsked")
        left = _user("LeftLater", opt_in=True)
        set_consent(left, False, "settings")
        bounced = _user("Bouncy", opt_in=True)
        suppress(bounced.email, "bounce")
        nomail = _user("NoAddress", email="", opt_in=True)
        owner = _user("Captain", opt_in=True)
        team = Team.objects.create(team_name="Lagos Titans", join_settings="open", team_creator=owner, team_owner=owner, country="NG")
        TeamMembers.objects.create(team=team, member=never, management_role="member")
        body = {"audience": {"user_ids": [yes.pk, never.pk, left.pk, bounced.pk, nomail.pk], "team_ids": [team.team_id]}}
        res = self.client.post("/mail/audience/", data=json.dumps(body), content_type="application/json", **_auth(admin))
        self.assertEqual(res.status_code, 200, res.content[:300])
        out = res.json()
        reasons = {row["name"]: row["reason"] for row in out["left_out"] if row["kind"] == "player"}
        self.assertEqual(reasons, {"NeverAsked": "no_consent", "LeftLater": "unsubscribed", "Bouncy": "bounced", "NoAddress": "no_address"})
        teams = [row for row in out["left_out"] if row["kind"] == "team"]
        self.assertEqual((teams[0]["name"], teams[0]["left"], teams[0]["total"]), ("Lagos Titans", 1, 2))
        self.assertEqual(out["recipients"], 2)  # Mailable + the captain
        names = {(row["kind"], row["name"]) for row in out["picked"]}
        self.assertIn(("team", "Lagos Titans"), names)
        self.assertIn(("player", "Mailable"), names)
        self.assertEqual(len(out["picked"]), 6)


# ── the composer's document ─────────────────────────────────────────────────────────────────────

class DocumentTests(_MediaMixin, TestCase):
    def test_parse_keeps_what_is_listed_and_drops_the_rest(self):
        raw = _doc(
            {"type": "codeBlock", "content": [_t("rm -rf /")]},
            {"type": "heading", "attrs": {"level": 9, "onclick": "x"}, "content": [_t("Title")]},
            _p(_t("bad", _link("javascript:alert(1)")), _t("ok", _link("https://x.example/a")), onerror="x", textAlign="justify"),
            {"type": "emailImage", "attrs": {"asset": "../etc/passwd", "src": "https://evil.example/x.png"}},
            {"type": "orderedList", "attrs": {"start": 3, "listStyle": "upper-roman"},
             "content": [{"type": "listItem", "content": [_p(_t("one"))]}]},
            {"type": "emailButton", "attrs": {"label": "Go", "href": "vbscript:x"}},
        )
        doc, tokens = parse_doc(raw)
        kinds = [n["type"] for n in doc["content"]]
        self.assertEqual(kinds, ["heading", "paragraph", "orderedList"])
        self.assertEqual(doc["content"][0]["attrs"], {"level": 2})
        para = doc["content"][1]
        self.assertEqual(para["attrs"], {})
        self.assertNotIn("marks", para["content"][0])
        self.assertEqual(para["content"][1]["marks"], [{"type": "link", "attrs": {"href": "https://x.example/a"}}])
        self.assertEqual(doc["content"][2]["attrs"], {"start": 3, "listStyle": "upper-roman"})
        self.assertEqual(tokens, [])

    def test_text_is_escaped_and_the_style_is_inlined(self):
        doc, _ = parse_doc(_doc(
            {"type": "heading", "attrs": {"level": 1, "textAlign": "center"}, "content": [_t("Hi <script>x</script>")]},
            _p(_t("big gold", {"type": "textStyle", "attrs": {"color": "rgb(234, 179, 8)", "fontSize": "24px", "fontFamily": "Georgia, serif"}})),
            {"type": "bulletList", "attrs": {"listStyle": "square"}, "content": [{"type": "listItem", "content": [_p(_t("dot"))]}]},
            {"type": "emailButton", "attrs": {"label": "Join", "href": TOURNAMENTS, "align": "left"}},
        ))
        style = parse_style({**THEMES["Gold night"], "theme": "Gold night", "headFont": "Georgia"})
        html = Renderer(style, lambda t, o: None).html(doc)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("font-size:28px", html)
        self.assertIn("color:#eab308", html)                       # Gold night headings
        self.assertIn("text-align:center", html)
        self.assertIn("color:#eab308;font-family:Georgia", html)   # the span's own colour and font
        self.assertIn("font-size:24px", html)
        self.assertIn("list-style-type:square", html)
        self.assertIn('bgcolor="#eab308"', html)                   # Gold night buttons
        self.assertIn('align="left"', html)

    def test_a_font_name_nobody_chose_is_dropped(self):
        doc, _ = parse_doc(_doc(_p(_t("x", {"type": "textStyle", "attrs": {"fontFamily": "Comic Sans MS"}}))))
        html = Renderer(parse_style({}), lambda t, o: None).html(doc)
        self.assertNotIn("Comic", html)

    def test_a_document_too_big_or_not_a_document_is_refused(self):
        with self.assertRaises(DocError) as big:
            parse_doc(_doc(*[_p(_t("x")) for _ in range(5000)]))
        self.assertEqual(big.exception.code, "mail_doc_too_big")
        with self.assertRaises(DocError) as wrong:
            parse_doc({"type": "paragraph"})
        self.assertEqual(wrong.exception.code, "mail_doc_invalid")

    def test_a_picture_is_an_afc_asset_and_transparency_is_baked_into_a_png(self):
        asset, code = asset_store.store_image_bytes(_png(), None, name="squad.png")
        self.assertIsNone(code)
        doc, tokens = parse_doc(_doc(
            {"type": "emailImage", "attrs": {"asset": asset.token, "alt": "Squad", "width": 45, "opacity": 0.5, "align": "right",
                                             "href": "https://x.example/teams"}},
            {"type": "emailImage", "attrs": {"asset": "a_" + "0" * 32}},   # a token the server never issued
        ))
        self.assertEqual(tokens, [asset.token, "a_" + "0" * 32])
        c = _campaign(docs={"en": doc})
        _subject, html = render_preview(c)
        self.assertIn(f"/media/mail/baked/{asset.token}-o50.png", html)
        self.assertTrue(default_storage.exists(f"mail/baked/{asset.token}-o50.png"))
        self.assertIn('width="248"', html)                          # 45% of 552 px, for Outlook
        self.assertIn("text-align:right", html)
        self.assertEqual(html.count("<img"), 1)                     # the unknown token renders nothing
        from PIL import Image
        with default_storage.open(f"mail/baked/{asset.token}-o50.png") as fh, Image.open(fh) as img:
            self.assertEqual(img.getpixel((1, 1))[3], 127)

    def test_an_uploaded_font_is_declared_with_its_fallback(self):
        font = SimpleUploadedFile("Squad Display.woff2", b"wOF2" + b"\x00" * 64, content_type="font/woff2")
        asset, code = asset_store.store_font(font, None)
        self.assertIsNone(code)
        self.assertEqual(asset.family, "Squad Display")
        style = parse_style({"fonts": [{"asset": asset.token, "fallback": "Georgia"}], "headFont": "Squad Display"},
                            font_assets={asset.token: asset})
        c = _campaign(style=style)
        _subject, html = render_preview(c)
        self.assertIn('@font-face{font-family:"Squad Display"', html)
        self.assertIn('format("woff2")', html)
        self.assertIn("font-weight:100 900;", html)  # bold headings use the face, not a fake bold
        self.assertIn("'Squad Display', Georgia", html)

    def test_a_recipient_gets_tracked_links_a_pixel_an_unsubscribe_link_and_the_preview_line(self):
        c = _campaign(preheader_en="41 squads are in")
        c.links = campaign_links(c)
        c.save()
        self.assertEqual(c.links, [RULES, TOURNAMENTS])
        r = CampaignRecipient.objects.create(campaign=c, email="a@gmail.com")
        subject, html, text = render_for_recipient(c, r)
        self.assertEqual(subject, "AFC news")
        self.assertIn(f"/mail/c/{r.token}/0/", html)
        self.assertIn(f"/mail/c/{r.token}/1/", html)
        self.assertIn(f"/mail/o/{r.token}.gif", html)
        self.assertIn(f"/email/unsubscribe/{r.token}", html)
        self.assertIn("41 squads are in", html)
        self.assertNotIn(f'href="{RULES}"', html)                  # never the raw link
        self.assertIn(f"/mail/c/{r.token}/0/", text)
        self.assertIn("See tournaments", text)

    def test_the_readers_language_is_used_when_the_campaign_has_it(self):
        c = _campaign(subject_fr="Nouvelles AFC", docs={"en": _doc_en(), "fr": _doc(_p(_t("Bonjour.")))})
        r = CampaignRecipient(campaign=c, email="b@gmail.com", language="fr", token="r_x")
        subject, html, _text = render_for_recipient(c, r)
        self.assertEqual(subject, "Nouvelles AFC")
        self.assertIn("Se désabonner", html)
        half = _campaign(subject_pt="Notícias")                      # a subject without an email
        r2 = CampaignRecipient(campaign=half, email="c@gmail.com", language="pt", token="r_y")
        self.assertEqual(render_for_recipient(half, r2)[0], "AFC news")

    def test_headers_carry_one_click_unsubscribe_and_the_ses_tags(self):
        with override_settings(SES_CONFIGURATION_SET="afc-marketing"):
            msg = build_message("a@gmail.com", "Hi", "<p>x</p>", "x", one_click_url="https://api.example/mail/unsubscribe/r_1/",
                                page_url="https://site.example/email/unsubscribe/r_1", tags={"campaign": "c_1", "recipient": "r_1"})
        self.assertEqual(msg["List-Unsubscribe"], "<https://api.example/mail/unsubscribe/r_1/>")
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(msg["X-SES-CONFIGURATION-SET"], "afc-marketing")
        self.assertIn("recipient=r_1", msg["X-SES-MESSAGE-TAGS"])
        self.assertIn("news.africanfreefirecommunity.com", msg["From"])


# ── your own design ─────────────────────────────────────────────────────────────────────────────

EVIL_HTML = f"""<!doctype html><html><head><meta http-equiv="refresh" content="0;url=https://evil.example">
<base href="https://evil.example/"><link rel="stylesheet" href="https://evil.example/x.css">
<style>@import url(https://evil.example/y.css); .a {{ color: red; background: expression(alert(1)); }}</style>
<script>alert(1)</script></head><body onload="alert(1)">
<!-- a note --><!--[if mso]><table><tr><td><![endif]-->
<p onclick="alert(1)">Hello <a href="javascript:alert(1)">bad</a> <a href="{RULES}">rules</a></p>
<iframe src="https://evil.example"></iframe><form action="https://evil.example"><input name="pw"></form>
<img src="data:image/png;base64,{base64.b64encode(_png()).decode()}" alt="packed">
<img src="https://cdn.example/web.png" alt="web">
<svg onload="alert(1)"></svg></body></html>"""


class DesignTests(_MediaMixin, TestCase):
    def test_an_html_design_is_cleaned_and_its_packed_picture_moves_to_afc(self):
        out = clean_design("news.html", EVIL_HTML.encode(), None)
        html = out["html"].lower()
        for bad in ("<script", "<iframe", "<form", "<input", "<base", "<link", "http-equiv", "onload", "onclick",
                    "javascript:", "@import", "expression(", "<svg", "a note"):
            self.assertNotIn(bad, html, bad)
        self.assertIn("[if mso]", out["html"])            # Outlook's own section is kept
        self.assertIn("/media/mail/", out["html"])        # the packed picture is now an AFC asset
        self.assertIn("https://cdn.example/web.png", out["html"])
        r = out["report"]
        self.assertEqual((r["packed"], r["web_pictures"], r["links"]), (1, 1, 1))
        self.assertGreaterEqual(r["removed"], 6)
        self.assertGreaterEqual(r["handlers"], 3)

    def test_a_zip_brings_its_pictures(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mail/index.html", '<html><body><img src="img/pic.png" alt="pic"><a href="https://x.example">x</a></body></html>')
            zf.writestr("mail/img/pic.png", _png())
        out = clean_design("design.zip", buf.getvalue(), None)
        self.assertIn("/media/mail/", out["html"])
        self.assertNotIn("img/pic.png", out["html"])
        self.assertEqual(out["report"]["packed"], 1)

    def test_mjml_is_compiled_and_its_includes_are_refused(self):
        out = clean_design("news.mjml", b'<mjml><mj-body><mj-section><mj-column><mj-text>Hi <a href="https://x.example/a">there</a></mj-text>'
                                        b'</mj-column></mj-section></mj-body></mjml>', None)
        self.assertIn("https://x.example/a", out["html"])
        self.assertNotIn("<mj-", out["html"])
        # mrml's own Google Fonts link is not added, so the admin is not told something was taken out
        self.assertNotIn("fonts.googleapis.com", out["html"])
        self.assertEqual(out["report"]["removed"], 0)
        with self.assertRaises(DesignError) as inc:
            clean_design("x.mjml", b'<mjml><mj-body><mj-include path="https://evil.example/x.mjml" /></mj-body></mjml>', None)
        self.assertEqual(inc.exception.code, "mail_design_mjml_include")

    def test_other_files_are_refused(self):
        with self.assertRaises(DesignError) as wrong:
            clean_design("notes.txt", b"just words", None)
        self.assertEqual(wrong.exception.code, "mail_design_wrong_type")
        with self.assertRaises(DesignError) as big:
            clean_design("big.html", b"<html>" + b"x" * (2 * 1024 * 1024) + b"</html>", None)
        self.assertEqual(big.exception.code, "mail_design_too_big")

    def test_a_design_goes_out_with_tracking_the_footer_and_the_pixel(self):
        out = clean_design("news.html", f'<html><body><p>Hi <a href="{RULES}">rules</a></p></body></html>'.encode(), None)
        c = _campaign(mode="design", designs={"en": out})
        c.links = campaign_links(c)
        c.save()
        self.assertEqual(c.links, [RULES])
        r = CampaignRecipient.objects.create(campaign=c, email="d@gmail.com")
        _subject, html, text = render_for_recipient(c, r)
        self.assertIn(f"/mail/c/{r.token}/0/", html)
        self.assertIn(f"/mail/o/{r.token}.gif", html)
        self.assertIn(f"/email/unsubscribe/{r.token}", html)
        self.assertIn("Unsubscribe", text)


# ── sending, tracking, SES ──────────────────────────────────────────────────────────────────────

class SendingTests(TestCase):
    def setUp(self):
        outbox.drain()
        self.admin = _head_admin()
        self.yes = _user("Reader", opt_in=True)

    def test_a_campaign_goes_out_to_the_outbox_only(self):
        c = _campaign(self.admin)
        with mock.patch("afc_mail.tasks.send_campaign_chunk.delay") as queued:
            res = self.client.post(f"/mail/campaigns/{c.token}/send/", **_auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.assertEqual(res.json()["recipients"], 1)
        queued.assert_called_once()
        self.assertEqual(send_chunk(c.pk, sleep=lambda s: None), 1)
        self.assertEqual(send_chunk(c.pk, sleep=lambda s: None), 0)
        c.refresh_from_db()
        self.assertEqual(c.status, "sent")
        self.assertEqual(c.links, [RULES, TOURNAMENTS])
        sent = outbox.drain()
        self.assertEqual([m["to"] for m in sent], [self.yes.email])
        self.assertEqual(CampaignRecipient.objects.get(campaign=c).status, "sent")

    def test_someone_who_opts_out_before_their_turn_is_skipped(self):
        c = _campaign(self.admin)
        c.status = "queued"
        c.save()
        freeze_recipients(c)
        set_consent(self.yes, False, "settings")
        send_chunk(c.pk, sleep=lambda s: None)
        self.assertEqual(CampaignRecipient.objects.get(campaign=c).status, "skipped")
        self.assertEqual(outbox.drain(), [])

    def test_send_twice_is_refused(self):
        c = _campaign(self.admin)
        with mock.patch("afc_mail.tasks.send_campaign_chunk.delay"):
            self.client.post(f"/mail/campaigns/{c.token}/send/", **_auth(self.admin))
            again = self.client.post(f"/mail/campaigns/{c.token}/send/", **_auth(self.admin))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(CampaignRecipient.objects.filter(campaign=c).count(), 1)

    def test_nobody_consenting_keeps_it_a_draft(self):
        set_consent(self.yes, False, "settings")
        c = _campaign(self.admin)
        with mock.patch("afc_mail.tasks.send_campaign_chunk.delay") as queued:
            res = self.client.post(f"/mail/campaigns/{c.token}/send/", **_auth(self.admin))
        self.assertEqual(res.json()["code"], "mail_no_recipients")
        queued.assert_not_called()
        c.refresh_from_db()
        self.assertEqual(c.status, "draft")

    def test_what_must_be_there_before_sending(self):
        empty = _campaign(self.admin, docs={"en": _doc(_p())})
        self.assertEqual(self.client.post(f"/mail/campaigns/{empty.token}/send/", **_auth(self.admin)).json()["code"], "mail_english_required")
        half = _campaign(self.admin, subject_fr="Nouvelles")
        self.assertEqual(self.client.post(f"/mail/campaigns/{half.token}/send/", **_auth(self.admin)).json()["code"], "mail_version_incomplete_fr")
        design = _campaign(self.admin, mode="design")
        self.assertEqual(self.client.post(f"/mail/campaigns/{design.token}/send/", **_auth(self.admin)).json()["code"], "mail_english_required")

    def test_cancel_marks_the_rest_skipped(self):
        c = _campaign(self.admin)
        c.status = "sending"
        c.save()
        freeze_recipients(c)
        res = self.client.post(f"/mail/campaigns/{c.token}/cancel/", **_auth(self.admin))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(CampaignRecipient.objects.get(campaign=c).status, "skipped")

    @override_settings(OUTBOUND_DELIVERY="live", SES_SMTP_USER="", SES_SMTP_PASSWORD="")
    def test_no_ses_settings_refuses_before_anything_is_frozen(self):
        # A server whose SES steps are not done yet (production on 17 Oct 2026): a plain refusal, and
        # the draft stays a draft, instead of a 500 on the test send and a campaign stuck "sending".
        c = _campaign(self.admin)
        with mock.patch("afc_mail.tasks.send_campaign_chunk.delay") as queued:
            send = self.client.post(f"/mail/campaigns/{c.token}/send/", **_auth(self.admin))
            test = self.client.post(f"/mail/campaigns/{c.token}/test/", **_auth(self.admin))
        for res in (send, test):
            # 409, never a 5xx: nginx swaps an API 502/503/504 for the maintenance page (17 Oct 2026)
            self.assertEqual(res.status_code, 409, res.content[:200])
            self.assertEqual(res.json()["code"], "mail_not_set_up")
        queued.assert_not_called()
        c.refresh_from_db()
        self.assertEqual(c.status, "draft")
        self.assertFalse(CampaignRecipient.objects.filter(campaign=c).exists())

    # A plain placeholder user, not an AWS-shaped key: the security hook (R55) reads AKIA... as a
    # hardcoded access key id even when it is fake, and blocked the v7.1.129 push on it.
    @override_settings(OUTBOUND_DELIVERY="live", SES_SMTP_USER="ses-smtp-test-user", SES_SMTP_PASSWORD="x" * 44)
    def test_each_ses_refusal_of_a_test_send_has_its_own_code(self):
        # The first live test (17 Oct 2026) was refused "not authorized to perform ses:SendRawEmail"
        # and the admin saw "Something went wrong". Each refusal now names what to fix, in a 4xx.
        import smtplib
        c = _campaign(self.admin)
        cases = [
            (smtplib.SMTPDataError(554, b"Access denied: User 'arn:aws:iam::1:user/x' is not authorized to perform 'ses:SendRawEmail'"), "mail_ses_not_authorized"),
            (smtplib.SMTPDataError(554, b"Message rejected: Email address is not verified. The following identities failed the check"), "mail_ses_unverified"),
            (smtplib.SMTPDataError(454, b"Throttling failure: Maximum sending rate exceeded."), "mail_ses_throttled"),
            (smtplib.SMTPAuthenticationError(535, b"Authentication Credentials Invalid"), "mail_ses_login_refused"),
            (ConnectionRefusedError("refused"), "mail_test_refused"),
        ]
        for exc, code in cases:
            with mock.patch("afc_mail.transport.send_one", side_effect=exc):
                res = self.client.post(f"/mail/campaigns/{c.token}/test/", **_auth(self.admin))
            self.assertEqual((res.status_code, res.json()["code"]), (424, code), res.content[:200])
            self.assertNotIn("arn:aws", res.json()["message"])  # SES's own words stay in the log
        self.assertEqual(outbox.drain(), [])

    def test_an_unreachable_mail_server_is_retried_then_the_campaign_is_closed(self):
        c = _campaign(self.admin)
        c.status = "queued"
        c.save()
        freeze_recipients(c)
        # First failures: tried again later, nothing marked, the row still waits its turn.
        with mock.patch("afc_mail.tasks.send_chunk", side_effect=OSError("no route to host")), \
                mock.patch.object(send_campaign_chunk, "retry", side_effect=RuntimeError("retry scheduled")) as retry:
            with self.assertRaises(RuntimeError):
                send_campaign_chunk.run(c.pk)
        retry.assert_called_once()
        self.assertEqual(CampaignRecipient.objects.get(campaign=c).status, "queued")
        # The last retry: the campaign is closed and the row says why, so the tracker adds up.
        send_campaign_chunk.push_request(retries=RETRIES)
        try:
            with mock.patch("afc_mail.tasks.send_chunk", side_effect=OSError("no route to host")):
                self.assertEqual(send_campaign_chunk.run(c.pk), 0)
        finally:
            send_campaign_chunk.pop_request()
        row = CampaignRecipient.objects.get(campaign=c)
        self.assertEqual((row.status, row.error), ("failed", "the mail server could not be reached"))
        c.refresh_from_db()
        self.assertEqual(c.status, "sent")
        self.assertIsNotNone(c.finished_at)


class TrackingAndUnsubscribeTests(TestCase):
    def setUp(self):
        self.user = _user("Clicker", opt_in=True)
        self.c = _campaign()
        self.c.links = campaign_links(self.c)
        self.c.save()
        self.r = CampaignRecipient.objects.create(campaign=self.c, user=self.user, email=self.user.email, status="sent")

    def test_the_pixel_counts_an_open(self):
        res = self.client.get(f"/mail/o/{self.r.token}.gif")
        self.assertEqual(res["Content-Type"], "image/gif")
        self.r.refresh_from_db()
        self.assertEqual(self.r.open_count, 1)
        self.assertIsNotNone(self.r.first_opened_at)

    def test_a_click_counts_and_goes_to_the_campaigns_own_link(self):
        res = self.client.get(f"/mail/c/{self.r.token}/1/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], TOURNAMENTS)
        self.r.refresh_from_db()
        self.assertEqual(self.r.click_count, 1)
        self.assertEqual(CampaignClick.objects.filter(recipient=self.r, link_index=1).count(), 1)

    def test_a_link_we_cannot_place_goes_home_and_counts_nothing(self):
        for path in (f"/mail/c/{self.r.token}/9/", "/mail/c/r_nope/0/"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 302)
            self.assertFalse(res["Location"].startswith("http://testserver"))  # the site, not the API
            self.assertTrue(res["Location"].endswith("/"))
        self.r.refresh_from_db()
        self.assertEqual(self.r.click_count, 0)
        self.assertFalse(CampaignClick.objects.exists())

    def test_get_changes_nothing_post_unsubscribes(self):
        page = self.client.get(f"/mail/unsubscribe/{self.r.token}/")
        self.assertTrue(page.json()["subscribed"])
        self.assertIn("*", page.json()["email"])
        self.assertTrue(EmailSubscription.objects.get(user=self.user).marketing_opt_in)
        # RFC 8058: the mail app posts this form body with no session at all.
        res = self.client.post(f"/mail/unsubscribe/{self.r.token}/", data="List-Unsubscribe=One-Click",
                               content_type="application/x-www-form-urlencoded")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(EmailSubscription.objects.get(user=self.user).marketing_opt_in)
        self.r.refresh_from_db()
        self.assertIsNotNone(self.r.unsubscribed_at)

    def test_resubscribe_and_unknown_token(self):
        self.client.post(f"/mail/unsubscribe/{self.r.token}/")
        res = self.client.post(f"/mail/resubscribe/{self.r.token}/")
        self.assertTrue(res.json()["subscribed"])
        self.assertEqual(self.client.get("/mail/unsubscribe/r_unknown/").status_code, 404)


def _signed(msg, key, version="1"):
    msg = dict(msg)
    msg["SignatureVersion"] = version
    msg["SigningCertURL"] = CERT_URL
    digest = hashes.SHA1() if version == "1" else hashes.SHA256()
    msg["Signature"] = base64.b64encode(key.sign(sns.canonical_string(msg), padding.PKCS1v15(), digest)).decode()
    return msg


@override_settings(SES_SNS_TOPIC_ARN=TOPIC)
class SesEventTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(cls.key.public_key())
                .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
                .sign(cls.key, hashes.SHA256()))
        cls.pem = cert.public_bytes(serialization.Encoding.PEM)

    def setUp(self):
        sns._cert_cache.clear()
        self.user = _user("Evented", opt_in=True)
        self.c = _campaign()
        self.r = CampaignRecipient.objects.create(campaign=self.c, user=self.user, email=self.user.email, status="sent")

    def _post(self, msg):
        fake = mock.Mock(content=self.pem, raise_for_status=lambda: None)
        with mock.patch("afc_mail.sns.requests.get", return_value=fake):
            return self.client.post("/mail/ses-events/", data=json.dumps(msg), content_type="text/plain")

    def _event(self, event):
        event.setdefault("mail", {"tags": {"recipient": [self.r.token], "campaign": [self.c.token]}})
        return {"Type": "Notification", "MessageId": "m1", "TopicArn": TOPIC, "Timestamp": "2026-10-16T12:00:00.000Z",
                "Message": json.dumps(event)}

    def test_delivery_marks_delivered(self):
        res = self._post(_signed(self._event({"eventType": "Delivery"}), self.key))
        self.assertEqual(res.json()["result"], "delivered")
        self.r.refresh_from_db()
        self.assertEqual(self.r.status, "delivered")

    def test_a_permanent_bounce_suppresses_the_address(self):
        res = self._post(_signed(self._event({"eventType": "Bounce", "bounce": {"bounceType": "Permanent", "bounceSubType": "General"}}), self.key, "2"))
        self.assertEqual(res.json()["result"], "bounced")
        self.assertTrue(EmailSuppression.objects.filter(email=self.user.email.lower(), reason="bounce").exists())

    def test_a_complaint_suppresses_and_switches_consent_off(self):
        self._post(_signed(self._event({"eventType": "Complaint", "complaint": {}}), self.key))
        self.assertFalse(EmailSubscription.objects.get(user=self.user).marketing_opt_in)
        self.assertTrue(EmailSuppression.objects.filter(reason="complaint").exists())

    def test_a_forged_signature_is_refused_and_changes_nothing(self):
        msg = _signed(self._event({"eventType": "Complaint", "complaint": {}}), self.key)
        msg["Message"] = msg["Message"].replace("Complaint", "Delivery")
        res = self._post(msg)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "sns_bad_signature")
        self.r.refresh_from_db()
        self.assertEqual(self.r.status, "sent")

    def test_another_topic_or_a_foreign_certificate_is_refused(self):
        other = self._event({"eventType": "Delivery"})
        other["TopicArn"] = "arn:aws:sns:eu-north-1:999:someone-else"
        self.assertEqual(self._post(_signed(other, self.key)).json()["code"], "sns_wrong_topic")
        foreign = _signed(self._event({"eventType": "Delivery"}), self.key)
        foreign["SigningCertURL"] = "https://evil.example.com/cert.pem"
        self.assertEqual(self._post(foreign).json()["code"], "sns_bad_certificate_url")

    def test_a_subscription_is_confirmed_only_through_sns(self):
        msg = {"Type": "SubscriptionConfirmation", "MessageId": "s1", "TopicArn": TOPIC, "Token": "t",
               "Timestamp": "2026-10-16T12:00:00.000Z", "Message": "confirm",
               "SubscribeURL": "https://sns.eu-north-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"}
        fake = mock.Mock(content=self.pem, raise_for_status=lambda: None)
        with mock.patch("afc_mail.sns.requests.get", return_value=fake) as get:
            res = self.client.post("/mail/ses-events/", data=json.dumps(_signed(msg, self.key)), content_type="text/plain")
        self.assertEqual(res.json()["result"], "subscribed")
        self.assertIn("ConfirmSubscription", get.call_args_list[-1].args[0])

    @override_settings(SES_SNS_TOPIC_ARN="")
    def test_no_topic_configured_accepts_nothing(self):
        self.assertEqual(self._post(_signed(self._event({"eventType": "Delivery"}), self.key)).json()["code"],
                         "sns_topic_not_configured")

    def test_a_raw_delivery_is_refused_with_its_own_code_and_logged_without_the_body(self):
        # What SNS posts with "Raw message delivery" ON: the bare SES event, no envelope, no signature
        # (17 Oct 2026, inbox #304 #308). Still refused, but named, and the journal says so.
        bare = {"eventType": "Delivery", "mail": {"destination": [self.user.email],
                                                  "tags": {"recipient": [self.r.token]}}}
        with self.assertLogs("afc_mail.views", level="WARNING") as logs:
            res = self.client.post("/mail/ses-events/", data=json.dumps(bare), content_type="text/plain",
                                   HTTP_X_AMZ_SNS_RAWDELIVERY="true")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "sns_raw_delivery")
        self.assertIn("sns_raw_delivery", logs.output[0])
        self.assertNotIn(self.user.email, " ".join(logs.output))
        self.r.refresh_from_db()
        self.assertEqual(self.r.status, "sent")

    def test_every_refusal_is_logged_with_its_code(self):
        other = self._event({"eventType": "Delivery"})
        other["TopicArn"] = "arn:aws:sns:eu-north-1:999:someone-else"
        with self.assertLogs("afc_mail.views", level="WARNING") as logs:
            self._post(_signed(other, self.key))
        self.assertIn("sns_wrong_topic", logs.output[0])

    def test_an_enveloped_notification_is_not_taken_for_a_raw_delivery(self):
        # The signed envelope also names a Type; only a body WITHOUT one is a raw delivery.
        self.assertFalse(sns.is_raw_delivery(self._event({"eventType": "Delivery"})))
        self.assertTrue(sns.is_raw_delivery({"notificationType": "Bounce", "bounce": {}}))
        self.assertFalse(sns.is_raw_delivery({"Type": "SubscriptionConfirmation"}))


# ── the admin API ───────────────────────────────────────────────────────────────────────────────

class AdminApiTests(_MediaMixin, TestCase):
    def setUp(self):
        self.admin = _head_admin()
        self.player = _user("Plain")

    def _json(self, method, url, body=None, user=None):
        return getattr(self.client, method)(url, data=json.dumps(body or {}), content_type="application/json",
                                            **_auth(user or self.admin))

    def test_no_session_and_a_player_are_refused_on_every_admin_address(self):
        c = _campaign(self.admin)
        tpl = EmailTemplate.objects.create(name="T", data={})
        for method, url in [("get", "/mail/campaigns/"), ("post", "/mail/campaigns/"), ("get", f"/mail/campaigns/{c.token}/"),
                            ("patch", f"/mail/campaigns/{c.token}/"), ("delete", f"/mail/campaigns/{c.token}/"),
                            ("post", f"/mail/campaigns/{c.token}/start-from/"), ("post", f"/mail/campaigns/{c.token}/design/"),
                            ("get", f"/mail/campaigns/{c.token}/preview/"), ("post", f"/mail/campaigns/{c.token}/test/"),
                            ("post", f"/mail/campaigns/{c.token}/send/"), ("post", "/mail/audience/"), ("post", "/mail/assets/"),
                            ("get", "/mail/templates/"), ("post", "/mail/templates/"), ("delete", f"/mail/templates/{tpl.token}/")]:
            self.assertEqual(getattr(self.client, method)(url).status_code, 401, url)
            res = getattr(self.client, method)(url, **_auth(self.player))
            self.assertEqual(res.status_code, 403, url)
            self.assertEqual(res.json()["code"], "head_admin_required")
        self.assertTrue(Campaign.objects.filter(pk=c.pk).exists())

    def test_create_list_page_and_detail(self):
        res = self._json("post", "/mail/campaigns/", {"subject_en": "The group stage", "docs": {"en": _doc_en()},
                                                      "audience": {"everyone": True}, "status": "sent"})
        self.assertEqual(res.status_code, 201, res.content[:300])
        self.assertEqual(res.json()["status"], "draft")  # status is not a writable field (R65)
        self.assertEqual(res.json()["name"], "The group stage")  # listed by its subject
        for i in range(3):
            _campaign(self.admin, name=f"C{i}")
        page = self.client.get("/mail/campaigns/?limit=2&offset=0", **_auth(self.admin)).json()
        self.assertEqual(len(page["results"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["total_count"], 4)
        detail = self.client.get(f"/mail/campaigns/{res.json()['token']}/", **_auth(self.admin)).json()
        for key in ("stats", "links", "docs", "style", "designs", "assets", "preheader_en", "mode"):
            self.assertIn(key, detail)

    def test_a_bad_document_or_style_is_refused_and_only_drafts_change(self):
        c = _campaign(self.admin)
        self.assertEqual(self._json("patch", f"/mail/campaigns/{c.token}/", {"docs": {"en": {"type": "nope"}}}).json()["code"], "mail_doc_invalid")
        self.assertEqual(self._json("patch", f"/mail/campaigns/{c.token}/", {"style": "dark"}).json()["code"], "mail_style_invalid")
        self.assertEqual(self._json("patch", f"/mail/campaigns/{c.token}/", {"mode": "html"}).json()["code"], "mail_mode_invalid")
        self.assertEqual(self._json("patch", f"/mail/campaigns/{c.token}/", {"designs": {"en": {"html": "<script>"}}}).json()["code"],
                         "mail_design_upload_only")
        ok = self._json("patch", f"/mail/campaigns/{c.token}/", {"style": {"theme": "AFC light", **THEMES["AFC light"], "page": "red"}})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["style"]["page"], THEMES["AFC light"]["page"])  # "red" is not a colour it keeps
        c.status = "sent"
        c.save()
        self.assertEqual(self._json("patch", f"/mail/campaigns/{c.token}/", {"subject_en": "x"}).status_code, 409)
        self.assertEqual(self._json("delete", f"/mail/campaigns/{c.token}/").status_code, 409)

    def test_a_draft_can_be_deleted(self):
        c = _campaign(self.admin)
        self.assertEqual(self._json("delete", f"/mail/campaigns/{c.token}/").json()["code"], "mail_draft_deleted")
        self.assertFalse(Campaign.objects.filter(pk=c.pk).exists())

    def test_pictures_and_fonts_upload_and_fakes_are_refused(self):
        auth = _auth(self.admin)
        pic = self.client.post("/mail/assets/", {"kind": "image", "file": SimpleUploadedFile("pic.png", _png(), content_type="image/png")}, **auth)
        self.assertEqual(pic.status_code, 201, pic.content[:300])
        self.assertTrue(pic.json()["url"].startswith("https://"))
        self.assertTrue(pic.json()["token"].startswith("a_"))
        fake = self.client.post("/mail/assets/", {"kind": "image", "file": SimpleUploadedFile("pic.png", b"<?php echo 1;", content_type="image/png")}, **auth)
        self.assertEqual(fake.json()["code"], "mail_not_an_image")
        font = self.client.post("/mail/assets/", {"kind": "font", "file": SimpleUploadedFile("Brand Bold.ttf", b"\x00\x01\x00\x00" + b"\x00" * 60)}, **auth)
        self.assertEqual(font.status_code, 201, font.content[:300])
        self.assertEqual(font.json()["family"], "Brand Bold")
        stored = MailAsset.objects.get(token=font.json()["token"])
        self.assertTrue(stored.file.name.endswith(".ttf") and "Brand" not in stored.file.name)  # opaque name (R87)
        notfont = self.client.post("/mail/assets/", {"kind": "font", "file": SimpleUploadedFile("x.woff2", b"<html>")}, **auth)
        self.assertEqual(notfont.json()["code"], "mail_not_a_font")
        self.assertEqual(self.client.post("/mail/assets/", {"kind": "video"}, **auth).json()["code"], "mail_asset_kind_invalid")
        got = self.client.get(f"/mail/assets/{font.json()['token']}/", **auth)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got["Content-Type"], "font/ttf")
        self.assertEqual(b"".join(got.streaming_content)[:4], b"\x00\x01\x00\x00")
        self.assertEqual(self.client.get(f"/mail/assets/{font.json()['token']}/", **_auth(self.player)).status_code, 403)
        self.assertEqual(self.client.get("/mail/assets/a_nope/", **auth).status_code, 404)

    def test_a_design_upload_is_cleaned_and_can_be_removed(self):
        c = _campaign(self.admin)
        auth = _auth(self.admin)
        res = self.client.post(f"/mail/campaigns/{c.token}/design/",
                               {"language": "fr", "file": SimpleUploadedFile("news.html", EVIL_HTML.encode(), content_type="text/html")}, **auth)
        self.assertEqual(res.status_code, 200, res.content[:300])
        fr = res.json()["designs"]["fr"]
        self.assertNotIn("<script", fr["html"].lower())
        self.assertGreaterEqual(fr["report"]["removed"], 6)
        wrong = self.client.post(f"/mail/campaigns/{c.token}/design/",
                                 {"language": "fr", "file": SimpleUploadedFile("x.txt", b"words")}, **auth)
        self.assertEqual(wrong.json()["code"], "mail_design_wrong_type")
        gone = self.client.delete(f"/mail/campaigns/{c.token}/design/?language=fr", **auth)
        self.assertNotIn("fr", gone.json()["designs"])

    def test_templates_save_list_start_from_and_delete(self):
        sent = _campaign(self.admin, subject_en="Group stage", preheader_en="41 squads")
        sent.status = "sent"
        sent.save()
        made = self._json("post", "/mail/templates/", {"campaign": sent.token})
        self.assertEqual(made.status_code, 201, made.content[:300])
        self.assertEqual(made.json()["name"], "Group stage")
        self.assertEqual(made.json()["source_campaign"], sent.token)
        listed = self.client.get("/mail/templates/", **_auth(self.admin)).json()
        self.assertEqual(listed["total_count"], 1)
        draft = _campaign(self.admin, subject_en="", docs={})
        started = self._json("post", f"/mail/campaigns/{draft.token}/start-from/", {"template": made.json()["token"]}).json()
        self.assertEqual((started["subject_en"], started["preheader_en"]), ("Group stage", "41 squads"))
        self.assertEqual(started["docs"]["en"]["content"][0]["type"], "heading")
        again = self._json("post", f"/mail/campaigns/{draft.token}/start-from/", {"campaign": sent.token}).json()
        self.assertEqual(again["subject_en"], "Group stage")
        blank = self._json("post", f"/mail/campaigns/{draft.token}/start-from/", {"blank": True}).json()
        self.assertEqual((blank["subject_en"], blank["docs"]), ("", {}))
        self.assertEqual(self._json("post", f"/mail/campaigns/{draft.token}/start-from/", {"template": "t_nope"}).status_code, 404)
        self.assertEqual(self._json("delete", f"/mail/templates/{made.json()['token']}/").json()["code"], "mail_template_deleted")
        self.assertEqual(self._json("delete", f"/mail/templates/{made.json()['token']}/").status_code, 404)

    def test_the_tracker_adds_up(self):
        c = _campaign(self.admin)
        c.links = campaign_links(c)
        c.save()
        users = [_user(f"T{i}", opt_in=True) for i in range(3)]
        rows = [CampaignRecipient.objects.create(campaign=c, user=u, email=u.email, status=s)
                for u, s in zip(users, ["delivered", "bounced", "sent"])]
        self.client.get(f"/mail/o/{rows[0].token}.gif")
        self.client.get(f"/mail/c/{rows[0].token}/0/")
        self.client.get(f"/mail/c/{rows[0].token}/0/")
        self.client.post(f"/mail/unsubscribe/{rows[2].token}/")
        d = self.client.get(f"/mail/campaigns/{c.token}/", **_auth(self.admin)).json()
        s = d["stats"]
        self.assertEqual((s["total"], s["sent"], s["delivered"], s["bounced"], s["opened"], s["clicked"], s["unsubscribed"]),
                         (3, 3, 1, 1, 1, 1, 1))
        self.assertEqual(d["links"][0]["clicks"], 2)
        self.assertEqual(d["links"][0]["people"], 1)

    def test_preview_and_test_send_go_to_the_outbox(self):
        outbox.drain()
        c = _campaign(self.admin, subject_pt="Notícias", docs={"en": _doc_en(), "pt": _doc(_p(_t("Olá.")))})
        prev = self.client.get(f"/mail/campaigns/{c.token}/preview/?language=pt", **_auth(self.admin)).json()
        self.assertEqual(prev["subject"], "Notícias")
        self.assertIn("Olá.", prev["html"])
        self.assertGreaterEqual(prev["size_kb"], 1)
        res = self.client.post(f"/mail/campaigns/{c.token}/test/", **_auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content[:300])
        sent = outbox.drain()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], self.admin.email)
        self.assertNotIn("/mail/c/", sent[0]["body"])  # a test send is never tracked
        self.assertNotIn("/mail/o/", sent[0]["body"])
