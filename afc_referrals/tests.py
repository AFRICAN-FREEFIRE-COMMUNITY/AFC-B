"""afc_referrals tests: claims and their refusals, counting through the REAL write paths (the signals,
not a direct call), awards, and the admin gate.

Run: .venv/Scripts/python.exe manage.py test afc_referrals --noinput --keepdb
"""
import secrets
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from afc_auth.models import Notifications, Roles, SessionToken, User, UserRoles
from afc_shop.models import Coupon, Order
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import Event, RegisteredCompetitors

from . import engine
from .models import ProgramPrize, Referral, ReferralProgram, Reward

PHONE = "Mozilla/5.0 (Linux; Android 14) Mobile"


def _bearer(user):
    session = SessionToken.objects.create(user=user, token=secrets.token_hex(16),
                                          expires_at=timezone.now() + timedelta(hours=3))
    return f"Bearer {session.token}"


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.referrer = User.objects.create_user(username="refhost", email="host@example.test", password="x",
                                                 country="Nigeria")
        self.head = User.objects.create_user(username="refhead", email="head@example.test", password="x", role="admin")
        UserRoles.objects.create(user=self.head, role=Roles.objects.get_or_create(role_name="head_admin")[0])
        now = timezone.now()
        self.program = ReferralProgram.objects.create(
            slug="bounty-drive", name="Bounty drive", starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=10), is_published=True, count_rule=ReferralProgram.RULE_SIGNUP)
        self.code = engine.code_for(self.program, self.referrer)

    def newcomer(self, name="newbie", active=True):
        return User.objects.create_user(username=name, email=f"{name}@example.test", password="x", is_active=active)

    def claim(self, user, code=None, click=""):
        return self.client.post("/referrals/claim/", {"code": code or self.code.code, "click_token": click},
                                format="json", HTTP_AUTHORIZATION=_bearer(user))


class ClaimTests(Base):
    def test_landing_and_click_then_claim_counts_a_verified_signup(self):
        land = self.client.get(f"/referrals/r/{self.code.code.lower()}/")
        self.assertEqual((land.status_code, land.data["referrer"]), (200, "refhost"))
        click = self.client.post("/referrals/click/", {"code": self.code.code}, format="json").data["click_token"]
        user = self.newcomer()
        r = self.claim(user, click=click)
        self.assertEqual((r.status_code, r.data["status"]), (200, "counted"))

    def test_refusals_carry_codes(self):
        self.assertEqual(self.claim(self.referrer).data["code"], "self_referral")
        self.assertEqual(self.claim(self.newcomer("a"), code="NOPE123").data["code"], "bad_code")
        old = self.newcomer("oldtimer")
        User.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(days=30))
        self.assertEqual(self.claim(User.objects.get(pk=old.pk)).data["code"], "account_not_new")
        twice = self.newcomer("twice")
        self.claim(twice)
        self.assertEqual(self.claim(twice).data["code"], "already_referred")
        ReferralProgram.objects.filter(pk=self.program.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.claim(self.newcomer("late")).data["code"], "program_not_active")

    def test_an_account_made_before_the_click_is_not_new(self):
        early = self.newcomer("early")
        click = self.client.post("/referrals/click/", {"code": self.code.code}, format="json").data["click_token"]
        self.assertEqual(self.claim(early, click=click).data["code"], "account_not_new")

    def test_scope_countries_and_users(self):
        ReferralProgram.objects.filter(pk=self.program.pk).update(scope="countries", countries=["Ghana"])
        self.program.refresh_from_db()
        self.assertFalse(engine.eligible_referrer(self.program, self.referrer))
        self.assertEqual(self.claim(self.newcomer("c1")).data["code"], "not_eligible")
        ReferralProgram.objects.filter(pk=self.program.pk).update(countries=["Ghana", "Nigeria"])
        self.program.refresh_from_db()
        self.assertTrue(engine.eligible_referrer(self.program, self.referrer))
        ReferralProgram.objects.filter(pk=self.program.pk).update(scope="users")
        self.program.refresh_from_db()
        self.assertFalse(engine.eligible_referrer(self.program, self.referrer))
        self.program.users.add(self.referrer)
        self.assertTrue(engine.eligible_referrer(self.program, self.referrer))

    def test_a_burst_from_one_network_is_held_not_thrown_away(self):
        for i in range(engine.BURST_LIMIT):
            self.assertEqual(self.claim(self.newcomer(f"b{i}")).data["status"], "counted")
        self.assertEqual(self.claim(self.newcomer("b-extra")).data["status"], "flagged")

    def test_claim_needs_an_account(self):
        r = self.client.post("/referrals/claim/", {"code": self.code.code}, format="json")
        self.assertEqual(r.status_code, 401)


class CountingTests(Base):
    """Each rule is triggered by the model save the site really does, so the signals are what is tested."""

    def pending(self, rule, **extra):
        ReferralProgram.objects.filter(pk=self.program.pk).update(count_rule=rule, **extra)
        user = self.newcomer(f"u-{rule}")
        self.assertEqual(self.claim(user).data["status"], "pending")
        return user

    def status_of(self, user):
        return Referral.objects.get(referred=user).status

    def test_signup_counts_when_the_email_is_confirmed(self):
        user = self.newcomer("unverified", active=False)
        self.assertEqual(self.claim(user).data["status"], "pending")
        user.is_active = True
        user.save()
        self.assertEqual(self.status_of(user), "counted")

    def test_team_join_counts(self):
        user = self.pending(ReferralProgram.RULE_TEAM)
        team = Team.objects.create(team_name="Ref Squad", team_owner=self.referrer, team_creator=self.referrer)
        TeamMembers.objects.create(team=team, member=user)
        self.assertEqual(self.status_of(user), "counted")

    def test_event_registration_counts_only_the_chosen_event(self):
        def event(slug):
            return Event.objects.create(
                competition_type="tournament", participant_type="solo", event_type="online",
                max_teams_or_players=12, event_name=slug, event_mode="br", start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 2), registration_open_date=date(2026, 9, 1),
                registration_end_date=date(2026, 9, 30), prizepool="0", prize_distribution={}, event_rules="-",
                event_status="upcoming", registration_link="", number_of_stages=1, slug=slug, is_draft=False)
        chosen, other = event("chosen-cup"), event("other-cup")
        user = self.pending(ReferralProgram.RULE_EVENT, count_event=chosen)
        RegisteredCompetitors.objects.create(event=other, user=user, status="registered")
        self.assertEqual(self.status_of(user), "pending")
        RegisteredCompetitors.objects.create(event=chosen, user=user, status="registered")
        self.assertEqual(self.status_of(user), "counted")

    def test_first_paid_order_counts(self):
        user = self.pending(ReferralProgram.RULE_PURCHASE)
        order = Order.objects.create(user=user, status="pending")
        self.assertEqual(self.status_of(user), "pending")
        order.status = "paid"
        order.save()
        self.assertEqual(self.status_of(user), "counted")

    def test_nothing_counts_after_the_program_ends(self):
        user = self.pending(ReferralProgram.RULE_TEAM)
        ReferralProgram.objects.filter(pk=self.program.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        team = Team.objects.create(team_name="Late Squad", team_owner=self.referrer, team_creator=self.referrer)
        TeamMembers.objects.create(team=team, member=user)
        self.assertEqual(self.status_of(user), "pending")


class AwardTests(Base):
    def test_milestone_welcome_and_coupon(self):
        ProgramPrize.objects.create(program=self.program, kind="milestone", threshold=2, prize_type="cash",
                                    cash_amount=Decimal("5"))
        ProgramPrize.objects.create(program=self.program, kind="welcome", prize_type="coupon",
                                    coupon_discount_type="percent", coupon_discount_value=Decimal("10"))
        first, second = self.newcomer("w1"), self.newcomer("w2")
        self.claim(first)
        self.assertFalse(Reward.objects.filter(user=self.referrer).exists())
        self.claim(second)
        cash = Reward.objects.get(user=self.referrer)
        self.assertEqual((cash.prize.prize_type, cash.status), ("cash", "pending"))
        welcome = Reward.objects.get(user=first)
        self.assertEqual(welcome.status, "delivered")
        self.assertEqual(Coupon.objects.get(pk=welcome.coupon_id).max_uses, 1)
        self.assertTrue(Notifications.objects.filter(user=self.referrer, notification_type="referral_reward").exists())
        # a third referral does not pay the same milestone twice
        self.claim(self.newcomer("w3"))
        self.assertEqual(Reward.objects.filter(user=self.referrer).count(), 1)

    def test_rank_prizes_after_the_end_only(self):
        ProgramPrize.objects.create(program=self.program, kind="rank", rank=1, prize_type="custom", custom_text="Jersey")
        self.claim(self.newcomer("r1"))
        with self.assertRaises(engine.ClaimRefused):
            engine.award_ranks(self.program)
        self.program.ends_at = timezone.now() - timedelta(minutes=1)
        self.program.save()
        self.assertEqual(len(engine.award_ranks(self.program)), 1)
        self.assertEqual(engine.award_ranks(self.program), [])  # idempotent


class MineAndAdminTests(Base):
    def test_profile_card_shows_code_and_numbers(self):
        ProgramPrize.objects.create(program=self.program, kind="milestone", threshold=3, prize_type="custom",
                                    custom_text="Hoodie")
        self.claim(self.newcomer("m1"))
        r = self.client.get("/referrals/mine/", HTTP_AUTHORIZATION=_bearer(self.referrer))
        card = r.data["programs"][0]
        self.assertEqual((card["code"], card["counted"], card["next_milestone"]["remaining"], card["rank"]),
                         (self.code.code, 1, 2, 1))

    def test_admin_gate_and_create(self):
        payload = {"name": "Ghana push", "starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T00:00:00Z",
                   "scope": "countries", "countries": ["Ghana"], "count_rule": "team", "program_code": "GHANA26",
                   "prizes": [{"kind": "milestone", "threshold": 5, "prize_type": "custom", "custom_text": "Jersey"}]}
        self.assertEqual(self.client.post("/referrals/admin/programs/", payload, format="json",
                                          HTTP_AUTHORIZATION=_bearer(self.referrer)).status_code, 403)
        r = self.client.post("/referrals/admin/programs/", payload, format="json",
                             HTTP_AUTHORIZATION=_bearer(self.head))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["slug"], r.data["program_code"], len(r.data["prizes"])), ("ghana-push", "GHANA26", 1))
        bad = dict(payload, name="x", scope="countries", countries=[])
        self.assertEqual(self.client.post("/referrals/admin/programs/", bad, format="json",
                                          HTTP_AUTHORIZATION=_bearer(self.head)).data["code"], "countries_required")

    def test_admin_can_count_a_held_referral_and_deliver_a_prize(self):
        ProgramPrize.objects.create(program=self.program, kind="milestone", threshold=1, prize_type="custom",
                                    custom_text="Cap")
        for i in range(engine.BURST_LIMIT + 1):
            self.claim(self.newcomer(f"h{i}"))
        held = Referral.objects.get(status="flagged")
        r = self.client.post(f"/referrals/admin/referrals/{held.public_token}/decide/", {"action": "count"},
                             format="json", HTTP_AUTHORIZATION=_bearer(self.head))
        self.assertEqual(r.data["status"], "counted")
        reward = Reward.objects.get(user=self.referrer)
        d = self.client.post(f"/referrals/admin/rewards/{reward.public_token}/deliver/", {"note": "sent"},
                             format="json", HTTP_AUTHORIZATION=_bearer(self.head))
        self.assertEqual(d.data["status"], "delivered")
        again = self.client.post(f"/referrals/admin/rewards/{reward.public_token}/deliver/", {},
                                 format="json", HTTP_AUTHORIZATION=_bearer(self.head))
        self.assertEqual(again.data["code"], "reward_not_pending")
        funnel = self.client.get("/referrals/admin/programs/bounty-drive/", HTTP_AUTHORIZATION=_bearer(self.head)).data["funnel"]
        # every claim in this test typed the code: none came through an opened link
        self.assertEqual((funnel["signups_via_link"], funnel["signups_typed"]), (0, funnel["signups"]))
        export = self.client.get("/referrals/admin/programs/bounty-drive/export/", HTTP_AUTHORIZATION=_bearer(self.head))
        self.assertEqual(export.status_code, 200)
        self.assertIn(b"refhost", export.content)
