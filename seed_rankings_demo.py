"""One-off demo seeder: adds an Elite + Competitive team (2 tournaments each) so the
Tiers view is populated across bands. Run: .venv/Scripts/python.exe seed_rankings_demo.py"""
import os, datetime, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
django.setup()

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings.models import Season
from afc_rankings import recalc as R

admin = User.objects.get(username="headadmin")
MONTH = datetime.date(2026, 5, 1)
PLAYED = datetime.date(2026, 5, 20)


def make_event(name):
    ev, _ = Event.objects.get_or_create(event_name=name, defaults=dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_mode="virtual", start_date=PLAYED, end_date=PLAYED,
        registration_open_date=MONTH, registration_end_date=PLAYED, prizepool="$1500",
        prizepool_cash_value=1500, event_rules="rules", event_status="completed",
        registration_link="https://afc.test/reg", tournament_tier="tier_1", number_of_stages=1,
        creator=admin, is_draft=False))
    stage, _ = Stages.objects.get_or_create(event=ev, stage_name="Grand Final", defaults=dict(
        start_date=PLAYED, end_date=PLAYED, number_of_groups=1, stage_format="br - normal",
        teams_qualifying_from_stage=2, stage_status="completed", is_finals_stage=True))
    grp, _ = StageGroups.objects.get_or_create(stage=stage, group_name="Finals", defaults=dict(
        playing_date=PLAYED, playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1))
    match, _ = Match.objects.get_or_create(group=grp, match_number=1, defaults=dict(
        match_map="alpine", played_on=PLAYED, result_inputted=True))
    return ev, match


def add_result(ev, match, team, owner, placement, kills, won, finals):
    tt, _ = TournamentTeam.objects.get_or_create(event=ev, team=team, defaults=dict(
        is_tournament_winner=won, reached_finals=True, finals_appearances=finals, result_finalized=True))
    if not tt.result_finalized:
        tt.is_tournament_winner = won; tt.reached_finals = True
        tt.finals_appearances = finals; tt.result_finalized = True; tt.save()
    ts, _ = TournamentTeamMatchStats.objects.get_or_create(match=match, tournament_team=tt,
        defaults=dict(placement=placement, kills=kills, played=True))
    TournamentPlayerMatchStats.objects.get_or_create(team_stats=ts, player=owner,
        defaults=dict(kills=kills, played=True))


def team(name, owner_username, country):
    u = User.objects.get(username=owner_username)
    t, _ = Team.objects.get_or_create(team_name=name, defaults=dict(
        join_settings="open", team_creator=u, team_owner=u, country=country, team_captain=u))
    return t, u


# two tier-1 events
e2, m2 = make_event("AFC Elite Series — Leg 1")
e3, m3 = make_event("AFC Elite Series — Leg 2")

# Elite: Omega wins both, 300 kills each → ~84/event → ~168 → Elite (>=150)
omega, ow = team("Team Omega", "NovaStrike", "Kenya")
add_result(e2, m2, omega, ow, placement=1, kills=300, won=True, finals=1)
add_result(e3, m3, omega, ow, placement=1, kills=300, won=True, finals=1)

# Competitive: Echo wins one, finals both, ~100 kills → ~98 → Competitive (90-149)
echo, ek = team("Team Echo", "PhantomX", "South Africa")
add_result(e2, m2, echo, ek, placement=1, kills=100, won=True, finals=1)
add_result(e3, m3, echo, ek, placement=2, kills=100, won=False, finals=1)

# Rising: Delta no wins, modest → ~52 → Rising (40-89)
delta, dk = team("Team Delta", "IronClaw", "Egypt")
add_result(e2, m2, delta, dk, placement=2, kills=30, won=False, finals=1)
add_result(e3, m3, delta, dk, placement=3, kills=30, won=False, finals=1)

season = Season.objects.filter(is_active=True).first()
R.recalc_month(MONTH)
R.recalc_season(season)

from afc_rankings.models import TeamQuarterlyScore
print("=== TEAM TIERS (Season Q2 2026) ===")
TIER = {0: "Elite", 1: "Competitive", 2: "Rising", 3: "Entry"}
for s in TeamQuarterlyScore.objects.filter(season=season).order_by("rank"):
    print("  #%-2s %-14s %-12s total=%-6s (%s tournaments)" % (
        s.rank, s.team.team_name, TIER.get(s.tier_assigned, "?"), s.total_score, s.participated_in_tournaments))
print("done")
