"""
Tests for resolving a placement's TEAM from the players that were read (backlog item 36, second half:
"not matching teams to what exists on the platform").

WHY THIS EXISTS
A Free Fire result screen very often shows a logo, a 2-4 character clan tag, or nothing at all where
the team name belongs, so the team cell alone is thin evidence. The players are named in full. Until
now afc_leaderboard.ocr.build_team_ocr_rows computed a per-player platform match for every read
player and then threw that signal away: the row's team came only from match_team_name(team cell).

Real case from the production clone that motivated this - a placement whose team cell read "KN":
    team-name match : "KNIGHTS X4"        @0.90   (auto-resolved, no confidence gate at all)
    its players     : "SABATH24" -> user "KN SABATH24", a member of "KNIGHTS E-SPORTS"
The players were right and the team cell was not.

WHAT IS UNDER TEST
  team_from_players  : tally the platform teams of the confidently matched players
  _resolve_row_team  : combine that tally with the team-name match (agree / carry / conflict)
These are pure functions over the players_detail row shape, so no HTTP, no leaderboard, no images.
"""
from django.test import TestCase

from afc_leaderboard.ocr import PLAYER_TRUST, _resolve_row_team, team_from_players


def _player(name, team_id, confidence, team_name=None):
    """One entry of a row's players_detail, in the shape build_team_ocr_rows emits."""
    return {
        "name": name,
        "kills": 0,
        "matched_user_id": abs(hash(name)) % 100000,
        "matched_username": name,
        "matched_team_id": team_id,
        "matched_team_name": team_name or (f"Team {team_id}" if team_id else None),
        "confidence": confidence,
        "top_candidates": [],
        "is_unmatched": team_id is None,
    }


def _name_match(team_id, confidence, team_name="Name Match FC"):
    """One match_team_name() result, in the shape _resolve_row_team consumes."""
    return {
        "matched_team_id": team_id,
        "matched_team_name": team_name if team_id else None,
        "confidence": confidence,
        "top_candidates": (
            [{"team_id": team_id, "team_name": team_name, "confidence": confidence}]
            if team_id else []
        ),
    }


class TeamFromPlayersTests(TestCase):
    def test_only_confident_players_vote(self):
        # A weak player match is exactly the kind of guess that must not decide a team.
        detail = [
            _player("a", 10, 0.95),
            _player("b", 10, 0.90),
            _player("c", 99, PLAYER_TRUST - 0.01),   # below the gate, ignored
        ]
        team_id, _name, votes, voters = team_from_players(detail)
        self.assertEqual((team_id, votes, voters), (10, 2, 2))

    def test_free_agents_do_not_vote(self):
        # Most standalone-leaderboard players are on no AFC team; they carry no team signal.
        detail = [_player("a", None, 1.0), _player("b", None, 1.0)]
        self.assertEqual(team_from_players(detail), (None, None, 0, 0))

    def test_no_players_is_no_signal(self):
        self.assertEqual(team_from_players([]), (None, None, 0, 0))


class ResolveRowTeamTests(TestCase):
    def test_agreement_raises_confidence(self):
        # Two independent signals on the same team is the strongest evidence available.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, _name, confidence, _cands = _resolve_row_team(_name_match(10, 0.80), detail)
        self.assertEqual(team_id, 10)
        self.assertGreaterEqual(confidence, 0.9)

    def test_players_carry_an_unreadable_team_cell(self):
        # The team cell read nothing (a logo). Two roster-confirmed teammates decide the row.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, name, confidence, cands = _resolve_row_team(_name_match(None, 0.0), detail)
        self.assertEqual(team_id, 10)
        self.assertEqual(name, "Team 10")
        self.assertGreaterEqual(confidence, PLAYER_TRUST)   # above the gate: it may auto-resolve
        self.assertLess(confidence, 1.0)                    # but it is an inference, never certainty
        self.assertEqual(cands[0]["team_id"], 10)

    def test_players_override_a_weak_team_name_match(self):
        # The "KN" case: a weak tag match loses to two teammates who agree.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, _name, _conf, _c = _resolve_row_team(_name_match(77, 0.55), detail)
        self.assertEqual(team_id, 10)

    def test_conflict_drops_below_the_gate_and_offers_both(self):
        # Both signals are confident and they DISAGREE. Auto-resolving either one could bind a wrong
        # team to a tournament standing, so the row must surface for a human with both options.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, _name, confidence, cands = _resolve_row_team(_name_match(77, 0.95), detail)
        self.assertLess(confidence, PLAYER_TRUST, "a conflicted row must not auto-resolve")
        self.assertEqual(team_id, 77, "the read team stays as the suggestion")
        self.assertIn(10, [c["team_id"] for c in cands], "the players' team is offered too")

    def test_single_voter_is_not_enough_to_override(self):
        # One matched player on a team is a coincidence away from being wrong; the name match stands.
        detail = [_player("a", 10, 0.95), _player("b", None, 0.95)]
        team_id, _name, confidence, _c = _resolve_row_team(_name_match(77, 0.80), detail)
        self.assertEqual(team_id, 77)
        self.assertEqual(confidence, 0.80)

    def test_no_player_signal_leaves_the_name_match_untouched(self):
        detail = [_player("a", None, 1.0)]
        team_id, name, confidence, cands = _resolve_row_team(_name_match(77, 0.80), detail)
        self.assertEqual((team_id, name, confidence), (77, "Name Match FC", 0.80))
        self.assertEqual(cands, [{"team_id": 77, "team_name": "Name Match FC", "confidence": 0.80}])
