"""Organization badges, perks and the Verified rules (open organizations, plan 1 Task 1; AFC inbox #582 to #601).

Badges are trust and reach only (owner, 16 Nov 2026): Community, Verified, AFC Partner. What they decide, through
afc_organizers/badges.py: whose results count for the rankings, which events may email players, and whether an AFC
admin may give the Verified tick (six rules, #587 #592).
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_tournament_and_scrims.models import Event

from . import badges
from .models import EventRating, Organization, OrganizationBadgeHistory, OrganizationReport


def make_event(name, org=None, **over):
    kwargs = dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=datetime.date(2026, 7, 1), end_date=datetime.date(2026, 9, 6),
        registration_open_date=datetime.date(2026, 6, 1), registration_end_date=datetime.date(2026, 6, 30),
        prizepool="0", event_rules="rules", event_status="upcoming",
        registration_link="https://example.com/reg", number_of_stages=1, organization=org,
    )
    kwargs.update(over)
    return Event.objects.create(**kwargs)


class BadgeBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="tolu", email="t@x.com", password="x", full_name="Tolu",
                                              role="admin")
        self.org = Organization.objects.create(slug="kopa", name="Kopa Esports")


class BadgeModelTests(BadgeBase):
    def test_default_badge_is_verified_and_self_started_false(self):
        self.assertEqual(self.org.badge, "verified")
        self.assertFalse(self.org.self_started)
        self.assertEqual(self.org.perks, {})

    def test_counts_for_rankings(self):
        for badge, want in (("community", False), ("verified", True), ("partner", True)):
            self.org.badge = badge
            self.assertIs(badges.counts_for_rankings(self.org), want, badge)

    def test_event_may_email(self):
        afc_event = make_event("AFC CUP")
        self.assertTrue(badges.event_may_email(afc_event))
        ev = make_event("KOPA CUP", org=self.org)
        for badge, perks, want in (("community", {}, False), ("verified", {}, False),
                                   ("partner", {"promo": True}, True), ("partner", {"promo": False}, False)):
            Organization.objects.filter(pk=self.org.pk).update(badge=badge, perks=perks)
            ev.refresh_from_db()
            self.assertIs(badges.event_may_email(ev), want, (badge, perks))


class VerifiedRulesTests(BadgeBase):
    def rules(self):
        self.org.refresh_from_db()
        return {r["key"]: r for r in badges.verified_rules(self.org)}

    def test_order_and_keys(self):
        self.assertEqual([r["key"] for r in badges.verified_rules(self.org)], list(badges.RULES))
        self.assertEqual(badges.RULES, ("events", "trophies", "rating", "reports", "profile", "community"))

    def test_finished_events_at_twenty(self):
        for i in range(19):
            make_event("E%d" % i, org=self.org, event_status="completed")
        make_event("NOT DONE", org=self.org, event_status="ongoing")
        self.assertEqual(self.rules()["events"]["value"], 19)
        self.assertFalse(self.rules()["events"]["met"])
        make_event("E19", org=self.org, event_status="completed")
        self.assertTrue(self.rules()["events"]["met"])

    def test_rating_at_four(self):
        self.assertFalse(self.rules()["rating"]["met"])     # no ratings yet
        self.assertIsNone(self.rules()["rating"]["value"])
        ev = make_event("RATED", org=self.org, event_status="completed")
        users = [User.objects.create_user(username="u%d" % i, email="u%d@x.com" % i, password="x", full_name="U")
                 for i in range(20)]
        for i, u in enumerate(users):                        # 19 fours and one 3: 3.95
            EventRating.objects.create(event=ev, user=u, score=3 if i == 0 else 4)
        self.assertFalse(self.rules()["rating"]["met"])
        EventRating.objects.filter(event=ev, user=users[0]).update(score=4)
        self.assertTrue(self.rules()["rating"]["met"])
        self.assertEqual(self.rules()["rating"]["value"], 4.0)

    def test_upheld_reports_in_ninety_days(self):
        self.assertTrue(self.rules()["reports"]["met"])
        r = OrganizationReport.objects.create(organization=self.org, details="no trophy", status="resolved")
        OrganizationReport.objects.filter(pk=r.pk).update(updated_at=timezone.now() - datetime.timedelta(days=89))
        self.assertFalse(self.rules()["reports"]["met"])
        OrganizationReport.objects.filter(pk=r.pk).update(updated_at=timezone.now() - datetime.timedelta(days=91))
        self.assertTrue(self.rules()["reports"]["met"])
        OrganizationReport.objects.create(organization=self.org, details="nothing", status="dismissed")
        self.assertTrue(self.rules()["reports"]["met"])

    def test_profile_needs_logo_about_and_one_social(self):
        self.assertFalse(self.rules()["profile"]["met"])
        Organization.objects.filter(pk=self.org.pk).update(logo="organization_logos/k.png", description="We run cups",
                                                            socials={"x": ""})
        self.assertFalse(self.rules()["profile"]["met"])
        Organization.objects.filter(pk=self.org.pk).update(socials={"x": "", "discord": "https://discord.gg/kopa"})
        self.assertTrue(self.rules()["profile"]["met"])

    def test_admin_ticks(self):
        self.assertFalse(self.rules()["trophies"]["met"])
        self.assertFalse(self.rules()["community"]["met"])
        Organization.objects.filter(pk=self.org.pk).update(trophies_confirmed_at=timezone.now(),
                                                            community_access_at=timezone.now())
        self.assertTrue(self.rules()["trophies"]["met"])
        self.assertTrue(self.rules()["community"]["met"])


class SetBadgeTests(BadgeBase):
    def test_set_badge_partner_switches_perks_on_and_logs(self):
        row = badges.set_badge(self.org, "partner", self.admin, note="Signed")
        self.org.refresh_from_db()
        self.assertEqual(self.org.badge, "partner")
        self.assertEqual(self.org.perks, {k: True for k in badges.PERKS})
        self.assertEqual((row.action, row.from_badge, row.to_badge, row.note, row.by_id),
                         ("badge", "verified", "partner", "Signed", self.admin.pk))
        self.assertEqual(OrganizationBadgeHistory.objects.filter(organization=self.org).count(), 1)

    def test_leaving_partner_clears_perks(self):
        badges.set_badge(self.org, "partner", self.admin)
        badges.set_badge(self.org, "verified", self.admin)
        self.org.refresh_from_db()
        self.assertEqual(self.org.perks, {})

    def test_set_perk_logs(self):
        badges.set_badge(self.org, "partner", self.admin)
        badges.set_perk(self.org, "promo", False, self.admin)
        self.org.refresh_from_db()
        self.assertFalse(self.org.perks["promo"])
        self.assertTrue(self.org.perks["featured"])
        last = OrganizationBadgeHistory.objects.filter(organization=self.org).first()
        self.assertEqual((last.action, last.note), ("perk_off", "promo"))

    def test_unknown_badge_or_perk_refused(self):
        with self.assertRaises(ValueError):
            badges.set_badge(self.org, "gold", self.admin)
        with self.assertRaises(ValueError):
            badges.set_perk(self.org, "free_money", True, self.admin)

    def test_perks_list(self):
        self.assertEqual(badges.PERKS, ("featured", "promo", "stream", "qualifiers", "support", "early",
                                        "fast_reader"))


class PublicBadgeTests(BadgeBase):
    def test_public_page_and_directory_carry_the_badge(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="partner")
        page = self.client.get("/organizers/get-organization-public/kopa/").json()
        self.assertEqual(page["badge"], "partner")
        make_event("PUBLISHED", org=self.org, is_draft=False)
        rows = self.client.get("/organizers/get-organizations-public/").json()["organizations"]
        self.assertEqual([(r["slug"], r["badge"]) for r in rows], [("kopa", "partner")])
