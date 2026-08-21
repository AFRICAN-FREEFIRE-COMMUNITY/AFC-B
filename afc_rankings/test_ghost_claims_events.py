"""
afc_rankings.test_ghost_claims_events - claiming a ghost also inherits its TOURNAMENT history.

Sibling flat test module to afc_rankings/test_ghost_claims.py, which covers the standalone
leaderboard half of the same claim process. afc_rankings uses flat tests modules, so this is
auto-discovered without a package.

WHY THIS EXISTS
    Before the external-results-import work, a ghost team could only ever score through STANDALONE
    LEADERBOARDS, so claims.reattribute_ghost_team only re-pointed afc_leaderboard
    .LeaderboardParticipant rows. A ghost can now ALSO hold tournament registrations, because
    afc_tournament_and_scrims.TournamentTeam gained a ghost_team FK (team XOR ghost_team, enforced by
    the tt_team_xor_ghost CheckConstraint).

    A claim that ignored those registrations would hand the real team HALF its history and silently
    leave the rest attached to a ghost nobody can find afterwards. These tests lock the other half.

HOW IT CONNECTS
    - Exercises afc_rankings.claims.reattribute_ghost_team (the re-attribution service called by
      admin_ghost.ghost_approve_claim) and claims.conflict_for_team_claim (the fail-fast pre-check
      called by admin_ghost.ghost_team_request_claim before a claim ever goes pending).
    - The (kind, name) return shape asserted below is consumed by afc_rankings/admin_ghost.py:899,
      which interpolates the kind into the user-facing 400 message so it names the right noun.

Run: python manage.py test afc_rankings.test_ghost_claims_events
"""
import datetime

from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_rankings.models import GhostTeam
from afc_rankings.claims import (
    reattribute_ghost_team, conflict_for_team_claim, ClaimConflict,
)
from afc_tournament_and_scrims.models import Event, TournamentTeam


def _make_event(name, **over):
    """An Event with every non-defaulted field filled. Shape copied from
    afc_tournament_and_scrims/tests_ghost_competitor.py so both read the same way."""
    kwargs = dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=datetime.date(2026, 7, 1), end_date=datetime.date(2026, 9, 6),
        registration_open_date=datetime.date(2026, 6, 1),
        registration_end_date=datetime.date(2026, 6, 30),
        prizepool="0", event_rules="rules", event_status="upcoming",
        registration_link="https://example.com/reg", number_of_stages=4,
    )
    kwargs.update(over)
    return Event.objects.create(**kwargs)


class GhostClaimTournamentReattributionTests(TestCase):
    """A claim moves the ghost's TournamentTeam rows onto the real team, or refuses cleanly."""

    def setUp(self):
        self.actor = User.objects.create(username="claimadmin", email="claim@example.com")
        self.event = _make_event("FFWS Africa 2026 Fall")
        self.real_team = Team.objects.create(
            team_name="Laxus Esports", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        # The ghost spells the same club differently, which is the real FFWS case: the published
        # standings say "LAXUS E-SPORTS" and the AFC team is "Laxus Esports".
        self.ghost = GhostTeam.objects.create(
            team_name="LAXUS E-SPORTS", country="Angola", created_by=self.actor,
        )

    def test_claim_repoints_the_tournament_registration(self):
        """The core of this task: the ghost's registration becomes the real team's registration."""
        tt = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)

        result = reattribute_ghost_team(self.ghost, self.real_team, self.actor)

        tt.refresh_from_db()
        self.assertIsNone(tt.ghost_team)
        self.assertEqual(tt.team, self.real_team)
        self.assertEqual(result["reattributed_tournament_teams"], 1)

    def test_claim_with_no_tournament_history_still_works(self):
        """Regression guard for the standalone-only path: a ghost that never entered a tournament
        must still claim cleanly, reporting zero rather than raising or skipping the recompute."""
        result = reattribute_ghost_team(self.ghost, self.real_team, self.actor)

        self.assertEqual(result["reattributed_tournament_teams"], 0)
        self.assertEqual(result["real_team_id"], self.real_team.pk)

    def test_claim_moves_every_registration_across_several_events(self):
        """FFWS spans four stages across multiple linked events, so a ghost routinely holds more
        than one registration. All of them move, not just the first."""
        second = _make_event("FFWS Africa 2026 Spring")
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        TournamentTeam.objects.create(event=second, ghost_team=self.ghost)

        result = reattribute_ghost_team(self.ghost, self.real_team, self.actor)

        self.assertEqual(result["reattributed_tournament_teams"], 2)
        self.assertEqual(TournamentTeam.objects.filter(ghost_team=self.ghost).count(), 0)
        self.assertEqual(TournamentTeam.objects.filter(team=self.real_team).count(), 2)

    def test_conflict_when_the_real_team_is_already_in_that_event(self):
        """Re-pointing would violate uniq_event_team_registration and abort the claim halfway, so
        the guard refuses BEFORE mutating and names the EVENT so an admin can act on it."""
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        TournamentTeam.objects.create(event=self.event, team=self.real_team)

        with self.assertRaises(ClaimConflict) as ctx:
            reattribute_ghost_team(self.ghost, self.real_team, self.actor)

        self.assertIn("FFWS Africa 2026 Fall", str(ctx.exception))

    def test_conflict_leaves_everything_untouched(self):
        """The guard runs before any write, so a refused claim must not have moved anything."""
        tt = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        existing = TournamentTeam.objects.create(event=self.event, team=self.real_team)

        with self.assertRaises(ClaimConflict):
            reattribute_ghost_team(self.ghost, self.real_team, self.actor)

        tt.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(tt.ghost_team, self.ghost)
        self.assertIsNone(tt.team)
        self.assertEqual(existing.team, self.real_team)


class ConflictPrecheckKindTests(TestCase):
    """conflict_for_team_claim returns (kind, name), never a bare name.

    The kind exists because afc_rankings/admin_ghost.py:899 interpolates it into the message a user
    sees. Returning an event name through a channel that always said "leaderboard" would send
    somebody looking for a leaderboard that does not exist.
    """

    def setUp(self):
        self.actor = User.objects.create(username="precheck", email="pre@example.com")
        self.event = _make_event("FFWS Africa 2026 Fall")
        self.real_team = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.ghost = GhostTeam.objects.create(
            team_name="BERSERK GEN", country="Nigeria", created_by=self.actor,
        )

    def test_no_conflict_returns_none(self):
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)

        self.assertIsNone(conflict_for_team_claim(self.ghost, self.real_team))

    def test_event_conflict_reports_the_event_kind(self):
        """The new half: an event conflict is reported as an EVENT, with the event's name."""
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        TournamentTeam.objects.create(event=self.event, team=self.real_team)

        result = conflict_for_team_claim(self.ghost, self.real_team)

        self.assertIsNotNone(result)
        kind, name = result
        self.assertEqual(kind, "event")
        self.assertEqual(name, "FFWS Africa 2026 Fall")

    def test_leaderboard_conflict_still_reports_the_leaderboard_kind(self):
        """The regression guard for the EXISTING behaviour. The return shape changed from a bare
        name to a tuple, so this proves the standalone-leaderboard half still detects its own
        conflict and labels it correctly rather than being shadowed by the new event branch."""
        from afc_leaderboard.models import StandaloneLeaderboard, LeaderboardParticipant

        # `effective_date` is a read-only PROPERTY on this model, not a field, so it cannot be
        # passed to create(). `played_on` is the underlying date column it derives from, and the
        # creator FK is spelled `creator`, not `created_by` (that spelling belongs to GhostTeam).
        lb = StandaloneLeaderboard.objects.create(
            name="Africa Open Ladder",
            format="team",
            played_on=datetime.date(2026, 7, 1),
            creator=self.actor,
        )
        LeaderboardParticipant.objects.create(leaderboard=lb, ghost_team=self.ghost)
        LeaderboardParticipant.objects.create(leaderboard=lb, team=self.real_team)

        result = conflict_for_team_claim(self.ghost, self.real_team)

        self.assertIsNotNone(result)
        kind, name = result
        self.assertEqual(kind, "leaderboard")
        self.assertEqual(name, "Africa Open Ladder")
