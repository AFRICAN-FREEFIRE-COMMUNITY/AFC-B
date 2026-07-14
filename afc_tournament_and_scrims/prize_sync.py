# ── EVENT -> SEASON PRIZE AUTO-SYNC (owner 2026-07-02) ──────────────────────────
# "Prize pools are entered when creating an event - the season's Prize Money page + evaluation
# should fill automatically." This derives EventPrizePayout rows from an event's prize_distribution
# ({position: amount}) + its FINAL overall standings (the same cumulative + tie-breaker chain every
# ranking surface uses), converting the event's prize_currency to NGN via FxRate (the rankings
# evaluation is Naira-based).
#
# IDEMPOTENT + MANUAL-SAFE: each sync deletes ONLY the event's auto_synced rows and recreates them;
# rows added or edited by an admin (auto_synced=False) are never touched, so manual entries act as
# evaluation-only additions/overrides exactly as before.
#
# CALLED FROM: maybe_autocomplete_event + the complete-event path (event just finished) and the
# rankings prize list's on-read sweep (admin_prize.tournament_prizes_list) so already-completed
# events backfill the moment the page is opened. Prize points then flow into the evaluation through
# the existing EventPrizePayout -> recalc pipeline.

import re
from collections import defaultdict
from decimal import Decimal

from .models import Event, EventPrizePayout


def _amount_ngn(raw, currency, rate_map):
    """Parse a prize_distribution value ("300000", "$2,000", "2000 Diamonds"...) and convert the
    event's currency to NGN. Unparseable/zero -> None (skip)."""
    digits = re.sub(r"[^0-9.]", "", str(raw or ""))
    try:
        amt = Decimal(digits)
    except Exception:
        return None
    if amt <= 0:
        return None
    ccy = (currency or "USD").upper()
    if ccy == "NGN":
        return amt
    # rate = units per 1 USD. NGN amount = amt / rate[ccy] * rate[NGN].
    ngn_rate = rate_map.get("NGN")
    src_rate = Decimal("1") if ccy == "USD" else rate_map.get(ccy)
    if not ngn_rate or not src_rate:
        return amt  # no FX data: keep the raw number rather than dropping the payout
    return (amt / Decimal(src_rate)) * Decimal(ngn_rate)


def _award_pool(dist, ordered_ids, currency, rate_map, totals):
    """Map ONE prize_distribution ({position: amount}) onto an ordered team list, accumulating the
    NGN amount per team into `totals` ({tournament_team_id: Decimal}). position N (1-based) pays the
    Nth team in `ordered_ids`. Unparseable positions/amounts and out-of-range positions are skipped.
    Shared by the event pool + every stage/group pool so a team that places in several of them has its
    winnings SUMMED (owner 2026-07-14: "combine all money the team earned across all stages/groups")."""
    for pos_str, raw_amount in (dist or {}).items():
        try:
            pos = int(re.sub(r"[^0-9]", "", str(pos_str)) or 0)
        except ValueError:
            continue
        if pos < 1 or pos > len(ordered_ids):
            continue
        amount = _amount_ngn(raw_amount, currency, rate_map)
        if amount is None:
            continue
        totals[ordered_ids[pos - 1]] += amount


def compute_event_prize_totals(event):
    """PURE (no writes): the NGN winnings each team SHOULD get, per the current rule.

    Owner rule (2026-07-14): a team's prize is the SUM of every prize it earned across the event —
    the event-level pool PLUS any per-stage and per-group pools. Each pool is mapped onto the
    standings it belongs to:
      - EVENT pool  -> the event's FINAL standings (rank by LAST STAGE PLAYED; the same table the
                       team profile shows as "Final placement", so Nth place == Nth-place prize).
      - STAGE pool  -> that stage's cumulative standings.
      - GROUP pool  -> that group's standings.
    Returns {tournament_team_id: Decimal(ngn, 2dp)} for POSITIVE winnings only. Used by
    sync_event_prize_payouts (to write) and by event_prize_is_stale (to compare vs what is stored).
    (Today all events keep the whole pool at the event level; the stage/group loops are inert until an
    organizer sets a per-stage/group prize.)
    """
    from .final_standings import event_final_standings
    from .round_robin import cumulative_standings, group_standings
    from .models import Stages, StageGroups

    currency = getattr(event, "prize_currency", "USD")
    from afc_auth.models import FxRate
    rate_map = {f.currency: f.rate for f in FxRate.objects.all()}

    # tournament_team_id -> summed NGN winnings across every pool.
    totals = defaultdict(lambda: Decimal("0"))

    # (1) EVENT pool -> FINAL standings (last stage played). This is the deciding leaderboard.
    ordered, _rank_by_tt, _reached, _final_stage = event_final_standings(event)
    final_ids = [row["tournament_team_id"] for row in ordered]
    _award_pool(event.prize_distribution, final_ids, currency, rate_map, totals)

    # (2) Per-STAGE pools -> that stage's standings. (3) Per-GROUP pools -> that group's standings.
    for stage in Stages.objects.filter(event=event):
        if getattr(stage, "prize_distribution", None):
            _award_pool(
                stage.prize_distribution,
                [r["tournament_team_id"] for r in cumulative_standings(stage)],
                currency, rate_map, totals,
            )
        for grp in StageGroups.objects.filter(stage=stage):
            if getattr(grp, "prize_distribution", None):
                _award_pool(
                    grp.prize_distribution,
                    [r["tournament_team_id"] for r in group_standings(grp)],
                    currency, rate_map, totals,
                )
    return {tt: amt.quantize(Decimal("0.01")) for tt, amt in totals.items() if amt and amt > 0}


def event_prize_is_stale(event):
    """True when re-syncing WOULD change the event's auto payouts: the stored auto rows differ from
    what compute_event_prize_totals() now says. Lets the on-read self-heal + the sweep CORRECT payouts
    left over from an earlier ranking rule (the cumulative -> last-stage-played change, 2026-07-14)
    instead of skipping any event that already has auto rows. One standings computation; cheap enough
    for a gated read path."""
    expected = compute_event_prize_totals(event)
    stored = {
        p.tournament_team_id: p.amount
        for p in EventPrizePayout.objects.filter(event=event, auto_synced=True)
    }
    return expected != stored


def sync_event_prize_payouts(event):
    """Rewrite the event's AUTO payouts to compute_event_prize_totals(). Returns the row count.

    Idempotent + manual-safe: deletes ONLY auto_synced rows (manual entries untouched) and recreates
    them, so a re-sync after the ranking rule changed cleanly overwrites a stale amount/team.
    """
    totals = compute_event_prize_totals(event)

    # Per-player prize distribution (owner 2026-06-15 feature): a TEAM payout must also write one
    # PlayerWinning row per active roster member, so the prize shows on each player's OWN profile
    # (afc_player stats -> tournament_winnings), not only in the team's total_earnings. We reuse the
    # same helper manual entry uses so auto + manual behave identically. Lazy import: admin_prize
    # imports THIS module (its on-read sweep), so importing it at module load would cycle.
    from afc_rankings.admin_prize import _distribute_payout

    # Replace ONLY the previous auto rows (manual rows untouched). PlayerWinning.payout is CASCADE, so
    # deleting an auto payout also drops its per-player shares -> the recreation below is a clean,
    # idempotent rewrite (no double-counting on re-sync). Done even when totals is empty so a now-invalid
    # auto payout (e.g. after the finals were re-scored) is cleared.
    EventPrizePayout.objects.filter(event=event, auto_synced=True).delete()
    created = 0
    for tt_id, amount in totals.items():
        payout = EventPrizePayout.objects.create(
            event=event,
            tournament_team_id=tt_id,
            amount=amount,
            auto_synced=True,
        )
        # Split this team payout across its active roster -> PlayerWinning rows. Idempotent (keyed on
        # payout) and failure-safe (a distribution hiccup can never block the payout the sync created).
        _distribute_payout(payout)
        created += 1
    return created


def sync_completed_events(seasons_start=None, seasons_end=None):
    """On-read sweep for the rankings prize page: sync every EFFECTIVELY-completed event (optionally
    inside the season window) that has a prize_distribution but no auto rows yet. Cheap when idle.

    EFFECTIVE, not raw event_status (owner 2026-07-14 bug — DYNASTY CUP GRAND FINALS SSA): the
    ongoing->completed auto-complete sweep is NOT scheduled in the live celery beat, so a finished
    event can sit at raw event_status "ongoing"/"upcoming" long after its end instant while every
    badge/leaderboard already reads it as "completed" via effective_event_status (read-time
    derivation). Gating this sweep on raw event_status="completed" silently skipped those events, so
    their prize pool never attributed to the winners (the earnings panel showed "-"). Same
    raw-vs-effective gap already fixed for roster locks + the registered-events list.

    effective_event_status is a read-time function, so it can't be a SQL filter. We widen the DB
    candidate set to raw-completed OR past-end_date, then confirm effective status in Python (which
    also correctly excludes cancelled + manually-reopened `auto_complete_suppressed` events, and
    events whose end DATE has passed but end TIME has not)."""
    import datetime
    from django.db.models import Q
    # Lazy import: prize_sync is imported BY views.py (complete-event path), so importing views at
    # module load would cycle. Local import breaks the cycle (same pattern as the roster-lock fix).
    from .views import effective_event_status

    today = datetime.date.today()
    qs = (Event.objects
          .exclude(prize_distribution={})
          .filter(Q(event_status="completed") | Q(end_date__lte=today)))
    if seasons_start:
        qs = qs.filter(end_date__gte=seasons_start)
    if seasons_end:
        qs = qs.filter(end_date__lte=seasons_end)
    total = 0
    for ev in qs:
        # Confirm the event is genuinely finished by the SAME rule every other surface uses. Skips
        # past-end-date events whose end time has not arrived, plus cancelled / reopened events.
        if effective_event_status(ev) != "completed":
            continue
        if EventPrizePayout.objects.filter(event=ev, auto_synced=True).exists():
            continue
        try:
            total += sync_event_prize_payouts(ev)
        except Exception:
            continue  # one broken event must not block the page
    return total
