"""Anyone starts an organization (open organizations, plan 1 Task 2; owner 16 Nov 2026, AFC inbox #582 #583).

organizers/start/check-name/ and organizers/start/: a signed-in person starts a Community organization; reserved
names are refused by whole word; a name is taken whatever its case or spacing; three self-started organizations per
person, three starts a day; the bot check guards the start.
"""
import io
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from PIL import Image

from afc_auth.models import Roles, SessionToken, User, UserRoles

from .models import Organization, OrganizationBadgeHistory, OrganizationMember
from . import views_start


def user_and_auth(name):
    user = User.objects.create(username=name, email=f"{name}@x.com", full_name=name.title(), role="player",
                               password="x")
    tok = SessionToken.objects.create(user=user, token=f"tok_{name}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


def png():
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 160, 80)).save(out, "PNG")
    out.seek(0)
    out.name = "logo.png"
    return out


class StartBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user, self.auth = user_and_auth("ade")

    def start(self, name="Kopa Esports", auth=None, **over):
        data = {"name": name, "country": "NG", "description": "Weekly cups in Lagos.", "email": "hi@kopa.gg",
                "rules_accepted": "true"}
        data.update(over)
        return self.client.post("/organizers/start/", data, **(auth if auth is not None else self.auth))

    def check(self, name, auth=None):
        return self.client.post("/organizers/start/check-name/", {"name": name}, content_type="application/json",
                                **(auth if auth is not None else self.auth))


class NameTests(StartBase):
    def test_check_name_reserved_whole_word(self):
        for name, ok in (("AFC Lagos Cup", False), ("Freefire Kings", False), ("Free  Fire Nation", False),
                         ("garena", False), ("Booyah Boys", False), ("African Free Fire Community", False),
                         ("Garena-ish Cup", False), ("Garenaish Cup", True), ("Afcon Stars", True), ("Booyahs", True)):
            r = self.check(name)
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r.json()["ok"], ok, name)
            if not ok:
                self.assertEqual(r.json()["code"], "org_name_reserved", name)

    def test_check_name_taken(self):
        Organization.objects.create(slug="kopa-esports", name="Kopa Esports")
        r = self.check("kopa   esports ")
        self.assertEqual((r.json()["ok"], r.json()["code"]), (False, "org_name_taken"))

    def test_check_name_length(self):
        for name in ("K", "Ko", "x" * 61, "   "):
            self.assertEqual(self.check(name).json()["code"], "org_name_invalid", name)

    def test_check_name_ok_gives_slug(self):
        r = self.check("Kopa Esports")
        self.assertEqual(r.json(), {"ok": True, "slug": "kopa-esports"})

    def test_normalize(self):
        self.assertEqual(views_start.normalize_name("  Kopa   ESPORTS "), "kopa esports")


class StartTests(StartBase):
    def test_start_creates_community_org_owner_and_role(self):
        r = self.start(logo=png())
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()["organization"]
        self.assertEqual((body["slug"], body["name"], body["badge"]), ("kopa-esports", "Kopa Esports", "community"))
        org = Organization.objects.get(slug="kopa-esports")
        self.assertEqual((org.badge, org.self_started, org.country, org.created_by_id),
                         ("community", True, "NG", self.user.pk))
        self.assertTrue(org.logo.name.startswith("organization_logos/"))
        self.assertTrue(OrganizationMember.objects.filter(organization=org, user=self.user, role="owner",
                                                          status="active").exists())
        self.assertTrue(UserRoles.objects.filter(user=self.user, role__role_name="organizer").exists())
        self.assertEqual(OrganizationBadgeHistory.objects.get(organization=org).action, "started")

    def test_start_refuses_without_rules(self):
        r = self.start(rules_accepted="")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "org_rules_not_accepted"))
        self.assertFalse(Organization.objects.exists())

    def test_start_refuses_bad_country_and_reserved_name(self):
        self.assertEqual(self.start(country="XX").json()["code"], "org_country_invalid")
        self.assertEqual(self.start(name="AFC Cup").json()["code"], "org_name_reserved")
        self.assertFalse(Organization.objects.exists())

    def test_start_refuses_a_file_that_is_not_a_picture(self):
        bad = io.BytesIO(b"<html>not a logo</html>")
        bad.name = "logo.png"
        r = self.start(logo=bad)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "not_an_image"))

    def test_start_limit_three(self):
        for i in range(3):
            self.assertEqual(self.start(name="Kopa %d" % i).status_code, 201)
        cache.clear()
        r = self.start(name="Kopa 4")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "org_start_limit"))

    def test_start_rate_limited(self):
        Organization.objects.all().delete()
        for i in range(3):
            self.assertEqual(self.start(name="Kopa %d" % i).status_code, 201)
        Organization.objects.all().delete()
        r = self.start(name="Kopa 9")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "org_start_rate_limited"))

    def test_start_anonymous_401(self):
        r = self.start(auth={})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "authorization_header_required"))
        self.assertEqual(self.check("Kopa", auth={}).status_code, 401)

    def test_start_bot_check_refuses(self):
        with mock.patch("afc_auth.bot_protection.verify", return_value=(False, "no_token")):
            r = self.start()
        self.assertEqual((r.status_code, r.json()["code"]), (400, "bot_check_failed"))
        self.assertFalse(Organization.objects.exists())

    def test_same_name_twice_one_wins(self):
        _other, other_auth = user_and_auth("bisi")
        self.assertEqual(self.start(name="Kopa Esports").status_code, 201)
        r = self.start(name="kopa  esports", auth=other_auth)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "org_name_taken"))
        self.assertEqual(Organization.objects.count(), 1)
