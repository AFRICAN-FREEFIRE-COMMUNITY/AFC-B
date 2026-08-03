"""
Regression tests for OCR name/team IDENTITY matching (backlog item 36: "OCR is not matching players
to teams, and not matching teams to what exists on the platform").

WHY THESE EXACT STRINGS
Every read name and every team name in this file was taken from REAL data in the production clone:
the single stored OCRSession (match 3701) and the stored LeaderboardOcrJob, replayed against the real
608-team / 6790-user platform pool. They are not invented examples. Each one is a case where the old
matcher (rapidfuzz WRatio over RAW strings) picked a different, wrong record, and the comment on each
row records what it used to pick and at what confidence, so a regression is caught by the same data
that broke it.

WHAT IS UNDER TEST
  - match_name          : a read resolves to the right USER (afc_ocr.services.matching)
  - match_team_name     : a read resolves to the right TEAM, or to nothing when the read is a bare tag
  - derive_team_tag     : a placement's shared clan tag, prefix OR suffix, by plurality
  - score_names         : the shared scoring ladder both matchers sit on
The fixture idiom mirrors test_platform_matchers / test_match_name: real Users + real Teams, with the
candidate pool built by the same all_platform_* helpers the production flow uses.
"""
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_ocr.services import matching
from afc_ocr.services.matching import (
    MATCH_FLOOR, SHORT_TAG_MAX_LEN, derive_team_tag, match_name, match_team_name, score_names,
)


def _user(username):
    return User.objects.create(username=username, email=f"{abs(hash(username))}@x.com",
                               full_name="Player", role="player", password="x")


def _team(name, owner, tag=None):
    return Team.objects.create(team_name=name, team_owner=owner, team_creator=owner,
                               join_settings="open", country="NG", team_tag=tag)


# ── PLAYERS ──────────────────────────────────────────────────────────────────────────────────────
# (read name, the real platform username, the username the OLD matcher wrongly picked, old score)
# All six were confirmed on the clone: the read and the real username are character-for-character
# identical once decoration/case/punctuation/look-alike digits are folded, yet a shorter neighbour won.
REAL_PLAYER_MISSES = [
    ("ZN.MALX09",  "ZN.MALXO9",   "AL",           0.90),   # zero read where the name has letter O
    ("AW.TRAP7",   "AW.trap7",    "TRAPPIE_APK",  0.80),   # differs only in CASE
    ("NP.KILLUA",  "NP. KILLUA",  "KILLUA",       1.00),   # differs only by a SPACE, auto-applied
    ("LMG LE00",   "LMG   LEOO",  "LE",           0.90),   # double zero + collapsed spaces
    ("NP.BLOOD彡", "NP. BLOOD",   "BLOOD",        0.91),   # decorative CJK flourish U+5F61
    ("NJ Solozin", "Nj solozin",  "Solo",         0.90),   # case only
]


class RealPlayerMissTests(TestCase):
    """Each read must resolve to the real user, not to the shorter lookalike that used to win."""

    def setUp(self):
        self.pool = []
        for read, real, decoy, _old in REAL_PLAYER_MISSES:
            for username in (real, decoy):
                if not any(p["username"] == username for p in self.pool):
                    user = _user(username)
                    self.pool.append({"user_id": user.pk, "username": username,
                                      "team_id": None, "team_name": None})

    def test_real_user_wins_over_the_lookalike(self):
        for read, real, decoy, old_score in REAL_PLAYER_MISSES:
            with self.subTest(read=read):
                row = match_name(read, self.pool)
                self.assertEqual(
                    row["matched_username"], real,
                    f"{read!r} must resolve to {real!r}; before the fix it picked {decoy!r} @{old_score}",
                )
                # Normalized identity is certainty, not a guess.
                self.assertEqual(row["confidence"], 1.0)

    def test_certain_match_is_unambiguous(self):
        # A unique normalized-identity hit short-circuits the fuzzy pass (it is certain, and skipping
        # the scan matters: match_name runs per read player against the whole ~6.8k-user pool). The
        # row therefore carries exactly the one user it resolved to; anything else the reviewer needs
        # is one free-search away in the review table.
        row = match_name("NP.KILLUA", self.pool)
        self.assertEqual([c["username"] for c in row["top_candidates"]], ["NP. KILLUA"])


class AmbiguousIdentityTests(TestCase):
    """Two platform users that normalize identically must NOT auto-resolve to either one.

    These are not hypothetical. Replaying the clone's reads against the real user table turned up
    three reads that each land on TWO separate AFC accounts, indistinguishable on a screenshot:
        read "SHADOW"      -> accounts "SHADOW "     and "SHADOW."
        read "MAESTRO CN"  -> accounts "MAESTRO CN"  and ".MAESTRO CN"
        read "FNS.KAISER+" -> accounts "FNS.KAISER"  and ".FNS.KAISER"
    Picking one is a coin flip that silently credits the wrong player's kills.
    """

    REAL_DUPLICATE_ACCOUNTS = [
        ("SHADOW", ["SHADOW ", "SHADOW."]),
        ("MAESTRO CN", ["MAESTRO CN", ".MAESTRO CN"]),
        ("FNS.KAISER+", ["FNS.KAISER", ".FNS.KAISER"]),
    ]

    def test_collision_surfaces_for_review_instead_of_guessing(self):
        for read, accounts in self.REAL_DUPLICATE_ACCOUNTS:
            with self.subTest(read=read):
                pool = [{"user_id": _user(a).pk, "username": a, "team_id": None, "team_name": None}
                        for a in accounts]

                row = match_name(read, pool)

                self.assertIsNone(row["matched_user_id"], "an ambiguous read must not pick a user")
                self.assertEqual(row["confidence"], 0.0)
                # Both accounts are handed to the reviewer to choose between.
                self.assertEqual({c["username"] for c in row["top_candidates"]}, set(accounts))

    def test_leet_digit_collision(self):
        # "K1LLUA" and "KILLUA" also collide, because LEET_DIGITS folds 1 -> i.
        pool = [{"user_id": _user(u).pk, "username": u, "team_id": None, "team_name": None}
                for u in ("KILLUA", "K1LLUA")]

        row = match_name("KILLUA", pool)

        self.assertIsNone(row["matched_user_id"])
        self.assertEqual({c["username"] for c in row["top_candidates"]}, {"KILLUA", "K1LLUA"})


class FuzzyTieTests(TestCase):
    """A tie at the top of the FUZZY list is a coin flip and must not bind either side.

    AmbiguousIdentityTests above covers the read that IS one of two colliding names (the exact
    pass). These are the same coin flip one rung down, inside the fuzzy scan, where the read is not
    identical to anything but two or more accounts score the SAME against it. All four were bound
    silently, by alphabetical order, when the clone's stored standalone job was replayed.
    """

    # (read, the accounts that all tie at the top, what the old matcher bound)
    REAL_FUZZY_TIES = [
        # Two accounts differing only in trailing punctuation; both normalize to "shadow" and so
        # both score 100 against the tag-stripped read.
        ("IL.SHADOW", ["SHADOW ", "SHADOW."], "SHADOW "),
        # The placement is the KN team, so "KN.DANTE" was the RIGHT answer - and the matcher took
        # the other one purely because "Dante" sorts first.
        ("KN DANTE F♡", ["Dante", "KN.DANTE"], "Dante"),
        # Five accounts contain "snipe"; none of them is this player.
        ("NXT.SNIPE", ["Gil Sniper GS", "Jacasonsnipe ", "Korexsnipe "], "Gil Sniper GS"),
        ("☆DEON☆™ja", ["D3MON", "DEMON", "DEMON. "], "D3MON"),
    ]

    def test_tied_top_scores_surface_instead_of_binding(self):
        for read, accounts, old_pick in self.REAL_FUZZY_TIES:
            with self.subTest(read=read):
                pool = [{"user_id": _user(a).pk, "username": a, "team_id": None, "team_name": None}
                        for a in accounts]

                row = match_name(read, pool)

                self.assertIsNone(
                    row["matched_user_id"],
                    f"{read!r} ties across {accounts}; before the fix it bound {old_pick!r}",
                )
                self.assertEqual(row["unmatched_reason"], "ambiguous")
                # Every tied account is handed to the reviewer to choose between.
                listed = {c["username"] for c in row["top_candidates"]}
                self.assertTrue(set(accounts).issubset(listed))

    def test_a_clear_winner_still_binds(self):
        # Guard against over-correcting: the tie rule must only fire on an ACTUAL tie. "SABATH24"
        # beats every other account outright on the real data and still resolves.
        for username in ("KN SABATH24", "Abula", "Bs BAN"):
            _user(username)
        pool = [{"user_id": u.pk, "username": u.username, "team_id": None, "team_name": None}
                for u in User.objects.all()]

        row = match_name("SABATH24", pool)

        self.assertEqual(row["matched_username"], "KN SABATH24")
        self.assertEqual(row["unmatched_reason"], "")


class ShortTagCorroborationTests(TestCase):
    """A 2-4 character team_tag hit is one signal, never proof (see SHORT_TAG_MAX_LEN).

    THE CASE: the clone's stored standalone job has a placement whose team cell read "ST". "ST" is
    the registered team_tag of the platform team "Satolas", so it scored a normalized-identity 100
    and bound the standing outright - while that placement's four players ("bopFR@g™R",
    "Deceit S4X™R", "SNEIJDER ™R", "RENASAR™R") have nothing to do with Satolas: none of them is a
    member, and none of them wears "ST" on their IGN. With 608 teams on the platform a two-letter
    collision is close to certain, and a wrong team rewrites a tournament standing, so the tag now
    has to be backed by something outside the team cell before it binds.
    """

    def setUp(self):
        owner = _user("owner")
        self.satolas = _team("Satolas", owner, tag="ST")
        self.il = _team("IL ESPORTS", owner, tag="IL")
        self.teams = matching.all_platform_teams()

    def test_uncorroborated_short_tag_surfaces_with_the_team_as_top_candidate(self):
        row = match_team_name("ST", self.teams)

        self.assertIsNone(row["matched_team_id"],
                          "before the fix 'ST' bound 'Satolas' at 1.00 on the tag alone")
        self.assertEqual(row["unmatched_reason"], "tag_needs_corroboration")
        # Recall is untouched: the team is still the first thing the reviewer sees, at full score.
        self.assertEqual(row["top_candidates"][0]["team_id"], self.satolas.team_id)
        self.assertEqual(row["top_candidates"][0]["confidence"], 1.0)

    def test_membership_corroborates(self):
        # The real "IL" row: its player "IL.DAMIAN" is a member of "IL ESPORTS", so the team cell
        # and a roster record agree and the row binds.
        row = match_team_name("IL", self.teams, player_team_ids={self.il.team_id})

        self.assertEqual(row["matched_team_id"], self.il.team_id)
        self.assertEqual(row["unmatched_reason"], "")

    def test_the_players_worn_tag_corroborates(self):
        # The read team cell says "IL" and the placement's IGNs ("IL.DAMIAN", "IL.BOPA", "IL.SHADOW")
        # independently carry the same tag. Different pixels, so it is a real second observation.
        players_tag = derive_team_tag(["IL.DAMIAN", "IL.BOPA", "IL.SHADOW", "IL.Xtc**"])
        self.assertEqual(players_tag, "il")

        row = match_team_name("IL", self.teams, players_tag=players_tag)

        self.assertEqual(row["matched_team_id"], self.il.team_id)

    def test_a_different_teams_players_do_not_corroborate(self):
        # The exact "ST" failure: the players resolved to a team, just not this one.
        row = match_team_name("ST", self.teams, player_team_ids={self.il.team_id})

        self.assertIsNone(row["matched_team_id"])
        self.assertEqual(row["unmatched_reason"], "tag_needs_corroboration")

    def test_a_tag_that_also_opens_the_team_name_needs_no_corroboration(self):
        # The real "NXT" row: "NXT ESP" is matched by its tag AND by its NAME (the read opens it,
        # 0.88 through the prefix cap). The second signal is already inside the team record, so the
        # guard does not apply and the row keeps binding with no player evidence at all.
        owner = User.objects.get(username="owner")
        nxt = _team("NXT ESP", owner, tag="NXT")

        row = match_team_name("NXT", matching.all_platform_teams())

        self.assertEqual(row["matched_team_id"], nxt.team_id)
        self.assertEqual(row["unmatched_reason"], "")

    def test_a_long_tag_is_specific_enough_on_its_own(self):
        # Team.team_tag is max_length=5, so "2 to 4 needs corroboration" leaves exactly one exempt
        # width: a full-length 5-character tag.
        owner = User.objects.get(username="owner")
        longtag = _team("Phantom Crew", owner, tag="PHNTM")
        self.assertGreater(len("phntm"), SHORT_TAG_MAX_LEN)

        row = match_team_name("PHNTM", matching.all_platform_teams())

        self.assertEqual(row["matched_team_id"], longtag.team_id)
        self.assertEqual(row["unmatched_reason"], "")


class AmbiguousTeamTests(TestCase):
    """Two teams that score identically are a coin flip, exactly like two colliding accounts."""

    def test_three_way_prefix_tie_surfaces(self):
        # Real clone row: the team cell read "TRY" and three platform teams start with it, so all
        # three scored 0.88 through the prefix cap. The matcher bound "TRY HARD " on nothing but
        # alphabetical order.
        owner = _user("owner")
        for name in ("TRY HARD ", "TRY HARDS ", "TRY US."):
            _team(name, owner, tag=None)

        row = match_team_name("TRY", matching.all_platform_teams())

        self.assertIsNone(row["matched_team_id"])
        self.assertEqual(row["unmatched_reason"], "ambiguous")
        self.assertEqual(len({c["team_id"] for c in row["top_candidates"]}), 3)


class WeakPlayerMatchTests(TestCase):
    def test_below_floor_returns_unmatched_but_keeps_candidates(self):
        # Real clone case: the read "salttos" scored 0.88 against the 3-character stylized username
        # "☬ˢ{ÄL}~..." purely because that name is a prefix of it. A short platform record sitting
        # inside a longer read is not evidence, so the row must come back unmatched.
        _user("SAL")
        pool = [{"user_id": u.pk, "username": u.username, "team_id": None, "team_name": None}
                for u in User.objects.all()]

        row = match_name("salttos", pool)

        self.assertIsNone(row["matched_user_id"])
        self.assertEqual(row["confidence"], 0.0)


# ── TEAMS ────────────────────────────────────────────────────────────────────────────────────────
class RealTeamTagTests(TestCase):
    """Bare clan tags read off a result screen must not bind a standing to a coincidental team."""

    def setUp(self):
        owner = _user("owner")
        # Real platform teams the clone matched these tags to, wrongly.
        self.qx4 = _team("QX4", owner, tag="X")
        self.fluxo = _team("FLUXOmz", owner, tag="FX")
        self.aethelgard = _team("AETHELGARD", owner, tag=None)
        self.nxt = _team("NXT ESP", owner, tag=None)
        self.red = _team("RED COMMAND", owner, tag=None)
        self.teams = matching.all_platform_teams()

    def test_short_tag_does_not_resolve_to_a_coincidental_team(self):
        # (read, the team it used to bind to, its old confidence)
        for read, old_pick, old_score in [
            ("XIT", "QX4",        0.90),   # QX4's tag is the single letter "X"
            ("FXP", "FLUXOmz",    0.90),   # FLUXOmz's tag is "FX"
            ("IPW", "AETHELGARD", 0.72),
        ]:
            with self.subTest(read=read):
                row = match_team_name(read, self.teams)
                self.assertIsNone(
                    row["matched_team_id"],
                    f"{read!r} must not bind a team; before the fix it bound {old_pick!r} @{old_score}",
                )

    def test_tag_that_opens_a_team_name_still_resolves(self):
        # Recall: a real tag prefix must still match, or organizers lose the useful case.
        row = match_team_name("NXT", self.teams)
        self.assertEqual(row["matched_team_id"], self.nxt.team_id)

    def test_trailing_punctuation_does_not_flip_the_winner(self):
        # Real clone bug: "NXT." scored "QX4" 0.90 OVER the correct "NXT ESP" 0.857, purely because
        # of the dot. Normalization removes the dot before anything is compared.
        row = match_team_name("NXT.", self.teams)
        self.assertEqual(row["matched_team_id"], self.nxt.team_id)

    def test_leet_digits_fold_to_the_real_team(self):
        # "R3D" used to match "AETHELGARD" @0.72. Folding 3 -> e makes it "red", which is the team.
        row = match_team_name("R3D", self.teams)
        self.assertEqual(row["matched_team_id"], self.red.team_id)

    def test_symbol_only_team_name_matches_nothing(self):
        # A logo-only team cell carries no identity and must not score noise against every team.
        # ("彡☯" normalizes to "". Note NFKD does decompose some symbols into letters - "™" becomes
        # "tm" - so those are NOT symbol-only and take the normal scoring path.)
        row = match_team_name("彡☯", self.teams)
        self.assertIsNone(row["matched_team_id"])
        self.assertEqual(row["top_candidates"], [])


class DeriveTeamTagTests(TestCase):
    """The shared clan tag of a placement, taken from REAL placements in the clone job.

    Every one of these returned "" under the old commonprefix-of-all-players rule.
    """

    def test_real_placements(self):
        cases = [
            # (player names as read, expected tag, why the old rule failed)
            (["生活 X FNS", "IL.PrimeX69", "里克 X FNS", "米奇 X FNS"], "fns", "tag is a SUFFIX"),
            (["KN DANTE F♡", "KN SABATH24", "OBINNA", "AMIRツNE"], "kn", "prefix on only 2 of 4"),
            (["R3D FIREX77", "R3D FAMEZYX1", "MATEツX11TR", "PINTAX11"], "red", "prefix on only 2 of 4"),
            (["NXT.EZIO", "NXT.LEDXpcK", "NXT.SNIPE", "NXT.OBITOpck"], "nxt", "already worked"),
            (["IPW_SUGAR", "IPW_NITRO", "IPW_WIRTZ"], "ipw", "underscore separator"),
        ]
        for names, expected, why in cases:
            with self.subTest(why=why):
                self.assertEqual(derive_team_tag(names), expected)

    def test_no_shared_affix_returns_empty(self):
        # A single stray token must never be promoted into a tag.
        self.assertEqual(derive_team_tag(["Alpha", "Beta", "Gamma", "Delta"]), "")

    def test_single_player_is_not_enough(self):
        self.assertEqual(derive_team_tag(["NXT.EZIO"]), "")


class ScoreNamesTests(TestCase):
    """The shared ladder both matchers sit on."""

    def test_normalized_identity_is_100(self):
        self.assertEqual(score_names("npkillua", "npkillua"), 100.0)

    def test_empty_side_never_matches(self):
        # A name made only of symbols normalizes to "" and must not match anything.
        self.assertEqual(score_names("", "anyone"), 0.0)
        self.assertEqual(score_names("anyone", ""), 0.0)

    def test_single_character_never_matches_a_longer_string(self):
        self.assertEqual(score_names("xit", "x"), 0.0)

    def test_short_coincidence_stays_below_the_assert_floor(self):
        self.assertLess(score_names("fxp", "fx"), MATCH_FLOOR)
