"""
test_player_role_ladders.py
───────────────────────────
Covers the per-role player ladders the owner asked for ("sniper rankings, rusher rankings, etc")
AND the role history that now backs them.

THE BUG THESE TESTS EXIST TO PREVENT (owner 2026-08-04)
    The role tables used to read afc_team.TeamMembers.in_game_role, the role a player holds RIGHT
    NOW. A player who was a sniper in July and is a rusher today was listed in July's RUSHER table,
    so a table described the present while claiming to describe the past. The role is now recorded
    when the points are earned and stored on the period's score row, and
    ``RoleHistoryTests.test_a_player_who_switched_roles_keeps_the_old_role_for_the_old_month`` is the
    test that fails if anything ever reads the live roster again.

WHAT THESE TESTS PIN DOWN
    1. Role history: the stored role is the one held WHEN the points were earned, it survives a
       later role change, and it survives the result being re-recorded (the write paths delete and
       re-insert a match's rows, which is exactly when a live read would corrupt history).
    2. A role table is a FILTER, not a second scoring system: the scores are byte-for-byte the ones
       the main ladder shows, only the population differs.
    3. Ranks inside a role table are ranks WITHIN the role (1, 2, 3...), not the global ones, and the
       global number survives as ``overall_rank``.
    4. The publish gates are the SAME as the main ladder's, including the coverage block, which must
       not leak how much role data a gated period holds.
    5. Players with no recorded role, and ghost players, are absent from every role table but still
       present in the unfiltered one.
    6. A mixed-role period is filed under the role played most, the split is reported rather than
       flattened, and the player is still listed exactly once.
    7. ``role_coverage`` tells the client when a period has no stored role data at all, so the UI can
       say so instead of showing four empty tabs as fact.

Exercised through APIRequestFactory, the function-view idiom the rest of afc_rankings uses (see
test_public_gating.py). The role-history tests go through the real aggregation + recalc path on the
same object graph aggregation walks (Event -> Stages -> StageGroups -> Match -> TournamentTeam ->
TournamentTeamMatchStats -> TournamentPlayerMatchStats), because the point is precisely that the
stored value is derived from real match rows.
"""
import datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims import roster_roles
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam, TournamentTeamMatchStats,
    TournamentPlayerMatchStats, TournamentTeamMember,
)
from afc_rankings import aggregation, player_roles, recalc, views
from afc_rankings.models import (
    GhostPlayer, GhostTeam, PLAYER_ROLE_CHOICES, PlayerMonthlyScore, PlayerQuarterlyScore, Season,
)

User = get_user_model()

MONTH = datetime.date(2098, 5, 1)


def _season(**overrides):
    """A season whose window contains MONTH. Published unless a test says otherwise."""
    defaults = dict(
        name="Role Q", quarter=2, year=2098,
        start_date=datetime.date(2098, 4, 1), end_date=datetime.date(2098, 7, 1),
        transfer_window_open=datetime.date(2098, 4, 1),
        transfer_window_close=datetime.date(2098, 4, 14),
        is_active=True, rankings_published=True, tiers_published=True,
    )
    defaults.update(overrides)
    return Season.objects.create(**defaults)


class RoleCatalogTests(TestCase):
    """The role list must not drift from the model that owns it."""

    def test_rankings_role_choices_match_the_team_model(self):
        """afc_rankings copies IN_GAME_ROLE_CHOICES rather than importing it (see the comment on
        PLAYER_ROLE_CHOICES: afc_team -> afc_auth -> afc_tournament_and_scrims is already an import
        cycle). This test is the thing that keeps the copy honest, so adding a role to the team model
        fails here instead of silently producing a tab nobody can ever land in."""
        self.assertEqual(list(PLAYER_ROLE_CHOICES), list(TeamMembers.IN_GAME_ROLE_CHOICES))
        self.assertEqual(list(player_roles.ROLE_KEYS),
                         [key for key, _ in TeamMembers.IN_GAME_ROLE_CHOICES])


class RoleHistoryTests(TestCase):
    """The headline behaviour: the role is the one held WHEN the points were earned, and it is
    stored, so nothing later can rewrite it."""

    PLAY_DAY = datetime.date(2098, 5, 10)

    def setUp(self):
        self.season = _season()
        self.user = User.objects.create(username="switcher", email="sw@example.com")
        self.team = Team.objects.create(
            team_name="History FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        # The player is a SNIPER on the club roster today, and plays one match as a sniper.
        self.membership = TeamMembers.objects.create(
            team=self.team, member=self.user, in_game_role="sniper",
        )
        self.event = self._event("May Cup")
        self._play(self.event, self.user, role="sniper", kills=9)

    # ── fixture helpers (mirrors test_scrim_counting.py's object graph) ──
    def _event(self, name, competition_type="tournament", day=None):
        day = day or self.PLAY_DAY
        return Event.objects.create(
            event_name=name, competition_type=competition_type, participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=day, end_date=day,
            registration_open_date=day - datetime.timedelta(days=5),
            registration_end_date=day - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.user, is_draft=False,
        )

    def _tournament_team(self, event, roster_role):
        """A registered team plus the FROZEN event roster row carrying ``roster_role``."""
        tt = TournamentTeam.objects.create(
            event=event, team=self.team, registered_by=self.user, status="active",
        )
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.user, event=event, in_game_role=roster_role,
        )
        return tt

    def _play(self, event, player, *, role, kills, day=None, placement=1):
        """One played match, with the per-match role stamped exactly as a result path would."""
        day = day or self.PLAY_DAY
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=day, end_date=day,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=day,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=day,
        )
        tt = self._tournament_team(event, role)
        ts = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=placement, kills=kills,
        )
        return TournamentPlayerMatchStats.objects.create(
            team_stats=ts, player=player, kills=kills, played=True, role_at_match=role,
        )

    # ── 1. the bug the owner reported ──
    def test_a_player_who_switched_roles_keeps_the_old_role_for_the_old_month(self):
        """May was played as a sniper. Becoming a rusher afterwards must not rewrite May."""
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        self.assertEqual(PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).role,
                         "sniper")

        # The transfer window opens and the player becomes a rusher TODAY.
        self.membership.in_game_role = "rusher"
        self.membership.save(update_fields=["in_game_role"])

        # Recalculating May must still say sniper: the month is read from what was stamped on the
        # month's matches, not from the club roster.
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        self.assertEqual(PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).role,
                         "sniper")

    def test_the_old_month_lists_them_in_the_old_roles_table(self):
        """End to end: the public ladder, not just the stored column."""
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        self.membership.in_game_role = "rusher"
        self.membership.save(update_fields=["in_game_role"])

        factory = APIRequestFactory()

        def table(role):
            return [r["username"] for r in player_roles.players_by_role(
                factory.get(f"/rankings/players/by-role/?month=2098-05&role={role}")
            ).data["results"]]

        self.assertIn("switcher", table("sniper"))
        self.assertNotIn("switcher", table("rusher"))

    # ── 2. re-recording a result must reproduce the old role, not today's ──
    def test_the_frozen_event_roster_survives_a_club_role_change(self):
        """The result paths stamp role_at_match from the FROZEN event roster. If that read went to
        the live club roster instead, re-uploading a July match in September would stamp September's
        role onto July, which is the whole bug. This pins the resolution helper they all use."""
        self.membership.in_game_role = "rusher"
        self.membership.save(update_fields=["in_game_role"])

        roles = roster_roles.frozen_roles_for_event(self.event.event_id)
        self.assertEqual(roles[self.user.user_id], "sniper")

    def test_a_re_recorded_result_keeps_the_historical_role(self):
        """Simulates what every result path does on a re-upload: delete the match's player rows and
        write them again from the frozen roster. The role must come back as it was."""
        stats = TournamentPlayerMatchStats.objects.get(player=self.user)
        match = stats.team_stats.match
        team_stats = stats.team_stats

        self.membership.in_game_role = "grenader"
        self.membership.save(update_fields=["in_game_role"])

        TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
        roles = roster_roles.frozen_roles_for_match(match)
        TournamentPlayerMatchStats.objects.create(
            team_stats=team_stats, player=self.user, kills=9, played=True,
            role_at_match=roles.get(self.user.user_id),
        )

        recalc.recalc_player_monthly(self.user.pk, MONTH)
        self.assertEqual(PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).role,
                         "sniper")

    # ── 3. the breakdown is real, role-scoped data ──
    def test_the_breakdown_records_matches_and_kills_in_the_role(self):
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        score = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)
        self.assertEqual(score.role_breakdown, {"sniper": {"matches": 1, "kills": 9}})

    def test_an_unstamped_match_contributes_no_role(self):
        """A match written before the stamping existed leaves role_at_match NULL. It must NOT be
        filled in from the current roster - an empty answer is the honest one."""
        TournamentPlayerMatchStats.objects.filter(player=self.user).update(role_at_match=None)
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        score = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)
        self.assertIsNone(score.role)
        self.assertIsNone(score.role_breakdown)

    # ── 4. several roles in one period ──
    def test_a_mixed_role_month_is_filed_under_the_role_played_most(self):
        """Two more matches as a rusher in the same month: 2 rusher vs 1 sniper, so the month is a
        rusher month, but the sniper play is still reported rather than thrown away."""
        for number, day in enumerate([datetime.date(2098, 5, 12), datetime.date(2098, 5, 14)], 1):
            other = self._event(f"Rusher Cup {number}", day=day)
            self._play(other, self.user, role="rusher", kills=4, day=day)

        recalc.recalc_player_monthly(self.user.pk, MONTH)
        score = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)
        self.assertEqual(score.role, "rusher")
        self.assertEqual(score.role_breakdown, {
            "sniper": {"matches": 1, "kills": 9},
            "rusher": {"matches": 2, "kills": 8},
        })

    def test_a_mixed_role_player_appears_in_exactly_one_role_table(self):
        """The tables must stay a partition of the ladder, or one player inflates two tab counts."""
        for number, day in enumerate([datetime.date(2098, 5, 12), datetime.date(2098, 5, 14)], 1):
            other = self._event(f"Rusher Cup {number}", day=day)
            self._play(other, self.user, role="rusher", kills=4, day=day)
        recalc.recalc_player_monthly(self.user.pk, MONTH)

        factory = APIRequestFactory()
        appearances = []
        for role in player_roles.ROLE_KEYS:
            body = player_roles.players_by_role(
                factory.get(f"/rankings/players/by-role/?month=2098-05&role={role}")).data
            appearances += [(role, r["username"]) for r in body["results"]]
        self.assertEqual([role for role, name in appearances if name == "switcher"], ["rusher"])

    def test_the_row_says_it_is_mixed_and_reports_role_scoped_counts(self):
        for number, day in enumerate([datetime.date(2098, 5, 12), datetime.date(2098, 5, 14)], 1):
            other = self._event(f"Rusher Cup {number}", day=day)
            self._play(other, self.user, role="rusher", kills=4, day=day)
        recalc.recalc_player_monthly(self.user.pk, MONTH)

        row = player_roles.players_by_role(
            APIRequestFactory().get("/rankings/players/by-role/?month=2098-05&role=rusher")
        ).data["results"][0]
        self.assertTrue(row["role_is_mixed"])
        # role-SCOPED: their rusher matches and rusher kills, not the month's totals.
        self.assertEqual(row["role_matches"], 2)
        self.assertEqual(row["role_kills"], 8)
        self.assertEqual(row["kills"], 17)          # the month total is unchanged beside it

    # ── 5. staff hold no in-game role, so no role table is theirs ──
    def test_a_staff_member_gets_no_role(self):
        coach = User.objects.create(username="coach_c", email="cc@example.com")
        TeamMembers.objects.create(team=self.team, member=coach, management_role="coach")
        event = self._event("Coach Cup", day=datetime.date(2098, 5, 20))
        # A staff member on the event roster carries no in_game_role, so nothing is stamped.
        self._play(event, coach, role=None, kills=3, day=datetime.date(2098, 5, 20))

        recalc.recalc_player_monthly(coach.pk, MONTH)
        self.assertIsNone(PlayerMonthlyScore.objects.get(player=coach, month=MONTH).role)

    # ── 6. the score is untouched by any of this ──
    def test_recording_the_role_does_not_change_the_score(self):
        """A role table is a filter, not separate scoring. Stamping a role must not move a number."""
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        with_role = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).total_score

        TournamentPlayerMatchStats.objects.filter(player=self.user).update(role_at_match=None)
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        without_role = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).total_score

        self.assertEqual(with_role, without_role)


class PrimaryRoleTests(TestCase):
    """aggregation.primary_role - the rule that files a mixed period under one role."""

    def test_most_matches_wins(self):
        self.assertEqual(aggregation.primary_role({
            "sniper": {"matches": 2, "kills": 50},
            "rusher": {"matches": 3, "kills": 1},
        }), "rusher")

    def test_kills_break_a_match_count_tie(self):
        self.assertEqual(aggregation.primary_role({
            "sniper": {"matches": 2, "kills": 4},
            "rusher": {"matches": 2, "kills": 9},
        }), "rusher")

    def test_a_total_tie_is_broken_deterministically_by_model_order(self):
        """An exact tie must not depend on dict iteration order, or the same data could file a
        player under a different role on a re-run. Model order is the arbitrary-but-stable rule."""
        tie = {"sniper": {"matches": 2, "kills": 4}, "rusher": {"matches": 2, "kills": 4}}
        self.assertEqual(aggregation.primary_role(tie), "rusher")     # rusher is first in the model
        self.assertEqual(aggregation.primary_role(dict(reversed(list(tie.items())))), "rusher")

    def test_no_breakdown_means_no_role(self):
        self.assertIsNone(aggregation.primary_role({}))
        self.assertIsNone(aggregation.primary_role(None))


class RoleLadderTests(TestCase):
    """The monthly role ladder: filtering, within-role ranks, counts, and who is excluded.

    These build the score rows directly (role already stored) because the subject here is the
    ENDPOINT. How the stored role gets there is RoleHistoryTests' job.
    """

    def setUp(self):
        self.factory = APIRequestFactory()
        self.season = _season()

        owner = User.objects.create(username="role_owner", email="ro@example.com")
        self.team = Team.objects.create(
            team_name="Role FC", join_settings="open",
            team_creator=owner, team_owner=owner, country="NG",
        )

        # Scores descend so the global ranks are 1..6 in the order created. Two snipers sit at
        # global 2 and 5, which is what makes the within-role renumbering visible: they must come
        # back as 1 and 2, not 2 and 5.
        self.players = {}
        roster = [
            ("rush_a", 100.0, "rusher"),
            ("snipe_a", 90.0, "sniper"),
            ("supp_a", 80.0, "support"),
            ("nade_a", 70.0, "grenader"),
            ("snipe_b", 60.0, "sniper"),
            ("staff_a", 50.0, None),        # a coach/manager: on the roster, no in-game role
        ]
        for rank, (username, score, role) in enumerate(roster, start=1):
            user = User.objects.create(username=username, email=f"{username}@example.com")
            self.players[username] = user
            TeamMembers.objects.create(
                team=self.team, member=user,
                management_role="coach" if role is None else "member",
                in_game_role=role,
            )
            PlayerMonthlyScore.objects.create(
                player=user, month=MONTH, total_score=score, rank=rank,
                role=role,
                role_breakdown=({role: {"matches": 3, "kills": 12}} if role else None),
            )

        # A ghost player: interleaved into the ladder by score, but with no roster row and therefore
        # no role. Global rank 7, below everyone above.
        ghost_team = GhostTeam.objects.create(
            team_name="Ghost FC", country="NG", created_by=owner,
        )
        self.ghost = GhostPlayer.objects.create(ghost_team=ghost_team, ign="GhostSniper", slot=1)
        PlayerMonthlyScore.objects.create(
            ghost_player=self.ghost, month=MONTH, total_score=40.0, rank=7,
        )

    def _get(self, **params):
        params.setdefault("month", "2098-05")
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return player_roles.players_by_role(
            self.factory.get(f"/rankings/players/by-role/?{qs}")
        ).data

    # ── 1. the unfiltered ladder is the ladder ──
    def test_no_role_returns_the_whole_ladder_with_global_ranks(self):
        body = self._get()
        self.assertIsNone(body["role"])
        self.assertEqual(body["period"], "monthly")
        self.assertEqual(len(body["results"]), 7)          # 6 real + 1 ghost
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2, 3, 4, 5, 6, 7])
        # overall_rank is present on every shape so the client renders one row component.
        self.assertEqual([r["overall_rank"] for r in body["results"]], [1, 2, 3, 4, 5, 6, 7])

    def test_all_sentinel_is_the_same_as_no_role(self):
        self.assertEqual(self._get(role="all")["results"],
                         self._get()["results"])

    def test_unknown_role_degrades_to_the_full_ladder(self):
        """A stale bookmark shows the ladder rather than an error page."""
        body = self._get(role="igl")
        self.assertIsNone(body["role"])
        self.assertEqual(len(body["results"]), 7)

    # ── 2. within-role ranks, the headline behaviour ──
    def test_role_table_ranks_are_within_the_role(self):
        body = self._get(role="sniper")
        self.assertEqual(body["role"], "sniper")
        self.assertEqual([r["username"] for r in body["results"]], ["snipe_a", "snipe_b"])
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2])
        # ...and the global position is still available for a "2nd overall" label.
        self.assertEqual([r["overall_rank"] for r in body["results"]], [2, 5])

    def test_scores_are_identical_to_the_main_ladder(self):
        """A role table re-orders, it never re-scores."""
        main = {r["username"]: r["total_score"]
                for r in views.players_monthly(
                    self.factory.get("/rankings/players/monthly/?month=2098-05")).data["results"]}
        for row in self._get(role="sniper")["results"]:
            self.assertEqual(row["total_score"], main[row["username"]])

    def test_within_role_ranks_continue_across_pages(self):
        """Page 2 continues 2, 3... - it does not restart at 1."""
        body = self._get(role="sniper", limit=1, offset=1)
        self.assertEqual([r["rank"] for r in body["results"]], [2])
        self.assertEqual(body["pagination"]["total_count"], 2)

    # ── 3. who is excluded ──
    def test_roleless_player_is_in_no_role_table(self):
        for role in player_roles.ROLE_KEYS:
            names = [r["username"] for r in self._get(role=role)["results"]]
            self.assertNotIn("staff_a", names, f"staff_a leaked into the {role} table")
        # ...but is still on the full ladder.
        self.assertIn("staff_a", [r["username"] for r in self._get()["results"]])

    def test_ghost_player_is_in_no_role_table(self):
        """A ghost has no roster row, so it has no role. It stays on the unfiltered ladder."""
        for role in player_roles.ROLE_KEYS:
            self.assertFalse(
                any(r["is_ghost"] for r in self._get(role=role)["results"]),
                f"a ghost row leaked into the {role} table",
            )
        self.assertTrue(any(r["is_ghost"] for r in self._get()["results"]))

    def test_the_live_roster_no_longer_decides_the_table(self):
        """The regression guard. Moving a player's CLUB role must not move them between tables -
        only the role stored on the period's score row decides."""
        membership = TeamMembers.objects.get(member=self.players["snipe_a"])
        membership.in_game_role = "grenader"
        membership.save(update_fields=["in_game_role"])

        self.assertIn("snipe_a", [r["username"] for r in self._get(role="sniper")["results"]])
        self.assertNotIn("snipe_a", [r["username"] for r in self._get(role="grenader")["results"]])

    # ── 4. the tab bar + coverage ──
    def test_catalog_lists_every_role_with_scored_counts(self):
        counts = {r["role"]: r["player_count"] for r in self._get()["roles"]}
        self.assertEqual(counts, {"rusher": 1, "support": 1, "grenader": 1, "sniper": 2})
        # every role is listed even at zero, so the tab bar keeps its shape
        self.assertEqual(len(self._get()["roles"]), len(player_roles.ROLE_KEYS))

    def test_counts_ignore_rostered_players_with_no_score_this_month(self):
        """A role can be full of players and still read 0 for a month nobody played."""
        extra = User.objects.create(username="snipe_c", email="sc@example.com")
        TeamMembers.objects.create(team=self.team, member=extra, in_game_role="sniper")
        counts = {r["role"]: r["player_count"] for r in self._get()["roles"]}
        self.assertEqual(counts["sniper"], 2)          # snipe_c has no score row this month

    def test_counts_are_the_same_whichever_role_is_selected(self):
        """The tabs describe the period, so they must not move as the user clicks between them."""
        self.assertEqual(self._get(role="sniper")["roles"], self._get(role="rusher")["roles"])

    def test_coverage_reports_the_scored_population_and_how_much_has_a_role(self):
        coverage = self._get()["role_coverage"]
        self.assertEqual(coverage["players_scored"], 7)      # 6 real + 1 ghost
        self.assertEqual(coverage["players_with_role"], 5)   # staff + ghost have none
        self.assertTrue(coverage["has_role_data"])

    def test_a_period_with_no_stored_roles_says_so(self):
        """The case that matters for honesty: a month recorded before the stamping existed must
        report has_role_data=False so the UI can explain the empty tabs instead of implying nobody
        played those roles."""
        PlayerMonthlyScore.objects.filter(month=MONTH).update(role=None, role_breakdown=None)
        body = self._get()
        self.assertFalse(body["role_coverage"]["has_role_data"])
        self.assertEqual(body["role_coverage"]["players_with_role"], 0)
        self.assertTrue(all(r["player_count"] == 0 for r in body["roles"]))


class RoleLadderGatingTests(TestCase):
    """The role ladder must obey exactly the gates the main ladder obeys."""

    def setUp(self):
        self.factory = APIRequestFactory()
        owner = User.objects.create(username="gate_owner", email="go@example.com")
        self.team = Team.objects.create(
            team_name="Gate Role FC", join_settings="open",
            team_creator=owner, team_owner=owner, country="NG",
        )
        self.user = User.objects.create(username="gate_sniper", email="gs@example.com")
        TeamMembers.objects.create(team=self.team, member=self.user, in_game_role="sniper")

    def _get(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return player_roles.players_by_role(
            self.factory.get(f"/rankings/players/by-role/?{qs}")
        ).data

    def test_unpublished_season_serves_nothing_but_keeps_the_tab_bar(self):
        _season(rankings_published=False, tiers_published=False)
        PlayerMonthlyScore.objects.create(player=self.user, month=MONTH, total_score=10, rank=1,
                                          role="sniper")
        body = self._get(role="sniper", month="2098-05")
        self.assertFalse(body["published"])
        self.assertEqual(body["results"], [])
        # the UI keeps its shape rather than collapsing to nothing
        self.assertEqual(len(body["roles"]), len(player_roles.ROLE_KEYS))
        self.assertTrue(all(r["player_count"] == 0 for r in body["roles"]))

    def test_a_gated_period_does_not_leak_its_role_coverage(self):
        """The counts are zeroed behind the gate, and so is the coverage block - otherwise a hidden
        season would still advertise "14 snipers scored" through the back door."""
        _season(rankings_published=False, tiers_published=False)
        PlayerMonthlyScore.objects.create(player=self.user, month=MONTH, total_score=10, rank=1,
                                          role="sniper")
        coverage = self._get(role="sniper", month="2098-05")["role_coverage"]
        self.assertEqual(coverage, {"players_with_role": 0, "players_scored": 0,
                                    "has_role_data": False})

    def test_quarterly_role_table_renumbers_and_carries_the_tier(self):
        season = _season()
        other = User.objects.create(username="q_sniper_b", email="qsb@example.com")
        TeamMembers.objects.create(team=self.team, member=other, in_game_role="sniper")
        middle = User.objects.create(username="q_rusher", email="qr@example.com")
        TeamMembers.objects.create(team=self.team, member=middle, in_game_role="rusher")
        for rank, (user, score, role) in enumerate(
            [(self.user, 90.0, "sniper"), (middle, 80.0, "rusher"), (other, 70.0, "sniper")],
            start=1,
        ):
            PlayerQuarterlyScore.objects.create(
                player=user, season=season, total_score=score, rank=rank, tier_assigned=1,
                role=role,
            )
        body = self._get(role="sniper", period="quarterly")
        self.assertEqual(body["period"], "quarterly")
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2])
        self.assertEqual([r["overall_rank"] for r in body["results"]], [1, 3])
        self.assertEqual([r["tier"] for r in body["results"]], [1, 1])

    def test_quarterly_tier_stays_hidden_until_tiers_are_published(self):
        """tiers_published is a SECOND, independent gate (views._gated_quarterly)."""
        season = _season(tiers_published=False)
        PlayerQuarterlyScore.objects.create(
            player=self.user, season=season, total_score=90, rank=1, tier_assigned=0, role="sniper",
        )
        body = self._get(role="sniper", period="quarterly")
        self.assertTrue(body["published"])
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNone(body["results"][0]["tier"])
        self.assertIsNone(body["results"][0]["tier_label"])

    def test_period_defaults_to_monthly_for_an_unrecognised_value(self):
        _season()
        PlayerMonthlyScore.objects.create(player=self.user, month=MONTH, total_score=10, rank=1,
                                          role="sniper")
        self.assertEqual(self._get(period="annual", month="2098-05")["period"], "monthly")


class BackfillPlayerRolesTests(TestCase):
    """The backfill must fill in only what it can defend, and leave the rest empty.

    The whole risk of a backfill here is that it stamps TODAY's role onto a period played months
    ago, which would recreate the exact bug the feature removes. These tests are the guard.
    """

    PLAY_DAY = datetime.date(2098, 5, 10)

    def setUp(self):
        _season()
        self.user = User.objects.create(username="bf_player", email="bf@example.com")
        self.team = Team.objects.create(
            team_name="Backfill FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.membership = TeamMembers.objects.create(
            team=self.team, member=self.user, in_game_role="sniper",
        )

    def _event(self, name, day=None):
        day = day or self.PLAY_DAY
        return Event.objects.create(
            event_name=name, competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=day, end_date=day,
            registration_open_date=day - datetime.timedelta(days=5),
            registration_end_date=day - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.user, is_draft=False,
        )

    def _register(self, event, role=None):
        """An event roster row with NO frozen role, as every pre-feature registration has."""
        tt = TournamentTeam.objects.create(
            event=event, team=self.team, registered_by=self.user, status="active",
        )
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.user, event=event, in_game_role=role,
        )
        return tt

    def _result(self, event, tt, *, kills=5, role_at_match=None):
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=self.PLAY_DAY, end_date=self.PLAY_DAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=self.PLAY_DAY,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=self.PLAY_DAY,
        )
        ts = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=kills,
        )
        return TournamentPlayerMatchStats.objects.create(
            team_stats=ts, player=self.user, kills=kills, played=True, role_at_match=role_at_match,
        )

    def _run(self, apply=True):
        out = StringIO()
        call_command("backfill_player_roles", *(["--apply"] if apply else []), stdout=out)
        return out.getvalue()

    # ── the thing it must refuse to do ──
    def test_it_refuses_to_stamp_an_event_that_already_has_results(self):
        """History. The player's role during this event was never recorded, and their CURRENT club
        role is not evidence of it, so the roster row stays empty and the month reports no role."""
        event = self._event("Played Cup")
        tt = self._register(event)
        self._result(event, tt)
        recalc.recalc_player_monthly(self.user.pk, MONTH)

        self._run()

        member = TournamentTeamMember.objects.get(tournament_team=tt, user=self.user)
        self.assertIsNone(member.in_game_role)
        self.assertIsNone(TournamentPlayerMatchStats.objects.get(player=self.user).role_at_match)
        self.assertIsNone(PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).role)

    # ── the things it can defend ──
    def test_it_freezes_the_roster_of_an_event_with_no_results(self):
        """Nothing has been awarded, so there is no past performance to mis-describe: writing the
        current club role now is exactly what registration would have written."""
        event = self._event("Unplayed Cup")
        tt = self._register(event)

        self._run()

        member = TournamentTeamMember.objects.get(tournament_team=tt, user=self.user)
        self.assertEqual(member.in_game_role, "sniper")

    def test_it_stamps_match_rows_from_a_frozen_roster_role(self):
        """A roster row that DOES carry a frozen role (written at registration) can legitimately
        fill in an unstamped match row for the same event."""
        event = self._event("Frozen Cup")
        tt = self._register(event, role="grenader")
        self._result(event, tt, role_at_match=None)

        self._run()

        self.assertEqual(
            TournamentPlayerMatchStats.objects.get(player=self.user).role_at_match, "grenader")

    def test_it_never_overwrites_an_existing_stamp(self):
        event = self._event("Stamped Cup")
        tt = self._register(event, role="rusher")
        self._result(event, tt, role_at_match="sniper")     # recorded as sniper at the time

        self._run()

        self.assertEqual(
            TournamentPlayerMatchStats.objects.get(player=self.user).role_at_match, "sniper")

    def test_it_rebuilds_the_period_columns_from_the_stamps(self):
        event = self._event("Rebuild Cup")
        tt = self._register(event, role="support")
        self._result(event, tt, kills=7, role_at_match="support")
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        # Simulate a score row written before the columns existed.
        PlayerMonthlyScore.objects.filter(player=self.user).update(role=None, role_breakdown=None)

        self._run()

        score = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)
        self.assertEqual(score.role, "support")
        self.assertEqual(score.role_breakdown, {"support": {"matches": 1, "kills": 7}})

    # ── operational safety ──
    def test_a_dry_run_writes_nothing(self):
        event = self._event("Dry Cup")
        tt = self._register(event)

        output = self._run(apply=False)

        self.assertIn("DRY RUN", output)
        member = TournamentTeamMember.objects.get(tournament_team=tt, user=self.user)
        self.assertIsNone(member.in_game_role)

    def test_it_is_idempotent(self):
        event = self._event("Twice Cup")
        tt = self._register(event, role="sniper")
        self._result(event, tt, kills=6, role_at_match="sniper")
        recalc.recalc_player_monthly(self.user.pk, MONTH)

        self._run()
        first = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)
        first_role, first_breakdown, first_score = first.role, first.role_breakdown, first.total_score
        self._run()
        second = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH)

        self.assertEqual((second.role, second.role_breakdown), (first_role, first_breakdown))
        # and it must never move a score - it only writes the two role columns.
        self.assertEqual(second.total_score, first_score)
