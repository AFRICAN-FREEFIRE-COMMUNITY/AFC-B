"""
afc_auth/test_tokens.py - the nameless things get an opaque address, and the old numeric one
keeps working.

Owner rule R22 (2026-09-13). Proves the token half of afc_auth/slugs.py on the two models that use
it, Order and RecruitmentApplication: a token is minted once on save and never changes, a narrowed
save still carries it, a legacy numeric id resolves with `moved_to` (200, never a 301), a token is
never resolved for somebody else's order, and the backfill command fills every row it finds.
"""
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_auth.slugs import resolve_by_token
from afc_player_market.models import RecruitmentApplication, RecruitmentPost
from afc_shop.models import Order
from afc_team.models import Team


def _user(name):
    user = User.objects.create(username=name, email=f"{name}@x.com", full_name=name.title(), password="x")
    tok = SessionToken.objects.create(user=user, token=f"tok_{name}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


class OrderTokenTests(TestCase):
    def setUp(self):
        self.buyer, self.buyer_auth = _user("buyer")
        self.other, self.other_auth = _user("other")
        self.order = Order.objects.create(user=self.buyer, total="10.00")

    def test_a_token_is_minted_once_and_never_changes(self):
        self.assertRegex(self.order.public_token, r"^o_[0-9a-f]{10}$")
        before = self.order.public_token
        self.order.status = "paid"
        self.order.save()
        self.assertEqual(self.order.public_token, before)

    def test_a_row_that_predates_tokens_gets_one_on_a_narrowed_save(self):
        Order.objects.filter(pk=self.order.pk).update(public_token=None)
        order = Order.objects.get(pk=self.order.pk)
        order.save(update_fields=["public_token"])
        order.refresh_from_db()
        self.assertRegex(order.public_token, r"^o_[0-9a-f]{10}$")

    def test_two_orders_never_share_a_token(self):
        second = Order.objects.create(user=self.buyer, total="5.00")
        self.assertNotEqual(second.public_token, self.order.public_token)

    def test_detail_by_token_and_by_legacy_id(self):
        c = Client()
        r = c.get("/shop/get-order-details/", {"ref": self.order.public_token}, **self.buyer_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["order"]["public_token"], self.order.public_token)
        self.assertNotIn("moved_to", r.json())
        # the old /orders/<id> link: the order, plus where it lives now
        r = c.get("/shop/get-order-details/", {"order_id": self.order.pk}, **self.buyer_auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], f"/orders/{self.order.public_token}")
        # nothing: 404 with a sentence
        r = c.get("/shop/get-order-details/", {"ref": "o_0000000000"}, **self.buyer_auth)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["message"], "Order not found.")

    def test_somebody_elses_order_is_not_found_by_token_or_by_id(self):
        c = Client()
        for params in ({"ref": self.order.public_token}, {"order_id": self.order.pk}):
            r = c.get("/shop/get-order-details/", params, **self.other_auth)
            self.assertEqual(r.status_code, 404, params)

    def test_my_orders_carry_the_token(self):
        r = Client().get("/shop/get-my-orders/", **self.buyer_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = r.json()["orders"]
        self.assertEqual([row["public_token"] for row in rows], [self.order.public_token])


class ApplicationTokenTests(TestCase):
    def setUp(self):
        self.owner, self.owner_auth = _user("owner")
        self.player, self.player_auth = _user("player")
        self.stranger, self.stranger_auth = _user("stranger")
        self.team = Team.objects.create(
            team_name="Falcons", team_tag="FAL", country="KE", join_settings="open",
            team_owner=self.owner, team_creator=self.owner,
        )
        self.post = RecruitmentPost.objects.create(
            post_type="TEAM_RECRUITMENT", post_expiry_date=timezone.now().date(),
            created_by=self.owner, team=self.team,
        )
        self.app = RecruitmentApplication.objects.create(
            player=self.player, recruitment_post=self.post, team=self.team,
        )

    def test_a_token_is_minted_with_the_application_prefix(self):
        self.assertRegex(self.app.public_token, r"^a_[0-9a-f]{10}$")

    def test_detail_by_token_and_by_legacy_id(self):
        c = Client()
        r = c.get("/player-market/application-details/", {"ref": self.app.public_token}, **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["public_token"], self.app.public_token)
        self.assertNotIn("moved_to", r.json())
        r = c.get("/player-market/application-details/", {"application_id": self.app.pk}, **self.owner_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["moved_to"], f"/player-markets/applications/{self.app.public_token}")
        # a stranger is still turned away, by either address
        r = c.get("/player-market/application-details/", {"ref": self.app.public_token}, **self.stranger_auth)
        self.assertEqual(r.status_code, 403)
        r = c.get("/player-market/application-details/", {"ref": "a_0000000000"}, **self.player_auth)
        self.assertEqual(r.status_code, 404)

    def test_my_applications_carry_the_token(self):
        r = Client().get("/player-market/view-my-applications/", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        rows = body if isinstance(body, list) else (body.get("applications") or body.get("data") or [])
        self.assertTrue(any(row.get("public_token") == self.app.public_token for row in rows), r.content[:300])


class ResolveByTokenTests(TestCase):
    def test_the_prefix_is_checked(self):
        user, _ = _user("u")
        order = Order.objects.create(user=user, total="1.00")
        # an application token never resolves an order, even with the same hex
        self.assertEqual(resolve_by_token(Order, "a_" + order.public_token[2:], "o"), (None, None))
        self.assertEqual(resolve_by_token(Order, "", "o"), (None, None))
        self.assertEqual(resolve_by_token(Order, "not-a-thing", "o"), (None, None))
        self.assertEqual(resolve_by_token(Order, str(order.pk), "o"), (order, order.public_token))


class BackfillCommandTests(TestCase):
    def test_fills_every_row_and_is_idempotent(self):
        user, _ = _user("u")
        a = Order.objects.create(user=user, total="1.00")
        b = Order.objects.create(user=user, total="2.00")
        Order.objects.filter(pk__in=[a.pk, b.pk]).update(public_token=None)
        out = StringIO()
        call_command("backfill_public_tokens", stdout=out)
        self.assertIn("2 orders minted", out.getvalue())
        tokens = set(Order.objects.values_list("public_token", flat=True))
        self.assertEqual(len(tokens), 2)
        self.assertTrue(all(t and t.startswith("o_") for t in tokens))
        out = StringIO()
        call_command("backfill_public_tokens", stdout=out)
        self.assertIn("0 orders minted", out.getvalue())
        self.assertEqual(set(Order.objects.values_list("public_token", flat=True)), tokens)
