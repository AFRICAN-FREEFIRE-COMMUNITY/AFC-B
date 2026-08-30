# ──────────────────────────────────────────────────────────────────────────────
# THE data-release policy for "Sign in with AFC". Nothing else in the codebase
# decides what a partner org sees; if you are adding a field, add it here.
#
# A field is released only if ALL FOUR gates agree:
#   1. AFC toggle      - AFCSSOApplication.allowed_scopes() (afc_sso/models.py)
#   2. Requested scope - what the org asked for in this authorization
#   3. Player consent  - the granted scopes on the token; the consent screen
#                        (afc_sso/views.py) is what puts them there
#   4. AFC rules       - User.stats_visible, the under-18 contact rule, account status
#
# Called by AFCOAuth2Validator (afc_sso/validators.py) for both the ID token and
# /sso/userinfo/, so the two can never disagree.
#
# Every resolver below was checked field by field against the real models
# (afc_auth, afc_team, afc_tournament_and_scrims, afc_rankings). Three of them differ
# from the shapes sketched in tasks/afc-sso-provider-plan.md because the plan guessed:
#   - UserProfile.user is a plain ForeignKey, NOT a OneToOne, so there is no
#     `user.userprofile` accessor. Reading it would have returned None for everyone and
#     silently classed every player as a minor.
#   - BannedPlayer's column is `banned_player`, not `user`.
#   - PlayerMonthlyScore has `total_score` and `rank`, there is no `points` field.
# ──────────────────────────────────────────────────────────────────────────────
import datetime

from django.utils import timezone

# Never released, no toggle exists, not reachable from admin. Listed so the intent is
# greppable and the test in test_claims.py has something to assert against.
DENYLIST = (
    "password", "ip_country", "role", "status", "whatsapp_number",
    "session_token", "audit", "room_id", "room_password",
)

MINIMUM_AGE_FOR_CONTACT_DATA = 18

# EMPTY SINCE 2026-08-30, and this is the owner's decision, taken with the measurement in
# front of them. It used to be {"email"}.
#
# WHAT WENT WRONG. `email` was gated on _is_adult, which fails closed on an unknown date of
# birth. UserProfile.date_of_birth was READ by exactly one line, this module's, and WRITTEN
# by nothing: not signup, not profile settings, not admin, and nothing in the frontend
# collects it either. Measured on production data: 0 of 6,780 profile rows carry one, and
# _is_adult returned False for 500 of 500 users sampled.
#
# So the gate did not protect minors. It removed the email claim from EVERY player on every
# authorization, and always had. Meanwhile discovery advertises `email` in scopes_supported
# and the consent screen asks the player to share "Your email address". AFC approved
# partners for a scope it could not fulfil, asked players to consent to it, and sent
# nothing. V-ENT reported exactly that.
#
# The tests passed throughout because each one CREATES a profile carrying a date of birth.
# A test that hands the code the input it wants proves the code reads that input, never
# that it arrives. Same shape as the auth_token cookie that never reached the api subdomain.
#
# WHY EMPTY RATHER THAN "TREAT UNKNOWN AS ADULT": that spelling would read as a claim that
# AFC has checked, when it has not. Empty says the true thing, which is that AFC holds no
# age signal for anybody and therefore enforces no age rule. The player's own consent is
# what releases the address.
#
# TO REINSTATE IT, both of these must be true first, in this order:
#   1. a date of birth is actually COLLECTED and stored (signup, profile settings, or at
#      the moment of consent), and
#   2. this set names the scopes to withhold from a minor.
# Put a scope in here before step 1 and it is withheld from everyone, which is the bug
# above. The machinery below is left wired for exactly that reason.
CONTACT_SCOPES = frozenset()

# How many past events the history claim carries. Bounded because a partner reading
# userinfo must never be able to make AFC walk a player's entire tournament record.
HISTORY_LIMIT = 50


def _is_adult(user):
    """Fail CLOSED: an unknown or unparseable date of birth counts as a minor.

    UserProfile is a ForeignKey to User rather than a OneToOne, so a player may have zero
    or several profile rows. The newest one wins, and no row at all means minor.
    """
    from afc_auth.models import UserProfile

    profile = UserProfile.objects.filter(user=user).order_by("-profile_id").first()
    dob = getattr(profile, "date_of_birth", None)
    if not isinstance(dob, datetime.date):
        return False
    today = timezone.now().date()
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years >= MINIMUM_AGE_FOR_CONTACT_DATA


def _profile_claims(user):
    """The `profile` scope. `picture` is the standard OIDC claim name for an avatar, so a
    partner's off-the-shelf login library picks it up with no AFC-specific code.

    Owner enabled avatar sharing on 2026-08-03. It is deliberately an ABSOLUTE url: these
    claims are built without a Django request (oauthlib passes its own object, so
    build_absolute_uri is unavailable), and a bare "/media/..." path would resolve against
    the PARTNER's domain and 404. Base comes from settings.AFC_API_BASE_URL.

    Omitted entirely when the player has no picture, rather than sent as null: build_claims
    drops None, and a partner must treat every claim as optional anyway.
    """
    from django.conf import settings

    from afc_auth.models import canonical_profile

    picture = None
    profile = canonical_profile(user)
    if profile is not None and getattr(profile, "profile_pic", None):
        try:
            picture = f"{settings.AFC_API_BASE_URL.rstrip('/')}{profile.profile_pic.url}"
        except ValueError:
            # FileField.url raises when no file is actually associated.
            picture = None

    return {
        "preferred_username": user.username,
        "picture": picture,
        "country": user.country or None,
        "locale": user.language or None,
    }


def _team_claims(user):
    from afc_team.models import TeamMembers

    membership = TeamMembers.objects.filter(member=user).select_related("team").first()
    if not membership or not membership.team:
        return {"afc_team": None}
    return {"afc_team": {
        "name": membership.team.team_name,
        "role": membership.management_role or "member",
    }}


def _history_claims(user):
    from afc_tournament_and_scrims.models import RegisteredCompetitors

    rows = (RegisteredCompetitors.objects
            .filter(user=user)
            .select_related("event")
            .order_by("-registration_date")[:HISTORY_LIMIT])
    return {"afc_history": [
        {"event": r.event.event_name, "slug": r.event.slug} for r in rows if r.event
    ]}


def _stats_claims(user):
    from afc_tournament_and_scrims.models import TournamentPlayerMatchStats

    # aggregate() rather than summing in Python: a long-serving player has thousands of
    # per-match rows and this runs on every userinfo call.
    from django.db.models import Count, Sum

    totals = TournamentPlayerMatchStats.objects.filter(player=user).aggregate(
        matches=Count("player_stats_id"), kills=Sum("kills"),
    )
    return {"afc_stats": {
        "matches": totals["matches"] or 0,
        "kills": totals["kills"] or 0,
    }}


def _ranking_claims(user):
    from afc_rankings.models import PlayerMonthlyScore

    # Newest scored month. `month` is the real ordering key; id order is only incidental.
    latest = (PlayerMonthlyScore.objects
              .filter(player=user)
              .order_by("-month", "-id")
              .first())
    if not latest:
        return {"afc_ranking": None}
    return {"afc_ranking": {
        "points": latest.total_score,
        "rank": latest.rank,
        "month": latest.month.isoformat() if latest.month else None,
    }}


def _standing_claims(user):
    from afc_auth.models import BannedPlayer

    banned = BannedPlayer.objects.filter(banned_player=user).exists()
    return {"afc_standing": {
        "in_good_standing": (not banned) and user.status == "active",
    }}


def _resolvers():
    """scope -> callable(user) -> dict of claims. One entry per scope in settings.SCOPES.

    Kept as a dict rather than if/elif so that adding a scope is one line here, one line
    in models.TOGGLE_TO_SCOPE, and one consent string. Nothing else changes.
    """
    return {
        "profile": _profile_claims,
        # email_verified was hardcoded True, which was a claim AFC could not actually back.
        # It is now derived from is_active, which IS the verification signal here: signup
        # creates the user with is_active=False and verify_code flips it True only once the
        # emailed code is confirmed (afc_auth/views.py, and Google signups set it directly
        # because Google already verified the address). A partner may gate account linking
        # on this flag, so it has to be true or absent, never optimistic.
        "email": lambda u: {"email": u.email, "email_verified": bool(u.is_active)},
        "afc.freefire": lambda u: {"ff_uid": u.uid or None},
        "afc.team": _team_claims,
        "afc.history": _history_claims,
        "afc.stats": _stats_claims,
        "afc.ranking": _ranking_claims,
        "afc.standing": _standing_claims,
    }


def describe_scopes(scopes):
    """Plain-language lines for the consent screen. Kept beside the resolvers so the
    promise made to the player and the data actually released cannot drift.

    The catalogue values are gettext_lazy strings (afc/settings.py), so str() resolves
    each one HERE, against whichever language afc_sso.middleware.SSOLanguageMiddleware
    activated for this request. Forcing them to real str rather than passing the lazy
    proxies on is deliberate: these lines are also returned as JSON by the Connected apps
    API (afc_sso/api.py), and a lazy proxy is not serializable by a plain json.dumps.
    """
    from django.conf import settings

    catalogue = settings.OAUTH2_PROVIDER["SCOPES"]
    return [str(catalogue[s]) for s in sorted(scopes) if s in catalogue and s != "openid"]


def build_claims(user, application, granted_scopes):
    """Return exactly the claims this org may receive about this player, right now."""
    permitted = set(application.allowed_scopes())          # gate 1
    requested = set(granted_scopes or [])                  # gates 2 and 3
    effective = permitted & requested

    # Gate 4: AFC's own rules, applied before any resolver runs.
    # Guarded on CONTACT_SCOPES being non-empty so an empty set costs nothing: _is_adult
    # runs a UserProfile query, and doing that on every token and every userinfo call to
    # subtract nothing is a query per request for no answer.
    if CONTACT_SCOPES and not _is_adult(user):
        effective -= CONTACT_SCOPES
    if not getattr(user, "stats_visible", False):
        effective.discard("afc.stats")
    if getattr(user, "status", "active") != "active":
        # A suspended account still answers "is this player in good standing", because
        # that is the one question a partner has a legitimate reason to ask about it.
        effective &= {"openid", "afc.standing"}

    claims = {}
    for scope, resolve in _resolvers().items():
        if scope in effective:
            claims.update({k: v for k, v in resolve(user).items() if v is not None})

    for forbidden in DENYLIST:
        claims.pop(forbidden, None)
    return claims
