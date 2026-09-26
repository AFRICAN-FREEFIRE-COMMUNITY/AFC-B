"""afc_qr tests: the four page types (targets.py) and the four endpoints (views.py).

Run: .venv/Scripts/python.exe manage.py test afc_qr --noinput --keepdb
"""
from datetime import date

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from afc_auth.models import News, Roles, SessionToken, User, UserRoles
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event

from . import targets
from .models import QrLink
from .views import QR_LINKS_PER_HOUR

PHONE = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Mobile Safari/537.36"


def _bearer(user):
    """A real SessionToken for `user` (the afc_feedback test pattern: no password handled)."""
    session = SessionToken.objects.create(user=user, token=f"qr-test-{user.user_id}",
                                          expires_at=timezone.now() + timezone.timedelta(hours=3))
    return f"Bearer {session.token}"


class QrTestBase(APITestCase):
    def setUp(self):
        cache.clear()  # rate limits and the repeat-scan filter live in the cache, which outlives a test
        self.owner = User.objects.create_user(username="qrowner", email="qrowner@example.test", password="x")
        self.stranger = User.objects.create_user(username="qrstranger", email="qrstranger@example.test", password="x")
        self.head_admin = User.objects.create_user(username="qrhead", email="qrhead@example.test", password="x",
                                                   role="admin")
        UserRoles.objects.create(user=self.head_admin, role=Roles.objects.get_or_create(role_name="head_admin")[0])
        self.team = Team.objects.create(team_name="QR Squad", team_owner=self.owner, team_creator=self.owner)
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="team", event_type="online",
            max_teams_or_players=12, event_name="QR Cup", event_mode="br",
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 2),
            registration_open_date=date(2026, 9, 1), registration_end_date=date(2026, 9, 30),
            prizepool="1000", prize_distribution={}, event_rules="none", event_status="upcoming",
            registration_link="", number_of_stages=1, slug="qr-cup", is_draft=False,
        )
        self.news = News.objects.create(news_title="QR news", content="x", category="general",
                                        author=self.head_admin, slug="qr-news", is_published=True)

    def make(self, target_type, ref, **extra):
        return self.client.post("/qr/link/", {"target_type": target_type, "ref": ref}, format="json", **extra)


class TargetTests(QrTestBase):
    def test_find_each_type_by_its_public_ref(self):
        self.assertEqual(targets.find_target("team", "QR Squad").team_id, self.team.team_id)
        self.assertEqual(targets.find_target("player", "qrowner").user_id, self.owner.user_id)
        self.assertEqual(targets.find_target("event", "qr-cup").event_id, self.event.event_id)
        self.assertEqual(targets.find_target("news", "qr-news").news_id, self.news.news_id)
        self.assertIsNone(targets.find_target("team", "no such team"))
        self.assertIsNone(targets.find_target("nonsense", "x"))

    def test_unpublished_pages_have_no_qr(self):
        Event.objects.filter(pk=self.event.pk).update(is_draft=True)
        News.objects.filter(pk=self.news.pk).update(is_published=False)
        self.assertIsNone(targets.find_target("event", "qr-cup"))
        self.assertIsNone(targets.find_target("news", "qr-news"))

    def test_path_follows_a_rename(self):
        link = QrLink.objects.create(token="q_0000000001", target_type="team", target_id=self.team.team_id)
        self.team.team_name = "Renamed Squad"
        self.team.save()
        self.assertEqual(targets.describe(link, None)["path"], "/teams/Renamed%20Squad")

    def test_only_owners_see_stats(self):
        link = QrLink.objects.create(token="q_0000000002", target_type="team", target_id=self.team.team_id)
        self.assertTrue(targets.can_see_stats(self.owner, link))
        self.assertFalse(targets.can_see_stats(self.stranger, link))
        self.assertFalse(targets.can_see_stats(None, link))
        self.assertTrue(targets.can_see_stats(self.head_admin, link))
        mine = QrLink.objects.create(token="q_0000000003", target_type="player", target_id=self.stranger.user_id)
        self.assertTrue(targets.can_see_stats(self.stranger, mine))
        self.assertFalse(targets.can_see_stats(self.owner, mine))


class EndpointTests(QrTestBase):
    def test_link_is_created_once_and_shared(self):
        a = self.make("team", "QR Squad")
        b = self.make("team", "QR Squad", HTTP_AUTHORIZATION=_bearer(self.stranger))
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.data["token"], b.data["token"])
        self.assertRegex(a.data["token"], r"^q_[0-9a-f]{10}$")
        self.assertEqual(a.data["url_path"], f"/q/{a.data['token']}")
        self.assertEqual(a.data["name"], "QR Squad")
        self.assertEqual(QrLink.objects.count(), 1)

    def test_link_refusals_carry_codes(self):
        self.assertEqual(self.make("car", "x").data["code"], "bad_target_type")
        self.assertEqual(self.make("team", "  ").data["code"], "missing_ref")
        r = self.make("team", "ghost")
        self.assertEqual((r.status_code, r.data["code"]), (404, "target_not_found"))

    def test_scan_counts_once_per_minute_per_scanner(self):
        token = self.make("news", "qr-news").data["token"]
        first = self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=PHONE, HTTP_X_FORWARDED_FOR="41.1.1.1")
        again = self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=PHONE, HTTP_X_FORWARDED_FOR="41.1.1.1")
        other = self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=PHONE, HTTP_X_FORWARDED_FOR="41.1.1.2")
        self.assertEqual(first.data, {"path": "/news/qr-news", "counted": True})
        self.assertFalse(again.data["counted"])
        self.assertTrue(other.data["counted"])
        link = QrLink.objects.get(token=token)
        self.assertEqual(link.scan_count, 2)
        self.assertIsNotNone(link.last_scanned_at)

    def test_link_previews_and_info_do_not_count(self):
        token = self.make("event", "qr-cup").data["token"]
        for agent in ("WhatsApp/2.23.20.0 A", "Mozilla/5.0 (compatible; Discordbot/2.0)", "TelegramBot (like TwitterBot)",
                      "facebookexternalhit/1.1", "Slackbot-LinkExpanding 1.0", "Googlebot/2.1", ""):
            self.assertFalse(self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=agent).data["counted"], agent)
        info = self.client.get(f"/qr/info/{token}/", HTTP_USER_AGENT=PHONE)
        self.assertEqual((info.data["name"], info.data["path"]), ("QR Cup", "/tournaments/qr-cup"))
        self.assertEqual(QrLink.objects.get(token=token).scan_count, 0)

    def test_stats_owner_only(self):
        token = self.make("team", "QR Squad").data["token"]
        self.assertEqual(self.client.get(f"/qr/stats/{token}/").data["code"], "authentication_credentials_not_provided")
        self.assertEqual(self.client.get(f"/qr/stats/{token}/", HTTP_AUTHORIZATION="Bearer nope").data["code"],
                         "invalid_expired_token")
        refused = self.client.get(f"/qr/stats/{token}/", HTTP_AUTHORIZATION=_bearer(self.stranger))
        self.assertEqual((refused.status_code, refused.data["code"]), (403, "not_page_owner"))
        ok = self.client.get(f"/qr/stats/{token}/", HTTP_AUTHORIZATION=_bearer(self.owner))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data, {"scan_count": 0, "last_scanned_at": None})

    def test_unknown_or_malformed_token(self):
        for token in ("q_ffffffffff", "1", "q_FFFFFFFFFF"):
            r = self.client.get(f"/qr/info/{token}/")
            self.assertEqual((r.status_code, r.data["code"]), (404, "qr_not_found"), token)
            r = self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=PHONE)
            self.assertEqual((r.status_code, r.data["code"]), (404, "qr_not_found"), token)

    def test_a_page_unpublished_after_printing_stops_resolving(self):
        token = self.make("news", "qr-news").data["token"]
        News.objects.filter(pk=self.news.pk).update(is_published=False)
        r = self.client.post(f"/qr/scan/{token}/", HTTP_USER_AGENT=PHONE)
        self.assertEqual((r.status_code, r.data["code"]), (404, "qr_not_found"))

    def test_create_is_rate_limited(self):
        for _ in range(QR_LINKS_PER_HOUR):
            self.assertEqual(self.make("team", "QR Squad", HTTP_X_FORWARDED_FOR="9.9.9.9").status_code, 200)
        r = self.make("team", "QR Squad", HTTP_X_FORWARDED_FOR="9.9.9.9")
        self.assertEqual((r.status_code, r.data["code"]), (429, "rate_limited"))
        # another address is unaffected
        self.assertEqual(self.make("team", "QR Squad", HTTP_X_FORWARDED_FOR="9.9.9.8").status_code, 200)
