"""
afc_auth/test_workspace_addresses.py - the admin, organizer and vendor addresses carry a slug,
a username or a token, and the old numeric ones keep working.

Owner rule R22 (2026-09-13), the workspace half. Proves: a coupon's slug follows its code (and
a rename keeps the old address); the admin coupon, admin order and admin player detail endpoints
answer `moved_to` for a legacy id; events/resolve/ and leaderboards/standalone/resolve/ answer
the id for a slug and the slug for an id; a standalone leaderboard gets its slug on save and the
backfill fills the rows that predate it.
"""
from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from afc_auth.models import SessionToken, SlugHistory, User
from afc_leaderboard.models import StandaloneLeaderboard
from afc_shop.models import Coupon, Order
from afc_tournament_and_scrims.models import Event


def _user(name, role="player"):
    user = User.objects.create(username=name, email=f"{name}@x.com", full_name=name.title(), role=role, password="x")
    tok = SessionToken.objects.create(user=user, token=f"tok_{name}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


def _event(creator, name="Address Cup"):
    return Event.objects.create(
        event_name=name, competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )


class CouponAddressTests(TestCase):
    def setUp(self):
        self.admin, self.auth = _user("admin", role="admin")

    def test_slug_follows_the_code_and_a_second_coupon_does_not_collide(self):
        a = Coupon.objects.create(code="WELCOME10", discount_type="percent", discount_value="10.00")
        b = Coupon.objects.create(code="SUMMER25", discount_type="percent", discount_value="25.00")
        self.assertEqual((a.slug, b.slug), ("welcome10", "summer25"))

    def test_detail_by_slug_legacy_id_and_retired_slug(self):
        c = Coupon.objects.create(code="WELCOME10", discount_type="percent", discount_value="10.00")
        client = Client()
        r = client.post("/shop/get-coupon-details/", {"ref": "welcome10"}, content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["coupon_details"]["slug"], "welcome10")
        self.assertNotIn("moved_to", r.json())
        r = client.post("/shop/get-coupon-details/", {"coupon_id": c.pk}, content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], "/a/shop/coupons/welcome10")
        c.code = "WELCOME15"
        c.save()
        self.assertTrue(SlugHistory.objects.filter(model="coupon", old_slug="welcome10").exists())
        r = client.post("/shop/get-coupon-details/", {"ref": "welcome10"}, content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], "/a/shop/coupons/welcome15")
        r = client.post("/shop/get-total-coupon-uses/", {"ref": "welcome15"}, content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["total_uses"], 0)
        r = client.post("/shop/get-coupon-details/", {"ref": "gone"}, content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 404)

    def test_list_carries_the_slug(self):
        Coupon.objects.create(code="LISTED", discount_type="fixed", discount_value="5.00")
        r = Client().get("/shop/view-all-coupons/", **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = r.json().get("coupons") or r.json().get("data") or r.json()
        self.assertTrue(any(row.get("slug") == "listed" for row in rows), r.content[:300])


class AdminOrderAddressTests(TestCase):
    def test_detail_by_token_and_by_legacy_id(self):
        admin, auth = _user("admin", role="admin")
        buyer, _ = _user("buyer")
        order = Order.objects.create(user=buyer, total="10.00")
        client = Client()
        r = client.get("/shop/get-order-details-for-admin/", {"ref": order.public_token}, **auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["order"]["public_token"], order.public_token)
        self.assertNotIn("moved_to", r.json())
        r = client.get("/shop/get-order-details-for-admin/", {"order_id": order.pk}, **auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], f"/a/shop/orders/{order.public_token}")


class AdminPlayerAddressTests(TestCase):
    def test_detail_by_username_and_by_legacy_id(self):
        admin, auth = _user("admin", role="admin")
        player, _ = _user("sharpshooter")
        client = Client()
        r = client.post("/player/get-player-details/", {"ref": "sharpshooter"}, content_type="application/json", **auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["player_id"], player.user_id)
        self.assertNotIn("moved_to", r.json())
        r = client.post("/player/get-player-details/", {"player_id": player.user_id}, content_type="application/json", **auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["moved_to"], "/a/players/sharpshooter")
        r = client.post("/player/get-player-details/", {"ref": "nobody"}, content_type="application/json", **auth)
        self.assertEqual(r.status_code, 404)


class EventResolveTests(TestCase):
    def test_slug_and_id_both_resolve(self):
        user, auth = _user("viewer")
        event = _event(user)
        client = Client()
        r = client.get("/events/resolve/", {"ref": event.slug}, **auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json(), {"event_id": event.event_id, "slug": "address-cup", "event_name": "Address Cup"})
        r = client.get("/events/resolve/", {"ref": event.event_id}, **auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["slug"], "address-cup")
        self.assertEqual(client.get("/events/resolve/", {"ref": "missing"}, **auth).status_code, 404)
        self.assertEqual(client.get("/events/resolve/", {"ref": event.slug}).status_code, 400)


class StandaloneAddressTests(TestCase):
    def setUp(self):
        self.admin, self.auth = _user("admin", role="admin")

    def _lb(self, name):
        return StandaloneLeaderboard.objects.create(
            name=name, format="team", placement_points={"1": 12}, kill_point=1.0, creator=self.admin,
        )

    def test_slug_follows_the_name_and_resolves(self):
        lb = self._lb("Friday Night Scrims")
        self.assertEqual(lb.slug, "friday-night-scrims")
        client = Client()
        r = client.get("/leaderboards/standalone/resolve/", {"ref": "friday-night-scrims"}, **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json(), {"id": lb.id, "slug": "friday-night-scrims", "name": "Friday Night Scrims"})
        r = client.get("/leaderboards/standalone/resolve/", {"ref": lb.id}, **self.auth)
        self.assertEqual(r.json()["slug"], "friday-night-scrims")
        lb.name = "Saturday Night Scrims"
        lb.save()
        r = client.get("/leaderboards/standalone/resolve/", {"ref": "friday-night-scrims"}, **self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["slug"], "saturday-night-scrims")
        self.assertEqual(client.get("/leaderboards/standalone/resolve/", {"ref": "nope"}, **self.auth).status_code, 404)

    def test_rows_carry_the_slug(self):
        self._lb("Listed Board")
        r = Client().get("/leaderboards/standalone/", **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = r.json().get("results") or r.json().get("leaderboards") or []
        self.assertTrue(any(row.get("slug") == "listed-board" for row in rows), r.content[:300])

    def test_backfill_fills_the_rows_that_predate_slugs(self):
        a = self._lb("Board A")
        b = self._lb("Board B")
        StandaloneLeaderboard.objects.filter(pk__in=[a.pk, b.pk]).update(slug=None)
        out = StringIO()
        call_command("backfill_standalone_slugs", stdout=out)
        self.assertIn("2 changed", out.getvalue())
        self.assertEqual(
            set(StandaloneLeaderboard.objects.values_list("slug", flat=True)), {"board-a", "board-b"},
        )
        out = StringIO()
        call_command("backfill_standalone_slugs", stdout=out)
        self.assertIn("0 changed", out.getvalue())
