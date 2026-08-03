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

from afc_leaderboard.ocr import (
    PLAYER_TRUST, _resolve_row_team, team_from_players, trusted_player_team_ids,
)


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


def _name_match(team_id, confidence, team_name="Name Match FC", reason=None):
    """One match_team_name() result, in the shape _resolve_row_team consumes.

    `reason` defaults to "" when the match bound and "below_floor" when it did not, which is what
    match_team_name really returns; pass it explicitly to model one of its other refusals (a tie,
    or an uncorroborated short tag).
    """
    if reason is None:
        reason = "" if team_id else "below_floor"
    return {
        "matched_team_id": team_id,
        "matched_team_name": team_name if team_id else None,
        "confidence": confidence,
        "unmatched_reason": reason,
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
        team_id, _name, confidence, _cands, reason = _resolve_row_team(_name_match(10, 0.80), detail)
        self.assertEqual(team_id, 10)
        self.assertGreaterEqual(confidence, 0.9)
        self.assertEqual(reason, "", "a bound row has nothing to explain")

    def test_players_carry_an_unreadable_team_cell(self):
        # The team cell read nothing (a logo). Two roster-confirmed teammates decide the row.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, name, confidence, cands, reason = _resolve_row_team(_name_match(None, 0.0), detail)
        self.assertEqual(team_id, 10)
        self.assertEqual(name, "Team 10")
        self.assertGreaterEqual(confidence, PLAYER_TRUST)   # above the gate: it may auto-resolve
        self.assertLess(confidence, 1.0)                    # but it is an inference, never certainty
        self.assertEqual(cands[0]["team_id"], 10)
        self.assertEqual(reason, "")

    def test_players_override_a_weak_team_name_match(self):
        # The "KN" case: a weak tag match loses to two teammates who agree.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, _name, _conf, _c, _r = _resolve_row_team(_name_match(77, 0.55), detail)
        self.assertEqual(team_id, 10)

    def test_players_rescue_a_tag_the_matcher_refused_to_bind(self):
        # The short-tag guard (matching.SHORT_TAG_MAX_LEN) withheld the team cell's own match, but
        # two confidently matched teammates agree on a team, so the row still resolves - and the
        # refusal no longer needs explaining, because nothing is being asked of the reviewer.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        withheld = _name_match(None, 0.0, reason="tag_needs_corroboration")

        team_id, _name, confidence, _c, reason = _resolve_row_team(withheld, detail)

        self.assertEqual(team_id, 10)
        self.assertGreaterEqual(confidence, PLAYER_TRUST)
        self.assertEqual(reason, "")

    def test_a_withheld_tag_with_no_player_signal_keeps_its_reason(self):
        # The real "ST" -> "Satolas" row: the tag matched a team at 1.00, nothing corroborated it,
        # and none of the four players resolved to a team either. The row must reach the review
        # table still carrying WHY it declined, or the reviewer just sees an unexplained blank.
        detail = [_player("a", None, 0.68), _player("b", None, 0.72)]
        withheld = _name_match(None, 0.0, reason="tag_needs_corroboration")

        team_id, _name, _conf, _c, reason = _resolve_row_team(withheld, detail)

        self.assertIsNone(team_id)
        self.assertEqual(reason, "tag_needs_corroboration")

    def test_conflict_drops_below_the_gate_and_offers_both(self):
        # Both signals are confident and they DISAGREE. Auto-resolving either one could bind a wrong
        # team to a tournament standing, so the row must surface for a human with both options.
        detail = [_player("a", 10, 0.95), _player("b", 10, 0.90)]
        team_id, _name, confidence, cands, reason = _resolve_row_team(_name_match(77, 0.95), detail)
        self.assertLess(confidence, PLAYER_TRUST, "a conflicted row must not auto-resolve")
        self.assertEqual(team_id, 77, "the read team stays as the suggestion")
        self.assertIn(10, [c["team_id"] for c in cands], "the players' team is offered too")
        self.assertEqual(reason, "team_conflict", "the reviewer is told the two signals disagree")

    def test_single_voter_is_not_enough_to_override(self):
        # One matched player on a team is a coincidence away from being wrong; the name match stands.
        detail = [_player("a", 10, 0.95), _player("b", None, 0.95)]
        team_id, _name, confidence, _c, _r = _resolve_row_team(_name_match(77, 0.80), detail)
        self.assertEqual(team_id, 77)
        self.assertEqual(confidence, 0.80)

    def test_no_player_signal_leaves_the_name_match_untouched(self):
        detail = [_player("a", None, 1.0)]
        team_id, name, confidence, cands, reason = _resolve_row_team(_name_match(77, 0.80), detail)
        self.assertEqual((team_id, name, confidence), (77, "Name Match FC", 0.80))
        self.assertEqual(cands, [{"team_id": 77, "team_name": "Name Match FC", "confidence": 0.80}])
        self.assertEqual(reason, "")


class TrustedPlayerTeamIdsTests(TestCase):
    """The corroboration input match_team_name's short-tag guard consumes."""

    def test_only_confident_players_with_a_team_are_collected(self):
        detail = [
            _player("a", 10, 0.95),
            _player("b", 20, 0.90),
            _player("c", 30, PLAYER_TRUST - 0.01),   # too weak to vouch for anything
            _player("d", None, 1.0),                 # a free agent carries no team signal
        ]
        self.assertEqual(trusted_player_team_ids(detail), {10, 20})

    def test_a_single_player_is_enough_to_corroborate(self):
        # Deliberately NOT a plurality (unlike team_from_players): corroboration only has to show
        # that the tag's team is really present in this placement. The real "IL" row had exactly one
        # matched member ("IL.DAMIAN" of "IL ESPORTS") and that is what let it bind.
        self.assertEqual(trusted_player_team_ids([_player("a", 10, 0.95)]), {10})

    def test_no_players_is_an_empty_set(self):
        self.assertEqual(trusted_player_team_ids([]), set())
        self.assertEqual(trusted_player_team_ids(None), set())
