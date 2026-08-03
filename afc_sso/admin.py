# Interim management surface for partner SSO apps until the styled screens land in the
# Next.js (a) admin area. Deliberately shows the toggles as a plain checkbox list so an
# AFC admin can see, at a glance, everything an org is permitted to receive.
#
# This is where an AFC staff member creates a partner: fill in the org's name and redirect
# URI, tick only the data that org is approved for, save, then hand them the generated
# client_id and client_secret. What they can then actually read is enforced in
# afc_sso/claims.py, not here.
from django.contrib import admin

from .models import AFCSSOApplication, SSO_FIELD_TOGGLES

# django-oauth-toolkit registers its OWN admin for whatever model
# OAUTH2_PROVIDER_APPLICATION_MODEL points at, which is ours. `oauth2_provider` is listed
# before `afc_sso` in INSTALLED_APPS, so its registration always wins the race and this
# has to take it back. The try/except keeps import order from being load bearing.
try:
    admin.site.unregister(AFCSSOApplication)
except admin.sites.NotRegistered:
    pass


@admin.register(AFCSSOApplication)
class AFCSSOApplicationAdmin(admin.ModelAdmin):
    list_display = ("display_name", "name", "status", "client_id")
    list_filter = ("status",) + SSO_FIELD_TOGGLES
    search_fields = ("name", "display_name", "client_id")
    readonly_fields = ("client_id",)
    # skip_authorization would let an org bypass the player's consent screen entirely.
    # AFCSSOApplication.save() pins it off, and hiding it here stops an admin trying to
    # tick it and assuming it worked.
    exclude = ("skip_authorization",)
