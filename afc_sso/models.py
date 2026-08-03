# ──────────────────────────────────────────────────────────────────────────────
# The partner app record for "Sign in with AFC".
#
# One row per approved org. It is the ONLY description of what that org may ever
# see about a player: every toggle defaults False (least privilege), exactly like
# afc_partner_api.Partner and afc_organizers PERMISSION_FIELDS.
#
# Read by: AFCOAuth2Validator (afc_sso/validators.py) at claim time, and by the
# admin screens that create and edit partner apps. django-oauth-toolkit uses this
# in place of its own Application model via OAUTH2_PROVIDER_APPLICATION_MODEL
# (afc/settings.py), so oauth2_provider.models.get_application_model() returns
# THIS class everywhere in the codebase, including the /sso/ views the library
# ships and the consent screen added in Task 6.
# ──────────────────────────────────────────────────────────────────────────────
from django.db import models
from oauth2_provider.models import AbstractApplication

# Toggle -> the OIDC scope it unlocks. Drives allowed_scopes(), the admin edit form,
# and the consent screen copy, so the three can never drift apart. Adding a scope
# means adding one line here and one claim resolver in validators.py.
TOGGLE_TO_SCOPE = {
    "share_profile": "profile",
    "share_email": "email",
    "share_freefire_uid": "afc.freefire",
    "share_team": "afc.team",
    "share_history": "afc.history",
    "share_stats": "afc.stats",
    "share_ranking": "afc.ranking",
    "share_standing": "afc.standing",
}
SSO_FIELD_TOGGLES = tuple(TOGGLE_TO_SCOPE)


class AFCSSOApplication(AbstractApplication):
    # Human-facing identity, shown on the consent screen so the player knows who is asking.
    display_name = models.CharField(max_length=120, blank=True)

    # ── The partner's logo (owner 2026-08-03: was a URL, is now a real upload) ──
    # `logo` is AFC's OWN copy of the mark; `logo_url` is the legacy third-party URL some
    # rows still hold. NEVER read either directly - read resolved_logo_url() below, so no
    # caller has to know which of the two is set.
    #
    # WHY THE UPLOAD REPLACED THE URL: this image is rendered on the CONSENT SCREEN
    # (afc_sso/templates/afc_sso/authorize.html), the page where a player decides whether
    # to trust this org with their data. A URL field meant AFC embedded a
    # third-party-controlled image on its own security-critical page: the partner could
    # swap it for anything at any moment, and every player load pinged their server.
    # Hosting the file ourselves means what AFC staff approved is what players see.
    #
    # WHY logo_url SURVIVES: rows provisioned before the upload existed still hold one,
    # and a partner may legitimately have no file yet. Keeping it as a read-time fallback
    # is what makes the migration a non-event - see resolved_logo_url().
    logo = models.ImageField(upload_to="sso_partner_logos/", null=True, blank=True)
    logo_url = models.URLField(blank=True)

    homepage_url = models.URLField(blank=True)

    status = models.CharField(
        max_length=20,
        default="active",
        choices=[("active", "Active"), ("suspended", "Suspended")],
    )

    # Where a deletion signal is sent when a player revokes (spec 7b). Used in a later plan.
    deletion_webhook_url = models.URLField(blank=True)

    # ── Field toggles, all default OFF ──
    share_profile = models.BooleanField(default=False)
    share_email = models.BooleanField(default=False)
    share_freefire_uid = models.BooleanField(default=False)
    share_team = models.BooleanField(default=False)
    share_history = models.BooleanField(default=False)
    share_stats = models.BooleanField(default=False)
    share_ranking = models.BooleanField(default=False)
    share_standing = models.BooleanField(default=False)

    class Meta(AbstractApplication.Meta):
        swappable = "OAUTH2_PROVIDER_APPLICATION_MODEL"

    def save(self, *args, **kwargs):
        """No partner org ever skips the player's consent screen.

        django-oauth-toolkit honours a per-application `skip_authorization` flag and, when
        it is set, issues a code without showing anyone anything (views/base.py). That is
        meant for first-party in-house apps. Every application here is a THIRD PARTY, so the
        flag is pinned off rather than left as something an admin can tick by accident. The
        Django admin also hides it, see afc_sso/admin.py.
        """
        self.skip_authorization = False
        return super().save(*args, **kwargs)

    def allowed_scopes(self):
        """The maximum scope set AFC permits this org, regardless of what it requests.

        `openid` is always present: it carries only the pairwise `sub` and is what makes
        this OIDC rather than plain OAuth.
        """
        scopes = {"openid"}
        for toggle, scope in TOGGLE_TO_SCOPE.items():
            if getattr(self, toggle, False):
                scopes.add(scope)
        return scopes

    def logo_file_url(self):
        """URL of the UPLOADED logo, or "" when this partner has no file of its own.

        FileField.url raises ValueError when no file is associated (the same trap noted in
        afc_sso/claims.py), so the emptiness check and the guard both live here rather than
        being repeated, and forgotten, at each call site.
        """
        if not self.logo:
            return ""
        try:
            return self.logo.url
        except ValueError:
            return ""

    def resolved_logo_url(self):
        """THE one logo value every caller should read: uploaded file first, legacy
        `logo_url` second, "" when this partner has neither.

        CONSUMED BY:
          * the consent screen - afc_sso/views.py AFCAuthorizationView.get_context_data
            passes it to authorize.html as `afc_logo_url`;
          * the admin API - afc_sso/admin_api.py _serialize_detail, which makes it absolute
            for the Next.js dashboard (a different origin), feeding the logo preview in
            frontend/app/(a)/a/partners/_components/SsoAppsPanel.tsx.

        The fallback is what makes the URL-to-upload switch safe in both directions: a row
        that still holds only a legacy URL keeps rendering exactly as it did, and the moment
        staff upload a file that file wins, with no second step and no data migration.
        Returning "" rather than None matters too - authorize.html tests this value before
        drawing an <img>, so a partner with no logo simply gets no logo, never an error.
        """
        return self.logo_file_url() or (self.logo_url or "")

    def is_active_partner(self):
        return self.status == "active"

    def __str__(self):
        return self.display_name or self.name
