"""
afc_fantasy.views - the fan-facing half of the Fantasy League.

HOUSE IDIOMS (mirrors afc_polls.views / afc_feedback.views)
    - Function-based @api_view, Bearer SessionToken resolved by afc_auth.views.validate_token.
    - Errors: Response({"message": ...}, status=4xx).
    - Pagination envelope {results, has_more, next_offset, total_count}, limit <= 100, default 25.

ENDPOINTS (mounted at fantasy/ via afc/urls.py)
    GET  fantasy/                       list_leagues    PUBLIC, auth optional
    GET  fantasy/<slug>/                league_detail   PUBLIC, auth optional
    GET  fantasy/<slug>/players/        league_players  PUBLIC, the priced pool for the builder
    GET  fantasy/<slug>/standings/      league_table    PUBLIC
    GET  fantasy/<slug>/my-squad/       my_squad        AUTH REQUIRED
    PUT  fantasy/<slug>/my-squad/       save_squad      AUTH REQUIRED

THE THREE RULES THAT MATTER MOST HERE
  1. EVERY SQUAD RULE IS RE-CHECKED ON SAVE. The builder shows the same breakdown live, but that
     is a courtesy to the UI. squad_rules.check_squad runs again inside save_squad before anything
     is written, because a squad that breaks the budget is not a cosmetic problem: it wins.
  2. A LOCKED LEAGUE TAKES NO WRITES, and the lock is evaluated on the request rather than trusted
     from the stored status. A league whose lock time passed between the last scheduled sweep and
     this request is locked NOW, or the last entry in is the one that saw the first result.
  3. SCORES ARE NEVER READ FROM A SQUAD. They come from scoring.standings, which derives them from
     current match stats, so a corrected result moves the table rather than leaving it stale.

CONSUMED BY
    frontend app/(user)/fantasy/page.tsx                -> list_leagues
    frontend app/(user)/fantasy/[slug]/page.tsx         -> league_detail + league_table
    frontend app/(user)/fantasy/[slug]/build/page.tsx   -> league_players + my_squad + save_squad
"""
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.audience import parse_audience_spec, resolve_audience, spec_is_empty
from afc_auth.views import validate_token

from .models import FantasyLeague, FantasyScoringRules, FantasySquad, PlayerPrice, SquadPick
from .permissions import can_manage_league
from .scoring import standings
from .squad_rules import check_squad

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


# ── auth helpers (same shape as afc_polls.views) ──────────────────────────────────────────────
def _user_from_request(request):
    """The signed-in user, or None. Used by the PUBLIC endpoints, where being signed out is a
    normal state: a league page renders fine, it just cannot say whether YOU may enter."""
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    return validate_token(header.split(" ", 1)[1])


def _require_user(request):
    """(user, error_response). Entering a league always needs an account: the table has to count
    people, and an anonymous entry is a row nobody can be awarded."""
    user = _user_from_request(request)
    if not user:
        return None, Response({"message": "You need to be signed in to do this"},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def _paginate(request, queryset):
    """The house pagination envelope. Never returns an unbounded list."""
    try:
        limit = min(int(request.GET.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    return rows, {"has_more": offset + len(rows) < total,
                  "next_offset": offset + len(rows), "total_count": total}


def _may_enter(league, user):
    """Whether `user` passes this league's audience. Empty spec means anyone with an account.

    Uses afc_auth.audience, the SAME engine polls and broadcasts resolve, so the people who may
    enter are exactly the people an admin would have picked to announce it to.
    """
    if not user:
        return False
    spec = parse_audience_spec(league.eligibility_spec or {})
    if spec_is_empty(spec):
        return True
    return resolve_audience(spec).filter(pk=user.pk).exists()


def _sync_lock(league):
    """Lock the league if its moment has passed. Returns the league.

    Called on every read AND every write rather than left to a scheduled task. A task alone leaves
    a window between two runs in which a fan can enter after the first match has started, and the
    person who slips through that window is the person who already knows a result.
    """
    if league.should_lock_now():
        league.status = "locked"
        league.locked_at = timezone.now()
        league.save(update_fields=["status", "locked_at", "updated_at"])
        FantasySquad.objects.filter(league=league, locked_at__isnull=True).update(
            locked_at=league.locked_at)
    return league


def _serialize_league(league, user=None):
    """One league, plus what THIS viewer may do with it."""
    return {
        "slug": league.slug,
        "name": league.name,
        "description": league.description,
        "status": league.status,
        "scope": league.scope,
        "event": {"event_id": league.event_id, "event_name": league.event.event_name},
        "squad_size": league.squad_size,
        "max_per_team": league.max_per_team,
        "captain_multiplier": league.captain_multiplier,
        "use_budget": league.use_budget,
        "budget_seeds": league.budget_seeds if league.use_budget else None,
        "team_premium_seeds": league.team_premium_seeds,
        "entry_type": league.entry_type,
        "entry_fee": str(league.entry_fee) if league.entry_fee is not None else None,
        "entry_fee_currency": league.entry_fee_currency or None,
        # The real moment, not the bare TimeField: the client renders this in the viewer's
        # timezone, and a time with no date cannot be rendered as one. See
        # FantasyLeague.event_starts_at for why the two columns have to be combined.
        "locks_at": league.locks_at or league.event_starts_at(),
        "locked_at": league.locked_at,
        "is_locked": league.is_locked,
        "entries": league.squads.count(),
        # Said explicitly rather than left for the client to infer from status + eligibility, so
        # there is ONE authority on whether the button is live.
        "can_enter": bool(user) and not league.is_locked and league.status == "open"
                     and _may_enter(league, user),
        "has_entered": bool(user) and league.squads.filter(user=user).exists(),
    }


# ── PUBLIC ────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def list_leagues(request):
    """GET fantasy/ - the leagues a fan can see.

    Drafts are hidden from everyone but the people who may manage them: a league still being set up
    has no prices and no pool, so showing it would offer a game that cannot be played.

    Consumed by: frontend app/(user)/fantasy/page.tsx.
    """
    user = _user_from_request(request)
    qs = FantasyLeague.objects.select_related("event").exclude(status="draft")
    event_id = request.GET.get("event_id")
    if event_id:
        qs = qs.filter(event_id=event_id)
    rows, meta = _paginate(request, qs)
    return Response({
        "results": [_serialize_league(_sync_lock(l), user) for l in rows],
        "pagination": meta,
    })


@api_view(["GET"])
def league_detail(request, slug):
    """GET fantasy/<slug>/ - one league, and this viewer's standing with it.

    Consumed by: frontend app/(user)/fantasy/[slug]/page.tsx.
    """
    league = FantasyLeague.objects.select_related("event").filter(slug=slug).first()
    if not league:
        return Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)
    user = _user_from_request(request)
    if league.status == "draft" and not can_manage_league(user, league):
        # 404, not 403: a draft league's existence is not public information.
        return Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(_serialize_league(_sync_lock(league), user))


@api_view(["GET"])
def league_players(request, slug):
    """GET fantasy/<slug>/players/ - the priced pool the squad builder picks from.

    Every row carries the REASON for its price. That is not decoration: a price a fan can check is
    a price they do not argue with twice, and it is the whole justification for pricing players by
    rank rather than by something cleverer that cannot be explained in one line.

    Consumed by: frontend app/(user)/fantasy/[slug]/build/page.tsx.
    """
    league = FantasyLeague.objects.select_related("event").filter(slug=slug).first()
    if not league:
        return Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)

    qs = (PlayerPrice.objects.filter(league=league)
          .select_related("player", "team").order_by("-price_seeds", "player__username"))
    rows, meta = _paginate(request, qs)
    return Response({
        "results": [{
            "player_id": p.player_id,
            "username": p.player.username,
            "team": ({"team_id": p.team_id, "team_name": p.team.team_name} if p.team else None),
            "price_seeds": p.price_seeds,
            "is_unproven": p.is_unproven,
            "reason": p.reason,
        } for p in rows],
        "pagination": meta,
        "budget_seeds": league.budget_seeds if league.use_budget else None,
        "squad_size": league.squad_size,
        "max_per_team": league.max_per_team,
    })


@api_view(["GET"])
def league_table(request, slug):
    """GET fantasy/<slug>/standings/ - the table, recomputed from current results.

    `computed_from_live_results` is returned so the page can say so. AFC corrects results, the
    table moves when it does, and a fan who drops a place deserves to know that is why.

    Consumed by: frontend app/(user)/fantasy/[slug]/page.tsx.
    """
    league = FantasyLeague.objects.select_related("event").filter(slug=slug).first()
    if not league:
        return Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)
    rows = standings(league)
    return Response({
        "results": [{
            "position": r["position"],
            "squad_id": r["squad"].squad_id,
            "squad_name": r["squad"].squad_name or r["squad"].user.username,
            "username": r["squad"].user.username,
            # The id, so the page can highlight YOUR row without comparing display names.
            # AFC players rename themselves often, and a name match would quietly stop
            # highlighting the moment somebody did.
            "user_id": r["squad"].user_id,
            "total": r["total"],
            "matches": r["matches"],
        } for r in rows],
        "computed_from_live_results": True,
    })


# ── ENTERING ──────────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "PUT"])
def my_squad(request, slug):
    """GET / PUT fantasy/<slug>/my-squad/ - read or save YOUR squad. Auth required.

    PUT body: ``{"squad_name": "...", "picks": [{"player_id": 1, "is_captain": true}, ...],
                 "dry_run": false}``

    `dry_run` returns the rule breakdown WITHOUT writing, which is what the builder calls on every
    change so the checklist is always the server's opinion rather than a second implementation of
    the rules in TypeScript that can drift from this one.

    Response 409 when the league is locked, 403 when the viewer may not enter, 400 with the full
    rule breakdown when the squad is illegal.

    Consumed by: frontend app/(user)/fantasy/[slug]/build/page.tsx.
    """
    user, err = _require_user(request)
    if err:
        return err
    league = FantasyLeague.objects.select_related("event").filter(slug=slug).first()
    if not league:
        return Response({"message": "League not found."}, status=status.HTTP_404_NOT_FOUND)
    _sync_lock(league)

    squad = FantasySquad.objects.filter(league=league, user=user).prefetch_related("picks").first()

    if request.method == "GET":
        return Response({
            "has_squad": bool(squad),
            "squad_name": squad.squad_name if squad else "",
            "spent_seeds": squad.spent_seeds if squad else 0,
            "is_locked": league.is_locked,
            "picks": ([{"player_id": p.player_id, "is_captain": p.is_captain,
                        "price_seeds": p.price_seeds} for p in squad.picks.all()]
                      if squad else []),
        })

    # ── PUT ───────────────────────────────────────────────────────────────────────────────────
    if league.status != "open":
        return Response(
            {"message": ("Picks are final for this league." if league.is_locked
                         else "This league is not open for entries yet.")},
            status=status.HTTP_409_CONFLICT,
        )
    if not _may_enter(league, user):
        return Response({"message": "You are not eligible to enter this league."},
                        status=status.HTTP_403_FORBIDDEN)

    picks = request.data.get("picks") or []
    if not isinstance(picks, list):
        return Response({"message": "`picks` must be a list."},
                        status=status.HTTP_400_BAD_REQUEST)

    prices = {p.player_id: p for p in PlayerPrice.objects.filter(league=league)}
    verdict = check_squad(league, picks, prices)

    if request.data.get("dry_run"):
        # Writes nothing. The builder calls this on every change so its checklist is always THIS
        # function's answer and can never drift from the one that actually gates the save.
        return Response({"ok": verdict["ok"], "spent": verdict["spent"],
                         "rules": verdict["rules"]}, status=status.HTTP_200_OK)

    if not verdict["ok"]:
        return Response({"message": "That squad is not valid yet.", "rules": verdict["rules"],
                         "spent": verdict["spent"]}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        squad, _created = FantasySquad.objects.get_or_create(league=league, user=user)
        squad.squad_name = (request.data.get("squad_name") or "").strip()[:80]
        squad.spent_seeds = verdict["spent"]
        squad.save()
        # Replaced wholesale rather than diffed: a squad IS its five picks, and rebuilding is both
        # simpler and immune to a partial update leaving two captains behind.
        squad.picks.all().delete()
        SquadPick.objects.bulk_create([
            SquadPick(squad=squad, player_id=p["player_id"],
                      is_captain=bool(p.get("is_captain")),
                      price_seeds=prices[p["player_id"]].price_seeds)
            for p in picks
        ])

    return Response({"message": "Squad saved.", "spent": verdict["spent"],
                     "rules": verdict["rules"]}, status=status.HTTP_200_OK)
