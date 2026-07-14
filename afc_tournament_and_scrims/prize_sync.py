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
from decimal import Decimal

from .models import Event, EventPrizePayout, TournamentTeamMatchStats


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


def sync_event_prize_payouts(event):
    """Derive the event's auto payouts. Returns the number of rows created (0 = nothing to do)."""
    dist = event.prize_distribution or {}
    if not dist:
        return 0
    # Final overall standings, event-wide, with the event's configured tie-breakers applied.
    from .round_robin import _aggregate_team_standings
    qs = TournamentTeamMatchStats.objects.filter(match__group__stage__event=event)
    standings = _aggregate_team_standings(qs, event=event)
    if not standings:
        return 0

    from afc_auth.models import FxRate
    rate_map = {f.currency: f.rate for f in FxRate.objects.all()}

    # Per-player prize distribution (owner 2026-06-15 feature): a TEAM payout must also write one
    # PlayerWinning row per active roster member, so the prize shows on each player's OWN profile
    # (afc_player stats -> tournament_winnings), not only in the team's total_earnings.
    # admin_prize.prize_create does this for MANUAL entries, but the auto-sync path here never did,
    # so every player profile showed 0 winnings for prize-pool events even after the team page showed
    # the prize (bug found alongside the DYNASTY GRAND FINALS attribution fix, 2026-07-14). We reuse
    # the same helper so auto + manual behave identically. Lazy import: admin_prize imports THIS module
    # (its on-read sweep), so importing it at module load would cycle.
    from afc_rankings.admin_prize import _distribute_payout

    # Replace ONLY the previous auto rows (manual rows untouched). PlayerWinning.payout is CASCADE, so
    # deleting an auto payout also drops its per-player shares -> the recreation below is a clean,
    # idempotent rewrite (no double-counting on re-sync).
    EventPrizePayout.objects.filter(event=event, auto_synced=True).delete()
    created = 0
    for pos_str, raw_amount in dist.items():
        try:
            pos = int(re.sub(r"[^0-9]", "", str(pos_str)) or 0)
        except ValueError:
            continue
        if pos < 1 or pos > len(standings):
            continue
        amount = _amount_ngn(raw_amount, getattr(event, "prize_currency", "USD"), rate_map)
        if amount is None:
            continue
        payout = EventPrizePayout.objects.create(
            event=event,
            tournament_team_id=standings[pos - 1]["tournament_team_id"],
            amount=amount.quantize(Decimal("0.01")),
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
