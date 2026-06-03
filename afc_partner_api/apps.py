# App config for afc_partner_api — the read-only, versioned Partner Data API.
# AFC-provisioned partners pull completed/published per-event data through scoped,
# toggle-gated endpoints. See WEBSITE/tasks/partner-api-design.md.
from django.apps import AppConfig


class AfcPartnerApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_partner_api"
