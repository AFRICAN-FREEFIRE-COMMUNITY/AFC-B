"""Award the top-referrer prizes of every program that has ended and not been awarded yet.

    python manage.py award_referral_ranks

Safe to run as often as you like (engine.award_ranks is idempotent); meant for a daily cron beside the
other maintenance commands. Admins can also press "Award leaderboard prizes" on the program page.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_referrals import engine
from afc_referrals.models import ReferralProgram


class Command(BaseCommand):
    help = "Award leaderboard prizes for referral programs that have ended."

    def handle(self, *args, **options):
        now = timezone.now()
        for program in ReferralProgram.objects.filter(is_published=True, ends_at__lt=now, ranks_awarded_at__isnull=True):
            given = engine.award_ranks(program, now=now)
            self.stdout.write(f"{program.slug}: {len(given)} leaderboard prizes awarded")
