from celery import shared_task
from django.utils.timezone import now
from afc_auth.models import BannedPlayer, TeamBan

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
