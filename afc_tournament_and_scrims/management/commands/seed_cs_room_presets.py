"""
Seed the six built-in Clash Squad room presets (owner 2026-08-12).

WHAT IT DOES: turns cs_room_catalogue.PRESET_MODES - Free Fire's own one-tap modes (Random
Store, Competitive Store, Crazy Store, Hardcore Mode, CS Elite, Esports Mode) - into AFC-global
CSRoomPreset rows, so an organizer opening the room-settings editor finds sensible starting
points instead of a blank form.

WHY A COMMAND AND NOT A DATA MIGRATION: the catalogue changes every Garena patch. A migration
would freeze the values at the moment it was written and could never be re-run; this is
IDEMPOTENT (update_or_create on the name) so re-running it after a catalogue edit refreshes the
built-ins in place without touching anybody's own presets.

RUN: python manage.py seed_cs_room_presets
Deploy note: run once after migrating, and again whenever cs_room_catalogue.PRESET_MODES changes.
"""
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims import cs_room
from afc_tournament_and_scrims import cs_room_catalogue as cat
from afc_tournament_and_scrims.models import CSRoomPreset


class Command(BaseCommand):
    help = "Create or refresh the built-in AFC-global Clash Squad room presets."

    def handle(self, *args, **options):
        created_count = 0
        updated_count = 0

        for key, mode in cat.PRESET_MODES.items():
            # Each built-in mode is a PARTIAL patch over a fresh room, exactly as the in-game
            # button behaves, so apply it to a blank config to get the full document.
            values = cs_room.apply_builtin_mode(key)
            values["preset_key"] = key  # the machine key, so the FE can match its own labels

            _preset, created = CSRoomPreset.objects.update_or_create(
                organization=None,
                name=mode["label"],
                defaults={
                    **values,
                    "description": mode["description"],
                    "is_builtin": True,
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
            self.stdout.write(f"  {'created' if created else 'refreshed'}: {mode['label']}")

        self.stdout.write(self.style.SUCCESS(
            f"Built-in CS room presets: {created_count} created, {updated_count} refreshed."))
