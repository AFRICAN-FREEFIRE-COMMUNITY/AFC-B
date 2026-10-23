# afc_player/views_directory.py
#
# The PUBLIC players directory behind the Players tab of "Teams & Players" (inbox #153 / #161,
# owner 2026-10-05: "let there be a page for players also ... people can choose the teams tab or
# the players tab and search for and view the profiles of players").
#
# WHO IS LISTED, and why not everybody
#   The full account list was locked to admins on 2026-08-11 (get_all_users' docstring): ~6,800
#   accounts answered to strangers in one response, and a username is one of the three things
#   sign-in accepts. This directory does not undo that. It lists only players who are ALREADY
#   public somewhere else on the site:
#     - on a team (every roster is public on the team page), or
#     - with a scored match in a squad or solo event (public on leaderboards and rankings).
#   An account that has done neither stays unlisted, exactly as today. Soft-deleted and suspended
#   accounts are never listed. The answer is paged (at most 50 rows a call), and each row carries
#   named fields only (R71): no uid, email, role, status or ban record.
#
# How it connects:
#   - Route     : GET /player/directory/ (afc_player/urls.py).
#   - Data      : afc_auth.User (+ the first UserProfile for the picture, the same row
#                 basic_player_profile reads), afc_team.TeamMembers for the current team,
#                 TournamentPlayerMatchStats / SoloPlayerMatchStats for "has played".
#   - Consumed  : frontend components/teams/PlayersDirectory.tsx (the Players tab on /teams);
#                 each row links to /players/<username>, the public profile
#                 (get_public_player_stats in views.py).
from functools import lru_cache

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.functions import Coalesce, Lower, NullIf, Trim
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.country_grouping import canonical_country, country_label, expand_country_keys
from afc_auth.models import User, UserProfile
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import SoloPlayerMatchStats, TournamentPlayerMatchStats

# Paging (Best practices section 10): a limit, a ceiling, and whether more remain.
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 50
# A search longer than any username is a mistake or an attack, never a name.
MAX_QUERY_LENGTH = 50
# A search of this many digits or more ALSO finds the player whose Free Fire UID is exactly that
# (inbox #166, owner 2026-10-08: "should be able to find players by also searching for UID").
# Real Free Fire UIDs run 8 to 12 digits; below 6 a run of digits is a name like "000", not a UID.
UID_SEARCH_MIN_DIGITS = 6

# ONE COUNTRY, ONE OPTION. The country columns hold the same country under several spellings
# ('NG' 1,817 and 'Nigeria' 2,892 on production, written by different writers over the years), so
# the raw values are folded with the house normalizer, afc_auth/country_grouping.py, exactly as the
# broadcast audience builder does: an option per country, and a picked country matches every
# spelling of it. The fold is a pycountry lookup, so it is remembered per raw value in-process.
_canonical = lru_cache(maxsize=4096)(canonical_country)


def _country_groups(raw_values):
    """{canonical key: (label, {raw spellings})} for the raw country values present."""
    groups = {}
    for raw in raw_values:
        key = _canonical(raw)
        if key:
            groups.setdefault(key, set()).add(raw)
    return {key: (country_label(key, raws), raws) for key, raws in groups.items()}


def _listed_players():
    """Every player the directory may show, with `shown_country` annotated.

    shown_country follows the public profile's own rule (basic_player_profile): the IP-derived
    country first, the profile country as the fallback, so the filter and the flag on the
    profile always agree.
    """
    in_team = TeamMembers.objects.filter(member=OuterRef("pk"))
    played_squad = TournamentPlayerMatchStats.objects.filter(player=OuterRef("pk"))
    played_solo = SoloPlayerMatchStats.objects.filter(competitor__user=OuterRef("pk"))
    return (
        User.objects
        .exclude(status__in=("deleted", "suspended"))
        .filter(is_active=True)
        .filter(Exists(in_team) | Exists(played_squad) | Exists(played_solo))
        .annotate(shown_country=Coalesce(NullIf("ip_country", Value("")), "country"))
    )


def _rows(request, users):
    """The page's rows: two bulk queries (pictures, teams), never one per player."""
    ids = [u.user_id for u in users]

    def _abs(field):
        return request.build_absolute_uri(field.url) if field else None

    # The FIRST profile row per user, the one every reader resolves (afc_auth canonical_profile).
    pictures = {}
    for prof in UserProfile.objects.filter(user_id__in=ids).order_by("profile_id").only("user_id", "profile_pic"):
        pictures.setdefault(prof.user_id, prof.profile_pic)

    memberships = {}
    for m in TeamMembers.objects.filter(member_id__in=ids).select_related("team"):
        memberships.setdefault(m.member_id, m)

    rows = []
    for u in users:
        m = memberships.get(u.user_id)
        rows.append({
            "username": u.username,
            "country": u.shown_country or "",
            "profile_picture": _abs(pictures.get(u.user_id)),
            "in_game_role": m.in_game_role if m else None,
            "management_role": m.management_role if m else None,
            "team": {
                "team_name": m.team.team_name,
                "team_tag": m.team.team_tag,
                "team_logo": _abs(m.team.team_logo),
            } if m else None,
        })
    return rows


@api_view(["GET"])
def players_directory(request):
    """GET /player/directory/ - the public, paged, searchable list of players.

    AUTH      : none (public by design, R25). Lists only players already public elsewhere; see
                the module header for the rule and why.

    QUERY     : q        optional, part of an in-game name (case-insensitive), at most 50 chars.
                         Six or more digits ALSO match a Free Fire UID, EXACTLY (inbox #166). Never
                         a part of one: the UID is not shown on any public page (rows carry none,
                         R71), and a prefix search would let anybody rebuild a player's UID one
                         digit at a time from which rows come back.
                country  optional, a `value` from `countries` (any spelling of the country
                         also works: it is folded the same way).
                limit    optional int, 1..50, default 24.
                offset   optional int, >= 0, default 0.

    RESPONSE  : 200 {
                  "results":     [ {username, country, profile_picture, in_game_role,
                                    management_role, team: {team_name, team_tag, team_logo}|null} ],
                  "countries":   [ {value, label}, ... ]   # one per country among listed players,
                                                           # spellings folded; `value` is what
                                                           # goes back as ?country=
                  "total_count": int, "has_more": bool, "next_offset": int|null,
                  "limit": int, "offset": int
                }
                400 {message, code}: query_too_long, limit_offset_numbers.

    ORDER     : the Teams tab's order (owner 2026-08-24, "by name, teams that start with numbers,
                then letter a then b etc"): names starting with a digit, then a letter, then
                anything else, each run case-insensitive and ignoring leading spaces; user_id
                breaks ties, so paging never repeats or drops a row.

    CONSUMED BY: frontend components/teams/PlayersDirectory.tsx (the Players tab on /teams).
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) > MAX_QUERY_LENGTH:
        return Response({"message": "That search is too long.", "code": "query_too_long"},
                        status=status.HTTP_400_BAD_REQUEST)
    country = (request.GET.get("country") or "").strip()
    try:
        limit = int(request.GET.get("limit", DEFAULT_PAGE_SIZE))
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        return Response({"message": "limit and offset must be numbers.", "code": "limit_offset_numbers"},
                        status=status.HTTP_400_BAD_REQUEST)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    listed = _listed_players()

    # The filter's options come from everybody listed, before the search and country narrow
    # it, so the dropdown does not change as somebody types.
    raw_values = {c for c in listed.values_list("shown_country", flat=True).distinct() if c}
    groups = _country_groups(raw_values)
    countries = sorted(({"value": key, "label": label} for key, (label, _raws) in groups.items()),
                       key=lambda c: c["label"].casefold())
    label_of = {raw: label for _key, (label, raws) in groups.items() for raw in raws}

    found = listed
    if q:
        matches = Q(username__icontains=q)
        # ASCII digits only (str.isdigit() is True for superscripts too), the same rule a UID is
        # stored by (afc_auth/identifiers.py uid_format_error).
        if len(q) >= UID_SEARCH_MIN_DIGITS and all("0" <= ch <= "9" for ch in q):
            matches |= Q(uid=q)
        found = found.filter(matches)
    if country:
        found = found.filter(shown_country__in=expand_country_keys([country], raw_values))
    found = found.annotate(
        name_bucket=Case(
            When(username__regex=r"^\s*[0-9]", then=Value(0)),
            When(username__iregex=r"^\s*[a-z]", then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ),
    ).order_by("name_bucket", Lower(Trim("username")), "user_id")

    total_count = found.count()
    page = list(found.only("user_id", "username", "ip_country", "country")[offset:offset + limit])
    has_more = offset + len(page) < total_count

    rows = _rows(request, page)
    for row in rows:
        row["country"] = label_of.get(row["country"], row["country"])

    return Response({
        "results": rows,
        "countries": countries,
        "total_count": total_count,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "limit": limit,
        "offset": offset,
    }, status=status.HTTP_200_OK)
