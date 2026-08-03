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
CONTACT_SCOPES = {"email"}

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
    return {
        "preferred_username": user.username,
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
        "email": lambda u: {"email": u.email, "email_verified": True},
        "afc.freefire": lambda u: {"ff_uid": u.uid or None},
        "afc.team": _team_claims,
        "afc.history": _history_claims,
        "afc.stats": _stats_claims,
        "afc.ranking": _ranking_claims,
        "afc.standing": _standing_claims,
    }


def describe_scopes(scopes):
    """Plain-language lines for the consent screen. Kept beside the resolvers so the
    promise made to the player and the data actually released cannot drift."""
    from django.conf import settings

    catalogue = settings.OAUTH2_PROVIDER["SCOPES"]
    return [catalogue[s] for s in sorted(scopes) if s in catalogue and s != "openid"]


def build_claims(user, application, granted_scopes):
    """Return exactly the claims this org may receive about this player, right now."""
    permitted = set(application.allowed_scopes())          # gate 1
    requested = set(granted_scopes or [])                  # gates 2 and 3
    effective = permitted & requested

    # Gate 4: AFC's own rules, applied before any resolver runs.
    if not _is_adult(user):
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
