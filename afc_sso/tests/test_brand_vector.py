"""The AFC mark as a vector: that it is served, and that it is still the SAME drawing.

WHY THIS FILE EXISTS (owner, 2026-08-30: "create the vector of afc mark")
    afc_sso/brand.py used to publish, in writing, that AFC held no vector of its own mark and
    that a partner must not draw the PNG above 500px. tools/trace_afc_mark.py removed that
    ceiling by TRACING the 500x500 source, which is the second of the three honest moves the
    house rule allows when art is too small (the first, get the real vector, was unavailable:
    nobody has ever held one).

    A trace is only worth having while it still matches what it traced. That is the failure
    this file is here to catch, and it is invisible to everything else: a wrong SVG still
    parses, still serves, still renders, and still looks sharp. It just is not our logo.

THE TWO CHECKS, and why the cheap one carries most of the weight
    1. THE SOURCE HAS NOT MOVED. The provenance file records the sha256 of the PNG the vector
       was traced from. If somebody replaces afc-logo.png (a redesign, a re-export, a crop)
       and does not re-run the tracer, this fails and names the fix. No Chrome, no numpy, runs
       everywhere, and it is the scenario that actually happens.
    2. THE RENDER STILL AGREES. Rasterises the committed SVG and measures it against the
       committed PNG, the same measurement the tracer had to pass before it wrote anything.
       Needs Chrome and numpy, so it SKIPS where they are absent rather than failing. A skip
       here is honest; a green tick that measured nothing would not be.

Run: AFC_TEST_DB_NAME=test_afc_sso python manage.py test afc_sso.tests.test_brand_vector
"""
import hashlib
import json
import os
import re
import unittest

from django.test import Client, TestCase

from afc_sso.brand import _ASSETS_DIR, SVG_FILE, SVG_ON_DARK_FILE

BRAND_URL = "/sso/brand/"
SVG_URL = "/sso/brand/logo.svg"

PROVENANCE = os.path.join(_ASSETS_DIR, "afc-mark.provenance.json")
# The file the mark was traced FROM. Deliberately reached by path rather than imported from
# the tracer, so this test keeps working if the tool is ever moved or removed.
SOURCE_PNG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "afc_organizers", "assets", "afc-logo.png",
)


def _provenance():
    with open(PROVENANCE, encoding="utf-8") as fh:
        return json.load(fh)


class BrandVectorFilesTests(TestCase):
    """The files themselves: present, shaped right, and traced from the file we still ship."""

    def test_both_variants_and_the_provenance_are_committed(self):
        for name in (SVG_FILE, SVG_ON_DARK_FILE, "afc-mark.provenance.json"):
            path = os.path.join(_ASSETS_DIR, name)
            self.assertTrue(os.path.exists(path), f"{name} is missing from brand_assets/")
            self.assertGreater(os.path.getsize(path), 0, f"{name} is empty")

    def test_THE_SOURCE_HAS_NOT_MOVED_UNDER_THE_TRACE(self):
        """The check that earns this file. A new afc-logo.png with an old vector beside it
        means AFC publishes one logo and draws another, and nothing else would notice."""
        with open(SOURCE_PNG, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()
        recorded = _provenance()["source"]["sha256"]
        self.assertEqual(
            actual, recorded,
            "afc_organizers/assets/afc-logo.png has changed since the mark was traced. "
            "Re-run: backend/.venv/Scripts/python.exe tools/trace_afc_mark.py",
        )

    def test_the_recorded_agreement_meets_the_floor_it_was_written_against(self):
        """The tracer refuses to write below its floor, so a provenance file saying otherwise
        means the score or the file was edited by hand."""
        prov = _provenance()
        floor = prov["agreement_floor"]
        for key in ("silhouette_iou", "green_iou"):
            self.assertGreaterEqual(prov["agreement"][key], floor, f"{key} is below the floor")

    def test_it_scales_because_it_has_a_viewBox_and_no_fixed_size(self):
        """The whole point of shipping a vector. A width/height pair on the root would pin the
        mark to one size and hand a partner back the ceiling this replaced."""
        with open(os.path.join(_ASSETS_DIR, SVG_FILE), encoding="utf-8") as fh:
            head = fh.read(400)
        self.assertIn('viewBox="0 0 500 500"', head)
        self.assertIsNone(re.search(r"<svg[^>]*\swidth=", head), "the root svg pins a width")
        self.assertIsNone(re.search(r"<svg[^>]*\sheight=", head), "the root svg pins a height")

    def test_the_on_dark_variant_differs_ONLY_in_the_dark_fill(self):
        """Same paths, one colour swapped. If the two files ever diverge in geometry, one of
        them was edited by hand and the pair no longer describes one mark."""
        with open(os.path.join(_ASSETS_DIR, SVG_FILE), encoding="utf-8") as fh:
            light = fh.read()
        with open(os.path.join(_ASSETS_DIR, SVG_ON_DARK_FILE), encoding="utf-8") as fh:
            dark = fh.read()
        prov = _provenance()["colors"]
        self.assertNotEqual(light, dark)
        self.assertEqual(light.replace(prov["dark"], "X"), dark.replace(prov["on_dark"], "X"))


class BrandVectorEndpointTests(TestCase):
    """What a partner actually receives."""

    def setUp(self):
        self.client = Client()

    def test_the_vector_needs_no_authentication(self):
        """Same reason as the rest of the kit: this is drawn BEFORE anyone has signed in."""
        resp = self.client.get(SVG_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/svg+xml")

    def test_on_dark_serves_the_other_variant(self):
        default = b"".join(self.client.get(SVG_URL).streaming_content)
        on_dark = b"".join(self.client.get(SVG_URL, {"on": "dark"}).streaming_content)
        self.assertNotEqual(default, on_dark)

    def test_an_unknown_variant_falls_back_instead_of_404ing(self):
        """Unlike a PNG size, which 404s. A wrong variant still draws a correct mark, whereas
        a substituted SIZE silently changes how big our logo is on someone's page."""
        resp = self.client.get(SVG_URL, {"on": "chartreuse"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            b"".join(resp.streaming_content),
            b"".join(self.client.get(SVG_URL).streaming_content),
        )

    def test_the_kit_publishes_the_vector_and_says_to_prefer_it(self):
        logo = self.client.get(BRAND_URL).json()["logo"]
        self.assertEqual(logo["preferred"], "svg")
        self.assertIn("default", logo["vector"])
        self.assertIn("on_dark", logo["vector"])
        self.assertTrue(logo["vector"]["default"].endswith("/sso/brand/logo.svg"))

    def test_the_raster_contract_is_UNCHANGED(self):
        """format, source_resolution and mark were published before the vector existed. A
        partner may be reading them today, so they keep their meaning exactly; the vector was
        added beside them, never on top of them."""
        logo = self.client.get(BRAND_URL).json()["logo"]
        self.assertEqual(logo["format"], "png")
        self.assertEqual(logo["source_resolution"], 500)
        self.assertIn("500", logo["mark"])

    def test_every_published_vector_url_actually_resolves(self):
        vector = self.client.get(BRAND_URL).json()["logo"]["vector"]
        for key, url in vector.items():
            path = url.split("://", 1)[-1].split("/", 1)[-1]
            resp = self.client.get("/" + path)
            self.assertEqual(resp.status_code, 200, f"{key} -> {url} does not resolve")


class BrandVectorRendersTheSameShapeTests(TestCase):
    """The expensive half: rasterise the committed SVG and measure it against the PNG.

    This is the same measurement tools/trace_afc_mark.py had to pass before it wrote the file.
    Repeating it here is what makes the committed pair self-checking rather than trusted.
    """

    def test_the_committed_svg_still_matches_the_committed_png(self):
        try:
            import numpy as np  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("numpy/pillow not installed, so the render cannot be measured")

        import sys
        tools = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"
        )
        if tools not in sys.path:
            sys.path.insert(0, tools)
        try:
            import trace_afc_mark as tracer
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("tools/trace_afc_mark.py is not present")

        rgb, alpha, _digest, _size = tracer.load_source()
        scores = tracer.measure(os.path.join(_ASSETS_DIR, SVG_FILE), rgb, alpha)
        if scores is None:
            # No Chrome on this machine. A SKIP, never a pass: a green tick that measured
            # nothing is the failure mode this whole file exists to avoid.
            raise unittest.SkipTest("Chrome not found, so the vector could not be rasterised")

        floor = _provenance()["agreement_floor"]
        self.assertGreaterEqual(scores["silhouette_iou"], floor, f"silhouette drifted: {scores}")
        self.assertGreaterEqual(scores["green_iou"], floor, f"green drifted: {scores}")
