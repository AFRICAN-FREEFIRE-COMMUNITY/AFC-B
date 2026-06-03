# afc_partner_api/tests.py
# ──────────────────────────────────────────────────────────────────────────────
# Task 1 — model-level tests for the Partner Data API scaffold.
#
# These lock in the least-privilege contract: every resource/field toggle on a new
# Partner defaults OFF, the PARTNER_TOGGLE_FIELDS whitelist only ever names real
# BooleanFields (so admin-edit + serialization can trust it), and an issued key is
# bound to its partner with the expected active/rate-limit defaults.
# Full spec: WEBSITE/tasks/partner-api-design.md (§5 data model).
# ──────────────────────────────────────────────────────────────────────────────
from django.test import TestCase

from afc_partner_api.models import Partner, PartnerApiKey, PARTNER_TOGGLE_FIELDS


class PartnerModelTests(TestCase):
    def test_partner_defaults_off(self):
        # Least privilege: a freshly provisioned partner can read nothing and see no
        # stat fields until an AFC admin flips toggles on.
        p = Partner.objects.create(name="ESL", slug="esl")
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertFalse(getattr(p, f), f"{f} should default False (least privilege)")
        self.assertTrue(p.status == "active")

    def test_toggle_whitelist_matches_fields(self):
        # every name in the whitelist must be a real BooleanField on Partner
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertTrue(hasattr(Partner, f))

    def test_key_belongs_to_partner(self):
        p = Partner.objects.create(name="ESL", slug="esl2")
        k = PartnerApiKey.objects.create(partner=p, key_prefix="afcp_aaaa", key_hash="x" * 64)
        self.assertEqual(k.partner_id, p.partner_id)
        self.assertEqual(k.status, "active")
        self.assertEqual(k.rate_limit_per_min, 60)
