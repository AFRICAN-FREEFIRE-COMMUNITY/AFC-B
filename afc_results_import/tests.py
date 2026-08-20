"""
afc_results_import.tests - importing a tournament AFC did not run.

Builds real .xlsx bytes in memory with openpyxl and pushes them through the same parse -> resolve ->
commit path the endpoint uses, so the tests exercise the actual file format rather than a
hand-made dict that only resembles it.

The shapes under test are the two an external organizer really publishes (spec section 6):
  SUMMED     one row per team for a whole group, which is what a standings graphic gives you
  PER MATCH  ordinary results, published match by match

Data is taken from the real FFWS Africa 2026 Fall Group A standings, so the numbers here are the
numbers a person would actually type.

Run: python manage.py test afc_results_import
"""
import datetime
import io

from django.test import TestCase

from afc_auth.models import User
from afc_rankings.models import GhostTeam
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMatchStats,
    StageCompetitor, StageGroupCompetitor,
)

from .models import ResultsImport, ExternalResultTeamAlias
from .parsing import parse_workbook, ParseProblem
from .services import build_preview, commit_import

TODAY = datetime.date.today()


def _xlsx(sheets):
    """Real .xlsx bytes. `sheets` is {sheet_name: [row, row, ...]}."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# The real published FFWS Africa 2026 Fall, Play-ins Phase 1, Group A standings.
FFWS_GROUP_A = [
    ["TEAM", "MATCHES", "BOOYAH", "SCORE", "ELIMS", "TOTAL", "POSITION"],
    ["ELITE HUNTERS", 6, 3, 47, 82, 129, 1],
    ["LAXUS E-SPORTS", 6, 1, 54, 55, 109, 2],
    ["BERSERK GENERATION", 6, 1, 29, 62, 91, 3],
    ["FORSAKEN ESPORTS", 6, 1, 38, 49, 87, 4],
    ["AXIS HELLS", 6, 0, 36, 35, 71, 5],
    # Real data: teams in one group played DIFFERENT numbers of matches.
    ["OTAKU GAMER", 5, 0, 19, 12, 29, 10],
    ["VANGUARD ESPORTS", 4, 0, 5, 2, 7, 12],
]


class ParserTests(TestCase):
    """Pure parsing. No database."""

    def test_summed_sheet_is_recognised_and_read(self):
        parsed = parse_workbook(_xlsx({"Group A": FFWS_GROUP_A}))
        sheet = parsed["sheets"][0]

        self.assertEqual(sheet["kind"], "summed")
        self.assertEqual(len(sheet["rows"]), 7)
        first = sheet["rows"][0]
        self.assertEqual(
            (first["team"], first["matches"], first["booyah"], first["total"], first["position"]),
            ("ELITE HUNTERS", 6, 3, 129, 1),
        )

    def test_per_match_sheet_is_recognised_by_its_MATCH_column(self):
        parsed = parse_workbook(_xlsx({"Finals": [
            ["MATCH", "MAP", "TEAM", "PLACE", "KILLS"],
            [1, "Bermuda", "ELITE HUNTERS", 1, 14],
            [1, "Bermuda", "LAXUS E-SPORTS", 4, 9],
        ]}))
        sheet = parsed["sheets"][0]

        self.assertEqual(sheet["kind"], "per_match")
        self.assertEqual(sheet["rows"][0]["map"], "bermuda")
        self.assertEqual(sheet["rows"][0]["placement"], 1)

    def test_headers_are_case_and_punctuation_insensitive(self):
        """A real file says "Team Name" or "TOTAL_POINTS", not the canonical spelling."""
        parsed = parse_workbook(_xlsx({"G": [
            ["Team Name", "Matches Played", "Booyahs", "Placement Points", "Eliminations",
             "Total Points", "Rank"],
            ["ELITE HUNTERS", 6, 3, 47, 82, 129, 1],
        ]}))
        self.assertEqual(parsed["sheets"][0]["rows"][0]["total"], 129)

    def test_a_total_that_disagrees_with_its_parts_is_kept_and_reported(self):
        """The published total is the OFFICIAL result. An external organizer's scoring rules are not
        necessarily AFC's, so a mismatch is reported, never silently corrected."""
        parsed = parse_workbook(_xlsx({"G": [
            ["TEAM", "MATCHES", "SCORE", "ELIMS", "TOTAL"],
            ["ELITE HUNTERS", 6, 47, 82, 200],
        ]}))

        self.assertEqual(parsed["sheets"][0]["rows"][0]["total"], 200)
        self.assertTrue(any("Keeping 200 as published" in p for p in parsed["problems"]))

    def test_a_row_without_MATCHES_is_skipped_with_a_reason(self):
        """A summed row that does not say how many matches it covers cannot report matches played,
        so it is refused individually rather than silently counted as one."""
        parsed = parse_workbook(_xlsx({"G": [
            ["TEAM", "MATCHES", "TOTAL"],
            ["ELITE HUNTERS", 6, 129],
            ["NO MATCHES TEAM", None, 50],
        ]}))

        self.assertEqual(len(parsed["sheets"][0]["rows"]), 1)
        self.assertTrue(any("MATCHES is missing" in p for p in parsed["problems"]))

    def test_a_workbook_with_no_results_is_refused_clearly(self):
        with self.assertRaises(ParseProblem) as ctx:
            parse_workbook(_xlsx({"Notes": [["just a note"], ["nothing here"]]}))
        self.assertIn("No results were found", str(ctx.exception))


class ImportCommitTests(TestCase):
    """Parse, resolve and write, against a real event structure."""

    def setUp(self):
        self.actor = User.objects.create(username="importer", email="imp@example.com")
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=144, event_name="FFWS Africa 2026 Fall", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=4,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Play-ins Phase 1", start_date=TODAY, end_date=TODAY,
            number_of_groups=12, stage_format="br - normal", teams_qualifying_from_stage=43,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=3, match_count=6, match_maps=[],
        )
        # One competitor AFC already knows, so the import has both kinds to resolve.
        self.known = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.imp = ResultsImport.objects.create(
            event=self.event, uploaded_by=self.actor, source_filename="ffws.xlsx")

    def _commit(self, sheets=None):
        return commit_import(self.imp, _xlsx(sheets or {"Group A": FFWS_GROUP_A}), actor=self.actor)

    def test_preview_writes_nothing(self):
        """The whole safety argument: a bad file produces a report, not a half-imported event."""
        before = TournamentTeam.objects.count(), GhostTeam.objects.count()

        preview = build_preview(self.event, _xlsx({"Group A": FFWS_GROUP_A}))

        self.assertEqual((TournamentTeam.objects.count(), GhostTeam.objects.count()), before)
        self.assertEqual(preview["total_rows"], 7)
        self.assertGreaterEqual(preview["to_create"], 6)   # everyone except Berserk Generation

    def test_commit_writes_one_aggregate_row_per_team(self):
        summary = self._commit()

        self.assertEqual(summary["stats_rows"], 7)
        rows = TournamentTeamMatchStats.objects.filter(match__group=self.group)
        self.assertEqual(rows.count(), 7)
        self.assertTrue(all(r.is_aggregate for r in rows))
        self.assertTrue(all(r.placement is None for r in rows))

        elite = rows.get(tournament_team__ghost_team__team_name="ELITE HUNTERS")
        self.assertEqual(
            (elite.matches_counted, elite.booyah_count, elite.final_position,
             elite.placement_points, elite.kills, elite.total_points),
            (6, 3, 1, 47, 82, 129),
        )

    def test_a_known_team_is_matched_not_duplicated_as_a_ghost(self):
        self._commit()
        tt = TournamentTeam.objects.get(event=self.event, team=self.known)
        self.assertFalse(tt.is_ghost)
        self.assertFalse(GhostTeam.objects.filter(team_name__iexact="Berserk Generation").exists())

    def test_the_synthetic_match_says_multiple_maps(self):
        """A BO6 group spans several maps, so none of the six real map values is true."""
        self._commit()
        match = Match.objects.get(group=self.group, upload_method="xlsx_import")
        self.assertEqual(match.match_map, "multiple")
        self.assertEqual(match.played_on, self.group.playing_date)

    def test_membership_is_taken_from_the_sheet(self):
        """Appearing in a stage's sheet IS the statement that the team is in that stage and group.
        FFWS Phase 1 advances "top 3 per group plus the 7 best 4th places", which no per-stage
        qualifying number can express, so this is read from the file rather than derived."""
        self._commit()
        self.assertEqual(StageCompetitor.objects.filter(stage=self.stage).count(), 7)
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group=self.group).count(), 7)

    def test_reimport_replaces_and_is_idempotent(self):
        self._commit()
        first = list(TournamentTeamMatchStats.objects
                     .filter(match__group=self.group)
                     .values_list("total_points", flat=True).order_by("-total_points"))

        self._commit()
        second = list(TournamentTeamMatchStats.objects
                      .filter(match__group=self.group)
                      .values_list("total_points", flat=True).order_by("-total_points"))

        self.assertEqual(first, second)
        self.assertEqual(TournamentTeamMatchStats.objects.filter(match__group=self.group).count(), 7)

    def test_reimport_never_deletes_a_hand_entered_result(self):
        """Deletion is scoped to rows this importer wrote. A result somebody typed in, or uploaded
        from a match log, must survive a re-import untouched."""
        self._commit()
        manual_match = Match.objects.create(
            group=self.group, match_number=99, match_map="bermuda", upload_method="image_upload")
        tt = TournamentTeam.objects.filter(event=self.event).first()
        manual_row = TournamentTeamMatchStats.objects.create(
            match=manual_match, tournament_team=tt, placement=1, total_points=42)

        self._commit()

        self.assertTrue(TournamentTeamMatchStats.objects.filter(pk=manual_row.pk).exists())
        self.assertTrue(Match.objects.filter(pk=manual_match.pk).exists())

    def test_an_existing_ghost_is_reused_not_duplicated(self):
        """Without this, importing one tournament then another would create two unrelated ghosts for
        the same club, and a later claim would inherit half its history."""
        self._commit()
        ghosts_after_first = GhostTeam.objects.filter(team_name="ELITE HUNTERS").count()

        other_event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=48, event_name="FFWS Africa 2026 Spring", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=1,
        )
        stage2 = Stages.objects.create(
            event=other_event, stage_name="Play-ins", start_date=TODAY, end_date=TODAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=4)
        StageGroups.objects.create(
            stage=stage2, group_name="Group A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=2, match_count=6, match_maps=[])
        imp2 = ResultsImport.objects.create(event=other_event, uploaded_by=self.actor)
        commit_import(imp2, _xlsx({"Group A": FFWS_GROUP_A}), actor=self.actor)

        self.assertEqual(GhostTeam.objects.filter(team_name="ELITE HUNTERS").count(),
                         ghosts_after_first)

    def test_an_alias_overrides_the_matcher(self):
        """The pairing tool: an admin's recorded decision wins over whatever the name matcher would
        have chosen, and survives a re-import."""
        self._commit()
        alias = ExternalResultTeamAlias.objects.get(
            event=self.event, source_name="ELITE HUNTERS")
        real_tt = TournamentTeam.objects.get(event=self.event, team=self.known)
        alias.tournament_team = real_tt
        alias.resolution = "manually_paired"
        alias.save()

        self._commit()

        elite_rows = TournamentTeamMatchStats.objects.filter(
            match__group=self.group, tournament_team=real_tt)
        self.assertTrue(elite_rows.exists())

    def test_per_match_sheet_writes_ordinary_rows(self):
        summary = self._commit({"Group A": [
            ["MATCH", "MAP", "TEAM", "PLACE", "KILLS"],
            [1, "Bermuda", "ELITE HUNTERS", 1, 14],
            [1, "Bermuda", "LAXUS E-SPORTS", 4, 9],
            [2, "Alpine", "ELITE HUNTERS", 3, 6],
        ]})

        self.assertEqual(summary["stats_rows"], 3)
        rows = TournamentTeamMatchStats.objects.filter(match__group=self.group)
        self.assertTrue(all(not r.is_aggregate for r in rows))
        self.assertTrue(all(r.matches_counted == 1 for r in rows))
        self.assertEqual(Match.objects.filter(group=self.group).count(), 2)

    def test_the_event_is_marked_as_imported(self):
        """Provenance. NOT event_type="external", which already means registration happens
        off-platform and would put a Register button on a finished tournament."""
        self._commit()
        self.event.refresh_from_db()
        self.assertIsNotNone(self.event.results_imported_at)
        self.assertEqual(self.event.results_imported_by, self.actor)

    def test_no_player_rows_are_written(self):
        """TournamentPlayerMatchStats.player is a FK to a real User, and an external tournament has
        no AFC accounts for its players (FFWS Phase 1 alone is ~720). Zero rows is the only
        workable answer, not a gap."""
        from afc_tournament_and_scrims.models import TournamentPlayerMatchStats
        self._commit()
        self.assertEqual(
            TournamentPlayerMatchStats.objects.filter(
                team_stats__match__group=self.group).count(), 0)

    def test_matches_played_reports_the_summed_span(self):
        """End to end: the number a profile shows. Elite Hunters played 6, not 1."""
        from django.db.models import Sum
        self._commit()
        tt = TournamentTeam.objects.get(
            event=self.event, ghost_team__team_name="ELITE HUNTERS")
        played = TournamentTeamMatchStats.objects.filter(
            tournament_team=tt).aggregate(m=Sum("matches_counted"))["m"]
        self.assertEqual(played, 6)
