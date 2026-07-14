"""
A2 - tests for the roster-gated alias fast-path in afc_ocr.services.matching.match_name.

OCRNameAlias is a GLOBAL table (raw_name unique across all events), so an alias can point at a user
who is NOT in the current event roster. The old code trusted the alias blindly and auto-resolved such
a row to an off-event player at confidence 1.0 with an EMPTY candidate list, hiding the real fuzzy
options (and, on team events, yielding a null team). The fix roster-gates the fast-path: the alias is
honoured ONLY when the aliased user is in this event's `registered` roster; otherwise the row falls
through to the rapidfuzz pass so `top_candidates` is still surfaced.

Fixture idiom mirrors test_ringer_flag / test_platform_matchers: real Users + a real OCRNameAlias, but
the `registered` roster is an in-memory list of {"user_id","username","team_id","team_name"} dicts (the
exact shape get_registered_players produces), so these are pure matcher tests with NO HTTP and no event.
"""
from django.test import TestCase

from afc_auth.models import User
from afc_ocr.models import OCRNameAlias
from afc_ocr.services.matching import match_name


def _user(username):
    return User.objects.create(
        username=username, email=f"{username}@x.com",
        full_name=username.title(), role="player", password="x",
    )


def _reg(user, team_id=None, team_name=None):
    """One roster row in get_registered_players' shape."""
    return {"user_id": user.pk, "username": user.username, "team_id": team_id, "team_name": team_name}


class MatchNameAliasRosterGateTests(TestCase):
    def test_alias_in_roster_resolves_10(self):
        # The aliased user IS on the roster -> trust the alias: confidence 1.0, no candidates, and the
        # team is taken from that user's roster row (team-event roster carries team_id/team_name).
        player = _user("SniperKing")
        OCRNameAlias.objects.create(raw_name="SNP.King", user=player)
        registered = [_reg(player, team_id=777, team_name="Alpha")]

        row = match_name("SNP.King", registered)

        self.assertEqual(row["matched_user_id"], player.pk)
        self.assertEqual(row["confidence"], 1.0)
        self.assertEqual(row["top_candidates"], [])
        self.assertEqual(row["matched_team_id"], 777)
        self.assertEqual(row["matched_team_name"], "Alpha")

    def test_alias_not_in_roster_falls_through(self):
        # The alias points at an OFF-ROSTER user, but a fuzzy-similar roster user exists. The alias must
        # be ignored and the row must resolve to the FUZZY roster user (not the off-event alias), at a
        # confidence below 1.0, with a non-empty candidate list the reviewer can pick from.
        off_roster = _user("arendt_offevent")
        roster_user = _user("ARENDT")
        OCRNameAlias.objects.create(raw_name="ARDNT", user=off_roster)
        registered = [_reg(roster_user)]

        row = match_name("ARDNT", registered)

        self.assertEqual(row["matched_user_id"], roster_user.pk)   # fuzzy roster user, NOT the alias
        self.assertNotEqual(row["matched_user_id"], off_roster.pk)
        self.assertLess(row["confidence"], 1.0)                    # fuzzy score, never the 1.0 fast-path
        self.assertTrue(row["top_candidates"])                    # real candidates surfaced
        self.assertIn(roster_user.pk, [c["user_id"] for c in row["top_candidates"]])

    def test_alias_not_in_roster_no_fuzzy_match(self):
        # The alias is off-roster AND nothing on the roster is fuzzy-similar to the read name. The row
        # falls through to fuzzy, which matches nothing above the cutoff -> no user, empty candidates.
        off_roster = _user("ghost_alias_user")
        unrelated = _user("Qwopklmn")
        OCRNameAlias.objects.create(raw_name="XZXZXZ", user=off_roster)
        registered = [_reg(unrelated)]

        row = match_name("XZXZXZ", registered)

        self.assertIsNone(row["matched_user_id"])
        self.assertEqual(row["top_candidates"], [])

    def test_no_alias_unchanged(self):
        # Regression guard: with no alias row at all, behaviour is the plain fuzzy pass - an exact
        # username read resolves to that roster user with candidates, exactly as before the A2 change.
        roster_user = _user("ClutchGod")
        registered = [_reg(roster_user)]

        row = match_name("ClutchGod", registered)

        self.assertEqual(row["matched_user_id"], roster_user.pk)
        self.assertTrue(row["top_candidates"])
        self.assertGreater(row["confidence"], 0.0)
