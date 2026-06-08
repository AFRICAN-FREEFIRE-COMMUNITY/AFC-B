"""Seed a COMPLETE, fully-played BR Round-Robin event into the dev DB (afc_db).

What this builds (slug = "round-robin-demo-cup"):
  - ONE squad Tournament, status=completed, organization=None (native AFC event).
  - ONE Stage, stage_format = "br - round robin" (the SPACED 3-token value -- NOT the
    dead "br - roundrobin" legacy choice), plain scoring (champion_point / point_rush
    both OFF), status=completed.
  - 3 base groups (RoundRobinGroup) A/B/C, order 1/2/3, 2 distinct Teams each = 6 teams.
  - A real round robin over base groups: every PAIR of base groups meets exactly once ->
    C(3,2)=3 game-day lobbies (StageGroups):  Day 1 = A+B,  Day 2 = A+C,  Day 3 = B+C.
  - One Match per lobby (4 teams per lobby), each Match has its own Leaderboard.
  - TournamentTeamMatchStats per team per match, with placements 1..4 + kills, engineered
    so a CLEAR overall cumulative winner ("RR Alpha", group A) emerges, AND per-day order
    differs from cumulative (each base-group team plays exactly 2 of the 3 game-days).

Why these rows: get_round_robin_standings (views.py:11579) reads ONLY
TournamentTeamMatchStats, walking match->group(StageGroups, game_day set)->stage. The
`groups` block echoes RoundRobinGroup.label + .teams; `game_days`/`per_day` need lobbies
whose game_day is non-null; `cumulative` sums every lobby of the stage. We populate all of
those so cumulative AND per_day come back non-empty and sane.

Idempotent: deletes any prior event with this slug first (cascades stages -> groups /
RR-groups -> matches / stats / tournament-teams; M2M rows auto-drop). Teams are globally
unique and NOT event-scoped, so they are reused via get_or_create.

Run (from backend/):
  .venv/Scripts/python.exe manage.py shell -c "exec(open('seed_round_robin_demo.py').read())"

Mirrors the style of seed_scoring_demo.py and the construction order in
afc_tournament_and_scrims/tests_round_robin.py.
"""
import os
import datetime

import django

# Allow running either as `python seed_round_robin_demo.py` (needs setup) or piped through
# `manage.py shell -c exec(...)` (Django already configured). setup() is a no-op the 2nd time.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
try:
    django.setup()
except Exception:
    pass

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Stages,
    StageGroups,
    RoundRobinGroup,
    Leaderboard,
    Match,
    TournamentTeam,
    TournamentTeamMatchStats,
    StageGroupCompetitor,
)

# -- constants ----------------------------------------------------------------------
D = datetime.date(2026, 6, 1)          # single play date for everything in this demo
SLUG = "round-robin-demo-cup"
# Plain BR placement -> placement-points table (clear separation 1 > 2 > 3 > 4). kill_point
# is 1.0, so kill_points == kills. total_points is set by hand = placement_points + kills.
PP = {1: 12, 2: 9, 3: 8, 4: 7}


def _pick_event_admin():
    """Return a user that passes _is_event_admin (role in admin/moderator/support, or a
    granular event_admin/head_admin role). Prefer a real role-bearing admin; fall back to a
    superuser so the seed still has an owner on a fresh DB."""
    u = User.objects.filter(role__in=["admin", "moderator", "support"]).first()
    if u:
        return u
    u = User.objects.filter(is_superuser=True).first()
    assert u, "no admin/superuser found in afc_db to own the seed"
    return u


def run():
    admin = _pick_event_admin()

    # -- idempotency: drop any prior run by slug (cascades down the whole tree) --
    Event.objects.filter(slug=SLUG).delete()

    # -- Event: squad Tournament, COMPLETED, organization left None (native AFC) --
    # slug passed explicitly so Event.save() keeps it verbatim (it only autogenerates when
    # slug is blank). is_draft=False so the event is live/visible like a real completed cup.
    ev = Event.objects.create(
        slug=SLUG,
        event_name="Round Robin Demo Cup",
        competition_type="tournament",
        participant_type="squad",
        event_type="internal",
        max_teams_or_players=16,
        event_mode="virtual",
        start_date=D,
        end_date=D,
        registration_open_date=D,
        registration_end_date=D,
        prizepool="$1000",
        event_rules="Standard BR round-robin demo rules.",
        event_status="completed",
        registration_link="https://afc.test/reg",
        number_of_stages=1,
        creator=admin,
        is_draft=False,
        # organization defaults to None -> native AFC event.
    )

    # -- Stage: BR Round-Robin (EXACT 3-token spaced format), plain scoring, completed --
    stage = Stages.objects.create(
        event=ev,
        stage_name="Group Stage",
        start_date=D,
        end_date=D,
        number_of_groups=3,
        stage_format="br - round robin",        # MUST be the spaced value, not "br - roundrobin"
        teams_qualifying_from_stage=4,
        stage_status="completed",
        champion_point_enabled=False,           # plain scoring (no match-point crown)
        point_rush_enabled=False,               # plain scoring (no carry-over bonus)
    )

    # -- 6 teams -> 6 TournamentTeams. Team.team_name is globally unique -> get_or_create so a
    # second run reuses the same Team rows (they are not event-scoped, so the slug-delete
    # above does not remove them). TournamentTeam IS event-scoped and was cascaded away. --
    def make_tt(name):
        team, _ = Team.objects.get_or_create(
            team_name=name,
            defaults=dict(
                join_settings="open",
                team_creator=admin,
                team_owner=admin,
                team_captain=admin,
                country="Nigeria",
            ),
        )
        tt, _ = TournamentTeam.objects.get_or_create(
            event=ev, team=team, defaults=dict(status="active", registered_by=admin)
        )
        return tt

    # Group A listed first so RR Alpha (A) can sweep both of its lobbies (Day 1 + Day 2).
    a1, a2 = make_tt("RR Alpha"), make_tt("RR A2")
    b1, b2 = make_tt("RR B1"), make_tt("RR B2")
    c1, c2 = make_tt("RR C1"), make_tt("RR C2")

    # -- 3 base groups A/B/C (order 1/2/3 per the brief), 2 TournamentTeams each --
    grp_a = RoundRobinGroup.objects.create(stage=stage, label="A", order=1)
    grp_a.teams.add(a1, a2)
    grp_b = RoundRobinGroup.objects.create(stage=stage, label="B", order=2)
    grp_b.teams.add(b1, b2)
    grp_c = RoundRobinGroup.objects.create(stage=stage, label="C", order=3)
    grp_c.teams.add(c1, c2)

    # -- helper: one game-day lobby (StageGroups) + its Leaderboard + its single Match --
    # Mirrors create_event's _materialise_round_robin_lobby: StageGroups carries game_day +
    # source_groups M2M; the merged roster is seeded as StageGroupCompetitor rows; a
    # Leaderboard is auto-created; one Match per match_count lives under the lobby.
    def make_lobby(day, src_groups, teams):
        lobby = StageGroups.objects.create(
            stage=stage,
            group_name=f"Day {day} Lobby",
            playing_date=D,
            playing_time=datetime.time(19, 0),
            teams_qualifying=4,
            match_count=1,
            match_maps=["bermuda"],
            game_day=day,                      # MUST be non-null -> drives game_days / per_day
        )
        lobby.source_groups.add(*src_groups)   # record which base groups merged into this lobby
        # Seed the merged roster (union of the two base groups) -- matches real create-event
        # data shape (4 competitors per lobby). Not read by standings, but keeps data honest.
        for tt in teams:
            StageGroupCompetitor.objects.get_or_create(stage_group=lobby, tournament_team=tt)
        lb = Leaderboard.objects.create(
            leaderboard_name=f"Day {day} LB",
            event=ev,
            stage=stage,
            group=lobby,
            creator=admin,
            leaderboard_method="manual",
            placement_points={str(k): v for k, v in PP.items()},
            kill_point=1.0,
        )
        match = Match.objects.create(
            leaderboard=lb,
            group=lobby,
            match_number=1,
            match_map="bermuda",
            result_inputted=True,              # match has a result entered (fully played)
            played_on=D,                       # rankings bucketing date
        )
        return match

    # -- helper: write one team's per-match stats. total = placement_pts + kill_pts --
    def stat(match, tt, placement, kills):
        pp = PP[placement]
        kp = kills                              # kill_point == 1.0 -> kill_points == kills
        TournamentTeamMatchStats.objects.create(
            match=match,
            tournament_team=tt,
            placement=placement,
            kills=kills,
            placement_points=pp,
            kill_points=kp,
            total_points=pp + kp,               # no auto-compute on the model -> set it
            played=True,
        )

    # -- Day 1: A+B lobby. RR Alpha takes 1st. --
    m1 = make_lobby(1, [grp_a, grp_b], [a1, a2, b1, b2])
    stat(m1, a1, placement=1, kills=8)   # 12 + 8 = 20   (Alpha)
    stat(m1, b1, placement=2, kills=4)   #  9 + 4 = 13
    stat(m1, a2, placement=3, kills=2)   #  8 + 2 = 10
    stat(m1, b2, placement=4, kills=1)   #  7 + 1 =  8

    # -- Day 2: A+C lobby. RR Alpha takes 1st again -> runaway overall leader. --
    m2 = make_lobby(2, [grp_a, grp_c], [a1, a2, c1, c2])
    stat(m2, a1, placement=1, kills=7)   # 12 + 7 = 19   (Alpha cumulative = 20 + 19 = 39)
    stat(m2, c1, placement=2, kills=3)   #  9 + 3 = 12
    stat(m2, a2, placement=3, kills=2)   #  8 + 2 = 10
    stat(m2, c2, placement=4, kills=1)   #  7 + 1 =  8

    # -- Day 3: B+C lobby. Alpha is not in this day; B1 edges it (still < Alpha overall). --
    m3 = make_lobby(3, [grp_b, grp_c], [b1, b2, c1, c2])
    stat(m3, b1, placement=1, kills=5)   # 12 + 5 = 17   (B1 cumulative = 13 + 17 = 30)
    stat(m3, c1, placement=2, kills=4)   #  9 + 4 = 13   (C1 cumulative = 12 + 13 = 25)
    stat(m3, b2, placement=3, kills=2)   #  8 + 2 = 10   (B2 cumulative =  8 + 10 = 18)
    stat(m3, c2, placement=4, kills=1)   #  7 + 1 =  8   (C2 cumulative =  8 +  8 = 16)

    # Cumulative (each base-group team plays exactly its group's 2 pairings -> 2 stat rows):
    #   RR Alpha = 20 + 19 = 39   <- clear overall winner, plays Day 1 + Day 2
    #   RR B1    = 13 + 17 = 30   plays Day 1 + Day 3
    #   RR C1    = 12 + 13 = 25   plays Day 2 + Day 3
    #   RR A2    = 10 + 10 = 20   plays Day 1 + Day 2
    #   RR B2    =  8 + 10 = 18   plays Day 1 + Day 3
    #   RR C2    =  8 +  8 = 16   plays Day 2 + Day 3
    print(
        f"Seeded '{ev.event_name}'  slug={ev.slug}  event_id={ev.event_id}  stage_id={stage.stage_id}"
    )
    print("  Stage format:", stage.stage_format, "| status:", stage.stage_status)
    print("  Base groups: A/B/C (2 teams each) | game-day lobbies: 3 (A+B, A+C, B+C)")
    print("  Expected cumulative winner: RR Alpha (39); each team games_played == 2")
    return ev, stage


# Allow both `exec(open(...).read())` and direct execution to actually run the seed.
ev, stage = run()
