"""
Tests for the OCR name memory per organization (afc_ocr/services/name_memory.py), owner 2026-10-22,
inbox #334 (each organizer its own memory, gone when the organizer is) and #335 (it learns from every
confirmed player AND every failed one).

Players and spellings are invented; matching.match_name is exercised with the roster shape
get_registered_players returns ({user_id, username, team_id, team_name}).
"""
from django.test import TestCase

from afc_ocr.models import OCRNameAlias
from afc_ocr.services import matching
from afc_ocr.services.commit import save_name_corrections
from afc_ocr.services.name_memory import NameMemory, learn


def _roster(*users):
    return [{"user_id": u.pk, "username": u.username, "team_id": None, "team_name": None} for u in users]


class NameMemoryTests(TestCase):
    def setUp(self):
        from afc_organizers.test_ai_key import _org, _user
        self.owner, _ = _user("nm_owner")
        self.org = _org("nm-org", self.owner)
        self.other_owner, _ = _user("nm_other")
        self.other = _org("nm-other", self.other_owner)
        self.hajime, _ = _user("hajimexx")
        self.mickey, _ = _user("heismickey")
        self.roster = _roster(self.hajime, self.mickey)

    def test_a_confirmed_spelling_is_matched_from_memory_next_time(self):
        # a reader misread that looks like nobody on the roster (the site's normalizer already folds
        # look-alike letters such as Greek alpha, so a fancy-font spelling would match without memory)
        read = "ZZ9 TOTALLYOFF"
        before = matching.match_name(read, self.roster, memory=NameMemory(self.org))
        self.assertIsNone(before["matched_user_id"])
        self.assertEqual(before["match_source"], "")
        learn(self.org, [(read, None, self.hajime.pk)])
        after = matching.match_name(read, self.roster, memory=NameMemory(self.org))
        self.assertEqual(after["matched_user_id"], self.hajime.pk)
        self.assertEqual(after["confidence"], 1.0)
        self.assertEqual(after["match_source"], "memory")

    def test_a_close_spelling_is_recalled_only_when_one_player_fits(self):
        learn(self.org, [("HAJIME.Xx", None, self.hajime.pk)])
        close = matching.match_name("HAJIME.Xxx", self.roster, memory=NameMemory(self.org))
        self.assertEqual((close["matched_user_id"], close["match_source"], close["confidence"]),
                         (self.hajime.pk, "memory", 0.95))
        # two different players remembered for close spellings: memory stays out of it
        learn(self.org, [("HAJIME.Xy", None, self.mickey.pk)])
        user_id, how = NameMemory(self.org).recall("HAJIME.Xxy")
        self.assertIsNone(user_id)

    def test_memory_is_ignored_for_a_player_not_on_this_roster(self):
        learn(self.org, [("ZQ GHOSTLY", None, self.mickey.pk)])
        row = matching.match_name("ZQ GHOSTLY", _roster(self.hajime), memory=NameMemory(self.org))
        self.assertNotEqual(row["matched_user_id"], self.mickey.pk)
        self.assertEqual(row["match_source"], "")

    def test_each_organizer_has_its_own_memory(self):
        learn(self.other, [("XQ WEIRDREAD", None, self.mickey.pk)])
        mine = matching.match_name("XQ WEIRDREAD", self.roster, memory=NameMemory(self.org))
        theirs = matching.match_name("XQ WEIRDREAD", self.roster, memory=NameMemory(self.other))
        self.assertIsNone(mine["matched_user_id"])
        self.assertEqual(theirs["matched_user_id"], self.mickey.pk)
        # AFC-run events (no organization) have their own memory too
        self.assertIsNone(NameMemory(None).recall("XQ WEIRDREAD")[0])

    def test_the_memory_goes_with_the_organizer(self):
        learn(self.org, [("ANY READ", None, self.hajime.pk)])
        self.assertEqual(OCRNameAlias.objects.filter(organization=self.org).count(), 1)
        self.org.delete()
        self.assertEqual(OCRNameAlias.objects.filter(raw_norm="anyread").count(), 0)

    def test_a_turned_down_suggestion_is_not_picked_again_for_that_spelling(self):
        # the matcher binds "heismickey" for this read by normalized identity...
        first = matching.match_name("HEIS MICKEY", self.roster, memory=NameMemory(self.org))
        self.assertEqual(first["matched_user_id"], self.mickey.pk)
        # ...a reviewer said that read was not him (marked "not on the platform")
        learn(self.org, [("HEIS MICKEY", self.mickey.pk, None)])
        again = matching.match_name("HEIS MICKEY", self.roster, memory=NameMemory(self.org))
        self.assertIsNone(again["matched_user_id"])
        self.assertEqual(again["unmatched_reason"], matching.REASON_REJECTED_BEFORE)
        self.assertIn(self.mickey.pk, [c["user_id"] for c in again["top_candidates"]], "still pickable")
        # confirmed twice since: the confirmations win
        learn(self.org, [("HEIS MICKEY", None, self.mickey.pk), ("HEIS MICKEY", None, self.mickey.pk)])
        back = matching.match_name("HEIS MICKEY", self.roster, memory=NameMemory(self.org))
        self.assertEqual((back["matched_user_id"], back["match_source"]), (self.mickey.pk, "memory"))

    def test_a_correction_confirms_one_player_and_rejects_the_other(self):
        learn(self.org, [("MICKEY H", self.mickey.pk, self.hajime.pk)])
        rows = {(r.user_id, r.match_count, r.rejected_count)
                for r in OCRNameAlias.objects.filter(organization=self.org)}
        self.assertEqual(rows, {(self.hajime.pk, 1, 0), (self.mickey.pk, 0, 1)})

    def test_the_event_commit_teaches_every_committed_row(self):
        original = [{"row_id": "a", "raw_name": "SEN T0KY0", "matched_user_id": self.mickey.pk},
                    {"row_id": "b", "raw_name": "HAJIME", "matched_user_id": self.hajime.pk}]
        final = [{"row_id": "a", "raw_name": "SEN T0KY0", "matched_user_id": self.hajime.pk},
                 {"row_id": "b", "raw_name": "HAJIME", "matched_user_id": self.hajime.pk}]
        self.assertEqual(save_name_corrections(final, original, self.org), 3)
        memory = NameMemory(self.org)
        self.assertEqual(memory.recall("SEN T0KY0"), (self.hajime.pk, "same"))
        self.assertEqual(memory.rejected("SEN T0KY0"), {self.mickey.pk})
        self.assertEqual(memory.recall("HAJIME"), (self.hajime.pk, "same"))

    def test_no_memory_given_means_no_memory_step(self):
        learn(None, [("QQ STRANGE", None, self.mickey.pk)])
        row = matching.match_name("QQ STRANGE", self.roster)
        self.assertEqual(row["match_source"], "")


class ReviewedDecisionsTests(TestCase):
    """afc_leaderboard.views._reviewed_decisions: the apply payload's per-player decisions, named
    fields and types only."""

    def test_only_named_typed_fields_of_real_users_are_kept(self):
        from afc_leaderboard.views import _reviewed_decisions
        from afc_organizers.test_ai_key import _user
        u, _ = _user("rd_player")
        rows = [
            {"reviewed_players": [
                {"name": " SEN TOKYO ", "suggested_user_id": str(u.pk), "user_id": None, "role": "admin"},
                {"name": "GHOST ONE", "suggested_user_id": 99999999, "user_id": u.pk},
                {"name": "", "user_id": u.pk},
                {"name": "BOOL", "user_id": True},
                "not a dict",
            ]},
            {"reviewed_players": "nope"},
            {},
        ]
        self.assertEqual(_reviewed_decisions(rows), [
            ("SEN TOKYO", u.pk, None),
            ("GHOST ONE", None, u.pk),
            ("BOOL", None, None),
        ])


class LeaderboardRowsCarryTheSourceTests(TestCase):
    """The standalone leaderboard row builders copy named fields one by one; match_source must be one of
    them, or the review never says a row was remembered (found in the 22 Oct 2026 walk)."""

    def test_solo_and_team_rows_say_when_memory_decided(self):
        from afc_leaderboard.ocr import build_solo_ocr_rows, build_team_ocr_rows
        from afc_organizers.test_ai_key import _org, _user
        owner, _ = _user("src_owner")
        org = _org("src-org", owner)
        player, _ = _user("shadow_su")
        learn(org, [("S&USHADOW", None, player.pk)])
        pool = _roster(player)
        raw = {"placements": [{"placement": 1, "players": [{"name": "S&USHADOW", "kills": 3}]}]}
        solo = build_solo_ocr_rows(raw, pool, memory=NameMemory(org))
        self.assertEqual((solo[0]["matched_user_id"], solo[0]["match_source"]), (player.pk, "memory"))
        team = build_team_ocr_rows(raw, [], players_pool=pool, memory=NameMemory(org))
        detail = team[0]["players_detail"][0]
        self.assertEqual((detail["matched_user_id"], detail["match_source"]), (player.pk, "memory"))
