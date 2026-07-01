# ─────────────────────────────────────────────────────────────────────────────
# backfill_prize_currency — one-off fix (owner 2026-07-01).
#
# WHY: Event.prize_currency defaulted to "NGN" and create_event/edit_event never set it, so EVERY
# existing event is tagged NGN even though AFC enters prize pools in USD (the platform's base
# currency). get_total_prize_pool then divided those USD amounts by the NGN rate (~1375), so a $1750
# pool showed as ~$1.27 and the home "Total Prize Pool" read £1.18. The model default is now "USD"
# and create/edit set it explicitly; this command fixes the ALREADY-STORED rows.
#
# WHAT: flips every event whose prize_currency is the legacy default "NGN" to "USD". No event ever
# explicitly chose NGN (the create endpoint never wrote the field), so every "NGN" is the wrong
# default and is safe to convert. Idempotent. Use --dry-run to preview, --keep-ngn <id,id> to
# exclude specific events that really are NGN.
#
# RUN (prod, once, after deploy):  python manage.py backfill_prize_currency
# CONNECTS TO: get_total_prize_pool (home total) + the <Money from={prize_currency}> displays.
# ─────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand
from afc_tournament_and_scrims.models import Event


class Command(BaseCommand):
    help = "Set prize_currency='USD' on events left at the legacy 'NGN' default (AFC enters prizes in USD)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing.")
        parser.add_argument("--keep-ngn", default="", help="Comma-separated event_ids to LEAVE as NGN.")

    def handle(self, *args, **opts):
        keep = {int(x) for x in str(opts["keep_ngn"]).split(",") if x.strip().isdigit()}
        qs = Event.objects.filter(prize_currency__iexact="NGN").exclude(event_id__in=keep)
        total = qs.count()
        self.stdout.write(f"Events tagged NGN (excluding {len(keep)} kept): {total}")
        for e in qs.order_by("event_id"):
            self.stdout.write(f"  ev{e.event_id} {e.event_name!r} prizepool={e.prizepool!r} "
                              f"cash={e.prizepool_cash_value}  NGN -> USD")
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no changes written."))
            return
        updated = qs.update(prize_currency="USD")
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} event(s) to prize_currency='USD'."))
