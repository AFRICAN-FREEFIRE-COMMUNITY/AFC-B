"""
afc_auth/feature_interest.py - "I want this" on a feature that does not exist yet.

WHY IT IS GENERIC AND NOT `FantasyLeagueInterest`
    The first user is the Fantasy League coming-soon page (owner 2026-08-16), but the shape of the
    question - "one person, one feature, are they interested" - has nothing to do with fantasy
    football. A table per coming-soon page would mean a model, a migration, two endpoints and a
    frontend client every time AFC wants to gauge appetite for something, and the fifth one would
    be copied from the fourth. One table with a `feature` key costs nothing extra now and nothing
    at all next time.

WHY IT REQUIRES A LOGIN
    The number is only worth reading if it counts PEOPLE. An anonymous tick is a click, and a click
    can be repeated by one person with a bored finger, which turns the number into noise at exactly
    the moment somebody is using it to decide whether to build the thing. A signed-in tick is one
    person, enforced by the unique constraint rather than by trusting the client.

WHY IT IS A TOGGLE AND NOT AN INSERT
    Somebody who ticks by accident must be able to untick. "Interested" is a current opinion, not an
    event that happened, so the row is deleted rather than flagged: a table of `interested=False`
    rows would be a list of people who changed their mind, which nobody asked for and which is
    worth nothing.

HOW IT CONNECTS
    Model: afc_auth.models.FeatureInterest.
    Routes: GET/POST auth/feature-interest/ (afc_auth/urls.py).
    Read by: frontend app/(user)/fantasy/page.tsx, the Fantasy League coming-soon page.
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import FeatureInterest
from .views import validate_token

# The features a client may ask about. An allow-list rather than a free-text key: without it the
# table fills with typos ("fantasy-league", "fantasyLeague", "fantacy_league") that each count
# separately, and the number the owner reads is quietly wrong.
KNOWN_FEATURES = {
    "fantasy_league": "Free Fire Fantasy League",
}


def _payload(feature, user):
    """The one shape both endpoints answer with, so the client has a single thing to render."""
    return {
        "feature": feature,
        # Whether THIS person has ticked it. False for a signed-out reader, who still sees the count.
        "interested": (
            bool(user) and FeatureInterest.objects.filter(feature=feature, user=user).exists()
        ),
        "count": FeatureInterest.objects.filter(feature=feature).count(),
    }


def _user_or_none(request):
    """The signed-in user, or None. Reading the count must work signed out, so a missing or expired
    token is not an error here - it just means there is no personal tick to report. Same shape as
    afc_polls.views._user_from_request, which is the house pattern for an endpoint where being
    signed out is a normal state."""
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    return validate_token(header.split(" ", 1)[1])


@api_view(["GET", "POST"])
def feature_interest(request):
    """GET/POST auth/feature-interest/ - how many people want a feature, and whether you are one.

    GET   ?feature=<key>              -> {feature, interested, count}    (no auth required)
    POST  {feature, interested: bool} -> {feature, interested, count}    (auth REQUIRED)

    POST is idempotent in both directions: ticking twice leaves one row, unticking something never
    ticked is a no-op that still answers with the current state. That matters because the button is
    the kind a bored finger presses repeatedly, and because a double-submit on a slow connection
    must not produce a different answer from a single one.

    CONSUMED BY: frontend app/(user)/fantasy/page.tsx.
    """
    feature = (request.GET.get("feature") if request.method == "GET"
               else request.data.get("feature")) or ""
    feature = feature.strip()
    if feature not in KNOWN_FEATURES:
        return Response(
            {"message": f"Unknown feature. Expected one of: {', '.join(sorted(KNOWN_FEATURES))}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "GET":
        return Response(_payload(feature, _user_or_none(request)), status=status.HTTP_200_OK)

    user = _user_or_none(request)
    if user is None:
        # 401 rather than a silent no-op: the button changes state on the client, so a write that
        # quietly did nothing would leave the page claiming a tick that was never recorded.
        return Response({"message": "Sign in to register your interest."},
                        status=status.HTTP_401_UNAUTHORIZED)

    wants = bool(request.data.get("interested", True))
    if wants:
        FeatureInterest.objects.get_or_create(feature=feature, user=user)
    else:
        FeatureInterest.objects.filter(feature=feature, user=user).delete()
    return Response(_payload(feature, user), status=status.HTTP_200_OK)
