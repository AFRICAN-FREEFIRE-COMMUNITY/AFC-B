import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")

app = Celery("afc")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'check_expired_player_bans_every_hour': {
        'task': 'afc_auth.tasks.lift_expired_bans',
        'schedule': crontab(minute=0, hour='*'),  # Runs every hour
    },
    'check_expired_team_bans_every_hour': {
        'task': 'afc_auth.tasks.check_and_lift_team_bans',
        'schedule': crontab(minute=0, hour='*'),  # Runs every hour
    },
    "update-event-stage-statuses-hourly": {
        "task": "afc_tournament_and_scrims.tasks.update_event_and_stage_statuses",
        "schedule": crontab(minute=0),  # every hour
    }
}
