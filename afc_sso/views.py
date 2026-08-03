# ──────────────────────────────────────────────────────────────────────────────
# The consent screen for "Sign in with AFC".
#
# Overrides django-oauth-toolkit's AuthorizationView for two reasons:
#   1. to render AFC's own screen naming the org and listing, in plain language,
#      exactly what it will receive (afc_sso.claims.describe_scopes)
#   2. to enforce that WIDENING requires fresh consent - without this, one historic
#      "Allow" quietly authorises everything AFC ever grants that org later.
#
# request.user arrives from afc_sso.middleware.SSOSessionTokenMiddleware. An
# anonymous visitor is sent to the AFC login with the whole OAuth request preserved,
# so the partner's flow survives the detour.
#
# Mounted at /sso/authorize/ by afc_sso/urls.py, ahead of the library's own include.
# ──────────────────────────────────────────────────────────────────────────────
import logging
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.utils import timezone
# The refusal messages below are the only other English a player can be shown on this
# screen, so they are translated with the consent template itself. gettext (not lazy):
# each one is built inside a request, by which point SSOLanguageMiddleware has already
# activated the player's language. Catalogs: locale/<lang>/LC_MESSAGES/django.po.
from django.utils.translation import gettext as _
from oauth2_provider.http import OAuth2ResponseRedirect
from oauth2_provider.models import get_access_token_model, get_application_model
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import AuthorizationView
from oauth2_provider.views.oidc import RPInitiatedLogoutView
from oauthlib.common import add_params_to_uri

from .claims import describe_scopes
from .tokens import revoke_tokens_for

logger = logging.getLogger(__name__)

# The AFC login lives on the Next.js frontend, a different origin from this API, and it
# reads `?redirect=`, NOT `?next=` (frontend/app/(auth)/_components/LoginForm.tsx). The
# plan's placeholder said /login and next=; both were wrong, verified against the route.
LOGIN_PATH = "/login"
LOGIN_REDIRECT_PARAM = "redirect"


def _frontend_origin(request):
    """Frontend origin to send the player to, matched to the API host (local vs prod).

    Mirrors afc_auth.views._discord_frontend_origin, which does the same job for the
    Discord sign-in bounce. Kept as a local copy so afc_sso does not import a private
    helper out of that module.
    """
    host = request.get_host()
    if "localhost" in host or "127.0.0.1" in host:
        return settings.FRONTEND_URL_LOCAL
    return settings.FRONTEND_URL


def consent_is_current(previously_granted, now_requested):
    """False when the request asks for anything the player has not already approved."""
    return set(now_requested) <= set(previously_granted)


def previously_granted_scopes(user, application):
    """Every scope this player has a LIVE token for at this org, which is the record of
    what they actually consented to. Expired tokens are ignored on purpose: consent that
    has run out is not consent."""
    tokens = get_access_token_model().objects.filter(
        user=user, application=application, expires__gt=timezone.now()
    )
    granted = set()
    for token in tokens:
        granted.update((token.scope or "").split())
    return granted


class AFCAuthorizationView(AuthorizationView):
    template_name = "afc_sso/authorize.html"

    def dispatch(self, request, *args, **kwargs):
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            # Absolute URL: after login the player is on the frontend origin and has to be
            # sent back here, to the API, to finish the authorization.
            target = quote(request.build_absolute_uri(), safe="")
            login_url = f"{_frontend_origin(request)}{LOGIN_PATH}"
            return HttpResponseRedirect(f"{login_url}?{LOGIN_REDIRECT_PARAM}={target}")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        """Refuse outright, or force the consent screen when the request has widened.

        Order matters: refusals are checked first so a suspended org or an over-broad
        scope never reaches the point where a screen is rendered or a code is issued.
        """
        refusal = self._refuse(request)
        if refusal is not None:
            return refusal

        # Force the consent screen whenever this request asks for MORE than the player has
        # already approved. The library would otherwise reuse a past approval whenever a
        # live token covers the requested scopes (views/base.py, the
        # `require_approval == "auto"` branch). That is the same rule as
        # consent_is_current, but it is the LIBRARY's rule, not ours: state it here so a
        # settings change or a library upgrade cannot quietly relax it.
        client_id = request.GET.get("client_id")
        if client_id:
            application = (
                get_application_model().objects.filter(client_id=client_id).first()
            )
            requested = set((request.GET.get("scope") or "").split())
            if application and not consent_is_current(
                previously_granted_scopes(request.user, application), requested
            ):
                # approval_prompt=force is the library's own documented way to say
                # "ask again", so we steer it rather than reimplementing its flow.
                request.GET = request.GET.copy()
                request.GET["approval_prompt"] = "force"
        return super().get(request, *args, **kwargs)

    def _refuse(self, request):
        """Return an AFC-rendered refusal, or None to let the flow continue.

        Deliberately RENDERS rather than redirects. Bouncing a refusal to the redirect_uri
        in the query string would make AFC an open redirector: an attacker could send a
        player a link that fails on purpose and lands them on a phishing page carrying an
        africanfreefirecommunity.com referrer. Nothing here echoes attacker-controlled
        input back into a Location header.
        """
        application = (
            get_application_model()
            .objects.filter(client_id=request.GET.get("client_id", ""))
            .first()
        )
        if application is None:
            return HttpResponseBadRequest(_("Unknown application."))
        if not application.is_active_partner():
            return HttpResponseBadRequest(_("This application is suspended."))
        if getattr(request.user, "status", "active") != "active":
            return HttpResponseBadRequest(
                _("Your AFC account cannot sign in to partner sites.")
            )
        requested = set((request.GET.get("scope") or "").split())
        if not requested <= set(application.allowed_scopes()):
            # Gate 1 again, at the front door. build_claims would strip the extra scope
            # anyway, but letting the request through would show the player a consent
            # screen for data the org is not approved to receive.
            return HttpResponseBadRequest(
                _("This application requested data it is not approved for.")
            )
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context.get("application")
        scopes = context.get("scopes") or []
        context["afc_org_name"] = (
            getattr(application, "display_name", "") or getattr(application, "name", "")
        )
        # ONE resolved value, from AFCSSOApplication.resolved_logo_url(): the file AFC
        # HOSTS when staff have uploaded one, the legacy third-party URL otherwise, and ""
        # when the partner has neither - in which case authorize.html renders no <img> at
        # all. This screen must never fail to render because of a logo, so an application
        # that somehow lacks the method (or has none at all) resolves to "" rather than
        # raising: a player who cannot read this page cannot make a decision on it.
        resolve_logo = getattr(application, "resolved_logo_url", None)
        context["afc_logo_url"] = resolve_logo() if callable(resolve_logo) else ""
        context["afc_scope_lines"] = describe_scopes(scopes)
        return context


# ──────────────────────────────────────────────────────────────────────────────
# RP-initiated logout, scoped to the partner that asked
# ──────────────────────────────────────────────────────────────────────────────
# WHY AFC HAS ITS OWN: django-oauth-toolkit's RPInitiatedLogoutView.do_logout deletes
# every access token the player holds, filtered on user + client_type + grant_type and
# NOT on the application that made the request (site-packages/oauth2_provider/views/
# oidc.py, do_logout). Every AFC partner is a confidential authorization-code client, so
# that filter matches all of them: one partner calling /sso/logout/ with an id_token_hint
# would disconnect the player from every OTHER partner too, silently, with no consent
# screen and no way for the others to know why their tokens stopped working.
#
# That is not a theoretical path. must_prompt() returns False when the browser has no AFC
# session, so a partner's SERVER can call this endpoint with a valid (even expired,
# OIDC_RP_INITIATED_LOGOUT_ACCEPT_EXPIRED_TOKENS) id_token_hint and reach do_logout with
# no human present at all.
#
# The override below keeps everything the library does about VALIDATION (the id_token
# signature, the client_id match, the registered post-logout URI, the confirm prompt) and
# changes exactly one thing: which tokens get deleted. See test_logout.py.
#
# Mounted at /sso/logout/ by afc_sso/urls.py, ahead of the library's include, the same
# way AFCAuthorizationView takes /sso/authorize/.
class AFCRPInitiatedLogoutView(RPInitiatedLogoutView):
    """End the player's AFC session, and disconnect ONLY the partner that asked.

    REQUEST (GET /sso/logout/), all parameters standard OpenID Connect RP-Initiated
    Logout 1.0:
        id_token_hint             strongly recommended. The ID token AFC issued this
                                  partner for this player. It is what identifies whose
                                  session to end and which partner is asking.
        client_id                 optional. Must match the id_token_hint when both are
                                  sent (the library raises ClientIdMissmatch otherwise).
        post_logout_redirect_uri  optional. Must be registered on the application, and
                                  registration applies AFC's redirect URI policy
                                  (afc_sso/redirect_policy.py).
        state                     optional, echoed back on the redirect.

    RESPONSE: a redirect to post_logout_redirect_uri when one was supplied and matched,
    otherwise to the AFC API root. A signed-in player is shown a confirmation page first
    (OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT), which is what stops a partner ending an AFC
    session from a hidden iframe.

    WHAT IT DELETES: the requesting partner's access tokens, refresh tokens, grants and
    ID tokens for that player, which is the same set afc_sso/api.py revoke_connected_app
    removes when the player presses Remove on the Connected apps page. Other partners are
    untouched. When the request identifies no application at all (no id_token_hint and no
    client_id) nothing is deleted and the Django session is simply ended, because there is
    no partner to disconnect.
    """

    # Named explicitly rather than overriding oauth2_provider/logout_confirm.html:
    # `oauth2_provider` is listed BEFORE `afc_sso` in INSTALLED_APPS, so an override under
    # that path would lose to the library's own copy on the app-directories loader.
    template_name = "afc_sso/logout_confirm.html"

    def do_logout(self, application=None, post_logout_redirect_uri=None, state=None,
                  token_user=None):
        user = token_user or self.request.user

        # AnonymousUser has no tokens to remove; `application is None` means the request
        # never said who is asking, and disconnecting a partner AFC cannot name would be
        # a guess. Both cases fall through to the session logout and the redirect.
        if application is not None and not isinstance(user, AnonymousUser):
            deleted = revoke_tokens_for(user, application.pk)
            logger.info(
                "sso logout: disconnected user #%s from application #%s (%s)",
                user.pk, application.pk, deleted,
            )

        # ── Ending the AFC session itself ────────────────────────────────────────────
        # The library calls django.contrib.auth.logout, which clears a DJANGO session.
        # AFC players do not have one: they hold an `auth_token` cookie backed by an
        # afc_auth.SessionToken row, resolved per request by SSOSessionTokenMiddleware.
        # So the library's logout alone would leave the player signed in to AFC and the
        # partner would have been told otherwise.
        #
        # WHEN AFC ENDS IT, AND WHY NOT ALWAYS: only when the request carries the
        # player's own AFC cookie, which (with OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT on)
        # means a human just pressed "Sign out of AFC" on the confirm page. A partner's
        # SERVER can reach do_logout with nothing but an id_token_hint and no browser
        # present at all; letting that end an AFC session would hand every partner a
        # remote sign-out button for any player they hold a token for.
        #
        # One token, not every token: this signs the player out on THIS device, which is
        # what the button they pressed says. Their phone stays signed in.
        browser_user = getattr(self.request, "user", None)
        if browser_user is not None and browser_user.is_authenticated:
            from afc_auth.models import SessionToken  # local: avoids an app-loading cycle

            cookie_token = self.request.COOKIES.get("auth_token") or ""
            if cookie_token:
                SessionToken.objects.filter(user=browser_user, token=cookie_token).delete()

        # The library's own tail, kept so anything that DOES use a Django session (a
        # staff member signed into /admin/, say) behaves exactly as it documents.
        logout(self.request)

        if post_logout_redirect_uri:
            target = post_logout_redirect_uri
            if state:
                target = add_params_to_uri(post_logout_redirect_uri, [("state", state)])
            return OAuth2ResponseRedirect(target, application.get_allowed_schemes())

        return OAuth2ResponseRedirect(
            self.request.build_absolute_uri("/"),
            oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES,
        )
