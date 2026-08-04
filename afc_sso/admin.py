# Django-admin fallback for partner SSO apps. NO LONGER THE PLACE STAFF WORK.
#
# The real management surface is now the "Sign in with AFC" tab of the admin API Keys page
# (frontend/app/(a)/a/partners), driven by afc_sso/admin_api.py. Use that: it shows each
# toggle beside the exact sentence the player reads on the consent screen, and it is the
# only surface that can show a client secret at all (see below).
#
# This screen is kept for superuser break-glass access and for inspecting rows during
# debugging. One thing it CANNOT do, and never could: reveal a client secret.
# django-oauth-toolkit hashes client_secret on save, so the plaintext only ever exists in
# the moment it is generated - which is why issuing and rotating secrets lives in
# admin_api.py, where it is returned to the admin exactly once.
#
# What a partner can actually read is enforced in afc_sso/claims.py, not here.
from django import forms
from django.contrib import admin

from .models import AFCSSOApplication, SSO_FIELD_TOGGLES
from .provisioning import _clean_outbound_url

# django-oauth-toolkit registers its OWN admin for whatever model
# OAUTH2_PROVIDER_APPLICATION_MODEL points at, which is ours. `oauth2_provider` is listed
# before `afc_sso` in INSTALLED_APPS, so its registration always wins the race and this
# has to take it back. The try/except keeps import order from being load bearing.
try:
    admin.site.unregister(AFCSSOApplication)
except admin.sites.NotRegistered:
    pass


class AFCSSOApplicationAdminForm(forms.ModelForm):
    """Applies the outbound-URL rule to the one field this screen can point at AFC itself.

    deletion_webhook_url is the only URL on this model that AFC'S OWN SERVER fetches
    (afc_sso/tasks.py deliver_disconnect_signal); the rest are followed by a player's browser.
    Every other writer of that field already runs provisioning._clean_outbound_url: the public
    application form and its draft edit (afc_partner_apply/views_public.py), provisioning itself,
    and the admin API PATCH (afc_sso/admin_api.py). This break-glass screen was the one way in
    that did not, and a plain URLField happily accepts https://127.0.0.1/ or the cloud metadata
    address. Break-glass is exactly when a value gets typed in a hurry, so the rule holds here
    too rather than resting on the typist.
    """

    class Meta:
        model = AFCSSOApplication
        fields = "__all__"

    def clean_deletion_webhook_url(self):
        cleaned, err = _clean_outbound_url(
            self.cleaned_data.get("deletion_webhook_url"), "Deletion webhook URL")
        if err:
            raise forms.ValidationError(err)
        # Empty stays empty: the field is optional, and a partner that wants no signal is normal.
        return cleaned or ""


@admin.register(AFCSSOApplication)
class AFCSSOApplicationAdmin(admin.ModelAdmin):
    form = AFCSSOApplicationAdminForm
    list_display = ("display_name", "name", "status", "client_id")
    list_filter = ("status",) + SSO_FIELD_TOGGLES
    search_fields = ("name", "display_name", "client_id")
    readonly_fields = ("client_id",)
    # skip_authorization would let an org bypass the player's consent screen entirely.
    # AFCSSOApplication.save() pins it off, and hiding it here stops an admin trying to
    # tick it and assuming it worked.
    exclude = ("skip_authorization",)
