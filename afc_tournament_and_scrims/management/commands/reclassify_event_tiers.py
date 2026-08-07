# ─────────────────────────────────────────────────────────────────────────────
# reclassify_event_tiers - re-run the automatic tournament-tier classifier over EXISTING events.
#
# WHY: views.auto_classify_event only runs on event CREATE / EDIT, so an event's tournament_tier is
# frozen at whatever the rules said the last time somebody saved it. Two things make a stored tier
# go stale: an admin editing the EventTierRule set on the Tournament Tiers page, and the
# currency-conversion fix (owner 2026-08-03) that now compares the prize pool in NAIRA instead of
# in the event's own prize_currency. Before that fix a $400 event was compared as the bare number
# 400 against the ₦100,000 Tier-1 threshold, matched nothing, and fell through to Tier 3 - which is
# how DYNASTY CUP GRAND FINALS SSA (event 172) ended up tier_3. Editing every event by hand to
# refresh the tier is not realistic, hence this command.
#
# WHAT: recomputes the tier for every non-draft event via the SAME auto_classify_event the create /
# edit path uses (one classifier, no second implementation to drift). Events pinned by a head/super
# admin (tier_overridden=True) are NEVER touched - a manual decision outranks the rules, exactly as
# apply_event_tier treats it. Idempotent: re-running changes nothing once the tiers are current.
#
# The tier this writes is what afc_rankings.aggregation feeds the scoring engine as
# TournamentInput.tier (tier_1 = 2.0x, tier_2 = 1.5x, tier_3 = 1.0x), so a changed tier changes
# every TEAM score derived from that event. Since 2026-08-06 the recalculation is automatic: the
# `ev.save(update_fields=["tournament_tier"])` below is picked up by afc_rankings.signals
# .on_event_tier_change, which enqueues a recalc for every team in the event, for the months the
# event was actually played in. Nothing further is needed for the team ladders.
#
# TWO CAVEATS on a large --apply run. (1) Each changed event enqueues one recalc per registered
# team onto the rankings_recalc queue, so re-tiering hundreds of events at once produces a large
# burst - fine for an idempotent, debounced queue, but confirm a worker is actually draining it
# (deploy/systemd/celery-rankings.service) or the work simply piles up. (2) PLAYER scores are not
# affected at all: the player engine has no tier factor (scoring/engine._player_components), so
# there is nothing to recompute for them. `manage.py recalc_rankings` remains available as a
# belt-and-braces full rebuild.
#
# RUN:  python manage.py reclassify_event_tiers            # preview only, writes nothing
#       python manage.py reclassify_event_tiers --apply    # write the new tiers
#       python manage.py reclassify_event_tiers --apply --event-id 172
# ─────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.views import auto_classify_event, _prize_pool_ngn


class Command(BaseCommand):
    help = "Re-run the EventTierRule classifier over existing events (skips head/super-admin pinned tiers)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the recomputed tiers. Without this the command only previews.")
        parser.add_argument("--event-id", type=int, default=None,
                            help="Reclassify a single event instead of all of them.")
        parser.add_argument("--include-drafts", action="store_true",
                            help="Also reclassify draft events (skipped by default: they are not live yet).")

    def handle(self, *args, **opts):
        qs = Event.objects.all().order_by("event_id")
        if opts["event_id"]:
            qs = qs.filter(event_id=opts["event_id"])
        if not opts["include_drafts"]:
            qs = qs.filter(is_draft=False)

        # ONE FxRate read for the whole sweep (2026-08-07). Both sides of the comparison need it -
        # the event's pool AND, since tier-rule thresholds can be authored in any currency, the
        # rule's threshold. Before this, _prize_pool_ngn built its own map per event, so a run over
        # every event re-read the whole 166-row FxRate table once per event, twice per changed one.
        # Building it here also means every event in a run is classified at the SAME rate, so a
        # sweep cannot straddle an FX refresh and tier two identical events differently.
        from afc_rankings.admin_tournament_tiers import _fx_rate_map
        rate_map = _fx_rate_map()

        changed, pinned, unchanged = [], 0, 0
        for ev in qs:
            # A head/super admin pinned this tier - the rules must not overwrite it (same contract
            # apply_event_tier enforces on every edit).
            if ev.tier_overridden:
                pinned += 1
                continue
            new_tier = auto_classify_event(ev, rate_map)
            if new_tier == ev.tournament_tier:
                unchanged += 1
                continue
            changed.append((ev, new_tier))
            self.stdout.write(
                f"  ev{ev.event_id} {ev.event_name!r}  "
                # ASCII "NGN" not the naira sign: this prints to a Windows console (cp1252) where
                # the symbol raises UnicodeEncodeError and kills the command.
                f"pool={ev.prizepool_cash_value} {ev.prize_currency} "
                f"(= NGN {_prize_pool_ngn(ev, rate_map):,})  "
                f"{ev.tournament_tier} -> {new_tier}"
            )

        self.stdout.write(
            f"Scanned {qs.count()} event(s): {len(changed)} to change, "
            f"{unchanged} already correct, {pinned} pinned by an admin override."
        )
        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("Preview only. Re-run with --apply to write."))
            return

        for ev, new_tier in changed:
            ev.tournament_tier = new_tier
            ev.save(update_fields=["tournament_tier"])
        self.stdout.write(self.style.SUCCESS(f"Updated {len(changed)} event tier(s)."))
        if changed:
            self.stdout.write(
                "Team scores for these events are being recalculated automatically "
                "(afc_rankings.signals.on_event_tier_change). Confirm a worker is draining the "
                "rankings_recalc queue, or run `manage.py recalc_rankings` to rebuild in-process."
            )
