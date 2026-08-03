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
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model
from oauth2_provider.views import AuthorizationView

from .claims import describe_scopes

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
        """Force the consent screen whenever this request asks for MORE than the player
        has already approved.

        The library would otherwise reuse a past approval whenever a live token covers the
        requested scopes (views/base.py, the `require_approval == "auto"` branch). That is
        the same rule as consent_is_current, but it is the library's rule, not ours: state
        it here so a settings change or a library upgrade cannot quietly relax it.
        """
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context.get("application")
        scopes = context.get("scopes") or []
        context["afc_org_name"] = (
            getattr(application, "display_name", "") or getattr(application, "name", "")
        )
        context["afc_logo_url"] = getattr(application, "logo_url", "")
        context["afc_scope_lines"] = describe_scopes(scopes)
        return context
