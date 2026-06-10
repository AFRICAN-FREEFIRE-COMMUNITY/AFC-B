"""
afc_rankings.claims — ghost -> real-entity RE-ATTRIBUTION service (the core of the claim process).

PURPOSE
    When an admin APPROVES a ghost-team / ghost-player claim, the ghost's entire ranked history must
    move onto the real team / user so the real entity inherits the ghost's points, rank, and tier.
    Ghost rankings trace ENTIRELY to standalone leaderboard participation: a ghost only ever scores
    through afc_rankings.standalone (recalc_ghost_team_* / recalc_ghost_player_*), which reads the
    ghost's afc_leaderboard.LeaderboardParticipant rows. So re-attribution is exactly:

        1. find every LeaderboardParticipant pointing at this ghost,
        2. re-point each one to the real entity (ghost_team -> team / ghost_player -> user, honoring
           the participant XOR CheckConstraint),
        3. delete the ghost's now-orphaned score rows (TeamMonthly/QuarterlyScore(ghost_team=...) /
           PlayerMonthly/QuarterlyScore(ghost_player=...)),
        4. recompute the REAL entity for every affected month + season so it now owns the points.

    After this runs, the source-of-truth (the participant rows) points at the real entity, so any
    FUTURE recompute (a later result edit, a re-publish) stays correct and idempotent without ever
    looking at the ghost again.

CONFLICT GUARD (out of scope to merge — reject + manual, per the design §re-attribution)
    If a leaderboard the ghost participated in ALSO already has the real team/user as a separate
    participant, re-pointing would create two rows for the same entity in one leaderboard (a self-
    duplicate the standings + rerank cannot reconcile). We refuse the whole claim with a ``ClaimConflict``
    BEFORE mutating anything, naming the offending leaderboard so the admin can resolve it by hand.

CALLERS
    - admin_ghost.ghost_approve_claim       (teams)   -> reattribute_ghost_team(ghost, ghost.claimed_by, user)
    - admin_ghost.ghost_player_approve_claim (players) -> reattribute_ghost_player(ghost, ghost.claimed_by, user)
    Both catch ClaimConflict and surface it as a 400 (nothing is committed — the whole body runs inside
    the endpoint's transaction.atomic, and these services open their own nested atomic block too).

WHAT IT READS / WRITES
    Reads:  afc_leaderboard.LeaderboardParticipant (the ghost's participations + the conflict check),
            each participant's StandaloneLeaderboard.effective_date (the month + season to recompute).
    Writes: re-points the participant rows; deletes the ghost's score rows; then calls
            recalc.recalc_team_monthly/quarterly (or recalc_player_*), which recompute + rerank the
            real entity for the affected periods (those readers pick up the now-re-pointed participants
            via afc_rankings.standalone.standalone_team_inputs / standalone_player_inputs).

LOAD-ORDER NOTE
    afc_leaderboard.models imports afc_rankings.models, so afc_leaderboard is imported LAZILY inside
    the functions (same pattern as afc_rankings.standalone) to avoid a circular import at app load.
"""
from django.db import transaction

from . import recalc
from .standalone import _season_for
from .models import (
    TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)


class ClaimConflict(Exception):
    """Raised when a ghost cannot be re-attributed because the real entity is ALREADY a separate
    participant in one of the ghost's leaderboards (re-pointing would duplicate the entity in that
    leaderboard). Carries a human message naming the leaderboard; the approve endpoints turn it into
    a 400 with that message, and because the raise happens before (and rolls back) any write, nothing
    is committed. Merging the two histories is future scope (design §out-of-scope); for now an admin
    resolves it manually."""
    pass


def _affected_periods(leaderboards):
    """Collapse a set of leaderboards into the (months, seasons) that must be recomputed.

    For each leaderboard, its ``effective_date`` buckets into ONE month (first-of-month) and ONE
    season (``standalone._season_for``). Returns ``(months, season_ids)`` as de-duplicated lists so a
    real entity that scored several leaderboards in the same month/season is recomputed once per
    period, not once per leaderboard. Mirrors how standalone.recompute_for_leaderboard derives the
    month + season from effective_date.
    """
    months = set()
    season_ids = set()
    for lb in leaderboards:
        day = lb.effective_date
        if not day:
            continue
        months.add(day.replace(day=1))           # first-of-month bucket (matches the score tables)
        season = _season_for(day)
        if season:
            season_ids.add(season.season_id)
    return months, season_ids


def reattribute_ghost_team(ghost, real_team, actor):
    """Move a ghost TEAM's standalone history onto ``real_team`` (called on claim approval).

    Steps (all inside one transaction so a failure leaves nothing half-moved):
      1. Collect every LeaderboardParticipant with ``ghost_team=ghost``.
      2. CONFLICT GUARD (before any mutation): for each such participant's leaderboard, if a
         participant with ``team=real_team`` already exists there, raise ClaimConflict naming it.
      3. Re-point each participant: ``ghost_team=None, team=real_team`` (honors the participant XOR).
      4. Delete the ghost's TeamMonthlyScore / TeamQuarterlyScore rows (now orphaned — its
         participations have moved, so a ghost recompute would produce nothing anyway; deleting is the
         clean floor).
      5. Recompute the REAL team for every affected month + season (recalc.recalc_team_monthly /
         recalc_team_quarterly), which reads the now-re-pointed participants via
         standalone.standalone_team_inputs and writes/ranks the real team's rows.

    ``actor`` is the approving admin (kept for signature symmetry + future audit hooks; the endpoint
    writes the audit row). Returns a summary dict (counts) for the caller to put in the audit ``after``.
    """
    # Lazy import (load-order): afc_leaderboard.models imports afc_rankings.models.
    from afc_leaderboard.models import LeaderboardParticipant

    with transaction.atomic():
        # 1) every participation this ghost team holds. select_related the leaderboard so the
        #    conflict check + the effective_date period collapse read it without an N+1.
        participants = list(
            LeaderboardParticipant.objects
            .select_related("leaderboard")
            .filter(ghost_team=ghost)
        )

        # 2) conflict guard FIRST, across ALL participations, before mutating anything: if the real
        #    team is already a separate participant in any of these leaderboards, re-pointing would
        #    duplicate it there -> abort the whole claim with a clear message naming the leaderboard.
        for p in participants:
            lb = p.leaderboard
            if LeaderboardParticipant.objects.filter(leaderboard=lb, team=real_team).exists():
                raise ClaimConflict(
                    f"Cannot claim: the real team is already a participant in leaderboard "
                    f"'{lb.name}' (id {lb.id}) alongside this ghost. Resolve the duplicate manually "
                    f"before approving the claim."
                )

        # 3) collect the affected periods from the (still ghost-pointed) participants' leaderboards,
        #    then re-point each participant onto the real team.
        leaderboards = [p.leaderboard for p in participants]
        months, season_ids = _affected_periods(leaderboards)
        for p in participants:
            p.ghost_team = None
            p.team = real_team
            p.save(update_fields=["ghost_team", "team"])

        # 4) delete the ghost's now-orphaned score rows (the participations have moved off it).
        TeamMonthlyScore.objects.filter(ghost_team=ghost).delete()
        TeamQuarterlyScore.objects.filter(ghost_team=ghost).delete()

        # 5) recompute the real team for every affected month + season so it inherits the points,
        #    rank, and (quarterly) tier. recalc.* reads the re-pointed participants via
        #    standalone.standalone_team_inputs and reranks the whole period.
        for month in months:
            recalc.recalc_team_monthly(real_team.pk, month)
        for season_id in season_ids:
            recalc.recalc_team_quarterly(real_team.pk, season_id)

    return {
        "reattributed_participants": len(participants),
        "affected_months": sorted(m.isoformat() for m in months),
        "affected_seasons": sorted(season_ids),
        "real_team_id": real_team.pk,
    }


def reattribute_ghost_player(ghost, real_user, actor):
    """Move a ghost PLAYER's standalone solo history onto ``real_user`` (called on claim approval).

    Same shape as reattribute_ghost_team but for the solo side: participant ``ghost_player -> user``,
    delete the ghost's PlayerMonthlyScore / PlayerQuarterlyScore rows, recompute the real user with
    recalc.recalc_player_monthly / recalc_player_quarterly (which read the re-pointed participants via
    standalone.standalone_player_inputs). Conflict guard mirrors the team path: abort if the real user
    is already a separate participant in one of the ghost's solo leaderboards.

    ``actor`` = the approving admin (kept for symmetry + future hooks). Returns a summary dict.
    """
    from afc_leaderboard.models import LeaderboardParticipant

    with transaction.atomic():
        # 1) every solo participation this ghost player holds.
        participants = list(
            LeaderboardParticipant.objects
            .select_related("leaderboard")
            .filter(ghost_player=ghost)
        )

        # 2) conflict guard FIRST: real user already a separate participant in any of these LBs.
        for p in participants:
            lb = p.leaderboard
            if LeaderboardParticipant.objects.filter(leaderboard=lb, user=real_user).exists():
                raise ClaimConflict(
                    f"Cannot claim: the real player is already a participant in leaderboard "
                    f"'{lb.name}' (id {lb.id}) alongside this ghost. Resolve the duplicate manually "
                    f"before approving the claim."
                )

        # 3) affected periods, then re-point each participant onto the real user.
        leaderboards = [p.leaderboard for p in participants]
        months, season_ids = _affected_periods(leaderboards)
        for p in participants:
            p.ghost_player = None
            p.user = real_user
            p.save(update_fields=["ghost_player", "user"])

        # 4) delete the ghost player's now-orphaned score rows.
        PlayerMonthlyScore.objects.filter(ghost_player=ghost).delete()
        PlayerQuarterlyScore.objects.filter(ghost_player=ghost).delete()

        # 5) recompute the real user for every affected month + season.
        for month in months:
            recalc.recalc_player_monthly(real_user.pk, month)
        for season_id in season_ids:
            recalc.recalc_player_quarterly(real_user.pk, season_id)

    return {
        "reattributed_participants": len(participants),
        "affected_months": sorted(m.isoformat() for m in months),
        "affected_seasons": sorted(season_ids),
        "real_user_id": real_user.pk,
    }


# ───────────────────────── conflict pre-check (no mutation) ─────────────────────────
# The request endpoints (admin_ghost.ghost_team_request_claim / ghost_player_request_claim) run this
# read-only conflict check BEFORE setting a claim pending, so a request that could never be approved
# (the real entity already shares a leaderboard with the ghost) is rejected up front, not after an
# admin spends review time on it. The approval path re-runs the real guard inside the service above
# (the source of truth), so this is a fail-fast convenience, not a substitute.
def conflict_for_team_claim(ghost, real_team):
    """The conflicting leaderboard name if re-pointing ``ghost``'s team participations onto
    ``real_team`` would duplicate the team in any of them, else None. Read-only (no mutation)."""
    from afc_leaderboard.models import LeaderboardParticipant
    for p in (LeaderboardParticipant.objects.select_related("leaderboard").filter(ghost_team=ghost)):
        lb = p.leaderboard
        if LeaderboardParticipant.objects.filter(leaderboard=lb, team=real_team).exists():
            return lb.name
    return None


def conflict_for_player_claim(ghost, real_user):
    """The conflicting leaderboard name if re-pointing ``ghost``'s solo participations onto
    ``real_user`` would duplicate the user in any of them, else None. Read-only (no mutation)."""
    from afc_leaderboard.models import LeaderboardParticipant
    for p in (LeaderboardParticipant.objects.select_related("leaderboard").filter(ghost_player=ghost)):
        lb = p.leaderboard
        if LeaderboardParticipant.objects.filter(leaderboard=lb, user=real_user).exists():
            return lb.name
    return None
