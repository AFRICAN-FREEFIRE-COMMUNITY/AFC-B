"""
afc_results_import.tests_players - importing INDIVIDUAL player kills.

THE PROBLEM THIS SHAPE SOLVES. Some organizers publish per-player numbers, and until now none of
that could be imported: TournamentPlayerMatchStats.player is a foreign key to a real User and an
external tournament has no AFC accounts (FFWS Play-ins Phase 1 alone is roughly 720 people).
Inventing accounts would be far worse than having no data, so the row now points at either a real
User or a GhostPlayer, mirroring TournamentTeam.team / .ghost_team exactly.

WHAT THESE TESTS PIN DOWN, beyond "it writes rows":

  * the TEAM's line is REBUILT from its players' rows, so the team total and the per-player
    breakdown cannot disagree the way a hand-typed file can;
  * a real team's OWN players are matched to their real accounts, scoped to that team's roster for
    this event, because a global username search would attribute a stranger's kills to a real
    person's profile and ranking;
  * a player on a REAL team who is not on its roster is REPORTED, not invented as a ghost under
    that team, which would put a name on a team's public page its owner never added;
  * claiming a ghost player carries the imported kills onto the real account.

Run: python manage.py test afc_results_import.tests_players
"""
import datetime
import io
import secrets

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_rankings.models import GhostPlayer, GhostTeam
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, TournamentTeam, TournamentTeamMember,
    TournamentPlayerMatchStats, TournamentTeamMatchStats,
)

TODAY = datetime.date.today()

# Two players of one team in one map. PLACE is the TEAM's finish, repeated on both rows.
PLAYER_SHEET = [
    ["TEAM", "PLAYER", "MATCH", "MAP", "PLACE", "ELIMS"],
    ["ELITE HUNTERS", "AliFF", 1, "bermuda", 1, 6],
    ["ELITE HUNTERS", "Zed", 1, "bermuda", 1, 3],
]


def _xlsx(sheets):
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


def _upload(data, name="players.xlsx"):
    return SimpleUploadedFile(
        name, data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class PerPlayerImportTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create(username="pp_admin", email="pp@example.com", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token=secrets.token_hex(32)).token
        self.event = Event.objects.create(
            slug="per-player-event", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="Per Player",
            event_mode="virtual", start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=1)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Play-ins", start_date=TODAY, end_date=TODAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=2, match_count=1, match_maps=[])

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def _commit(self, sheet=None):
        return self.client.post(
            "/results-import/commit/",
            {"slug": self.event.slug,
             "file": _upload(_xlsx({"Group A": sheet or PLAYER_SHEET}))},
            **self._auth())

    # ── the shape is recognised ────────────────────────────────────────────────────────────
    def test_a_player_column_makes_it_a_per_player_sheet(self):
        from afc_results_import.parsing import parse_sheet

        parsed = parse_sheet("Group A", PLAYER_SHEET)

        self.assertEqual(parsed["kind"], "per_match_players")
        self.assertEqual(len(parsed["rows"]), 2)

    def test_a_row_with_no_player_name_is_reported_not_guessed(self):
        from afc_results_import.parsing import parse_sheet

        parsed = parse_sheet("Group A", PLAYER_SHEET + [["ELITE HUNTERS", "", 1, "b", 1, 2]])

        self.assertEqual(len(parsed["rows"]), 2)
        self.assertTrue(any("player name" in p for p in parsed["problems"]))

    # ── what a commit writes ───────────────────────────────────────────────────────────────
    def test_each_player_gets_their_own_row(self):
        r = self._commit()

        self.assertEqual(r.status_code, 200, r.content[:300])
        rows = TournamentPlayerMatchStats.objects.filter(
            team_stats__match__group=self.group)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            sorted(x.ghost_player.ign for x in rows), ["AliFF", "Zed"])

    def test_the_team_line_is_REBUILT_from_its_players(self):
        """6 + 3 = 9. Taken from the players' rows, not from a team total the file never carried,
        so the breakdown and the total cannot disagree."""
        self._commit()

        team_stat = TournamentTeamMatchStats.objects.get(match__group=self.group)
        self.assertEqual(team_stat.kills, 9)
        self.assertEqual(team_stat.placement, 1)
        self.assertFalse(team_stat.is_aggregate)

    def test_the_players_hang_off_the_ghost_TEAM_so_they_can_be_claimed(self):
        self._commit()

        ghost_team = GhostTeam.objects.get(team_name="ELITE HUNTERS")
        self.assertEqual(
            GhostPlayer.objects.filter(ghost_team=ghost_team).count(), 2)

    def test_re_importing_reuses_the_same_people(self):
        """A corrected file must not create a second copy of every player."""
        self._commit()
        self._commit()

        self.assertEqual(GhostPlayer.objects.count(), 2)

    def test_a_re_import_replaces_rather_than_doubling_the_kills(self):
        self._commit()
        self._commit()

        team_stat = TournamentTeamMatchStats.objects.get(match__group=self.group)
        self.assertEqual(team_stat.kills, 9)

    # ── real teams keep their real players ─────────────────────────────────────────────────
    def test_a_real_teams_own_roster_player_is_matched_to_their_account(self):
        owner = User.objects.create(username="AliFF", email="ali@example.com")
        team = Team.objects.create(team_name="ELITE HUNTERS", join_settings="open",
                                   team_creator=owner, team_owner=owner)
        tt = TournamentTeam.objects.create(event=self.event, team=team)
        TournamentTeamMember.objects.create(tournament_team=tt, user=owner)

        self._commit([PLAYER_SHEET[0], ["ELITE HUNTERS", "AliFF", 1, "bermuda", 1, 6]])

        row = TournamentPlayerMatchStats.objects.get()
        self.assertEqual(row.player, owner)
        self.assertIsNone(row.ghost_player_id)

    def test_a_stranger_on_a_REAL_team_is_reported_not_invented(self):
        """Creating a ghost player under a real team would put a name on that team's public page
        which its owner never added, and matching by username globally would credit a real
        stranger's account with kills from a tournament they never played."""
        owner = User.objects.create(username="realowner", email="ro@example.com")
        team = Team.objects.create(team_name="ELITE HUNTERS", join_settings="open",
                                   team_creator=owner, team_owner=owner)
        TournamentTeam.objects.create(event=self.event, team=team)
        User.objects.create(username="AliFF", email="stranger@example.com")

        r = self._commit([PLAYER_SHEET[0], ["ELITE HUNTERS", "AliFF", 1, "bermuda", 1, 6]])

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(TournamentPlayerMatchStats.objects.count(), 0)
        self.assertEqual(GhostPlayer.objects.count(), 0)
        self.assertTrue(r.json()["summary"].get("unmatched_players"))

    # ── claiming carries the history ───────────────────────────────────────────────────────
    def test_claiming_a_ghost_player_moves_their_imported_kills(self):
        from afc_rankings.claims import reattribute_ghost_player

        self._commit()
        ghost = GhostPlayer.objects.get(ign="AliFF")
        real = User.objects.create(username="ali_real", email="ar@example.com")

        result = reattribute_ghost_player(ghost, real, self.admin)

        self.assertEqual(result["reattributed_match_rows"], 1)
        row = TournamentPlayerMatchStats.objects.get(player=real)
        self.assertEqual(row.kills, 6)
        self.assertIsNone(row.ghost_player_id)

    def test_a_claim_that_would_double_a_players_map_is_skipped_not_merged(self):
        """If the real user already has a row on the same team line, moving the ghost's row would
        make one person appear twice in one map and double their kills for it."""
        from afc_rankings.claims import reattribute_ghost_player

        self._commit()
        ghost = GhostPlayer.objects.get(ign="AliFF")
        real = User.objects.create(username="ali_real2", email="ar2@example.com")
        team_stat = TournamentTeamMatchStats.objects.get(match__group=self.group)
        TournamentPlayerMatchStats.objects.create(
            team_stats=team_stat, player=real, kills=2)

        result = reattribute_ghost_player(ghost, real, self.admin)

        self.assertEqual(result["reattributed_match_rows"], 0)
        self.assertEqual(result["skipped_match_rows"], 1)
        self.assertEqual(
            TournamentPlayerMatchStats.objects.filter(team_stats=team_stat, player=real).count(), 1)


class PlayerRowConstraintTests(TestCase):
    """The row points at a real user OR a ghost, never both."""

    def test_a_row_cannot_name_a_user_and_a_ghost_at_once(self):
        from django.db import IntegrityError, transaction

        admin = User.objects.create(username="c_admin", email="c@example.com", role="admin")
        event = Event.objects.create(
            slug="constraint-event", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=8, event_name="C",
            event_mode="virtual", start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=1)
        stage = Stages.objects.create(
            event=event, stage_name="S", start_date=TODAY, end_date=TODAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1)
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=1, match_count=1, match_maps=[])
        from afc_tournament_and_scrims.models import Match
        match = Match.objects.create(group=group, match_number=1, match_map="m")
        ghost_team = GhostTeam.objects.create(
            team_name="G", country="NG", created_by=admin)
        tt = TournamentTeam.objects.create(event=event, ghost_team=ghost_team)
        team_stat = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=0)
        ghost_player = GhostPlayer.objects.create(ghost_team=ghost_team, ign="Both", slot=1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TournamentPlayerMatchStats.objects.create(
                    team_stats=team_stat, player=admin, ghost_player=ghost_player, kills=1)


class ImportedPlayersAreVisibleTests(PerPlayerImportTests):
    """Writing rows nobody can read is not a feature. The per-map payload the results screens
    render must name an imported player, not send a nameless row."""

    def test_the_match_payload_names_an_imported_player(self):
        self._commit()

        r = self.client.post(
            "/events/get-all-leaderboard-details-for-event/",
            {"event_id": self.event.event_id},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content[:200])

        names, ghost_ids = [], []
        def walk(node):
            if isinstance(node, dict):
                if "players" in node and isinstance(node["players"], list):
                    for p in node["players"]:
                        names.append(p.get("username"))
                        ghost_ids.append(p.get("ghost_player_id"))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(r.json())

        self.assertEqual(sorted(n for n in names if n), ["AliFF", "Zed"])
        self.assertNotIn(None, names)
        # And the payload says these are unclaimed names rather than real accounts.
        self.assertTrue(all(g is not None for g in ghost_ids))
