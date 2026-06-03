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
from django.db import models
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
        # every name in the whitelist must be a real BooleanField on Partner.
        # hasattr() alone is too weak: it passes for an attribute of ANY type, so a
        # CharField (or anything non-boolean) slipping into PARTNER_TOGGLE_FIELDS would
        # pass silently and break the least-privilege guarantee admin-edit/serialization
        # rely on. Assert the concrete field type via the model meta API instead.
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertIsInstance(Partner._meta.get_field(f), models.BooleanField)

    def test_key_belongs_to_partner(self):
        p = Partner.objects.create(name="ESL", slug="esl2")
        k = PartnerApiKey.objects.create(partner=p, key_prefix="afcp_aaaa", key_hash="x" * 64)
        self.assertEqual(k.partner_id, p.partner_id)
        self.assertEqual(k.status, "active")
        self.assertEqual(k.rate_limit_per_min, 60)
