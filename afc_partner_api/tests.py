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
from django.test.client import RequestFactory

from afc_partner_api import auth
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


# ──────────────────────────────────────────────────────────────────────────────
# Task 2 — X-API-Key auth tests.
#
# These lock in the credential contract the read endpoints depend on: a valid key
# resolves to its Partner and stamps last_used_at; the secret is only ever stored
# as a sha256 hash (never plaintext); and EVERY failure mode an attacker could try
# — bad/unknown key, missing header, revoked key, suspended partner, expired key —
# raises PartnerAuthError (which the views translate to a 401). RequestFactory lets
# us inject the X-API-Key header directly (HTTP_X_API_KEY) without a live request.
# Full spec: WEBSITE/tasks/partner-api-design.md (§6 auth).
# ──────────────────────────────────────────────────────────────────────────────
class PartnerAuthTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.partner = Partner.objects.create(name="ESL", slug="esl")

    def _issue(self):
        # Issue a key the way the admin endpoint will: generate, store ONLY prefix +
        # hash, hand back the plaintext to authenticate with.
        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=self.partner, key_prefix=prefix, key_hash=h)
        return full

    def _req(self, key):
        # RequestFactory maps HTTP_X_API_KEY -> the "X-API-Key" request header.
        return self.rf.get("/api/v1/partner/events/", HTTP_X_API_KEY=key or "")

    def test_valid_key_authenticates(self):
        full = self._issue()
        partner, key = auth.authenticate_partner(self._req(full))
        self.assertEqual(partner.partner_id, self.partner.partner_id)
        self.assertIsNotNone(key.last_used_at)  # stamped on successful auth

    def test_hash_is_stored_not_plaintext(self):
        full = self._issue()
        row = PartnerApiKey.objects.get()
        self.assertNotIn(full.split("_")[-1], row.key_hash)  # secret never stored raw
        self.assertEqual(row.key_hash, auth.hash_key(full))

    def test_bad_key_rejected(self):
        self._issue()
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req("afcp_zzzz_deadbeef"))

    def test_missing_header_rejected(self):
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(None))

    def test_revoked_key_rejected(self):
        full = self._issue()
        PartnerApiKey.objects.update(status="revoked")
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))

    def test_suspended_partner_rejected(self):
        full = self._issue()
        Partner.objects.update(status="suspended")
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))

    def test_expired_key_rejected(self):
        from datetime import timedelta

        from django.utils import timezone

        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=self.partner, key_prefix=prefix, key_hash=h,
                                     expires_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))
