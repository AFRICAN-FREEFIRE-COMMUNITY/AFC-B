"""
manage.py kol_import_contacts <file.json> --edition <slug> [--dry-run]

Loads the KOL creators' emails (and WhatsApp numbers, when a file has them) into a KOL edition (owner, 27 Nov 2026,
inbox #646: "send an email to the KOLs which we already have their mails"; #644: a field for email and WhatsApp).

THE FILE is made on a workstation from Garena's private master sheet by `tools/kol_sheet_to_json.py --contacts`: a
JSON list of {tiktok_handle, email?, whatsapp?}. It lives only in the git-ignored private folder and, on the server,
only for the minutes this command runs (copy it up, run, delete it).

NEVER OVERWRITES: a creator who already has an email (typed by an admin, or given by the creator on the ballot) keeps
it; the same for WhatsApp. So running it twice changes nothing, and a creator's own answer always wins over the sheet.
Values go through the ballot's own validation (kol_nominations.clean_email / clean_whatsapp); a bad one is counted and
skipped. A handle that is not in the round is counted, never created: creators come from kol_import_creators.
Prints counts only, never an address.

CONNECTS TO: afc_polls.models.KolCreator (email, whatsapp), afc_polls.kol_nominations (validation), the admin tab's
Contact column and "Email" buttons (afc_polls.kol_emails).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from afc_polls.kol_nominations import clean_email, clean_whatsapp
from afc_polls.models import AwardsEdition, KolCreator

from .kol_import_creators import clean_handle


class Command(BaseCommand):
    help = "Load KOL creators' emails and WhatsApp numbers from a JSON file, never overwriting one already set."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--edition", required=True, help="the KOL edition's slug")
        parser.add_argument("--dry-run", action="store_true", help="count what would change, write nothing")

    def handle(self, *args, **options):
        edition = AwardsEdition.objects.filter(slug=options["edition"]).first()
        if edition is None:
            raise CommandError(f"No awards edition with the slug {options['edition']!r}")
        try:
            with open(options["path"], encoding="utf-8") as handle:
                rows = json.load(handle)
        except (OSError, ValueError) as error:
            raise CommandError(f"Cannot read {options['path']}: {error}")
        if not isinstance(rows, list):
            raise CommandError("The file must hold a JSON list of {tiktok_handle, email, whatsapp}")

        creators = {creator.tiktok_handle: creator for creator in KolCreator.objects.filter(edition=edition)}
        counts = {"emails set": 0, "whatsapp set": 0, "kept (already set)": 0, "bad values": 0,
                  "not in the round": 0, "skipped rows": 0}
        for row in rows:
            if not isinstance(row, dict) or not clean_handle(row.get("tiktok_handle")):
                counts["skipped rows"] += 1
                continue
            creator = creators.get(clean_handle(row.get("tiktok_handle")))
            if creator is None:
                counts["not in the round"] += 1
                continue
            fields = []
            for key, clean, label in (("email", clean_email, "emails set"),
                                      ("whatsapp", clean_whatsapp, "whatsapp set")):
                value, problem = clean(row.get(key))
                if problem:
                    counts["bad values"] += 1
                elif value and getattr(creator, key):
                    counts["kept (already set)"] += 1
                elif value:
                    setattr(creator, key, value)
                    fields.append(key)
                    counts[label] += 1
            if fields and not options["dry_run"]:
                creator.save(update_fields=fields)

        prefix = "DRY RUN, nothing written: " if options["dry_run"] else ""
        self.stdout.write(prefix + ", ".join(f"{value} {key}" for key, value in counts.items()))
