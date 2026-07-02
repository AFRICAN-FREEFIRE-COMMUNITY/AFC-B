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

    # Replace ONLY the previous auto rows (manual rows untouched).
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
        EventPrizePayout.objects.create(
            event=event,
            tournament_team_id=standings[pos - 1]["tournament_team_id"],
            amount=amount.quantize(Decimal("0.01")),
            auto_synced=True,
        )
        created += 1
    return created


def sync_completed_events(seasons_start=None, seasons_end=None):
    """On-read sweep for the rankings prize page: sync every COMPLETED event (optionally inside the
    season window) that has a prize_distribution but no auto rows yet. Cheap when idle."""
    qs = Event.objects.filter(event_status="completed").exclude(prize_distribution={})
    if seasons_start:
        qs = qs.filter(end_date__gte=seasons_start)
    if seasons_end:
        qs = qs.filter(end_date__lte=seasons_end)
    total = 0
    for ev in qs:
        if EventPrizePayout.objects.filter(event=ev, auto_synced=True).exists():
            continue
        try:
            total += sync_event_prize_payouts(ev)
        except Exception:
            continue  # one broken event must not block the page
    return total
