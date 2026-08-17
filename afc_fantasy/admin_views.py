"""
afc_fantasy.admin_views - creating and running a fantasy league.

WHO MAY USE THESE: afc_fantasy.permissions.can_manage_league - head admins, plus organizers on
their own events. Not a new permission; see that module for what it inherits.

ENDPOINTS (mounted at fantasy/admin/ via afc/urls.py)
    GET    fantasy/admin/leagues/                  admin_list_leagues
    POST   fantasy/admin/leagues/                  admin_create_league
    GET    fantasy/admin/leagues/<slug>/           admin_league_detail
    PATCH  fantasy/admin/leagues/<slug>/           admin_update_league
    POST   fantasy/admin/leagues/<slug>/prices/    admin_build_prices   (preview or write)
    POST   fantasy/admin/leagues/<slug>/open/      admin_open_league
    POST   fantasy/admin/leagues/<slug>/recompute/ admin_recompute

THE ORDER THAT MATTERS
    draft -> (price the pool) -> open -> locked -> settled

    A league CANNOT be opened without prices, and that is enforced rather than documented: an open
    budget league with no PlayerPrice rows shows a fan an empty squad builder and no way to
    understand why. The check is here, at the one door into "open".

WHAT AN ADMIN MAY NEVER DO
    Change the rules of a league that has entrants. Squad size, the cap per team, the captain
    multiplier and the budget are frozen the moment the league opens, because every squad already
    built was built against them. A change would silently invalidate entries somebody spent time on
    and could hand the table to whoever entered last.

CONSUMED BY: frontend app/(a)/a/fantasy/**.
"""
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_tournament_and_scrims.models import Event

from .models import FantasyLeague, FantasyScoringRules, PlayerPrice
from .permissions import can_manage_league
from .pricing import apply_prices, band_for, compute_prices
from .roster import eligible_with_teams
from .scoring import recompute_league
from .views import _paginate, _require_user, _serialize_league

# Settings that define the GAME. Editable while a league is a draft, frozen once it opens, because
# every squad already entered was built against them.
FROZEN_ONCE_OPEN = (
    "squad_size", "max_per_team", "captain_multiplier_tenths",
    "use_budget", "budget_seeds", "team_premium_seeds", "entry_type",
)
# Settings that are only ever presentation or timing, so they stay editable.
ALWAYS_EDITABLE = ("name", "description", "locks_at", "eligibility_spec")


def _get_league(user, slug):
    """(league, error_response). One place decides both existence and permission, so a manageable
    league and a visible one can never disagree."""
    league = FantasyLeague.objects.select_related("event").filter(slug=slug).first()
    if not league:
        return None, Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_league(user, league):
        return None, Response({"message": "You cannot manage this league."},
                              status=status.HTTP_403_FORBIDDEN)
    return league, None


@api_view(["GET", "POST"])
def admin_leagues(request):
    """GET / POST fantasy/admin/leagues/ - list the leagues you manage, or create one.

    POST body: ``{"event_id": 12, "name": "...", "squad_size": 5, "max_per_team": 2,
                  "captain_multiplier_tenths": 20, "use_budget": true, "budget_seeds": 100,
                  "team_premium_seeds": 6, "entry_type": "free"}``

    A new league is always a DRAFT. It has no prices yet, so it is not a game anybody could play,
    and letting it be created straight into "open" would put an empty squad builder in front of a
    fan.
    """
    user, err = _require_user(request)
    if err:
        return err

    if request.method == "GET":
        qs = FantasyLeague.objects.select_related("event").order_by("-created_at")
        # Filtered in Python rather than SQL because can_manage_league composes an organizer check
        # that has no queryset form. The admin league list is small by nature (one or two per
        # event), so this is not the place to invent one.
        rows, meta = _paginate(request, qs)
        return Response({
            "results": [_serialize_league(l, user) for l in rows if can_manage_league(user, l)],
            "pagination": meta,
        })

    event = Event.objects.filter(event_id=request.data.get("event_id")).first()
    if not event:
        return Response({"message": "`event_id` must be an existing event."},
                        status=status.HTTP_400_BAD_REQUEST)
    # Permission is asked of an UNSAVED league carrying the intended event, so creating and editing
    # can never disagree about who is allowed (same trick as afc_polls).
    if not can_manage_league(user, FantasyLeague(event=event)):
        return Response({"message": "You cannot create a league on that event."},
                        status=status.HTTP_403_FORBIDDEN)

    name = (request.data.get("name") or f"{event.event_name} Fantasy").strip()[:160]
    squad_size = int(request.data.get("squad_size", 5))
    if not (FantasyLeague.MIN_SQUAD_SIZE <= squad_size <= FantasyLeague.MAX_SQUAD_SIZE):
        return Response(
            {"message": f"`squad_size` must be between {FantasyLeague.MIN_SQUAD_SIZE} and "
                        f"{FantasyLeague.MAX_SQUAD_SIZE}."},
            status=status.HTTP_400_BAD_REQUEST)
    max_per_team = int(request.data.get("max_per_team", 2))
    if not (1 <= max_per_team <= squad_size):
        # A cap above the squad size is not a cap, and a cap of 0 makes every squad illegal. Both
        # are worth refusing rather than saving as a league nobody can enter.
        return Response({"message": f"`max_per_team` must be between 1 and {squad_size}."},
                        status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        league = FantasyLeague.objects.create(
            event=event,
            name=name,
            slug=_unique_slug(name),
            description=(request.data.get("description") or "").strip(),
            squad_size=squad_size,
            max_per_team=max_per_team,
            captain_multiplier_tenths=int(request.data.get("captain_multiplier_tenths", 20)),
            use_budget=bool(request.data.get("use_budget", True)),
            budget_seeds=int(request.data.get("budget_seeds", 100)),
            team_premium_seeds=int(request.data.get("team_premium_seeds", 6)),
            entry_type=request.data.get("entry_type", "free"),
            eligibility_spec=request.data.get("eligibility_spec") or {},
            created_by=user,
        )
        # The scoring row is created WITH the league, never lazily: a league whose scoring row is
        # missing would score every squad zero and look like the results were wrong.
        FantasyScoringRules.objects.create(league=league)
    return Response(_serialize_league(league, user), status=status.HTTP_201_CREATED)


def _unique_slug(name):
    """A slug nobody else holds. Leagues are named after events and AFC re-runs events every year,
    so a collision is normal rather than exotic."""
    base = slugify(name)[:170] or "fantasy-league"
    slug, n = base, 2
    while FantasyLeague.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


@api_view(["GET", "PATCH"])
def admin_league(request, slug):
    """GET / PATCH fantasy/admin/leagues/<slug>/ - read or edit one league.

    PATCH refuses to change a rule of the game once the league is open (see FROZEN_ONCE_OPEN), and
    says which field it refused, because "saved" that silently dropped half the form is worse than
    a refusal.
    """
    user, err = _require_user(request)
    if err:
        return err
    league, err = _get_league(user, slug)
    if err:
        return err

    if request.method == "GET":
        return Response(_serialize_league(league, user))

    frozen = [f for f in FROZEN_ONCE_OPEN if f in request.data]
    if frozen and league.status != "draft":
        return Response(
            {"message": "This league is already open, so the rules of the game cannot change. "
                        f"Refused: {', '.join(frozen)}."},
            status=status.HTTP_409_CONFLICT,
        )
    for field in FROZEN_ONCE_OPEN + ALWAYS_EDITABLE:
        if field in request.data:
            setattr(league, field, request.data[field])
    league.save()
    return Response(_serialize_league(league, user))


@api_view(["POST"])
def admin_build_prices(request, slug):
    """POST fantasy/admin/leagues/<slug>/prices/ - price the pool.

    Body: ``{"dry_run": true}`` to PREVIEW without writing, which is what the admin screen calls
    first: a price list is the one part of this feature an admin will want to look at before
    committing, and a preview costs nothing because pricing.compute_prices is pure.

    Human overrides are never touched by a re-run (see pricing.apply_prices).
    """
    user, err = _require_user(request)
    if err:
        return err
    league, err = _get_league(user, slug)
    if err:
        return err

    entries = eligible_with_teams(league.event)
    if not entries:
        return Response(
            {"message": "Nobody is registered for this event yet, so there is nobody to price."},
            status=status.HTTP_400_BAD_REQUEST)

    floor, ceiling = band_for(league)
    if request.data.get("dry_run"):
        rows = compute_prices(league, entries)
        return Response({
            "dry_run": True, "count": len(rows), "floor": floor, "ceiling": ceiling,
            "results": sorted(
                ({"player_id": r["player_id"], "price_seeds": r["price_seeds"],
                  "is_unproven": r["is_unproven"], "reason": r["reason"]} for r in rows),
                key=lambda r: -r["price_seeds"]),
        })

    written, skipped = apply_prices(league, entries)
    return Response({"written": written, "skipped_overrides": skipped,
                     "floor": floor, "ceiling": ceiling})


@api_view(["POST"])
def admin_open_league(request, slug):
    """POST fantasy/admin/leagues/<slug>/open/ - let fans in.

    REFUSES A BUDGET LEAGUE WITH NO PRICES. Not a warning: an open budget league with no
    PlayerPrice rows renders an empty squad builder and gives the fan no way to understand why, and
    this is the only door into that state.
    """
    user, err = _require_user(request)
    if err:
        return err
    league, err = _get_league(user, slug)
    if err:
        return err
    if league.status != "draft":
        return Response({"message": f"This league is already {league.status}."},
                        status=status.HTTP_409_CONFLICT)
    if league.use_budget and not PlayerPrice.objects.filter(league=league).exists():
        return Response(
            {"message": "Price the players first. A budget league with no prices gives fans an "
                        "empty squad builder."},
            status=status.HTTP_400_BAD_REQUEST)

    league.status = "open"
    league.save(update_fields=["status", "updated_at"])
    return Response(_serialize_league(league, user))


@api_view(["POST"])
def admin_recompute(request, slug):
    """POST fantasy/admin/leagues/<slug>/recompute/ - rebuild every squad's points from current
    results.

    Needed because AFC corrects results: a kill count is fixed, a team is disqualified. This is the
    button that makes the table agree with the results page again, and it is safe to press at any
    time because scoring always REPLACES rather than accumulates.
    """
    user, err = _require_user(request)
    if err:
        return err
    league, err = _get_league(user, slug)
    if err:
        return err
    written = recompute_league(league)
    return Response({"message": "Scores rebuilt from current results.", "rows": written,
                     "computed_at": timezone.now()})
