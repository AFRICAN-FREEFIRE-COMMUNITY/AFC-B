# App config for afc_organizers — the Organizer/Organization feature (multi-tenant layer
# over the existing tournament engine). See WEBSITE/tasks/organizers-design.md.
from django.apps import AppConfig


class AfcOrganizersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_organizers"
