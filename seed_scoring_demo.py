"""Demo seeder for the new scoring modes (sub-project A). Builds a 2-stage event:
  Semifinals  — Point-Rush (reward {1:10, 2:7, 3:5}, carries into Finals)
  Finals      — Champion-Point (threshold 20)
Designed so BRAVO becomes champion (booyahs in Finals match 2 while already on match
point — helped by its +7 carry-over) EVEN THOUGH ALPHA ends with more raw points. This
shows the crown pinning the champion to #1 regardless of total, plus the +N carry-over
badges and the "decided" banner.

Run: .venv/Scripts/python.exe seed_scoring_demo.py
"""
import os, datetime, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
django.setup()

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Leaderboard, Match, TournamentTeam, TournamentTeamMatchStats,
)
from afc_tournament_and_scrims import scoring

admin = User.objects.get(username="headadmin")
D = datetime.date(2026, 6, 1)
PP = scoring.DEFAULT_PLACEMENT  # {1:12, 2:9, 3:8, ...}

# Idempotent: wipe any prior run (cascades stages/groups/matches/stats/tournament-teams).
Event.objects.filter(event_name="Scoring Demo Cup").delete()

ev = Event.objects.create(
    event_name="Scoring Demo Cup", competition_type="tournament", participant_type="squad",
    event_type="internal", max_teams_or_players=16, event_mode="virtual",
    start_date=D, end_date=D, registration_open_date=D, registration_end_date=D,
    prizepool="$1000", event_rules="rules", event_status="completed",
    registration_link="https://afc.test/reg", number_of_stages=2, creator=admin, is_draft=False)

# Stage 1 — Semifinals: Point-Rush reward carries the top 3 into the Finals.
semis = Stages.objects.create(
    event=ev, stage_name="Semifinals", start_date=D, end_date=D, number_of_groups=1,
    stage_format="br - normal", teams_qualifying_from_stage=3, stage_status="completed",
    point_rush_enabled=True, point_rush_reward={"1": 10, "2": 7, "3": 5})

# Stage 2 — Finals: Champion-Point, low threshold so it triggers in the demo.
finals = Stages.objects.create(
    event=ev, stage_name="Finals", start_date=D, end_date=D, number_of_groups=1,
    stage_format="br - normal", teams_qualifying_from_stage=1, stage_status="completed",
    is_finals_stage=True, champion_point_enabled=True, champion_point_threshold=20)

semis.point_rush_target_stage = finals   # wire the carry-over target
semis.save(update_fields=["point_rush_target_stage"])


def group_and_matches(stage, name, n_matches):
    g = StageGroups.objects.create(
        stage=stage, group_name=name, playing_date=D, playing_time=datetime.time(19, 0),
        teams_qualifying=stage.teams_qualifying_from_stage, match_count=n_matches, match_maps=["bermuda"])
    lb = Leaderboard.objects.create(
        leaderboard_name=f"{stage.stage_name} - {name}", event=ev, stage=stage, group=g,
        creator=admin, leaderboard_method="manual", placement_points={}, kill_point=1.0)
    matches = [Match.objects.create(
        group=g, leaderboard=lb, match_number=i + 1, match_map="bermuda", played_on=D,
        result_inputted=True,
        scoring_settings={"placement_points": {str(k): v for k, v in PP.items()}, "kill_point": 1})
        for i in range(n_matches)]
    return g, matches


semis_grp, semis_matches = group_and_matches(semis, "Semis Lobby", 1)
finals_grp, finals_matches = group_and_matches(finals, "Finals Lobby", 2)


def team(name):
    t, _ = Team.objects.get_or_create(team_name=name, defaults=dict(
        join_settings="open", team_creator=admin, team_owner=admin, team_captain=admin, country="Nigeria"))
    return t


def tt(team_obj):
    o, _ = TournamentTeam.objects.get_or_create(event=ev, team=team_obj)
    return o


alpha = tt(team("Demo Alpha"))
bravo = tt(team("Demo Bravo"))
charlie = tt(team("Demo Charlie"))


def result(match, tteam, placement, kills):
    """Store a team's per-match stats with points computed through the shared scoring module."""
    pts = scoring.compute_team_points(
        placement_points=PP, kill_point=1.0, points_per_assist=0, points_per_1000_damage=0,
        placement=placement, kills=kills, damage=0, assists=0, bonus=0, penalty=0, played=True)
    TournamentTeamMatchStats.objects.create(
        match=match, tournament_team=tteam, placement=placement, kills=kills, played=True,
        placement_points=pts["placement_points"], kill_points=pts["kill_points"],
        total_points=pts["total_points"])


# Semifinals (1 match) → standings Alpha(17) > Bravo(13) > Charlie(11) → carry 10 / 7 / 5
result(semis_matches[0], alpha, 1, 5)
result(semis_matches[0], bravo, 2, 4)
result(semis_matches[0], charlie, 3, 3)

# Finals M1: Bravo booyahs but pre-match total is only its +7 carry (<20) → does NOT win.
result(finals_matches[0], alpha, 2, 3)    # 12 → Alpha 10+12 = 22 (now on match point)
result(finals_matches[0], bravo, 1, 2)    # 14 → Bravo 7+14 = 21 (now on match point)
result(finals_matches[0], charlie, 3, 1)  # 9  → Charlie 5+9 = 14

# Finals M2: Bravo booyahs AGAIN, now pre-match 21 ≥ 20 → CHAMPION (even though Alpha ends higher).
result(finals_matches[1], bravo, 1, 2)    # Bravo 21+14 = 35  ← champion, pinned to #1
result(finals_matches[1], alpha, 2, 5)    # Alpha 22+14 = 36  ← more points, but #2
result(finals_matches[1], charlie, 3, 1)  # Charlie 14+9 = 23

print("Seeded 'Scoring Demo Cup'  slug =", ev.slug)
print("  Semifinals: Point-Rush reward", semis.point_rush_reward, "-> target", semis.point_rush_target_stage.stage_name)
print("  Finals: Champion-Point threshold", finals.champion_point_threshold)
print("  Expected Finals standings: #1 Demo Bravo (CHAMPION, 35), #2 Demo Alpha (36), #3 Demo Charlie (23)")
print("done")
