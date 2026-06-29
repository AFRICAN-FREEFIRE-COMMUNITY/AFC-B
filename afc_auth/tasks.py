from celery import shared_task
from django.utils.timezone import now
from afc_auth.models import BannedPlayer, News, TeamBan

@shared_task
def lift_expired_bans():
    """Check for expired player bans and lift them automatically."""
    expired_bans = BannedPlayer.objects.filter(is_active=True, ban_end_date__lte=now())
    for ban in expired_bans:
        ban.lift_ban()

@shared_task
def check_and_lift_team_bans():
    """Check for expired team bans and lift them automatically."""
    expired_bans = TeamBan.objects.filter(ban_end_date__lt=now())
    for ban in expired_bans:
        ban.lift_ban_if_expired()


@shared_task
def publish_scheduled_news():
    """Auto-release any News whose scheduled publish time has arrived.

    The "schedule publish" half of the News timer feature (see afc_auth.models.News for the
    field-level contract). Each run finds every news item that is still HIDDEN
    (is_published=False) and DUE (scheduled_publish_at is set and <= now), and flips it live.

    Design choices:
      • Timezone: now() is timezone-aware (settings.USE_TZ is True) and scheduled_publish_at is
        stored UTC-aware, so the <= comparison is a correct point-in-time check regardless of the
        admin's local timezone.
      • We KEEP scheduled_publish_at after publishing (we do NOT clear it) so the admin list can keep
        showing "went live at <time>"; is_published alone is the public-visibility gate.
      • A single bulk .update() flips all due rows atomically and efficiently. It deliberately
        bypasses News.save() - the slug already exists for every persisted row (so the slug
        back-fill is a no-op here) and we do NOT want to bump updated_at just for the auto-release.

    Scheduled in afc/celery_config.py (beat_schedule 'publish_scheduled_news_every_minute', runs
    every minute). Discovered via app.autodiscover_tasks(). Returns the number of items published.
    """
    due = News.objects.filter(
        is_published=False,
        scheduled_publish_at__isnull=False,
        scheduled_publish_at__lte=now(),
    )
    return due.update(is_published=True)
