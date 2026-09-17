"""
check_esport_images - look at every stored esport image and record what the picture check saw
(owner 2026-09-13).

WHY IT EXISTS
    The check only ran at upload time, and between 2026-07-06 and 2026-09-13 its verdict was
    thrown away (logged, never stored). So the images already on the site carry no verdict at all.
    This walks them once and fills that in, which is what puts the existing backlog into the
    per-event media-audit queue instead of leaving it invisible.

WHAT IT NEVER DOES
    It does not delete, replace or hide anybody's picture, and it does not touch a profile a human
    has already settled ("cleared"). It only writes UserProfile.esports_pic_check /
    esports_pic_checked_at.

USAGE
    python manage.py check_esport_images --dry-run     count only, write nothing
    python manage.py check_esport_images               record the verdicts
    python manage.py check_esport_images --recheck     re-check rows that already have a verdict
                                                       (skips "cleared" either way)
    python manage.py check_esport_images --limit 100   stop after N rows, for a first look

HOW IT CONNECTS
    afc_auth/face_check.py does the looking; afc_tournament_and_scrims/views_media_audit.py reads
    the verdict back per event; the same verdict is written at upload time by
    afc_auth.views.upload_esport_image.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_auth.face_check import check_esport_image
from afc_auth.models import UserProfile

# A human said this picture is fine. Nothing automatic overrules that.
CLEARED = "cleared"


class Command(BaseCommand):
    help = "Record what the picture check sees in every stored esport image (never deletes anything)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
        parser.add_argument("--recheck", action="store_true", help="also re-check rows that already have a verdict")
        parser.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = UserProfile.objects.exclude(esports_pic="").exclude(esports_pic__isnull=True)
        if not options["recheck"]:
            qs = qs.filter(esports_pic_check="")
        qs = qs.exclude(esports_pic_check=CLEARED).order_by("profile_id")
        if options["limit"]:
            qs = qs[: options["limit"]]

        counts = {}
        unreadable = 0
        now = timezone.now()
        for profile in qs.iterator(chunk_size=200):
            try:
                with profile.esports_pic.open("rb") as fh:
                    verdict = check_esport_image(fh)["verdict"]
            except Exception:
                # The row points at a file that is not on disk any more, or storage refused it.
                # That is a storage problem, not a picture problem, so it is counted and skipped
                # rather than recorded as a verdict about the image.
                unreadable += 1
                continue
            counts[verdict] = counts.get(verdict, 0) + 1
            if not dry_run:
                profile.esports_pic_check = verdict
                profile.esports_pic_checked_at = now
                profile.save(update_fields=["esports_pic_check", "esports_pic_checked_at"])

        total = sum(counts.values())
        self.stdout.write(f"{'would record' if dry_run else 'recorded'} {total} verdicts")
        for verdict in sorted(counts):
            self.stdout.write(f"  {verdict:<16} {counts[verdict]}")
        flagged = counts.get("no_face", 0) + counts.get("face_too_small", 0)
        self.stdout.write(f"  -> {flagged} for a human to look at on their event's media audit")
        if unreadable:
            self.stdout.write(f"  {unreadable} rows point at a file that could not be read (skipped)")
