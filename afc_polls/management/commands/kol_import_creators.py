"""
manage.py kol_import_creators <file.json> --edition <slug>

Loads the creators of Garena's KOL List into a KOL edition for the nomination round (inbox #633 / #634).

THE FILE is made on a workstation from the private master sheet by tools/kol_sheet_to_json.py: a JSON list of
{name, tiktok_handle, country, status, tiktok_id}. Any other key (an email, a game UID) is IGNORED here. Emails and
WhatsApp numbers have their own file and command since 27 Nov 2026 (#644 / #646): kol_import_contacts.

Upserts by TikTok handle (lower case, "@" dropped), so running it twice changes nothing and a corrected sheet
updates names, countries and statuses in place. A write-in (a person a creator added, #637) whose handle is on the
sheet becomes a list creator. It never deletes and never switches anybody on or off: that is the admin tab's job.
When the edition already has its round, every new creator also gets an option in each award.

CONNECTS TO: afc_polls.models.KolCreator, afc_polls.kol_nominations.ensure_round.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from afc_polls.kol_nominations import ensure_round
from afc_polls.models import AwardsEdition, KolCreator, KolNominationRound

FIELDS = {"name": 200, "country": 80, "status": 20, "tiktok_id": 40}


def clean_handle(raw):
    return str(raw or "").strip().lstrip("@").strip().lower()[:100]


class Command(BaseCommand):
    help = "Load KOL creators (name, TikTok handle, country, status, TT ID) from a JSON file into an edition."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--edition", required=True, help="the KOL edition's slug")

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
            raise CommandError("The file must hold a JSON list of creators")

        created = updated = unchanged = skipped = 0
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            handle_ = clean_handle(row.get("tiktok_handle"))
            name = str(row.get("name") or "").strip()[:FIELDS["name"]]
            if not handle_ or not name:
                skipped += 1
                continue
            values = {
                "name": name,
                "country": str(row.get("country") or "").strip()[:FIELDS["country"]],
                "sheet_status": str(row.get("status") or "").strip()[:FIELDS["status"]],
                "tiktok_id": str(row.get("tiktok_id") or "").strip()[:FIELDS["tiktok_id"]],
                # Somebody a creator added by hand (a write-in, #637) who turns out to be on the sheet becomes a
                # creator of the list: a voter too, codes and all.
                "origin": KolCreator.LIST,
            }
            creator = KolCreator.objects.filter(edition=edition, tiktok_handle=handle_).first()
            if creator is None:
                KolCreator.objects.create(edition=edition, tiktok_handle=handle_, **values)
                created += 1
            elif any(getattr(creator, key) != value for key, value in values.items()):
                KolCreator.objects.filter(pk=creator.pk).update(**values)
                updated += 1
            else:
                unchanged += 1

        if KolNominationRound.objects.filter(edition=edition).exists():
            ensure_round(edition)
        self.stdout.write(f"{created} created, {updated} updated, {unchanged} unchanged, {skipped} skipped")
