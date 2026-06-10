"""
§18 real-time recalculation triggers.

On result/scrim/prize edits, enqueue recalc for the affected team + players for the
match's month and active season. Runs after the DB commit (transaction.on_commit) so
the recalc reads the just-saved state. Score-model writes are NOT senders here, so no
recursion. In dev these run inline (RANKINGS_RECALC_SYNC); in prod they hit Celery.
"""
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from afc_tournament_and_scrims.models import (
    TournamentTeamMatchStats, TournamentPlayerMatchStats, TournamentTeam, EventPrizePayout,
)
# P3 standalone-leaderboard senders. signals.py is imported from apps.ready() AFTER every app's
# models have loaded, so importing afc_leaderboard.models here is safe (no load-order cycle).
from afc_leaderboard.models import ParticipantMatchResult, StandaloneLeaderboard
from .models import Season
from . import tasks
from . import standalone
from .aggregation import _match_day


def _season_for(day):
    if day:
        s = Season.objects.filter(is_active=True, start_date__lte=day, end_date__gte=day).first()
        if s:
            return s
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


def _enqueue_team(team_id, match):
    if not team_id:
        return
    day = _match_day(match)
    if not day:
        return
    season = _season_for(day)
    tasks.enqueue_team(team_id, day.replace(day=1), season.season_id if season else None)


def _enqueue_player(player_id, match):
    if not player_id:
        return
    day = _match_day(match)
    if not day:
        return
    season = _season_for(day)
    tasks.enqueue_player(player_id, day.replace(day=1), season.season_id if season else None)


# ───────────────────────── receivers ─────────────────────────
# Registered in apps.py AfcRankingsConfig.ready(). Senders live in
# afc_tournament_and_scrims.models; each handler enqueues via
# tasks.enqueue_team / tasks.enqueue_player, which run inline in dev
# (RANKINGS_RECALC_SYNC / DEBUG) or on the rankings_recalc Celery queue in prod.
@receiver(post_save, sender=TournamentTeamMatchStats)
def on_team_stats_save(sender, instance, **kwargs):
    team_id = instance.tournament_team.team_id
    match = instance.match
    player_ids = list(
        TournamentPlayerMatchStats.objects.filter(team_stats=instance).values_list("player_id", flat=True)
    )

    def fire():
        _enqueue_team(team_id, match)
        for pid in player_ids:
            _enqueue_player(pid, match)

    transaction.on_commit(fire)


@receiver(post_delete, sender=TournamentTeamMatchStats)
def on_team_stats_delete(sender, instance, **kwargs):
    team_id = instance.tournament_team_id and instance.tournament_team.team_id
    match = instance.match
    transaction.on_commit(lambda: _enqueue_team(team_id, match))


@receiver(post_save, sender=TournamentPlayerMatchStats)
def on_player_stats_save(sender, instance, **kwargs):
    player_id = instance.player_id
    match = instance.team_stats.match
    team_id = instance.team_stats.tournament_team.team_id

    def fire():
        _enqueue_player(player_id, match)
        _enqueue_team(team_id, match)

    transaction.on_commit(fire)


@receiver(post_save, sender=TournamentTeam)
def on_tournament_team_markers(sender, instance, **kwargs):
    """Win/finals markers changed → recalc the team for every month its matches fall in + the season."""
    team_id = instance.team_id
    event = instance.event

    def fire():
        from afc_tournament_and_scrims.models import Match
        days = set()
        for m in Match.objects.filter(group__stage__event=event):
            d = _match_day(m)
            if d:
                days.add(d.replace(day=1))
        season = _season_for(next(iter(days)) if days else None)
        for month in days:
            tasks.enqueue_team(team_id, month, season.season_id if season else None)
        if not days and season:
            tasks.enqueue_team(team_id, season.start_date.replace(day=1), season.season_id)

    transaction.on_commit(fire)


@receiver(post_save, sender=EventPrizePayout)
def on_prize_payout(sender, instance, **kwargs):
    if not instance.tournament_team_id:
        return
    team_id = instance.tournament_team.team_id
    day = instance.created_at.date() if instance.created_at else None

    def fire():
        season = _season_for(day)
        if season:
            tasks.enqueue_team(team_id, (day or season.start_date).replace(day=1), season.season_id)

    transaction.on_commit(fire)


# ───────────────────────── P3 standalone-leaderboard receivers ─────────────────────────
# These mirror the event receivers above but key off the standalone-leaderboard tables. Each calls
# standalone.recompute_for_leaderboard on commit, which enqueues a recompute for every participant
# (real team -> enqueue_team, ghost team -> enqueue_ghost_team, real user -> enqueue_player; ghost
# players skipped). recompute_for_leaderboard ALWAYS enqueues, so toggling counts_toward_rankings
# off / un-publishing also fires a recompute that drops the (now non-counting) contribution.
@receiver(post_save, sender=ParticipantMatchResult)
def on_standalone_result_save(sender, instance, **kwargs):
    """A per-map result was added/edited on a standalone LB -> recompute its participants. Resolve
    the LB via result.match.leaderboard. Runs on commit so the recompute reads the saved row."""
    lb = instance.match.leaderboard
    transaction.on_commit(lambda: standalone.recompute_for_leaderboard(lb))


@receiver(post_delete, sender=ParticipantMatchResult)
def on_standalone_result_delete(sender, instance, **kwargs):
    """A result was removed from a standalone LB -> recompute its participants so the dropped result
    no longer scores. (instance.match is still readable on a post_delete signal.)"""
    lb = instance.match.leaderboard
    transaction.on_commit(lambda: standalone.recompute_for_leaderboard(lb))


@receiver(post_save, sender=StandaloneLeaderboard)
def on_standalone_leaderboard_save(sender, instance, **kwargs):
    """The leaderboard header changed (publish / un-publish / toggle counts_toward_rankings / tier /
    played_on) -> recompute ALL of its participants. Covers every state transition that changes what
    the aggregation counts for this LB."""
    transaction.on_commit(lambda: standalone.recompute_for_leaderboard(instance))
