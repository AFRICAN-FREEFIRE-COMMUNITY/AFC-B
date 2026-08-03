"""
Task 2.2 - tests for the platform-wide OCR matchers in afc_ocr.services.matching.

The P2 standalone-leaderboard OCR assist matches read names against ALL platform teams (team
format) or ALL registered users (solo format), NOT the event roster. These helpers un-gate the
candidate pool:
  - all_platform_players(limit=None)  -> every registered user, roster-agnostic.
  - all_platform_teams()              -> every Team (team_name + team_tag).
  - match_team_name(raw_name, teams)  -> the team mirror of match_name (rapidfuzz WRatio,
    cutoff 40, top-3, over team_name + team_tag).
The event flow's get_registered_players stays roster-gated (untouched) - not retested here.
"""
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_ocr.services import matching


def _user(username):
    return User.objects.create(username=username, email=f"{username}@x.com",
                               full_name=username.title(), role="player", password="x")


def _team(name, owner, tag=None):
    return Team.objects.create(team_name=name, team_owner=owner, team_creator=owner,
                               join_settings="open", country="NG", team_tag=tag)


class AllPlatformPlayersTests(TestCase):
    def test_returns_every_registered_user_no_roster_gate(self):
        _user("alice")
        _user("bob")
        players = matching.all_platform_players()
        usernames = {p["username"] for p in players}
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)
        # Platform players carry no team context (solo flow): team_id / team_name are None.
        for p in players:
            self.assertIsNone(p["team_id"])
            self.assertIsNone(p["team_name"])
            self.assertIn("user_id", p)

    def test_limit_caps_results(self):
        for i in range(5):
            _user(f"u{i}")
        self.assertEqual(len(matching.all_platform_players(limit=3)), 3)


class AllPlatformTeamsTests(TestCase):
    def test_returns_every_team_with_tag(self):
        owner = _user("owner")
        _team("Alpha Squad", owner, tag="ALP")
        _team("Bravo", owner, tag=None)
        teams = matching.all_platform_teams()
        by_name = {t["team_name"]: t for t in teams}
        self.assertIn("Alpha Squad", by_name)
        self.assertIn("Bravo", by_name)
        self.assertEqual(by_name["Alpha Squad"]["team_tag"], "ALP")
        self.assertIn("team_id", by_name["Alpha Squad"])


class MatchTeamNameTests(TestCase):
    def setUp(self):
        self.owner = _user("owner")
        self.alpha = _team("Alpha Squad", self.owner, tag="ALP")
        self.bravo = _team("Bravo Team", self.owner, tag="BRV")
        self.teams = matching.all_platform_teams()

    def test_exact_name_match(self):
        row = matching.match_team_name("Alpha Squad", self.teams)
        self.assertEqual(row["matched_team_id"], self.alpha.team_id)
        self.assertEqual(row["matched_team_name"], "Alpha Squad")
        self.assertGreaterEqual(row["confidence"], 0.9)
        self.assertTrue(row["top_candidates"])
        self.assertIn("team_id", row["top_candidates"][0])

    def test_fuzzy_name_match(self):
        # A slightly mangled read still resolves to the closest team above the cutoff.
        row = matching.match_team_name("Alpha Squd", self.teams)
        self.assertEqual(row["matched_team_id"], self.alpha.team_id)

    def test_tag_match_is_a_suggestion_until_corroborated(self):
        # The raw read is the team TAG, not the full name; matching considers team_tag too, so the
        # right team is the top candidate at full confidence. It does NOT bind on its own: a 2-4
        # character tag collides across 608 platform teams too easily to be proof by itself (see
        # SHORT_TAG_MAX_LEN - the real "ST" -> "Satolas" case), so the row surfaces for review.
        row = matching.match_team_name("BRV", self.teams)

        self.assertIsNone(row["matched_team_id"])
        self.assertEqual(row["unmatched_reason"], "tag_needs_corroboration")
        self.assertEqual(row["top_candidates"][0]["team_id"], self.bravo.team_id)
        self.assertEqual(row["top_candidates"][0]["confidence"], 1.0)
        self.assertTrue(row["top_candidates"][0]["needs_corroboration"])

    def test_tag_match_binds_when_a_player_is_on_that_team(self):
        # Corroboration signal 1 (membership): one of the placement's confidently matched players is
        # actually a member of Bravo Team, so the tag hit stops being a coincidence and binds.
        row = matching.match_team_name("BRV", self.teams, player_team_ids={self.bravo.team_id})

        self.assertEqual(row["matched_team_id"], self.bravo.team_id)
        self.assertEqual(row["unmatched_reason"], "")

    def test_tag_match_binds_when_the_players_wear_that_tag(self):
        # Corroboration signal 2 (worn tag): the placement's player IGNs carry "BRV" (derive_team_tag
        # over names like "BRV.John" / "BRV.Mike"). Different pixels than the team cell, so it is a
        # genuine second observation.
        row = matching.match_team_name("BRV", self.teams, players_tag="brv")

        self.assertEqual(row["matched_team_id"], self.bravo.team_id)
        self.assertEqual(row["unmatched_reason"], "")

    def test_a_longer_tag_still_binds_on_its_own(self):
        # The guard is about SHORT tags only. Team.team_tag is max_length=5, so "2 to 4 characters
        # need corroboration" leaves exactly one exempt width: a full-length 5-character tag, which
        # is specific enough that an accidental collision across the platform is unlikely.
        longtag = _team("Sierra Team", self.owner, tag="SIERA")
        row = matching.match_team_name("SIERA", matching.all_platform_teams())

        self.assertEqual(row["matched_team_id"], longtag.team_id)
        self.assertEqual(row["unmatched_reason"], "")

    def test_no_match_returns_none(self):
        # A nonsense read must never RESOLVE to a team. It may still LIST a weak candidate: since the
        # matcher started comparing normalized strings, listing (CANDIDATE_CUTOFF) and asserting
        # (MATCH_FLOOR) are two separate thresholds on purpose, so the reviewer keeps the near misses
        # to pick from while nothing binds automatically. What this test guards is the assertion.
        row = matching.match_team_name("ZZZZZZ Nonexistent", self.teams)
        self.assertIsNone(row["matched_team_id"])
        self.assertIsNone(row["matched_team_name"])
        self.assertEqual(row["confidence"], 0.0)
        for candidate in row["top_candidates"]:
            self.assertLess(candidate["confidence"] * 100, matching.MATCH_FLOOR)

    def test_row_has_raw_name_and_id(self):
        row = matching.match_team_name("Alpha Squad", self.teams)
        self.assertEqual(row["raw_name"], "Alpha Squad")
        self.assertIn("row_id", row)
