"""The admin Organizations page's badge actions (open organizations plan 1 Task 6; AFC inbox #582 to #601, #590).

AFC platform admins give the Verified tick only when all six rules are met, make a Verified organization an AFC
Partner (every perk on), switch perks, tick the two rules nothing on the site records, and take badges away. The
owners hear about it by notification and email. Organizers and ordinary players are refused everywhere.
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth import outbox
from afc_auth.models import Notifications, Roles, SessionToken, User, UserRoles

from .models import EventRating, Organization, OrganizationMember
from .tests_badges import make_event


def person(name, role="player"):
    user = User.objects.create(username=name, email=f"{name}@gmail.com", full_name=name.title(), role=role,
                               password="x")
    tok = SessionToken.objects.create(user=user, token=f"tok_{name}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


class AdminBadgeTests(TestCase):
    def setUp(self):
        outbox.drain()
        self.admin, self.auth = person("tolu", role="admin")
        UserRoles.objects.create(user=self.admin, role=Roles.objects.get_or_create(role_name="organizer_admin")[0])
        self.owner, self.owner_auth = person("kemi")
        _p, self.player_auth = person("dayo")
        self.org = Organization.objects.create(slug="lagoon", name="Lagoon League", badge="community",
                                               self_started=True)
        OrganizationMember.objects.create(organization=self.org, user=self.owner, role="owner")

    def post(self, what, data, auth=None):
        return self.client.post(f"/organizers/admin/organization/lagoon/{what}/", data,
                                content_type="application/json", **(auth if auth is not None else self.auth))

    def meet_auto_rules(self):
        """20 finished events, a 4.5 rating, no reports, a complete profile."""
        events = [make_event("E%d" % i, org=self.org, event_status="completed") for i in range(20)]
        raters = [User.objects.create(username="r%d" % i, email="r%d@x.com" % i, full_name="R", password="x")
                  for i in range(2)]
        EventRating.objects.create(event=events[0], user=raters[0], score=5)
        EventRating.objects.create(event=events[0], user=raters[1], score=4)
        Organization.objects.filter(pk=self.org.pk).update(logo="organization_logos/l.png", description="Cups",
                                                            socials={"discord": "https://discord.gg/lagoon"})

    def badge(self):
        self.org.refresh_from_db()
        return self.org.badge

    def test_verify_refused_until_rules_met(self):
        r = self.post("badge", {"badge": "verified"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "badge_rules_not_met"))
        self.assertEqual([x["key"] for x in r.json()["rules"]][0], "events")
        self.meet_auto_rules()
        self.assertEqual(self.post("badge", {"badge": "verified"}).json()["code"], "badge_rules_not_met")
        self.assertEqual(self.post("rule-tick", {"rule": "trophies", "on": True}).status_code, 200)
        self.assertEqual(self.post("rule-tick", {"rule": "community", "on": True, "note": "Joined their Discord"})
                         .status_code, 200)
        self.assertEqual(self.badge(), "community")

    def test_verify_when_met_logs_and_notifies(self):
        self.meet_auto_rules()
        self.post("rule-tick", {"rule": "trophies", "on": True})
        self.post("rule-tick", {"rule": "community", "on": True})
        outbox.drain()
        r = self.post("badge", {"badge": "verified", "note": "Well run cups"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["organization"]["badge"], self.badge()), ("verified", "verified"))
        note = Notifications.objects.get(user=self.owner, notification_type="org_badge_changed")
        self.assertEqual((note.title, note.target_type, note.target_id),
                         ("Lagoon League is Verified", "organization", "lagoon"))
        mails = outbox.drain()
        self.assertEqual([(m["to"], m["subject"]) for m in mails], [("kemi@gmail.com", "Lagoon League: your AFC badge changed")])
        self.assertIn("Well run cups", mails[0]["body"])
        detail = self.client.get("/organizers/admin/get-organization/lagoon/", **self.auth).json()
        self.assertEqual(detail["badge_history"][0]["to_badge"], "verified")
        self.assertTrue(all(x["met"] for x in detail["verified_rules"]))

    def test_partner_needs_verified(self):
        r = self.post("badge", {"badge": "partner"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "badge_partner_needs_verified"))
        Organization.objects.filter(pk=self.org.pk).update(badge="verified")
        r = self.post("badge", {"badge": "partner"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(r.json()["organization"]["perks"].values()))

    def test_end_partnership_keeps_tick(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="partner", perks={"promo": True})
        self.assertEqual(self.post("badge", {"badge": "verified"}).status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual((self.org.badge, self.org.perks), ("verified", {}))

    def test_remove_tick_and_unchanged(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="verified")
        self.assertEqual(self.post("badge", {"badge": "verified"}).json()["code"], "badge_unchanged")
        self.assertEqual(self.post("badge", {"badge": "community", "note": "Trophies not handed over"}).status_code, 200)
        self.assertEqual(self.badge(), "community")
        self.assertEqual(self.post("badge", {"badge": "gold"}).json()["code"], "badge_invalid")

    def test_perk_needs_partner(self):
        r = self.post("perk", {"perk": "promo", "on": False})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "perk_needs_partner"))
        Organization.objects.filter(pk=self.org.pk).update(badge="partner", perks={"promo": True})
        r = self.post("perk", {"perk": "promo", "on": False})
        self.assertEqual((r.status_code, r.json()["organization"]["perks"]["promo"]), (200, False))
        self.assertEqual(self.post("perk", {"perk": "free_money", "on": True}).json()["code"], "perk_invalid")

    def test_rule_tick(self):
        r = self.post("rule-tick", {"rule": "trophies", "on": True, "note": "Checked with the winners"})
        rules = {x["key"]: x for x in r.json()["verified_rules"]}
        self.assertTrue(rules["trophies"]["met"])
        self.post("rule-tick", {"rule": "trophies", "on": False})
        self.org.refresh_from_db()
        self.assertIsNone(self.org.trophies_confirmed_at)
        self.assertEqual(self.post("rule-tick", {"rule": "vibes", "on": True}).json()["code"], "rule_tick_invalid")

    def test_waiting_filter_and_badge_tabs(self):
        Organization.objects.create(slug="kopa", name="Kopa Esports")                       # verified
        Organization.objects.create(slug="owls", name="Night Owls", badge="community", status="suspended")
        listed = lambda tab: [o["slug"] for o in self.client.get(
            "/organizers/admin/get-all-organizations/?badge=" + tab, **self.auth).json()["results"]]
        self.assertEqual(listed("waiting"), [])
        self.meet_auto_rules()
        self.assertEqual(listed("waiting"), ["lagoon"])
        self.assertEqual(listed("verified"), ["kopa"])
        self.assertEqual(listed("community"), ["lagoon"])
        self.assertEqual(listed("suspended"), ["owls"])
        row = self.client.get("/organizers/admin/get-all-organizations/?badge=waiting", **self.auth).json()["results"][0]
        self.assertEqual((row["badge"], row["self_started"], row["rules_met"]), ("community", True, 4))

    def test_non_admin_refused_everywhere(self):
        for auth in (self.owner_auth, self.player_auth):
            for what, data in (("badge", {"badge": "verified"}), ("perk", {"perk": "promo", "on": True}),
                               ("rule-tick", {"rule": "trophies", "on": True})):
                r = self.post(what, data, auth=auth)
                self.assertEqual((r.status_code, r.json()["code"]), (403, "not_permission_manage_organizations"))
            r = self.post("badge", {"badge": "verified"}, auth={})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.badge(), "community")
