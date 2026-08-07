r"""A blank finishing position is REJECTED on the solo edit path, and a real 0 is kept.

THE BUG (owner report 2026-08-06): "when they are updating leaderboards using the manual method,
if they put 0 as score for a particular player it glitches and does not calculate the results
well, and they can leave score blank for certain players."

The front-end half of that bug lives in lib/scoreInput.ts (a score box rendered with
`value || ""` and read back with `parseInt(value) || 0` made a typed 0 and an empty box the same
value). This file pins the SERVER half.

Three of the four manual-entry write paths already ran validate_placements:
    enter_team_match_result_manual, enter_solo_match_result_manual, edit_match_result.
edit_solo_match_result was the only one without it, so once the front end started sending a blank
placement as null (deliberately, so the server would reject it) the request reached
`int(r["placement"])` and answered with a 500 Django error page instead of a readable 400.
Verified against the running stack before the fix:
    POST placement=null -> 500 TypeError: int() argument must be a string, a bytes-like object
    or a real number, not 'NoneType'

Every test below fails against the pre-fix view.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_solo_edit_blank_placement
"""
import datetime
import json

from django.test import TestCase
from django.urls import reverse

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    RegisteredCompetitors,
    SoloPlayerMatchStats,
    StageGroups,
    Stages,
)

URL = "/events/edit-solo-match-result/"
TOKEN = "solo-edit-blank-placement-token"


class EditSoloMatchResultBlankPlacementTests(TestCase):
    """Drives the real endpoint through the full request -> transaction -> DB write path.

    CONNECTS TO: the three front-end surfaces that POST here - the admin leaderboard editor
    (app/(a)/a/leaderboards/[id]/edit/page.tsx, both its normal save and its adjustment save),
    the organizer editor and GroupResultsEditor.tsx. All three show the returned `message`
    verbatim as a toast, which is why the copy has to name the remedy.
    """

    def setUp(self):
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="solo_edit_admin", email="solo_edit_admin@x.com",
            full_name="Solo Edit Admin", role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token=TOKEN,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=48, event_name="Solo Edit Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Solo LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9, "3": 8}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda")

        # Three solo competitors already holding a saved result, which is the state the editor
        # loads: placement 1/2/3 with 5/3/1 kills.
        self.stats = []
        for index, (placement, kills) in enumerate(((1, 5), (2, 3), (3, 1)), start=1):
            player = User.objects.create(
                username=f"solo_edit_p{index}", email=f"solo_edit_p{index}@x.com",
                full_name=f"Solo Edit Player {index}", role="player", password="x",
                uid=f"90000{index}")
            competitor = RegisteredCompetitors.objects.create(
                event=self.event, user=player, status="approved")
            self.stats.append(SoloPlayerMatchStats.objects.create(
                match=self.match, competitor=competitor, placement=placement, kills=kills,
                played=True, bonus_points=0, penalty_points=0,
                placement_points=self.leaderboard.placement_points[str(placement)],
                kill_points=kills, total_points=
                self.leaderboard.placement_points[str(placement)] + kills))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _rows(self, overrides_by_index=None):
        """The full row set the editor posts, with per-row overrides keyed by row index."""
        overrides_by_index = overrides_by_index or {}
        rows = []
        for index, stat in enumerate(self.stats):
            row = {
                "competitor_id": stat.competitor_id,
                "placement": stat.placement,
                "kills": stat.kills,
                "played": True,
                "bonus_points": 0,
                "penalty_points": 0,
            }
            row.update(overrides_by_index.get(index, {}))
            rows.append(row)
        return rows

    def _post(self, rows):
        return self.client.post(
            URL, data=json.dumps({"match_id": str(self.match.match_id), "rows": rows}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def _reload(self, index):
        return SoloPlayerMatchStats.objects.get(
            match=self.match, competitor_id=self.stats[index].competitor_id)

    # ── "they can leave score blank for certain players" ─────────────────────

    def test_a_blank_placement_is_rejected_with_a_readable_message(self):
        # Arrange: the middle player's finishing position box was cleared.
        rows = self._rows({1: {"placement": None}})

        # Act
        resp = self._post(rows)

        # Assert: a 400 naming the remedy, NOT the 500 this used to raise.
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("needs a finishing position", resp.json()["message"])

    def test_a_rejected_save_leaves_the_stored_results_untouched(self):
        # The guard runs BEFORE transaction.atomic(), so a rejected save must not part-write.
        self._post(self._rows({1: {"placement": None}}))

        self.assertEqual(self._reload(1).placement, 2)
        self.assertEqual(self._reload(1).total_points, 12)

    def test_a_blank_placement_sent_as_empty_string_is_also_rejected(self):
        # A raw fetch caller (or an older client) can send "" rather than null; both mean blank.
        resp = self._post(self._rows({1: {"placement": ""}}))

        self.assertEqual(resp.status_code, 400, resp.content)

    def test_a_blank_placement_on_a_NOT_played_row_is_allowed_and_stores_zero(self):
        # Unticking Played is how an organizer says a competitor sat this map out, so that row is
        # exempt from the guard. It must still not raise on int(None).
        resp = self._post(self._rows({2: {"placement": None, "played": False}}))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._reload(2).placement, 0)

    # ── "if they put 0 as score ... it does not calculate the results well" ──

    def test_a_deliberate_zero_kills_is_stored_and_scored_as_zero(self):
        # Arrange: the map winner went scoreless. Their placement points must survive intact.
        resp = self._post(self._rows({0: {"kills": 0}}))

        self.assertEqual(resp.status_code, 200, resp.content)
        winner = self._reload(0)
        self.assertEqual(winner.kills, 0)
        self.assertEqual(winner.kill_points, 0)
        self.assertEqual(winner.placement_points, 12)  # placement is untouched by a 0-kill entry
        self.assertEqual(winner.total_points, 12)

    def test_zero_kills_for_every_player_keeps_every_placement_score(self):
        # A wiped lobby: nobody killed anyone, so only placement points remain.
        resp = self._post(self._rows(
            {0: {"kills": 0}, 1: {"kills": 0}, 2: {"kills": 0}}))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([self._reload(i).kill_points for i in range(3)], [0, 0, 0])
        self.assertEqual([self._reload(i).total_points for i in range(3)], [12, 9, 8])

    def test_a_valid_unchanged_resave_still_succeeds(self):
        # Regression guard: adding the validation must not break an ordinary save.
        resp = self._post(self._rows())

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([self._reload(i).total_points for i in range(3)], [17, 12, 9])
