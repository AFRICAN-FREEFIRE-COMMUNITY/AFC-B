"""Two guards that the Clash Squad groups work (owner backlog item 21) made necessary.

WHY THIS MODULE EXISTS
  1. `Stages.stage_format` gained a THIRD generation of values on 2026-08-13: plain `"br"` and
     plain `"cs"`, because the stage picker now asks which GAME a stage runs and leaves the mode
     to the group (see stage_formats.py). Every check written as a literal (`== "br - normal"`,
     `.startswith("cs - ")`) silently stopped matching stages created from that day's wizard.
     Registration auto-seed was one of them: a brand-new Battle Royale stage would have stopped
     placing its teams into groups, and a brand-new Clash Squad stage would have offered an empty
     Generate-bracket dialog. Both are silent - nothing raises, the teams simply never appear.
  2. The bracket seed list accepted any TournamentTeam belonging to the event (owner backlog
     item 11), so a withdrawn, disqualified or still-waitlisted team could be drawn into a real
     match. Everywhere else "confirmed participant" means status="active" and not waitlisted.

WHAT IT COVERS
  - autoseed_stage() for all THREE generations of both games, so a future fourth value has to
    break a test rather than a live event.
  - generate_h2h_bracket refusing unconfirmed teams, and the seed pool not offering them.

Run: .venv\\Scripts\\python.exe manage.py test
     afc_tournament_and_scrims.tests_stage_format_generations
"""
import datetime

from afc_tournament_and_scrims.models import (
    StageCompetitor,
    StageGroupCompetitor,
    StageGroups,
    Stages,
)
from afc_tournament_and_scrims.seeding_management import autoseed_stage
from afc_tournament_and_scrims.tests_head_to_head import H2HBase


class AutoSeedFormatGenerationTests(H2HBase):
    """Registration auto-seed must recognise a stage whichever generation named its format."""

    def _stage(self, stage_format, groups=0):
        """A second stage on the shared event, optionally with `groups` empty BR lobbies."""
        D = datetime.date(2026, 6, 1)
        stage = Stages.objects.create(
            event=self.event, stage_name=f"Stage {stage_format}", start_date=D, end_date=D,
            number_of_groups=groups, stage_format=stage_format, teams_qualifying_from_stage=2)
        for i in range(groups):
            StageGroups.objects.create(
                stage=stage, group_name=f"Group {chr(65 + i)}", playing_date=D,
                playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        return stage

    # ── Battle Royale: pool AND groups ──────────────────────────────────────────────────────
    def test_plain_br_stage_distributes_into_groups(self):
        """`"br"` is generation 3 of `"br - normal"`. The teams must land in the groups."""
        stage = self._stage("br", groups=2)
        result = autoseed_stage(stage)

        self.assertNotIn("skipped", result, result)
        self.assertEqual(StageCompetitor.objects.filter(stage=stage).count(), 6)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=stage).count(), 6,
            "a plain 'br' stage did not distribute its teams into its groups")

    def test_legacy_br_normal_still_distributes(self):
        stage = self._stage("br - normal", groups=2)
        autoseed_stage(stage)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=stage).count(), 6)

    # ── Clash Squad: pool ONLY (its "groups" are brackets, filled by generate_bracket) ──────
    def test_plain_cs_stage_is_pool_only(self):
        stage = self._stage("cs")
        result = autoseed_stage(stage)

        self.assertTrue(result.get("pool_only"), result)
        self.assertEqual(StageCompetitor.objects.filter(stage=stage).count(), 6,
                         "a plain 'cs' stage got no competitor pool, so the Generate-bracket "
                         "dialog would open empty")
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=stage).count(), 0)

    def test_legacy_cs_knockout_is_pool_only(self):
        stage = self._stage("cs - knockout")
        result = autoseed_stage(stage)
        self.assertTrue(result.get("pool_only"), result)
        self.assertEqual(StageCompetitor.objects.filter(stage=stage).count(), 6)

    # ── Round robin still owns its own placement, as before ────────────────────────────────
    def test_br_round_robin_is_left_alone(self):
        stage = self._stage("br - round robin", groups=2)
        result = autoseed_stage(stage)
        self.assertEqual(result.get("skipped"), "format:br - round robin")
        self.assertEqual(StageCompetitor.objects.filter(stage=stage).count(), 0)


class ConfirmedParticipantsOnlyTests(H2HBase):
    """Only status="active", non-waitlisted teams may be drawn into a bracket (item 11)."""

    def test_withdrawn_team_is_refused_with_its_name(self):
        withdrawn = self.tts[3]
        withdrawn.status = "withdrawn"
        withdrawn.save(update_fields=["status"])

        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("T4", resp.json()["message"])
        self.assertIn("withdrawn", resp.json()["message"])

    def test_waitlisted_team_is_refused(self):
        waiting = self.tts[1]
        waiting.is_waitlisted = True
        waiting.save(update_fields=["is_waitlisted"])

        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("waitlisted", resp.json()["message"])

    def test_confirmed_teams_still_generate(self):
        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_seed_pool_does_not_offer_an_unconfirmed_team(self):
        """The dialog reads this list, so an unpickable team must not be in it."""
        for tt in self.tts[:4]:
            StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
        disqualified = self.tts[2]
        disqualified.status = "disqualified"
        disqualified.save(update_fields=["status"])

        pool = self._get_bracket().json()["stage_competitors"]
        names = [row["team_name"] for row in pool]
        self.assertEqual(names, ["T1", "T2", "T4"])
