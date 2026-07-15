# afc_tournament_and_scrims/test_final_standings.py
# ──────────────────────────────────────────────────────────────────────────────
# Locks the owner rule (2026-07-14, DYNASTY CUP GRAND FINALS SSA):
#   - A team's FINAL PLACEMENT = its rank in the LAST STAGE it played. A team that reached a
#     deeper stage outranks a team eliminated earlier, EVEN IF the earlier team has more
#     cumulative points across the whole event.
#   - Teams that played the final (deciding) stage are flagged reached_final_stage=True.
#   - Inside a stage the order matches the site: the Champion-Point pin moves the crowned team
#     to #1 even when it isn't the points leader.
#   - Prize auto-sync maps the event pool onto those FINAL standings, and SUMS a team's winnings
#     across the event pool + any per-stage/group pool.
#
# Object graph per fixture: Event -> Stage(s) -> StageGroups -> Match(es) -> TournamentTeam +
# TournamentTeamMatchStats. prize_currency="NGN" so no FxRate table is needed.
# ──────────────────────────────────────────────────────────────────────────────
from datetime import date, time, timedelta
from decimal import Decimal

from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team

from .models import (
    Event, EventPrizePayout, Leaderboard, Match, PlayerWinning,
    StageGroups, Stages, TournamentTeam, TournamentTeamMatchStats, TournamentTeamMember,
)
from .final_standings import event_final_standings, official_stage_standings
from .prize_sync import sync_event_prize_payouts


class FinalStandingsTestBase(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username="fscreator", email="fs@example.com", password="x", role="admin"
        )

    def _event(self, *, dist=None, currency="NGN", stages=2):
        today = date.today()
        ev = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Final Standings Cup", event_mode="virtual",
            start_date=today - timedelta(days=5), end_date=today - timedelta(days=1),
            event_end_time=time(23, 59), registration_open_date=today - timedelta(days=6),
            registration_end_date=today - timedelta(days=5), prizepool="400 NGN",
            prizepool_cash_value=400, prize_currency=currency,
            prize_distribution=dist if dist is not None else {}, event_rules="No cheating",
            event_status="completed", registration_link="https://example.com/reg",
            tournament_tier="tier_1", number_of_stages=stages, creator=self.creator,
            is_draft=False, is_public=True,
        )
        return ev

    def _stage(self, ev, name, order, *, dist=None, champion=False, threshold=None):
        return Stages.objects.create(
            event=ev, stage_name=name, stage_order=order,
            start_date=ev.start_date, end_date=ev.end_date, number_of_groups=1,
            stage_format="br - normal", teams_qualifying_from_stage=8,
            prize_distribution=dist or {},
            champion_point_enabled=champion, champion_point_threshold=threshold,
        )

    def _group(self, stage, name="G", *, dist=None):
        g = StageGroups.objects.create(
            stage=stage, group_name=name, playing_date=stage.start_date,
            playing_time=time(19, 0), teams_qualifying=8, match_count=1, match_maps=["bermuda"],
            prize_distribution=dist or {},
        )
        Leaderboard.objects.create(
            leaderboard_name=f"{stage.stage_name} - {name}", event=stage.event, stage=stage,
            group=g, creator=self.creator, leaderboard_method="manual", placement_points={},
            kill_point=1.0,
        )
        return g

    def _team(self, ev, tag):
        t = Team.objects.create(
            team_name=f"Team {tag}", join_settings="open", team_creator=self.creator,
            team_owner=self.creator, team_captain=self.creator, country="Nigeria",
        )
        tt = TournamentTeam.objects.create(
            event=ev, team=t, registered_by=self.creator, status="active"
        )
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.creator, event=ev, status="active"
        )
        return tt

    def _match(self, group, number=1, mp="bermuda"):
        lb = Leaderboard.objects.filter(group=group).first()
        return Match.objects.create(leaderboard=lb, group=group, match_map=mp, match_number=number)

    def _stat(self, match, tt, *, placement, kills=0, points=0):
        return TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=placement, kills=kills, total_points=points,
        )


class TieredPlacementTests(FinalStandingsTestBase):
    def test_last_stage_played_outranks_cumulative_points(self):
        """A team eliminated in stage 1 with MANY points ranks BELOW a finalist with fewer points."""
        ev = self._event(stages=2)
        s1 = self._stage(ev, "Semis", 1)
        s2 = self._stage(ev, "Finals", 2)
        g1 = self._group(s1, "Semi A")
        g2 = self._group(s2, "Final A")

        big = self._team(ev, "BIG")    # dominates the semis, does NOT advance
        fin = self._team(ev, "FIN")    # scrapes through the semis, plays the finals

        m1 = self._match(g1, 1)
        self._stat(m1, big, placement=1, points=100)   # huge semi score
        self._stat(m1, fin, placement=8, points=5)     # weak semi score

        m2 = self._match(g2, 1)
        self._stat(m2, fin, placement=1, points=20)    # only FIN plays the finals

        ordered, rank_by_tt, reached, final_stage = event_final_standings(ev)
        self.assertEqual(final_stage.stage_id, s2.stage_id)
        # FIN reached the finals -> ranks #1 above BIG (eliminated in the semis) despite far fewer points.
        self.assertEqual(rank_by_tt[fin.pk], 1)
        self.assertEqual(rank_by_tt[big.pk], 2)
        self.assertIn(fin.pk, reached)
        self.assertNotIn(big.pk, reached)

    def test_single_stage_event_is_that_stage(self):
        ev = self._event(stages=1)
        s1 = self._stage(ev, "Only", 1)
        g1 = self._group(s1)
        a = self._team(ev, "A")
        b = self._team(ev, "B")
        m = self._match(g1, 1)
        self._stat(m, a, placement=1, points=30)
        self._stat(m, b, placement=2, points=20)
        _o, rank, reached, fs = event_final_standings(ev)
        self.assertEqual(rank[a.pk], 1)
        self.assertEqual(rank[b.pk], 2)
        # Single-stage event: no "finalist" distinction (would otherwise flag everyone).
        self.assertEqual(reached, set())


class ChampionPinTests(FinalStandingsTestBase):
    def test_champion_pinned_over_points_leader(self):
        """Champion-Point finals: the crowned team leads even though another team has more points."""
        ev = self._event(stages=1)
        s = self._stage(ev, "Finals", 1, champion=True, threshold=10)
        g = self._group(s)
        a = self._team(ev, "A")  # points leader
        b = self._team(ev, "B")
        c = self._team(ev, "C")  # champion (booyahs match 2 while already >= threshold)

        m1 = self._match(g, 1)
        self._stat(m1, a, placement=1, points=30)
        self._stat(m1, b, placement=2, points=20)
        self._stat(m1, c, placement=3, points=15)   # C pre-match2 total = 15 >= 10
        m2 = self._match(g, 2)
        self._stat(m2, c, placement=1, points=5)     # C booyah -> champion
        self._stat(m2, a, placement=2, points=3)
        self._stat(m2, b, placement=3, points=2)

        rows = official_stage_standings(s)
        # On raw points it would be A(33) > B(22) > C(20); the champion pin moves C to #1.
        self.assertEqual(rows[0]["tournament_team_id"], c.pk)
        _o, rank, _r, _fs = event_final_standings(ev)
        self.assertEqual(rank[c.pk], 1)


class PrizeFromFinalStandingsTests(FinalStandingsTestBase):
    def test_event_pool_maps_to_final_stage_not_cumulative(self):
        """Prize follows the FINAL stage, so a team huge in the semis but absent from the finals gets 0."""
        ev = self._event(stages=2, dist={"1": "200", "2": "100"})
        s1 = self._stage(ev, "Semis", 1)
        s2 = self._stage(ev, "Finals", 2)
        g1 = self._group(s1, "Semi A")
        g2 = self._group(s2, "Final A")
        big = self._team(ev, "BIG")   # semis only
        w = self._team(ev, "WIN")     # finals winner
        r = self._team(ev, "RUN")     # finals runner-up
        m1 = self._match(g1, 1)
        self._stat(m1, big, placement=1, points=500)
        self._stat(m1, w, placement=2, points=40)
        self._stat(m1, r, placement=3, points=30)
        m2 = self._match(g2, 1)
        self._stat(m2, w, placement=1, points=25)
        self._stat(m2, r, placement=2, points=15)

        created = sync_event_prize_payouts(ev)
        self.assertEqual(created, 2)
        by_team = {p.tournament_team_id: p.amount for p in EventPrizePayout.objects.filter(event=ev)}
        self.assertEqual(by_team.get(w.pk), Decimal("200.00"))   # finals winner -> 1st prize
        self.assertEqual(by_team.get(r.pk), Decimal("100.00"))   # finals runner-up -> 2nd prize
        self.assertNotIn(big.pk, by_team)                        # semis-only -> NOTHING
        # per-player share written for the winner
        self.assertTrue(PlayerWinning.objects.filter(event=ev, player=self.creator).exists())

    def test_stage_and_event_pools_sum_per_team(self):
        """A team placing in BOTH a stage pool and the event pool has its winnings summed."""
        ev = self._event(stages=1, dist={"1": "100"})
        s = self._stage(ev, "Only", 1, dist={"1": "50"})   # stage ALSO pays its 1st
        g = self._group(s)
        a = self._team(ev, "A")
        b = self._team(ev, "B")
        m = self._match(g, 1)
        self._stat(m, a, placement=1, points=30)
        self._stat(m, b, placement=2, points=20)
        sync_event_prize_payouts(ev)
        a_pay = EventPrizePayout.objects.get(event=ev, tournament_team=a)
        # event 1st (100) + stage 1st (50) both land on A -> ONE summed row of 150.
        self.assertEqual(a_pay.amount, Decimal("150.00"))
        self.assertEqual(EventPrizePayout.objects.filter(event=ev, tournament_team=a).count(), 1)

    def test_event_pool_withheld_until_final_stage_played(self):
        """Owner 2026-07-15 (DECA CUP SEASON 5): the organizer only uploaded the qualifiers; the
        event's Grand Finals stage is still empty. The whole-event pool must NOT attribute yet, since a
        qualifiers winner is not the event champion. A pool tied to the PLAYED qualifiers stage still
        pays, because that money belongs to the stage the team actually played."""
        ev = self._event(stages=2, dist={"1": "300"})       # event pool: 1st = 300
        quals = self._stage(ev, "Qualifiers", 1, dist={"1": "40"})  # stage pool on the PLAYED stage
        finals = self._stage(ev, "Grand Finals", 2)         # deciding stage: created but NEVER played
        gq = self._group(quals, "Group B")
        gf = self._group(finals, "Finals A")                # finals group exists but has NO matches yet
        winner = self._team(ev, "WIN")   # 1st in qualifiers, but the finals never happened
        other = self._team(ev, "OTH")
        m = self._match(gq, 1)
        self._stat(m, winner, placement=1, points=113)
        self._stat(m, other, placement=2, points=90)

        # The finals stage is the decider and has no results -> event pool is withheld.
        _o, _rank, reached, final_stage = event_final_standings(ev)
        self.assertEqual(final_stage.stage_name, "Grand Finals")
        self.assertEqual(reached, set())                    # nobody reached the (empty) finals

        sync_event_prize_payouts(ev)
        by_team = {p.tournament_team_id: p.amount for p in EventPrizePayout.objects.filter(event=ev)}
        # Event 1st (300) is WITHHELD; only the qualifiers stage pool (40) lands on its winner.
        self.assertEqual(by_team.get(winner.pk), Decimal("40.00"))
        self.assertNotIn(other.pk, by_team)

        # When the organizer later uploads the finals, the event pool attributes on the next sync.
        mf = self._match(gf, 1)
        self._stat(mf, winner, placement=1, points=20)
        self._stat(mf, other, placement=2, points=15)
        sync_event_prize_payouts(ev)
        after = {p.tournament_team_id: p.amount for p in EventPrizePayout.objects.filter(event=ev)}
        # Now decided: event 1st (300) + qualifiers stage 1st (40) sum on the champion.
        self.assertEqual(after.get(winner.pk), Decimal("340.00"))
