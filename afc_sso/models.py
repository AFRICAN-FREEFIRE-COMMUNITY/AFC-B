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
from django.core.exceptions import ValidationError
from django.db import models
from oauth2_provider.models import AbstractApplication

from .redirect_policy import RedirectURIPolicyError, validate_redirect_uris

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

    # Where the "this player disconnected you" signal is POSTed. Blank means the partner
    # has not asked for one and none is sent. The payload, the signature and the retry
    # behaviour all live in afc_sso/webhooks.py and afc_sso/tasks.py; the two things that
    # fire it are afc_sso/api.py revoke_connected_app (the player pressed Remove) and the
    # pre_delete receiver in afc_sso/signals.py (the AFC account itself went away).
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

    def clean(self):
        """AFC's redirect URI policy, enforced on the model so no editing path escapes it.

        The Django admin (and any ModelForm) runs full_clean, so a superuser editing a row
        at /admin/ hits exactly the same rules as the staff API in afc_sso/admin_api.py,
        which calls the same validator directly. Both redirect_uris and
        post_logout_redirect_uris are checked, because both are addresses AFC sends a real
        player to. The rules themselves, and why they are what they are, live in
        afc_sso/redirect_policy.py.

        Errors are raised against the FIELD rather than the form as a whole, so the admin
        prints the message next to the textarea the offending URI was typed into.

        AFC'S POLICY RUNS FIRST, before super().clean(), and that ordering is deliberate.
        The library's own clean() also validates redirect_uris, but it reports a
        non-field error ("URI validation error ... Enter a valid URL") that says nothing
        about which rule was broken. AFC's rules are strictly narrower, so anything that
        passes here would pass the library's check anyway; running ours first means the
        admin reads "Wildcards are not allowed" next to the field instead.
        """
        errors = {}

        try:
            self.redirect_uris = validate_redirect_uris(
                self.redirect_uris, required=True, label="redirect URI")
        except RedirectURIPolicyError as err:
            errors["redirect_uris"] = str(err)

        try:
            self.post_logout_redirect_uris = validate_redirect_uris(
                self.post_logout_redirect_uris,
                required=False,
                label="post-logout redirect URI",
            )
        except RedirectURIPolicyError as err:
            errors["post_logout_redirect_uris"] = str(err)

        if errors:
            raise ValidationError(errors)

        # Only once the URIs are known to be legal, so the library never gets the chance
        # to report the same problem in worse words.
        super().clean()

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
