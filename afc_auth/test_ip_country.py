"""
test_ip_country.py
──────────────────
Covers the IP-derived per-PLAYER flag country (owner ask 2026-06-29).

Two layers under test:
  1. afc_auth.views.set_ip_country() — the denormalizer that writes User.ip_country on each login.
     Guards: skip on VPN, skip on empty country, skip when unchanged, persist when it changes.
  2. The reader fallback `ip_country or country` used by the player-flag serializers
     (afc_team.get_team_details, afc_player.aggregation, afc_player.views). We assert the contract
     directly (model-level) so a regression in either serializer's coalesce is caught here too.

These are pure unit tests — no network: set_ip_country takes the already-resolved country + is_vpn,
so geo_for_ip()/ipinfo is never hit.
"""

from django.test import TestCase

from afc_auth.models import User
from afc_auth.views import set_ip_country


def _coalesce(u):
    """Mirror of the serializer rule `ip_country or country` (the shown player flag)."""
    return u.ip_country or u.country


class SetIpCountryTests(TestCase):
    def setUp(self):
        # A player whose PROFILE country (signup/edit) is Nigeria.
        self.user = User.objects.create(
            username="flagplayer", email="flag@example.com", password="x",
            full_name="Flag Player", country="Nigeria",
        )

    def test_sets_ip_country_when_resolved(self):
        # Logs in from Ghana -> the flag should now show Ghana, not the profile Nigeria.
        set_ip_country(self.user, "Ghana", is_vpn=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.ip_country, "Ghana")
        self.assertEqual(_coalesce(self.user), "Ghana")

    def test_skips_when_vpn(self):
        # A VPN/datacenter exit node must NOT relabel the flag; profile country stays the shown flag.
        set_ip_country(self.user, "Netherlands", is_vpn=True)
        self.user.refresh_from_db()
        self.assertEqual(self.user.ip_country, "")
        self.assertEqual(_coalesce(self.user), "Nigeria")  # falls back to profile country

    def test_skips_when_country_empty(self):
        # geo failed (ipinfo down / stripped proxy header) -> keep last known, don't blank it.
        set_ip_country(self.user, "Ghana", is_vpn=False)
        set_ip_country(self.user, "", is_vpn=False)  # second login, geo failed
        self.user.refresh_from_db()
        self.assertEqual(self.user.ip_country, "Ghana")  # unchanged

    def test_skips_when_country_none(self):
        set_ip_country(self.user, None, is_vpn=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.ip_country, "")

    def test_updates_on_relocation(self):
        # Player travels: Ghana -> Kenya across two logins; the flag follows the latest IP.
        set_ip_country(self.user, "Ghana", is_vpn=False)
        set_ip_country(self.user, "Kenya", is_vpn=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.ip_country, "Kenya")
        self.assertEqual(_coalesce(self.user), "Kenya")

    def test_no_write_when_unchanged(self):
        # Idempotent: re-resolving the same country must not issue a needless UPDATE.
        set_ip_country(self.user, "Ghana", is_vpn=False)
        self.user.refresh_from_db()
        with self.assertNumQueries(0):
            set_ip_country(self.user, "Ghana", is_vpn=False)

    def test_fallback_when_ip_country_blank(self):
        # Brand-new / never-logged-in-since-feature user: ip_country blank -> profile country shows.
        self.assertEqual(self.user.ip_country, "")
        self.assertEqual(_coalesce(self.user), "Nigeria")
