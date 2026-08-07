"""
§18 real-time recalculation triggers.

On result/scrim/prize edits, enqueue recalc for the affected team + players for the
match's month and active season. Runs after the DB commit (transaction.on_commit) so
the recalc reads the just-saved state. Score-model writes are NOT senders here, so no
recursion. In dev these run inline (RANKINGS_RECALC_SYNC); in prod they hit Celery.
"""
from django.db import transaction
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

from afc_tournament_and_scrims.models import (
    Event, TournamentTeamMatchStats, TournamentPlayerMatchStats, TournamentTeam, EventPrizePayout,
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
        from .models import auto_rollover_seasons
        auto_rollover_seasons()  # calendar-driven activation (owner 2026-07-02)
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


# ───────────────────────── tournament tier changes (owner backlog item 12/14) ─────────────────────────
# An event's tier is the single biggest multiplier on every result in it: aggregation feeds
# Event.tournament_tier to the engine as TournamentInput.tier, and tier_1 is worth 2.0x placement
# and kill points plus a 30-point win bonus against tier_3's 1.0x and 12 (scoring/constants.py).
#
# Nothing recalculated when it changed. The tier moves through three doors - a head/super admin's
# manual pick (afc_tournament_and_scrims.views.apply_event_tier), the automatic classifier when an
# event is created or edited, and the `reclassify_event_tiers` management command re-running that
# classifier in bulk after a rule or FX change - and none of them touched the rankings. So an admin
# could correct a mis-tiered tournament and every affected ladder would keep serving the old
# numbers until something unrelated touched those teams, or until the nightly sweep. That is
# exactly the "rankings must update automatically" gap, on the input most likely to be corrected.
#
# Deliberately NOT wired to EventTierRule edits: editing a RULE changes how FUTURE events are
# classified and mutates no stored tier, which is why admin_tournament_tiers documents that it does
# not enqueue. A rule edit only reaches the rankings once reclassify_event_tiers rewrites an event's
# tournament_tier - and that write lands here, so the bulk pass gets its recalculation for free.
@receiver(pre_save, sender=Event)
def stash_previous_event_tier(sender, instance, **kwargs):
    """Remember the stored tier so post_save can tell whether it actually moved.

    post_save cannot see the old value, and Event.save() is called often (the every-few-minutes
    status convergence sweep completes and reopens events). Firing a whole-event recalculation on
    every Event save would be a stampede, so the receiver below fires ONLY on a real tier change.
    The cost of that precision is this one extra row read per Event save, on an already-narrow set.
    """
    if not instance.pk:
        instance._rankings_prev_tier = None      # creating: there is no previous tier
        return
    instance._rankings_prev_tier = (
        Event.objects.filter(pk=instance.pk).values_list("tournament_tier", flat=True).first()
    )


@receiver(post_save, sender=Event)
def on_event_tier_change(sender, instance, created, **kwargs):
    """The event's tier moved -> recompute every TEAM whose results it weighted.

    Scope mirrors admin_results._enqueue_event_team_recalc (the Result Markers surface): every
    registered team. Periods come from recalc.event_periods, so the ladders repaired are the ones
    for the months the event was PLAYED in.

    TEAMS ONLY, and that is not an oversight. A player's score has no tier factor anywhere in it:
    scoring/engine._player_components computes compressed personal kills and placement plus the
    flat MVP / finals / team-win / participation weights, and never reads
    PlayerTournamentInput.tier (spec §7 - players reach a tier by INHERITING their team's at
    quarterly evaluation, §8.1, not by scoring more per point). Enqueueing every player in the
    event on a tier change would therefore be a guaranteed no-op pass over the whole roster. If the
    player engine is ever given a tier factor, add the player enqueue here - the field is already
    carried through aggregation._collect_player, so it is one line.

    A newly created event is skipped - it has no results yet, so there is nothing to reweight.
    """
    if created:
        return
    previous = getattr(instance, "_rankings_prev_tier", None)
    if previous is None or previous == instance.tournament_tier:
        return

    # Lazy import: admin_results is a view module and imports .admin_views, which pulls in DRF.
    # signals.py is loaded from apps.ready(), so keeping the import inside the handler avoids
    # dragging the whole admin surface into app startup.
    from .admin_results import _enqueue_event_team_recalc
    _enqueue_event_team_recalc(instance.event_id)


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
