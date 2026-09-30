"""
Tests for SITE ADDRESSES THAT CARRY A NAME (owner 2026-09-30, inbox #91) - afc_auth/site_paths.py and
the builders that use it.

Owner, after NG.KILLA's admin page said "Player not found": "it should not happen anywhere."

WHAT IS COVERED, AND WHY EACH ONE IS HERE
  - segment() encodes the characters that broke real addresses: a private-use glyph (NG.KILLA ends
    in U+F8FF), a space, "#" (starts a fragment), "?" (starts a query), "/" (splits the segment,
    which urllib's default quote() leaves alone, the way afc_qr/targets.py had it), and "%".
  - The encoding round-trips: the frontend decodes with decodeURIComponent semantics, which is
    urllib's unquote(), so what goes in comes back out unchanged.
  - build_notification_link, the "Take me there" link on every notification, encodes team and
    player names (it pasted them raw).

Pure functions and one function with no database, so SimpleTestCase.

Run: python manage.py test afc_auth.tests_route_links
"""
from urllib.parse import unquote

from django.test import SimpleTestCase

from afc_auth.notification_links import build_notification_link
from afc_auth.site_paths import admin_player_path, player_path, referral_path, segment, team_path

NASTY_NAMES = ["NG.KILLA", "SLF ZAZA", "A#1", "Who?", "AC/DC", "100%", "Élan", "★STAR★"]


class SegmentTests(SimpleTestCase):
    def test_every_breaking_character_is_encoded(self):
        self.assertEqual(segment("NG.KILLA"), "NG.KILLA%EF%A3%BF")
        self.assertEqual(segment("SLF ZAZA"), "SLF%20ZAZA")
        self.assertEqual(segment("A#1"), "A%231")
        self.assertEqual(segment("Who?"), "Who%3F")
        self.assertEqual(segment("AC/DC"), "AC%2FDC")
        self.assertEqual(segment("100%"), "100%25")

    def test_round_trip_gives_back_the_name(self):
        for name in NASTY_NAMES:
            self.assertEqual(unquote(segment(name)), name, name)

    def test_none_is_empty_not_the_word_none(self):
        self.assertEqual(segment(None), "")

    def test_paths(self):
        self.assertEqual(player_path("A#1"), "/players/A%231")
        self.assertEqual(team_path("AC/DC", "/applications"), "/teams/AC%2FDC/applications")
        self.assertEqual(admin_player_path("NG.KILLA"), "/a/players/NG.KILLA%EF%A3%BF")
        self.assertEqual(referral_path("ab 12"), "/r/ab%2012")


class NotificationLinkTests(SimpleTestCase):
    def test_team_and_player_links_are_encoded(self):
        self.assertEqual(build_notification_link("team", "AC/DC"), "/teams/AC%2FDC")
        self.assertEqual(build_notification_link("player", "NG.KILLA"), "/players/NG.KILLA%EF%A3%BF")

    def test_every_name_lands_on_one_segment(self):
        for name in NASTY_NAMES:
            link = build_notification_link("team", name)
            self.assertEqual(link.count("/"), 2, link)
            self.assertNotIn("#", link)
            self.assertNotIn("?", link)
