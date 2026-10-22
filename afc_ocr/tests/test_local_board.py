"""
Tests for the on-site OCR reader's board structuring (afc_ocr/services/local_ocr.py, stage 2) and
for the no-key rule in afc_ocr/services/extract.py (inbox #328, 22 Oct 2026).

The structuring is tested on a SYNTHETIC board: text boxes laid out exactly the way the reader
returns them for a Free Fire result screen (two columns of teams, two lines per team, two players
per line, "N Eliminations" after each name). The geometry is copied from the real 1792x828
screenshots measured on 22 Oct 2026; the names are invented. No model runs here: these tests need
neither rapidocr nor onnxruntime, so they run anywhere the backend runs.

What each case pins down is a fault the first version had, or a screen the owner described:
  - one team is TWO lines, not one (v0 numbered every line as a team);
  - a name the reader splits into words is one player ("SEN" + "TOKYO", v0 made two);
  - a top-3 badge read as junk text must not shift the teams below it;
  - the right column scrolls to reach team 12, cutting team 6 off the top, and sometimes only that
    screenshot is sent (owner, 22 Oct 2026): it is numbered from the team numbers on screen;
  - a right column with no team number seen is not trusted (counting on could be off by one);
  - an upside-down or sideways upload is recognised from the layout (owner, 22 Oct 2026).
"""
from unittest import mock

from django.test import TestCase, override_settings

from afc_ocr.services import extract, local_ocr

H = 24.0                      # text height of one line, as read on a 1792x828 screenshot
LINE = 44.0                   # two lines of one team
PITCH = 108.0                 # one team to the next
TOP = 214.0                   # first line of team 1 / team 6
LEFT_SLOTS = ((345, 465, 497, 607), (670, 800, 827, 938))      # (name x0, x1, kill x0, x1)
RIGHT_SLOTS = ((1060, 1200, 1222, 1334), (1340, 1420, 1466, 1556))


def _box(text, x0, x1, cy, h=H, score=0.97):
    return {"text": text, "score": score, "cx": (x0 + x1) / 2, "cy": cy, "h": h, "x0": x0, "x1": x1}


def _team_boxes(players, slots, line1_cy, glue=False):
    """Boxes for one team: players[0..1] on line 1, players[2..3] on line 2. A player name is
    "TAG NAME" read as two separate boxes, like the reader does. glue=True draws the right
    column the way the game does there: the first kill cell and the next player's name overlap
    and come back as ONE box ("1 EliminationsTAG NAME")."""
    out = []
    for i, (name, kills) in enumerate(players):
        line, slot = divmod(i, 2)
        cy = line1_cy + line * LINE
        nx0, nx1, kx0, kx1 = slots[slot]
        tag, rest = name.split(" ", 1)
        if not (glue and slot == 1):
            out.append(_box(tag, nx0, nx0 + 40, cy))
            out.append(_box(rest, nx0 + 50, nx1, cy))
        if glue and slot == 0 and i + 1 < len(players) and (i + 1) // 2 == line:
            nxt = players[i + 1][0]
            out.append(_box(f"{kills} Eliminations{nxt}", kx0, slots[1][1], cy))
        else:
            word = "Eliminatic" if glue and slot == 1 else "Eliminations"
            out.append(_box(f"{kills} {word}", kx0, kx1, cy))
    return out


def _players(team_no):
    return [(f"T{team_no}A PLAYER{team_no}{c}", (team_no + k) % 7) for k, c in enumerate("WXYZ")]


def _board(left=range(1, 6), right=range(6, 12), right_numbers=True, badge_junk=True, cut_first_right=False):
    """A whole board. left / right: the team numbers drawn in each column, top to bottom."""
    boxes = [_box("FREEFIRE", 731, 1053, 69, h=46)]
    for row, n in enumerate(left):
        cy = TOP + row * PITCH
        boxes += _team_boxes(_players(n), LEFT_SLOTS, cy)
        if n > 3:
            boxes.append(_box(str(n), 255, 275, cy + LINE / 2, h=30))
        elif badge_junk and n == 3:
            boxes.append(_box("E", 230, 300, cy + LINE / 2, h=95))   # the "3" badge, read as junk
    for row, n in enumerate(right):
        cy = TOP + row * PITCH
        players = _players(n)
        team = _team_boxes(players, RIGHT_SLOTS, cy, glue=True)
        if cut_first_right and row == 0:
            team = [b for b in team if b["cy"] > cy + 1]               # top line scrolled away
            cy_rank = cy + LINE
        else:
            cy_rank = cy + LINE / 2
        boxes += team
        if right_numbers:
            boxes.append(_box(str(n), 995, 1025, cy_rank, h=30))
    boxes.append(_box("BACK", 1409, 1477, 765, h=33))
    return boxes


def _engine():
    eng = object.__new__(local_ocr.LocalOCREngine)
    eng.model_version = "test"
    return eng


def _read(boxes, event_type="team"):
    eng = _engine()
    result, signals = eng._structure(boxes, event_type)
    conf = eng._confidence(boxes, result, signals, event_type)
    return result, conf


def _norm(players):
    return sorted((p["name"].replace(" ", ""), p["kills"]) for p in players)


class BoardStructureTests(TestCase):
    def test_full_board_reads_every_team_with_its_four_players(self):
        result, conf = _read(_board())
        got = {p["placement"]: p["players"] for p in result["placements"]}
        self.assertEqual(sorted(got), list(range(1, 12)))
        for n in range(1, 12):
            self.assertEqual(_norm(got[n]), _norm([{"name": a, "kills": k} for a, k in _players(n)]), n)
        self.assertTrue(conf["ok"], conf)

    def test_badge_read_as_junk_does_not_shift_the_teams_below(self):
        result, _ = _read(_board(badge_junk=True))
        team4 = next(p for p in result["placements"] if p["placement"] == 4)
        self.assertEqual(_norm(team4["players"]), _norm([{"name": a, "kills": k} for a, k in _players(4)]))

    def test_scrolled_right_column_is_numbered_from_the_numbers_on_screen(self):
        # Team 6 is gone, team 7 half cut at the top: the right column shows 7 to 12.
        result, conf = _read(_board(right=range(7, 13), cut_first_right=True))
        numbers = [p["placement"] for p in result["placements"]]
        self.assertEqual(numbers, [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12])
        team7 = next(p for p in result["placements"] if p["placement"] == 7)
        self.assertEqual(len(team7["players"]), 2, "only the visible line of the cut team")
        team12 = next(p for p in result["placements"] if p["placement"] == 12)
        self.assertEqual(_norm(team12["players"]), _norm([{"name": a, "kills": k} for a, k in _players(12)]))
        self.assertTrue(conf["ok"], conf)

    def test_right_column_without_a_team_number_is_not_trusted(self):
        result, conf = _read(_board(right_numbers=False))
        self.assertEqual([p["placement"] for p in result["placements"]][5], 6, "counted on from the left column")
        self.assertFalse(conf["ok"])
        self.assertEqual(conf["unanchored_columns"], 1)

    def test_conflicting_team_numbers_are_not_trusted(self):
        boxes = _board()
        boxes.append(_box("9", 995, 1025, TOP + LINE / 2, h=30))  # a second number on team 6
        _, conf = _read(boxes)
        self.assertFalse(conf["ok"])
        self.assertGreaterEqual(conf["rank_conflicts"], 1)

    def test_a_kill_cell_with_no_count_is_not_trusted(self):
        # team 1, first player: the count in front of "Eliminations" was not read
        boxes = [b for b in _board() if not (b["x0"] == 497 and b["cy"] == TOP)]
        self.assertEqual(len(boxes), len(_board()) - 1)
        boxes.append(_box("Eliminations", 497, 607, TOP))
        _, conf = _read(boxes)
        self.assertFalse(conf["ok"])
        self.assertGreaterEqual(conf["unknown_kills"], 1)

    def test_reader_slips_in_kill_cells_are_read(self):
        piece = local_ocr.LocalOCREngine._pieces
        self.assertEqual(piece(_box("O Eliminations", 0, 100, 0))[0]["kills"], 0)
        self.assertEqual(piece(_box("l Eliminatic", 0, 100, 0))[0]["kills"], 1)
        glued = piece(_box("3 EliminationsLORD JA", 0, 200, 0))
        self.assertEqual([(p["kind"], p["text"]) for p in glued], [("kill", "3 Eliminations"), ("name", "LORD JA")])

    def test_solo_board_always_escalates(self):
        _, conf = _read(_board(), event_type="solo")
        self.assertFalse(conf["ok"])

    def test_upright_and_flipped_layouts_are_told_apart(self):
        boxes = _board()
        width, height = 1792.0, 828.0
        flipped = [dict(b, x0=width - b["x1"], x1=width - b["x0"], cx=width - b["cx"], cy=height - b["cy"])
                   for b in boxes]
        up, n = local_ocr.LocalOCREngine._upright_margin(boxes)
        down, _ = local_ocr.LocalOCREngine._upright_margin(flipped)
        self.assertGreater(up, max(3, 0.25 * n))
        self.assertLess(down, 0)

    def test_read_oriented_keeps_the_upright_turn(self):
        # A flipped upload: the first read looks upside down, the half-turned read looks upright.
        import cv2
        import numpy as np
        eng = _engine()
        width, height = 1792.0, 828.0
        upright = _board()
        flipped = [dict(b, x0=width - b["x1"], x1=width - b["x0"], cx=width - b["cx"], cy=height - b["cy"])
                   for b in upright]
        reads = iter([flipped, upright])
        eng._boxes = lambda img: next(reads)
        ok, png = cv2.imencode(".png", np.zeros((828, 1792, 3), dtype=np.uint8))
        img, boxes = eng._read_oriented(png.tobytes())
        self.assertIs(boxes, upright)


_TRUSTED = ({"match_type": "team", "placements": [{"placement": 1, "players": [{"name": "Local", "kills": 2}]}]},
            {"ok": True, "mean_score": 0.95, "named_frac": 1.0, "oversized_frac": 0.0, "model_version": "t"})
_UNSURE = ({"match_type": "team", "placements": [{"placement": 1, "players": [{"name": "Local", "kills": 2}]}]},
           {"ok": False, "mean_score": 0.95, "named_frac": 1.0, "oversized_frac": 0.0, "model_version": "t"})
_AI = {"match_type": "team", "placements": [{"placement": 1, "players": [{"name": "Ai", "kills": 2}]}]}


@override_settings(OCR_LOCAL_FIRST=True, OCR_GEMINI_FALLBACK=True, GEMINI_API_KEY="afc-gemini-key")
class NoKeyRuleTests(TestCase):
    """extract_rows and can_read with the on-site reader installed (owner 2026-10-22: "all i need
    is for a way to use OCR on the site"). The reader itself is mocked; its output decides."""

    def setUp(self):
        from afc_organizers.test_ai_key import _org, _user
        self.owner, _ = _user("nk_owner")
        self.org = _org("nk-org", self.owner)
        self.org.ocr_free_reads_left = 0
        self.org.save(update_fields=["ocr_free_reads_left"])
        patcher = mock.patch.object(local_ocr, "is_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read(self, local, **kw):
        engine = mock.Mock()
        engine.run.return_value = local
        with mock.patch.object(local_ocr, "get_engine", return_value=engine), \
                mock.patch.object(extract, "ai_read", return_value=dict(_AI)) as ai:
            raw, label = extract.extract_rows(b"img", "image/png", "team", org=self.org, actor=self.owner, **kw)
        return raw, label, ai, engine

    def test_a_sure_local_read_needs_no_key_and_spends_nothing(self):
        raw, label, ai, _ = self._read(_TRUSTED)
        self.assertEqual(raw["placements"][0]["players"][0]["name"], "Local")
        self.assertTrue(label.startswith("local_student_"))
        ai.assert_not_called()

    def test_an_unsure_read_without_a_key_comes_back_as_a_draft_to_check(self):
        raw, label, ai, _ = self._read(_UNSURE)
        self.assertEqual(raw["placements"][0]["players"][0]["name"], "Local")
        self.assertTrue(label.startswith("local_best_effort_"))
        ai.assert_not_called()

    def test_without_a_review_step_an_unsure_read_is_still_refused(self):
        with self.assertRaises(extract.OcrKeyRequired):
            self._read(_UNSURE, allow_draft=False)

    def test_an_unsure_read_goes_to_the_organizations_own_key(self):
        from afc_organizers.models import OrganizationAiKey
        key = OrganizationAiKey(organization=self.org, provider="openai", model="gpt-4.1-mini")
        key.set_key("sk-org-key-1234")
        key.save()
        raw, label, ai, _ = self._read(_UNSURE)
        ai.assert_called_once()
        self.assertEqual(raw["placements"][0]["players"][0]["name"], "Ai")

    def test_an_organization_switched_off_is_refused_before_anything_is_read(self):
        self.org.ocr_disabled = True
        self.org.save(update_fields=["ocr_disabled"])
        with mock.patch.object(local_ocr, "get_engine") as get_engine:
            with self.assertRaises(extract.OcrKeyRequired):
                extract.extract_rows(b"img", "image/png", "team", org=self.org, actor=self.owner)
        get_engine.assert_not_called()
        self.assertFalse(extract.can_read(self.org, self.owner))

    def test_can_read_with_the_reader_installed_and_no_free_read_left(self):
        self.assertTrue(extract.can_read(self.org, self.owner))
        self.assertFalse(extract.key_available(self.org, self.owner))
        with mock.patch.object(local_ocr, "is_available", return_value=False):
            self.assertFalse(extract.can_read(self.org, self.owner))

    def test_one_unsure_screenshot_marks_the_whole_map_for_checking(self):
        self.assertEqual(extract.summary_engine(["local_best_effort_x", "local_student_x"]), "local_best_effort_x")
        self.assertEqual(extract.summary_engine(["gemini-2.5-flash", "local_student_x"]), "local_student_x")
        self.assertEqual(extract.summary_engine(["", None]), "")

    def test_a_reopened_session_says_which_reader_read_it(self):
        import datetime as dt
        from datetime import date
        from afc_ocr.models import OCRSession
        from afc_organizers.test_ai_key import _event, _user
        from afc_tournament_and_scrims.models import Leaderboard, Match, StageGroups, Stages
        admin, tok = _user("nk_admin", role="admin")
        event = _event(self.owner, self.org)
        st = Stages.objects.create(event=event, stage_name="S", start_date=date.today(), end_date=date.today(),
                                   number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1)
        grp = StageGroups.objects.create(stage=st, group_name="A", playing_date=date.today(),
                                         playing_time=dt.time(18, 0), teams_qualifying=1, match_count=1)
        lb = Leaderboard.objects.create(leaderboard_name="LB", event=event, stage=st, group=grp, creator=self.owner,
                                        placement_points={"1": 12}, kill_point=1.0, leaderboard_method="manual")
        match = Match.objects.create(leaderboard=lb, group=grp, match_number=1, match_map="bermuda",
                                     scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})
        s = OCRSession.objects.create(match=match, map_index=1, created_by=admin, event_type="team",
                                      raw_output={"placements": [], "_engine": "local_best_effort_t"}, draft_rows=[])
        from django.test import Client
        r = Client().get(f"/events/ocr-session/{s.session_id}/", HTTP_AUTHORIZATION=f"Bearer {tok}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["engine"], "local_best_effort_t")

    def test_the_leaderboard_batch_run_is_queued_without_a_key(self):
        # The fault that left seven production jobs "pending" since 24 and 26 Sept 2026.
        from afc_leaderboard.models import LeaderboardOcrJob, StandaloneLeaderboard
        from afc_organizers.models import OrganizationMember
        from afc_organizers.test_ai_key import _post
        OrganizationMember.objects.filter(organization=self.org, user=self.owner).update(can_upload_results=True)
        lb = StandaloneLeaderboard.objects.create(name="LB", format="team", organization=self.org, creator=self.owner)
        job = LeaderboardOcrJob.objects.create(leaderboard=lb, created_by=self.owner)
        with mock.patch("afc_leaderboard.views.process_leaderboard_ocr_job.delay") as delay:
            r = _post("tok_nk_owner", f"/leaderboards/standalone/{lb.pk}/ocr/jobs/{job.pk}/run/")
        self.assertEqual(r.status_code, 200, r.content)
        delay.assert_called_once()
