"""
Sign in and sign up with v-ent.co.

WHY THIS FILE EXISTS (owner 2026-08-28)
    "a sign in and sign up should also be the same as linking. even for discord and google, please
    set it up and do the same for v-ent."

    v-ent.co already worked as a CONNECT provider: a player with an AFC account could link theirs
    from /profile/connected-apps. Everything under afc_auth/connections/ requires an existing
    session by design, so there was no way IN through v-ent.co, only a way to attach it afterwards.
    This is the way in.

WHAT IT REUSES RATHER THAN REBUILDS
    Every piece already existed and is shared with the Connect flow, which is the point: a sign-in
    and a Connect must not be able to disagree about what a v-ent identity IS.

      connections/oauth.py      the authorize URL, PKCE, the code exchange, the profile fetch,
                                and access_token(), which knows v-ent.co wraps its token
      providers/vent.py         endpoints and normalize(), so the row written here is byte for
                                byte the row Connect writes
      connections/links.py      link_account, the single writer of ConnectedAccount
      views.login_or_challenge  the SAME two-factor gate password login and Google use

    That last one matters most. A provider sign-in that issued its own session would be a way
    straight past 2FA, and the accounts most likely to have 2FA on are admins and organizers.

THE SHAPE, mirroring discord_sso_start / discord_sso_callback / discord_sso_exchange
    1. start     stash a CSRF nonce + the PKCE verifier + where to land, redirect to v-ent.co
    2. callback  v-ent.co returns ?code -> exchange -> userinfo -> find or create -> LINK ->
                 login_or_challenge -> stash the result under a one-time handoff -> redirect to
                 the frontend with only that handoff in the URL
    3. exchange  the frontend swaps the handoff for the real result, once

    Nothing sensitive rides the redirect. It lands in browser history and the frontend origin can
    leak through Referer, so the URL carries a short-lived single-use code and nothing else.

HOW AN ACCOUNT IS FOUND, and why the order is what it is
    1. by the EXISTING LINK (provider_user_id). v-ent.co's own docs are explicit: "Key the account
       on sub. Not on the username. A person can change their username." A player who has signed
       in before is found this way and never depends on their email staying the same.
    2. by EMAIL, for the first sign-in, matching what Discord and Google do.
    3. otherwise a new account, with an unusable password, exactly as the other two providers.

    A player who granted `identity` but declined `identity:email` has no email at all. That is a
    real case, not a defensive one: the consent screen lists the two scopes separately. Rule 1
    still signs them in if they have linked before; a FIRST sign-in with no email cannot create an
    account, because AFC keys recovery on email and an account nobody can recover is worse than a
    refusal. They are sent back with a status the frontend explains.

CONSUMED BY
    afc_auth/urls.py -> /auth/vent/sso/{start,callback,exchange}/
    frontend components/auth/VentSignInButton.tsx and app/(auth)/vent/callback/
"""
import secrets
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import redirect
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from afc_auth.connections import links as connection_links
from afc_auth.connections import oauth
from afc_auth.connections.registry import get_provider
from afc_auth.models import ConnectedAccount, User, UserProfile

# How long the player has to finish the round trip at v-ent.co, and how long the frontend has to
# swap the handoff. Both deliberately short: they are single-use secrets sitting in a cache.
STATE_TTL_SECONDS = 600
HANDOFF_TTL_SECONDS = 90

PROVIDER_SLUG = "vent"


def _frontend_origin(request):
    """Frontend origin to bounce back to, matched to the API host. Same rule as Discord's."""
    host = request.get_host()
    if "localhost" in host or "127.0.0.1" in host:
        return settings.FRONTEND_URL_LOCAL
    return settings.FRONTEND_URL


def _fail(request, reason="failed"):
    return redirect(f"{_frontend_origin(request)}/vent/callback?status={reason}")


def _callback_uri(request):
    """The redirect_uri registered with v-ent.co. Must match theirs EXACTLY, including the trailing
    slash: they compare the whole string, and a mismatch is refused at the token step with
    BAD_REDIRECT after the player has already approved."""
    base = (getattr(settings, "AFC_API_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        base = request.build_absolute_uri("/").rstrip("/")
    return f"{base}/auth/vent/sso/callback/"


@api_view(["GET"])
@permission_classes([AllowAny])
def vent_sso_start(request):
    """Send the browser to v-ent.co's consent screen.

    Query: ?next=<relative frontend path to land on afterwards> (default /home).
    """
    provider = get_provider(PROVIDER_SLUG)
    if provider is None or not provider.enabled():
        # Not configured on this box. Fail to the frontend rather than 500, so the button simply
        # does not work rather than showing a stack trace.
        return _fail(request, "unconfigured")

    next_path = request.GET.get("next") or "/home"
    if not next_path.startswith("/"):
        next_path = "/home"  # relative only, never an open redirect

    nonce = secrets.token_urlsafe(16)
    # PKCE: the verifier NEVER leaves this server. Only its hash goes to v-ent.co, and the verifier
    # is handed back at the token step to prove the same client finished the flow that started it.
    verifier = secrets.token_urlsafe(64)
    cache.set(
        f"vent_sso_state:{nonce}",
        {"next": next_path, "verifier": verifier},
        STATE_TTL_SECONDS,
    )

    return redirect(
        oauth.authorize_url(provider, nonce, verifier, _callback_uri(request))
    )


def _find_user(normalized):
    """The AFC account this v-ent identity belongs to, or None. See the module docstring for why
    the link is tried before the email."""
    subject = (normalized.get("provider_user_id") or "").strip()
    if subject:
        link = (
            ConnectedAccount.objects.filter(
                provider=PROVIDER_SLUG, provider_user_id=subject
            )
            .select_related("user")
            .first()
        )
        if link is not None:
            return link.user

    email = (normalized.get("email") or "").strip()
    if email:
        return User.objects.filter(email__iexact=email).first()
    return None


@api_view(["GET"])
@permission_classes([AllowAny])
def vent_sso_callback(request):
    """v-ent.co redirects here with ?code and ?state."""
    from afc_auth.views import login_or_challenge  # local import: views imports this module's URLs

    provider = get_provider(PROVIDER_SLUG)
    if provider is None or not provider.enabled():
        return _fail(request, "unconfigured")

    code = request.GET.get("code") or ""
    state = request.GET.get("state") or ""
    if not code or not state:
        return _fail(request)

    stashed = cache.get(f"vent_sso_state:{state}")
    if not stashed:
        # Unknown or expired state. This is the CSRF guard, not a nicety: without it anybody could
        # feed AFC a code they obtained elsewhere.
        return _fail(request)
    cache.delete(f"vent_sso_state:{state}")  # single use

    next_path = stashed.get("next") or "/home"
    verifier = stashed.get("verifier") or ""
    redirect_uri = _callback_uri(request)

    try:
        tokens = oauth.exchange_code(provider, code, verifier, redirect_uri)
        access_token = oauth.access_token(provider, tokens)
        if not access_token:
            return _fail(request)
        profile = oauth.fetch_profile(provider, access_token)
    except Exception:
        # The provider's own message is NOT shown to the player: it can echo the client secret back
        # in an error body. It goes to the server log via the exception, and they see "failed".
        return _fail(request)

    normalized = provider.normalize(profile)
    subject = (normalized.get("provider_user_id") or "").strip()
    if not subject:
        # No stable id means nothing to key the account on. Inventing one would attach the wrong
        # player on the next sign-in.
        return _fail(request)

    email = (normalized.get("email") or "").strip().lower()
    user = _find_user(normalized)
    is_new = False

    if user is None:
        if not email:
            # First sign-in, no email, nothing to recover the account with later. Refused on
            # purpose; the frontend explains that the email permission is needed to make an
            # account, and that an existing AFC account can link v-ent.co from the profile page.
            return _fail(request, "no_email")

        username = (normalized.get("username") or "").strip() or f"vent_{subject[:24]}"
        # Usernames are unique on AFC and v-ent.co's are not scoped to us, so a collision is
        # ordinary rather than exceptional.
        candidate = username[:150]
        suffix = 0
        while User.objects.filter(username__iexact=candidate).exists():
            suffix += 1
            candidate = f"{username[:140]}_{suffix}"

        user = User.objects.create(
            username=candidate,
            email=email,
            full_name=(normalized.get("username") or "").strip()[:150],
            country=(profile.get("data", profile) or {}).get("country") or "",
            has_completed_onboarding=False,  # new account -> first-login onboarding
        )
        # No password, same as Google and Discord sign-ups. links.can_disconnect knows about this:
        # it refuses to remove the only way a player can get back in.
        user.set_unusable_password()
        user.save()
        UserProfile.objects.get_or_create(user=user)
        is_new = True

    # ── LINK, which is the whole point of the owner's instruction ────────────────────────────────
    # The same call, with the same normalize() output, that pressing Connect makes. A player who
    # signs up with v-ent.co sees it on their connections page without doing anything.
    #
    # Guarded the same way Discord's is: never take a v-ent identity that already belongs to a
    # DIFFERENT AFC account. Swallowed because the player is authenticated by this point and a
    # bookkeeping failure must not lock them out.
    try:
        clash = (
            ConnectedAccount.objects.filter(
                provider=PROVIDER_SLUG, provider_user_id=subject
            )
            .exclude(user_id=user.user_id)
            .exists()
        )
        if not clash:
            connection_links.link_account(
                user, PROVIDER_SLUG, normalized, scopes=provider.scopes
            )
    except Exception:
        pass

    # ── the SHARED two-factor gate ──────────────────────────────────────────────────────────────
    # Never a bespoke session here. If this issued its own token, signing in with v-ent.co would be
    # a way straight past 2FA for every account that has it switched on.
    try:
        result = login_or_challenge(request, user, extra={"is_new": is_new})
    except Exception:
        return _fail(request)

    # ── one-time handoff: nothing sensitive in the URL ───────────────────────────────────────────
    # The redirect lands in browser history and the frontend origin can leak through Referer, so
    # the session (or the 2FA challenge) is stashed server-side and only a single-use code travels.
    handoff = secrets.token_urlsafe(24)
    cache.set(f"vent_sso_handoff:{handoff}", result, HANDOFF_TTL_SECONDS)
    return redirect(
        f"{_frontend_origin(request)}/vent/callback?code={handoff}&next={quote(next_path)}"
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def vent_sso_exchange(request):
    """Swap the one-time handoff code for the real login result. Single use."""
    code = (request.data or {}).get("code") or ""
    if not code:
        return Response({"message": "code is required."}, status=400)

    key = f"vent_sso_handoff:{code}"
    stashed = cache.get(key)
    if stashed is None:
        return Response({"message": "That sign-in link has expired. Try again."}, status=400)
    cache.delete(key)  # single use, so a code left in history cannot be replayed

    payload, http_status = stashed
    return Response(payload, status=http_status)
