"""afc_mail: consent, unsubscribe, audience, sending, tracking, SES events, admin API (inbox #259).

Nothing here reaches a real mailbox: the test runner forces settings.OUTBOUND_DELIVERY to "outbox"
(afc/settings.py) and afc_mail.transport asks afc_auth.outbox first. The SNS tests sign their own
messages with a throwaway certificate, served in place of Amazon's through a patched requests.get.

Run: .venv\\Scripts\\python.exe manage.py test afc_mail
"""
import base64
import datetime
import json
import uuid
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth import outbox
from afc_auth.models import Roles, SessionToken, User, UserRoles

from . import sns
from .content import body_to_html, extract_links, render_for_recipient
from .models import Campaign, CampaignClick, CampaignRecipient, EmailSubscription, EmailSuppression
from .recipients import audience_count, freeze_recipients, set_consent, suppress
from .tasks import send_chunk
from .transport import build_message

TOPIC = "arn:aws:sns:eu-north-1:123456789012:afc-ses-events"
CERT_URL = "https://sns.eu-north-1.amazonaws.com/SimpleNotificationService-test.pem"


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


def _campaign(admin=None, **kw):
    data = dict(name="October news", subject_en="AFC news", body_en="Hello **players**.\n\nRead [the rules](https://africanfreefirecommunity.com/rules) or https://africanfreefirecommunity.com/tournaments",
                audience={"everyone": True})
    data.update(kw)
    return Campaign.objects.create(created_by=admin, **data)


class ConsentTests(TestCase):
    def test_no_session_is_refused(self):
        self.assertEqual(self.client.get("/mail/preferences/").status_code, 401)

    def test_off_by_default_then_on_with_a_timestamp(self):
        u = _user("Ama")
        self.assertFalse(self.client.get("/mail/preferences/", **_auth(u)).json()["marketing_opt_in"])
        res = self.client.post("/mail/preferences/", data=json.dumps({"marketing_opt_in": True}),
                               content_type="application/json", **_auth(u))
        self.assertTrue(res.json()["marketing_opt_in"])
        sub = EmailSubscription.objects.get(user=u)
        self.assertIsNotNone(sub.opted_in_at)
        self.assertEqual(sub.source, "settings")

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


class ContentTests(TestCase):
    def test_links_are_found_in_order_and_once(self):
        links = extract_links("[a](https://x.example/1) then https://x.example/2", "https://x.example/1 again")
        self.assertEqual(links, ["https://x.example/1", "https://x.example/2"])

    def test_text_is_escaped_and_bold_and_links_render(self):
        out = body_to_html("<script>x</script> **bold** [go](https://x.example)")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn('href="https://x.example"', out)

    def test_a_recipient_gets_tracked_links_a_pixel_and_an_unsubscribe_link(self):
        c = _campaign()
        c.links = extract_links(c.body_en)
        c.save()
        r = CampaignRecipient.objects.create(campaign=c, email="a@gmail.com")
        subject, html, text = render_for_recipient(c, r)
        self.assertIn(f"/mail/c/{r.token}/0/", html)
        self.assertIn(f"/mail/c/{r.token}/1/", html)
        self.assertIn(f"/mail/o/{r.token}.gif", html)
        self.assertIn(f"/email/unsubscribe/{r.token}", html)
        self.assertNotIn("https://africanfreefirecommunity.com/rules\"", html)  # never the raw link

    def test_the_readers_language_is_used_when_the_campaign_has_it(self):
        c = _campaign(subject_fr="Nouvelles AFC", body_fr="Bonjour.")
        r = CampaignRecipient(campaign=c, email="b@gmail.com", language="fr", token="r_x")
        subject, html, _text = render_for_recipient(c, r)
        self.assertEqual(subject, "Nouvelles AFC")
        self.assertIn("Se désabonner", html)

    def test_headers_carry_one_click_unsubscribe_and_the_ses_tags(self):
        with override_settings(SES_CONFIGURATION_SET="afc-marketing"):
            msg = build_message("a@gmail.com", "Hi", "<p>x</p>", "x", one_click_url="https://api.example/mail/unsubscribe/r_1/",
                                page_url="https://site.example/email/unsubscribe/r_1", tags={"campaign": "c_1", "recipient": "r_1"})
        self.assertEqual(msg["List-Unsubscribe"], "<https://api.example/mail/unsubscribe/r_1/>")
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(msg["X-SES-CONFIGURATION-SET"], "afc-marketing")
        self.assertIn("recipient=r_1", msg["X-SES-MESSAGE-TAGS"])
        self.assertIn("news.africanfreefirecommunity.com", msg["From"])


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

    def test_cancel_marks_the_rest_skipped(self):
        c = _campaign(self.admin)
        c.status = "sending"
        c.save()
        freeze_recipients(c)
        res = self.client.post(f"/mail/campaigns/{c.token}/cancel/", **_auth(self.admin))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(CampaignRecipient.objects.get(campaign=c).status, "skipped")


class TrackingAndUnsubscribeTests(TestCase):
    def setUp(self):
        self.user = _user("Clicker", opt_in=True)
        self.c = _campaign()
        self.c.links = extract_links(self.c.body_en)
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
        self.assertEqual(res["Location"], "https://africanfreefirecommunity.com/tournaments")
        self.r.refresh_from_db()
        self.assertEqual(self.r.click_count, 1)
        self.assertEqual(CampaignClick.objects.filter(recipient=self.r, link_index=1).count(), 1)

    def test_a_link_number_the_campaign_does_not_have_is_404(self):
        self.assertEqual(self.client.get(f"/mail/c/{self.r.token}/9/").status_code, 404)
        self.assertEqual(self.client.get("/mail/c/r_nope/0/").status_code, 404)

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


class AdminApiTests(TestCase):
    def setUp(self):
        self.admin = _head_admin()
        self.player = _user("Plain")

    def test_no_session_and_a_player_are_refused(self):
        self.assertEqual(self.client.get("/mail/campaigns/").status_code, 401)
        res = self.client.get("/mail/campaigns/", **_auth(self.player))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "head_admin_required")

    def test_create_list_page_and_detail(self):
        res = self.client.post("/mail/campaigns/", data=json.dumps({"name": "N1", "subject_en": "S", "body_en": "B",
                                                                    "audience": {"everyone": True}, "status": "sent"}),
                               content_type="application/json", **_auth(self.admin))
        self.assertEqual(res.status_code, 201, res.content[:300])
        self.assertEqual(res.json()["status"], "draft")  # status is not a writable field (R65)
        for i in range(3):
            _campaign(self.admin, name=f"C{i}")
        page = self.client.get("/mail/campaigns/?limit=2&offset=0", **_auth(self.admin)).json()
        self.assertEqual(len(page["results"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["total_count"], 4)
        token = res.json()["token"]
        detail = self.client.get(f"/mail/campaigns/{token}/", **_auth(self.admin)).json()
        self.assertIn("stats", detail)
        self.assertIn("links", detail)

    def test_a_name_is_required_and_only_drafts_are_edited(self):
        res = self.client.post("/mail/campaigns/", data=json.dumps({"subject_en": "S"}),
                               content_type="application/json", **_auth(self.admin))
        self.assertEqual(res.json()["code"], "mail_name_required")
        c = _campaign(self.admin)
        c.status = "sent"
        c.save()
        res = self.client.patch(f"/mail/campaigns/{c.token}/", data=json.dumps({"subject_en": "x"}),
                                content_type="application/json", **_auth(self.admin))
        self.assertEqual(res.status_code, 409)

    def test_the_tracker_adds_up(self):
        c = _campaign(self.admin)
        c.links = extract_links(c.body_en)
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
        c = _campaign(self.admin, subject_pt="Notícias", body_pt="Olá.")
        prev = self.client.get(f"/mail/campaigns/{c.token}/preview/?language=pt", **_auth(self.admin)).json()
        self.assertEqual(prev["subject"], "Notícias")
        res = self.client.post(f"/mail/campaigns/{c.token}/test/", **_auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content[:300])
        sent = outbox.drain()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], self.admin.email)
        self.assertNotIn("/mail/c/", sent[0]["body"])  # a test send is never tracked
