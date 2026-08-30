"""AFC's brand kit, published so a partner can draw a sign-in button that looks finished.

WHY THIS FILE EXISTS (owner, 2026-08-30)
    V-ENT shipped a "Sign in with AFC" button that came out as a wide bare button reading
    "Continue with African Free Fire Community", no mark, beside a compact Google button
    that had both. The owner: "you dont send brand kit with the api? logos and the all".

    AFC published nothing. Logos only ever travelled INWARDS: partners upload theirs, and
    AFC served its own nowhere. These tests pin the two things that must stay true for a
    partner to be able to render the button at all, and the resolution rule that stops us
    handing them art they will draw too big.

Run: AFC_TEST_DB_NAME=test_afc_sso python manage.py test afc_sso.tests.test_brand_kit
"""
import os
from urllib.parse import urlparse

from django.test import Client, TestCase

from afc_sso.brand import LOGO_SIZES, _ASSETS_DIR

BRAND_URL = "/sso/brand/"


class BrandKitTests(TestCase):
    def setUp(self):
        self.client = Client()

    # ── it must be reachable by someone who is NOT signed in ──
    def test_the_kit_needs_no_authentication(self):
        """The whole point. A partner draws this button BEFORE anyone has signed in, so a
        gated brand kit is a brand kit nobody can use."""
        self.assertEqual(self.client.get(BRAND_URL).status_code, 200)

    def test_the_logo_needs_no_authentication(self):
        resp = self.client.get(f"/sso/brand/logo/{LOGO_SIZES[0]}.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")

    # ── the two fields that fix the reported button ──
    def test_it_publishes_a_SHORT_name_and_a_button_label(self):
        """The reported bug in one assertion. A partner with no short name uses the legal
        one, and the button ends up twice the width of every other provider's."""
        body = self.client.get(BRAND_URL).json()
        self.assertEqual(body["name"], "AFC")
        self.assertEqual(body["button_label"], "Continue with AFC")
        # The full name is still published, for places where it belongs.
        self.assertEqual(body["full_name"], "African Free Fire Community")

    def test_every_published_logo_url_actually_resolves(self):
        """A url in the kit that 404s is worse than no kit: the partner ships a broken
        image and blames their own code."""
        marks = self.client.get(BRAND_URL).json()["logo"]["mark"]
        self.assertTrue(marks, "the kit published no logo at all")
        for size, url in marks.items():
            # The kit publishes ABSOLUTE urls (a partner pastes them straight in), so the
            # path is taken back off for the test client.
            resp = self.client.get(urlparse(url).path)
            self.assertEqual(resp.status_code, 200, f"size {size} -> {url}")

    # ── resolution, which is a house rule ──
    def test_no_size_is_ABOVE_the_source_resolution(self):
        """There is no vector of the AFC mark. Every file served is a downscale of one
        500x500 PNG, and publishing a larger size would be inviting a partner to draw it
        soft. See the module header in afc_sso/brand.py."""
        body = self.client.get(BRAND_URL).json()
        source = body["logo"]["source_resolution"]
        for size in body["logo"]["mark"]:
            self.assertLessEqual(int(size), source)

    def test_an_unknown_size_is_a_404_and_NOT_the_nearest_one(self):
        """Silently returning a different size is exactly how a partner ends up scaling the
        mark without knowing it."""
        self.assertEqual(self.client.get("/sso/brand/logo/999.png").status_code, 404)

    def test_every_declared_size_exists_on_disk(self):
        """LOGO_SIZES and the committed files must not drift: the endpoint reads the
        directory, so a missing file would silently vanish from the kit instead of failing
        loudly here."""
        for size in LOGO_SIZES:
            self.assertTrue(
                os.path.exists(os.path.join(_ASSETS_DIR, f"afc-mark-{size}.png")),
                f"afc-mark-{size}.png is declared in LOGO_SIZES but not committed",
            )

    # ── colours ──
    def test_the_colours_carry_hex_because_that_is_what_a_partner_pastes(self):
        """AFC's own tokens are oklch, which many toolchains still cannot parse. Publishing
        only oklch would make a partner eyeball the green."""
        colors = self.client.get(BRAND_URL).json()["colors"]
        self.assertEqual(colors["primary"]["hex"], "#15a249")
        for name, value in colors.items():
            self.assertTrue(value["hex"].startswith("#"), name)
            self.assertIn("rgb", value)
            self.assertIn("oklch", value)

    def test_it_says_what_NOT_to_do(self):
        """A kit without rules is a licence. The don'ts are the half that protects the mark."""
        usage = self.client.get(BRAND_URL).json()["usage"]
        self.assertTrue(usage["do"])
        self.assertTrue(usage["dont"])
        self.assertGreater(usage["min_size_px"], 0)
