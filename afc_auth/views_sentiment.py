# afc_auth/views_sentiment.py
# ──────────────────────────────────────────────────────────────────────────────
# FAN / HATER sentiment (owner 2026-06-20)
#
# Public "I'm a fan" / "I'm a hater" reactions on a player or team profile. Any
# logged-in user gets ONE stance per subject (fan XOR hater); tapping the active
# stance clears it, tapping the other switches. Counts are public (anyone can GET
# them); setting a stance requires a session.
#
# Mirrors the function-based @api_view + inline-validate_token convention used by
# the rest of afc_auth (see views_player_reports.py).
#
# Endpoints (afc_auth/urls.py):
#   • GET  /auth/sentiment/?subject_type=&target_id=   get_sentiment  (public)
#   • POST /auth/sentiment/set/                         set_sentiment  (auth)
# Frontend: components/profile/FanHater.tsx on the player profile + team page.
# ──────────────────────────────────────────────────────────────────────────────
from django.db.models import Count
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .views import validate_token
from .models import User, ProfileSentiment
from afc_team.models import Team


def _resolve_subject(subject_type, target_id):
    """Return (target_user, target_team, error_message). Exactly one FK is set."""
    if subject_type not in ("player", "team"):
        return None, None, "subject_type must be 'player' or 'team'."
    if not target_id:
        return None, None, "target_id is required."
    if subject_type == "team":
        team = Team.objects.filter(pk=target_id).first()
        if not team:
            return None, None, "Team not found."
        return None, team, None
    user = User.objects.filter(pk=target_id).first()
    if not user:
        return None, None, "Player not found."
    return user, None, None


def _counts(subject_type, target_user, target_team):
    """Public fan/hater counts for a subject. One grouped query."""
    qs = ProfileSentiment.objects.filter(
        target_team=target_team if subject_type == "team" else None,
        target_user=target_user if subject_type == "player" else None,
    )
    rows = qs.values("stance").annotate(n=Count("id"))
    by = {r["stance"]: r["n"] for r in rows}
    return {"fan_count": by.get("fan", 0), "hater_count": by.get("hater", 0)}


def _bearer_user(request):
    """Return the User for a valid Bearer token, or None (no error response - GET is public)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ")[1])


# ──────────────────────────────────────────────────────────────────────────────
# GET /auth/sentiment/?subject_type=&target_id=   — public counts + my stance
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def get_sentiment(request):
    subject_type = request.query_params.get("subject_type")
    target_id = request.query_params.get("target_id")
    target_user, target_team, err = _resolve_subject(subject_type, target_id)
    if err:
        return Response({"message": err}, status=400)

    data = _counts(subject_type, target_user, target_team)

    # The viewer's own stance (null if logged out or no stance yet).
    me = _bearer_user(request)
    my_stance = None
    if me:
        row = ProfileSentiment.objects.filter(
            voter=me,
            target_user=target_user if subject_type == "player" else None,
            target_team=target_team if subject_type == "team" else None,
        ).first()
        my_stance = row.stance if row else None
    data["my_stance"] = my_stance
    return Response(data, status=200)


# ──────────────────────────────────────────────────────────────────────────────
# POST /auth/sentiment/set/   — set / switch / clear the caller's stance
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def set_sentiment(request):
    """Body: { subject_type, target_id, stance: "fan"|"hater" }.

    Toggle rules: tapping the stance you already hold clears it; tapping the other
    switches; first tap sets it. Cannot react to your OWN player profile. Returns the
    fresh counts + your new stance (null if cleared).
    """
    me = _bearer_user(request)
    if not me:
        return Response({"message": "Please log in to react."}, status=401)

    subject_type = request.data.get("subject_type")
    target_id = request.data.get("target_id")
    stance = request.data.get("stance")
    if stance not in ("fan", "hater"):
        return Response({"message": "stance must be 'fan' or 'hater'."}, status=400)

    target_user, target_team, err = _resolve_subject(subject_type, target_id)
    if err:
        return Response({"message": err}, status=400)

    # Cannot fan/hate your own player profile (a team has no single owner-voter guard;
    # owning a team and being a fan of it is harmless).
    if subject_type == "player" and target_user.user_id == me.user_id:
        return Response({"message": "You cannot react to your own profile."}, status=400)

    lookup = {
        "voter": me,
        "target_user": target_user if subject_type == "player" else None,
        "target_team": target_team if subject_type == "team" else None,
    }
    existing = ProfileSentiment.objects.filter(**lookup).first()
    if existing is None:
        ProfileSentiment.objects.create(subject_type=subject_type, stance=stance, **lookup)
        my_stance = stance
    elif existing.stance == stance:
        existing.delete()          # tapped the active stance -> clear it
        my_stance = None
    else:
        existing.stance = stance   # switch fan<->hater
        existing.save(update_fields=["stance", "updated_at"])
        my_stance = stance

    data = _counts(subject_type, target_user, target_team)
    data["my_stance"] = my_stance
    return Response(data, status=200)
