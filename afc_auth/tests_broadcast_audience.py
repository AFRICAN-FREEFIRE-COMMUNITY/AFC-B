# afc_auth/tests_broadcast_audience.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for the BROADCAST AUDIENCE BUILDER (owner backlog item 15, 2026-08-03).
#
# Two things matter more here than the feature itself, and both are tested first-class:
#
#   1. COUNT BEFORE SEND. There is no undo on a broadcast. The send endpoint requires the number
#      the admin was shown (confirmed_count) and 409s when the audience has changed size since.
#      test_send_requires_confirmed_count and test_send_conflicts_when_audience_changed prove an
#      admin cannot send without having seen the number.
#
#   2. EMAIL VOLUME IS REAL. AFC's mail goes through Microsoft 365 (~30/minute, ~1,000/day to new
#      recipients), so a "send to everyone" cannot deliver as email. test_large_email_audience_*
#      prove the warning fires, that a merely-slow blast needs confirm_large_email, and that an
#      over-the-daily-cap blast is REFUSED rather than silently queued.
#
# The rest covers audience correctness (each filter counts the right people, the union/intersection
# rule, no double-counting) and the permission gate.
#
# Auth is a real bearer SessionToken (afc_auth.SessionToken) validated by validate_token, matching
# the sibling afc_auth test modules. deliver_broadcast's email leg starts a daemon thread and calls
# send_email per address; every send test here uses delivery="push" or a mocked deliver_broadcast,
# so no test sends mail or touches the network.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_team.models import Team, TeamMembers

from .audience import (
    EMAIL_COMFORTABLE_MAX,
    EMAIL_DAILY_CAP,
    audience_counts,
    email_volume_assessment,
    parse_audience_spec,
    resolve_audience,
)
from .models import Notifications, SessionToken, User


class BroadcastAudienceTests(TestCase):
    # ── auth helpers ─────────────────────────────────────────────────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    # ── fixtures ─────────────────────────────────────────────────────────────
    # A deliberately small, hand-countable population so every expected number below is obvious:
    #
    #   admin        NG  en  role=admin            (owns no team)
    #   ng_tier1_a   NG  en  Tier 1 team "Alpha"   (owner)
    #   ng_tier1_b   NG  fr  Tier 1 team "Alpha"   (member)
    #   gh_tier2     GH  en  Tier 2 team "Bravo"   (owner)
    #   ng_teamless  NG  pt  no team
    #   suspended    NG  en  no team, status=suspended  -> excluded from every audience
    #   deactivated  NG  en  no team, is_active=False   -> excluded from every audience
    #
    # ELIGIBLE population (what "everyone" means) = 5: admin, ng_tier1_a, ng_tier1_b, gh_tier2,
    # ng_teamless.
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_user", email="admin@x.com", password="x",
            role="admin", country="Nigeria", language="en",
        )
        self.ng_tier1_a = User.objects.create_user(
            username="ng_tier1_a", email="a@x.com", password="x",
            role="player", country="Nigeria", language="en",
        )
        self.ng_tier1_b = User.objects.create_user(
            username="ng_tier1_b", email="b@x.com", password="x",
            role="player", country="Nigeria", language="fr",
        )
        self.gh_tier2 = User.objects.create_user(
            username="gh_tier2", email="c@x.com", password="x",
            role="player", country="Ghana", language="en",
        )
        self.ng_teamless = User.objects.create_user(
            username="ng_teamless", email="d@x.com", password="x",
            role="player", country="Nigeria", language="pt",
        )
        self.suspended = User.objects.create_user(
            username="suspended_user", email="e@x.com", password="x",
            role="player", country="Nigeria", language="en", status="suspended",
        )
        self.deactivated = User.objects.create_user(
            username="deactivated_user", email="f@x.com", password="x",
            role="player", country="Nigeria", language="en",
        )
        self.deactivated.is_active = False
        self.deactivated.save(update_fields=["is_active"])

        # Tier 1 team: owner ng_tier1_a + member ng_tier1_b.
        self.alpha = Team.objects.create(
            team_name="Team Alpha", join_settings="open", team_tier="1",
            team_creator=self.ng_tier1_a, team_owner=self.ng_tier1_a, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.alpha, member=self.ng_tier1_a,
                                   management_role="team_captain")
        TeamMembers.objects.create(team=self.alpha, member=self.ng_tier1_b,
                                   management_role="member")

        # Tier 2 team: owner gh_tier2 only, and deliberately NO TeamMembers row for the owner, so
        # the "a picked team includes its owner even without a membership row" rule is exercised.
        self.bravo = Team.objects.create(
            team_name="Team Bravo", join_settings="open", team_tier="2",
            team_creator=self.gh_tier2, team_owner=self.gh_tier2, country="Ghana",
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _preview(self, actor, spec):
        return self.client.post(
            reverse("broadcast_audience_preview"),
            data=spec, content_type="application/json", **self._auth(actor),
        )

    def _send(self, actor, body):
        return self.client.post(
            reverse("broadcast_audience_send"),
            data=body, content_type="application/json", **self._auth(actor),
        )

    def _count(self, **spec):
        """Resolve a raw spec dict straight through the resolver (no HTTP), for the unit-level
        audience assertions."""
        return audience_counts(parse_audience_spec(spec))["recipient_count"]

    # ══════════════════════════════════════════════════════════════════════════
    # §1  AUDIENCE CORRECTNESS - each filter selects exactly the right people
    # ══════════════════════════════════════════════════════════════════════════

    def test_everyone_counts_eligible_users_only(self):
        # 7 users exist; the suspended and the deactivated one are never messaged.
        self.assertEqual(User.objects.count(), 7)
        self.assertEqual(self._count(everyone=True), 5)

    def test_everyone_can_include_suspended_when_asked(self):
        # An admin may legitimately need to tell suspended users about their suspension.
        self.assertEqual(self._count(everyone=True, include_suspended=True), 6)

    def test_explicit_players(self):
        self.assertEqual(
            self._count(user_ids=[self.ng_teamless.user_id, self.gh_tier2.user_id]), 2
        )

    def test_explicit_team_includes_members_and_owner(self):
        # Alpha: owner + member, both of whom have TeamMembers rows.
        self.assertEqual(self._count(team_ids=[self.alpha.team_id]), 2)
        # Bravo has NO TeamMembers rows at all - only an owner. Leaving the owner out of a message
        # to their own team would be wrong, so the owner is folded in.
        self.assertEqual(self._count(team_ids=[self.bravo.team_id]), 1)

    def test_tier_filter(self):
        # Tier 1 -> the two Alpha players. Tier 2 -> the Bravo owner.
        self.assertEqual(self._count(tiers=["1"]), 2)
        self.assertEqual(self._count(tiers=["2"]), 1)
        self.assertEqual(self._count(tiers=["1", "2"]), 3)

    def test_country_filter(self):
        # Nigeria: admin, ng_tier1_a, ng_tier1_b, ng_teamless (suspended/deactivated excluded).
        self.assertEqual(self._count(countries=["Nigeria"]), 4)
        self.assertEqual(self._count(countries=["Ghana"]), 1)

    def test_role_filter(self):
        self.assertEqual(self._count(roles=["admin"]), 1)
        self.assertEqual(self._count(roles=["player"]), 4)

    def test_language_filter(self):
        self.assertEqual(self._count(languages=["fr"]), 1)
        self.assertEqual(self._count(languages=["en"]), 3)

    def test_categories_intersect(self):
        # "Tier 1" AND "Nigeria" is the two Alpha players, not tier-1-plus-all-Nigerians.
        self.assertEqual(self._count(tiers=["1"], countries=["Nigeria"]), 2)
        # "Tier 1" AND "Ghana" is nobody: the Ghanaian player is Tier 2.
        self.assertEqual(self._count(tiers=["1"], countries=["Ghana"]), 0)
        # Narrowing further by language leaves one.
        self.assertEqual(self._count(tiers=["1"], countries=["Nigeria"], languages=["fr"]), 1)

    def test_selections_union_without_double_counting(self):
        # Explicit team Alpha (2 people) UNION the country Ghana (1 person) = 3.
        self.assertEqual(self._count(team_ids=[self.alpha.team_id], countries=["Ghana"]), 3)
        # A person reachable BOTH ways counts once: ng_tier1_a is on Alpha and is also picked
        # explicitly, so the total is still 2, not 3.
        self.assertEqual(
            self._count(team_ids=[self.alpha.team_id], user_ids=[self.ng_tier1_a.user_id]), 2
        )

    def test_empty_spec_selects_nobody(self):
        # An empty form must never resolve to "everyone". The endpoint 400s on this, and the
        # resolver independently returns an empty queryset.
        self.assertEqual(resolve_audience(parse_audience_spec({})).count(), 0)

    def test_email_recipient_count_excludes_users_without_email(self):
        User.objects.filter(pk=self.ng_teamless.pk).update(email="")
        counts = audience_counts(parse_audience_spec({"everyone": True}))
        self.assertEqual(counts["recipient_count"], 5)
        self.assertEqual(counts["email_recipient_count"], 4)

    # ══════════════════════════════════════════════════════════════════════════
    # §2  PERMISSIONS - this is a site-wide megaphone, not a general admin tool
    # ══════════════════════════════════════════════════════════════════════════

    def test_non_admin_is_refused_on_every_endpoint(self):
        for url_name, payload in (
            ("broadcast_audience_preview", {"everyone": True}),
            ("broadcast_audience_send",
             {"everyone": True, "message": "hi", "confirmed_count": 5}),
        ):
            resp = self.client.post(
                reverse(url_name), data=payload, content_type="application/json",
                **self._auth(self.ng_teamless),
            )
            self.assertEqual(resp.status_code, 403, f"{url_name}: {resp.content}")
        options = self.client.get(
            reverse("broadcast_audience_options"), **self._auth(self.ng_teamless)
        )
        self.assertEqual(options.status_code, 403, options.content)
        # Nothing was delivered.
        self.assertEqual(Notifications.objects.count(), 0)

    def test_unauthenticated_is_refused(self):
        resp = self.client.post(
            reverse("broadcast_audience_preview"),
            data={"everyone": True}, content_type="application/json",
        )
        self.assertIn(resp.status_code, (400, 401), resp.content)

    # ══════════════════════════════════════════════════════════════════════════
    # §3  PREVIEW - the count the admin must see before sending
    # ══════════════════════════════════════════════════════════════════════════

    def test_preview_returns_counts_and_sample(self):
        resp = self._preview(self.admin, {"everyone": True, "limit": 3})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["recipient_count"], 5)
        self.assertEqual(body["email_recipient_count"], 5)
        self.assertEqual(len(body["sample"]), 3)          # paged, not the whole audience
        self.assertEqual(body["sample_total_count"], 5)
        self.assertTrue(body["has_more"])
        # The sample never leaks email addresses, only whether the channel can reach the person.
        self.assertNotIn("email", body["sample"][0])
        self.assertIn("has_email", body["sample"][0])

    def test_preview_rejects_an_empty_selection(self):
        resp = self._preview(self.admin, {})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_preview_reports_email_volume_and_recommended_channel(self):
        resp = self._preview(self.admin, {"everyone": True})
        body = resp.json()
        # A 5-person audience is unremarkable, so both channels are fine.
        self.assertEqual(body["email_volume"]["level"], "ok")
        self.assertFalse(body["email_volume"]["requires_confirmation"])
        self.assertEqual(body["recommended_delivery"], "both")

    # ══════════════════════════════════════════════════════════════════════════
    # §4  SEND GUARD 1 - count before send, always
    # ══════════════════════════════════════════════════════════════════════════

    def test_send_requires_confirmed_count(self):
        # No confirmed_count means the admin never previewed. Refuse.
        resp = self._send(self.admin, {"everyone": True, "message": "Hello everyone"})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("confirm", resp.json()["message"].lower())
        self.assertEqual(Notifications.objects.count(), 0)

    def test_send_conflicts_when_audience_changed(self):
        # The admin previewed at 5, but a new player signed up before they hit send.
        User.objects.create_user(
            username="latecomer", email="late@x.com", password="x",
            role="player", country="Nigeria",
        )
        resp = self._send(
            self.admin, {"everyone": True, "message": "Hello", "confirmed_count": 5}
        )
        self.assertEqual(resp.status_code, 409, resp.content)
        body = resp.json()
        self.assertEqual(body["recipient_count"], 6)      # the NEW number, so they can re-read it
        self.assertEqual(body["confirmed_count"], 5)
        self.assertEqual(Notifications.objects.count(), 0)

    def test_send_with_matching_count_delivers_push(self):
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Server maintenance tonight",
             "title": "Heads up", "delivery": "push", "confirmed_count": 5},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["recipient_count"], 5)
        # One in-app notification per eligible recipient, and none for the suspended/deactivated.
        self.assertEqual(Notifications.objects.count(), 5)
        self.assertFalse(Notifications.objects.filter(user=self.suspended).exists())
        self.assertFalse(Notifications.objects.filter(user=self.deactivated).exists())

    def test_send_to_a_filtered_audience_reaches_only_those_people(self):
        resp = self._send(
            self.admin,
            {"tiers": ["1"], "countries": ["Nigeria"], "message": "Tier 1 briefing",
             "delivery": "push", "confirmed_count": 2},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        notified = set(Notifications.objects.values_list("user_id", flat=True))
        self.assertEqual(notified, {self.ng_tier1_a.user_id, self.ng_tier1_b.user_id})

    def test_send_requires_a_message(self):
        resp = self._send(self.admin, {"everyone": True, "confirmed_count": 5})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Notifications.objects.count(), 0)

    # ══════════════════════════════════════════════════════════════════════════
    # §5  SEND GUARD 2 - email volume (Microsoft 365: ~30/min, ~1,000/day)
    # ══════════════════════════════════════════════════════════════════════════

    def test_volume_assessment_levels(self):
        # Small: unremarkable.
        ok = email_volume_assessment(50)
        self.assertEqual(ok["level"], "ok")
        self.assertFalse(ok["requires_confirmation"])
        self.assertFalse(ok["blocked"])

        # Above the comfortable size: slow, and the admin must confirm.
        slow = email_volume_assessment(EMAIL_COMFORTABLE_MAX + 1)
        self.assertEqual(slow["level"], "slow")
        self.assertTrue(slow["requires_confirmation"])
        self.assertFalse(slow["blocked"])

        # Above the provider's daily cap: cannot deliver at all.
        blocked = email_volume_assessment(EMAIL_DAILY_CAP + 1)
        self.assertEqual(blocked["level"], "blocked")
        self.assertTrue(blocked["blocked"])
        # The warning is honest arithmetic the admin can act on, not a vague "this is a lot".
        self.assertGreater(blocked["estimated_minutes"], 0)
        self.assertIn(str(EMAIL_DAILY_CAP), blocked["message"])

    def _bulk_users(self, count, prefix):
        """Create `count` extra eligible users so a volume threshold can be crossed for real."""
        User.objects.bulk_create([
            User(username=f"{prefix}{i}", email=f"{prefix}{i}@x.com", password="x",
                 role="player", country="Kenya", status="active", is_active=True)
            for i in range(count)
        ])

    def test_large_email_audience_requires_explicit_confirmation(self):
        # Push the audience just past the comfortable size so the email channel needs a confirm.
        self._bulk_users(EMAIL_COMFORTABLE_MAX, "bulk")
        total = EMAIL_COMFORTABLE_MAX + 5

        # Warned, and refused without confirm_large_email.
        refused = self._send(
            self.admin,
            {"everyone": True, "message": "Big news", "delivery": "email",
             "confirmed_count": total},
        )
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(refused.json()["code"], "email_volume_confirmation_required")
        self.assertEqual(refused.json()["email_volume"]["level"], "slow")
        # The admin is steered at the channel that CAN deliver this many.
        self.assertEqual(refused.json()["recommended_delivery"], "push")

        # The same send with the confirmation goes through. deliver_broadcast is patched so the
        # test never opens an SMTP connection.
        with patch("afc_auth.views.deliver_broadcast", return_value=(total, total)) as mock_deliver:
            accepted = self._send(
                self.admin,
                {"everyone": True, "message": "Big news", "delivery": "email",
                 "confirmed_count": total, "confirm_large_email": True},
            )
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertTrue(mock_deliver.called)

    def test_over_daily_cap_email_is_refused_outright(self):
        # An audience larger than the provider's daily cap cannot deliver as email today, so we
        # say no rather than queueing mail that will never arrive.
        self._bulk_users(EMAIL_DAILY_CAP + 10, "mass")
        total = EMAIL_DAILY_CAP + 15

        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Site-wide announcement", "delivery": "email",
             "confirmed_count": total, "confirm_large_email": True},   # confirming does NOT help
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(resp.json()["email_volume"]["blocked"])
        self.assertEqual(resp.json()["recommended_delivery"], "push")
        self.assertEqual(Notifications.objects.count(), 0)

    def test_large_audience_can_still_be_pushed_in_app(self):
        # The whole point of steering people to in-app: it delivers instantly at any size.
        self._bulk_users(EMAIL_DAILY_CAP + 10, "mass")
        total = EMAIL_DAILY_CAP + 15
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Site-wide announcement", "delivery": "push",
             "confirmed_count": total},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Notifications.objects.count(), total)

    def test_preview_of_a_large_audience_defaults_to_push(self):
        self._bulk_users(EMAIL_COMFORTABLE_MAX, "bulk")
        body = self._preview(self.admin, {"everyone": True}).json()
        self.assertEqual(body["email_volume"]["level"], "slow")
        self.assertEqual(body["recommended_delivery"], "push")

    # ══════════════════════════════════════════════════════════════════════════
    # §6  OPTIONS - the filter dropdowns are built from real data
    # ══════════════════════════════════════════════════════════════════════════

    def test_options_lists_real_filter_values(self):
        resp = self.client.get(
            reverse("broadcast_audience_options"), **self._auth(self.admin)
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_users"], 5)

        countries = {row["value"]: row["count"] for row in body["countries"]}
        self.assertEqual(countries["Nigeria"], 4)
        self.assertEqual(countries["Ghana"], 1)

        tiers = {row["value"]: row["count"] for row in body["tiers"]}
        self.assertEqual(tiers["1"], 2)          # both Alpha members

        roles = {row["value"]: row["count"] for row in body["roles"]}
        self.assertEqual(roles["admin"], 1)
        self.assertEqual(roles["player"], 4)

        # The mail limits travel with the options so the composer's copy cannot drift from the
        # numbers the backend actually enforces.
        self.assertEqual(body["email_limits"]["daily_cap"], EMAIL_DAILY_CAP)
        self.assertEqual(body["email_limits"]["comfortable_max"], EMAIL_COMFORTABLE_MAX)
