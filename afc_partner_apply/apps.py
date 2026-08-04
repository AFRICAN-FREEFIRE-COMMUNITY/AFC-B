"""App config for the public partner application queue (owner 2026-08-04).

No ready() hook: unlike afc_sso this app registers no signal receivers. Everything it does
happens inside a request, and the emails it sends go out on daemon threads from
afc_partner_apply/emails.py.
"""
from django.apps import AppConfig


class AfcPartnerApplyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_partner_apply"
    verbose_name = "Partner applications"
