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
# every team/player score derived from that event. Scores are NOT recomputed here (the recalc layer
# is async by project rule); run `manage.py recalc_rankings` afterwards, or let the next result edit
# trigger it through afc_rankings.signals.
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

        changed, pinned, unchanged = [], 0, 0
        for ev in qs:
            # A head/super admin pinned this tier - the rules must not overwrite it (same contract
            # apply_event_tier enforces on every edit).
            if ev.tier_overridden:
                pinned += 1
                continue
            new_tier = auto_classify_event(ev)
            if new_tier == ev.tournament_tier:
                unchanged += 1
                continue
            changed.append((ev, new_tier))
            self.stdout.write(
                f"  ev{ev.event_id} {ev.event_name!r}  "
                # ASCII "NGN" not the naira sign: this prints to a Windows console (cp1252) where
                # the symbol raises UnicodeEncodeError and kills the command.
                f"pool={ev.prizepool_cash_value} {ev.prize_currency} (= NGN {_prize_pool_ngn(ev):,})  "
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
                "Scores derived from these events are now stale. Run `manage.py recalc_rankings` "
                "to rebuild the team/player scores with the new tier multipliers."
            )
