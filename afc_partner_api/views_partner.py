# afc_partner_api/views_partner.py
# ──────────────────────────────────────────────────────────────────────────────
# The seven read-only partner endpoints — the public face of the partner API.
#
# Every view runs the SAME pipeline, in this exact order, via the partner_endpoint
# decorator (so the order can never drift per-view):
#
#   1. authenticate_partner(request)   -> PartnerAuthError  => 401   (who are you?)
#   2. check_rate_limit(key)           -> RateLimitExceeded => 429   (slow down)
#   3. resource toggle (can_read_*)    -> not enabled       => 403   (you're known,
#                                                                      but not entitled
#                                                                      to THIS resource)
#   4. scope filter (partner_visible_events) inside the view -> out-of-scope => 404
#   5. serializer firewall (serialize.py) -> only public + toggled-on fields cross
#
# Two deliberate status choices the spec calls out (and the tests guard):
#   • 403 vs 404. A toggle the partner doesn't HAVE returns 403 (resource_not_enabled)
#     — we admit the resource exists but they aren't entitled. An EVENT the partner
#     can't see returns 404, NOT 403: confirming "this event exists but you can't read
#     it" would leak the existence of private/unpublished events, so out-of-scope reads
#     are indistinguishable from a typo'd slug. (spec §9 landmine)
#   • Lists are always paginated (limit<=100 default 25, offset) and wrapped in a
#     {results, has_more, next_offset, total_count} envelope so a partner can page
#     without ever loading an unbounded result set (best-practice §10).
#
# Resolution pattern shared by the nested resources (stages/matches/standings/teams/
# players): resolve the event via _visible_event_or_404 FIRST (so an out-of-scope slug
# 404s before we touch any child rows), then walk the event's relations and serialize.
# Full spec: WEBSITE/tasks/partner-api-design.md (§9 endpoints).
# ──────────────────────────────────────────────────────────────────────────────
from functools import wraps

from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import serialize
from .auth import authenticate_partner, PartnerAuthError
from .ratelimit import check_rate_limit, RateLimitExceeded
from .scope import partner_visible_events


# ── the pipeline decorator ──────────────────────────────────────────────────────
def partner_endpoint(resource_toggle=None):
    """Wrap a view so it only runs after auth + rate limit + the resource toggle pass.

    ``resource_toggle`` is the name of the Partner.can_read_* boolean that gates this
    endpoint (e.g. "can_read_events"). When set, the partner must have it ON or the
    request is rejected 403 before the view body runs. The wrapped view is called as
    ``fn(request, partner, *args, **kwargs)`` — it receives the authenticated Partner
    so it never re-derives it, and the rate-limit/auth plumbing stays out of the view.
    """
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            # 1) Authenticate the X-API-Key -> (Partner, PartnerApiKey), else 401.
            try:
                partner, key = authenticate_partner(request)
            except PartnerAuthError as exc:
                return Response({"error": str(exc)}, status=401)
            # 2) Count this request against the key's per-minute budget, else 429.
            #    Retry-After tells a well-behaved client exactly how long to back off
            #    (the window is one wall-clock minute — see ratelimit.py).
            try:
                check_rate_limit(key)
            except RateLimitExceeded:
                return Response({"error": "rate_limit_exceeded"}, status=429,
                                headers={"Retry-After": "60"})
            # 3) Resource toggle: the partner is authenticated, but is this endpoint's
            #    resource turned on for them? Toggles default OFF (least privilege), so
            #    a brand-new partner gets 403 on everything until an AFC admin opts in.
            if resource_toggle and not getattr(partner, resource_toggle):
                return Response({"error": "resource_not_enabled"}, status=403)
            # 4) Hand the authenticated partner to the view (scope + serialize happen there).
            return fn(request, partner, *args, **kwargs)
        return inner
    return deco


# ── pagination ──────────────────────────────────────────────────────────────────
# Default + maximum page sizes. The cap is hard: a partner can never request more than
# MAX_LIMIT rows in one call, so no endpoint ever loads an unbounded result set into
# memory (best-practice §10 "pagination, never unbounded").
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _page_params(request):
    """Parse + sanitize ?limit / ?offset. Caps limit at MAX_LIMIT, floors offset at 0,
    and falls back to safe defaults on any malformed value (rather than 500-ing)."""
    try:
        limit = min(int(request.GET.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT, 0
    return (limit if limit >= 1 else DEFAULT_LIMIT, offset if offset >= 0 else 0)


def _envelope(rows, total, offset, limit):
    """The shared pagination envelope: a page of ``rows`` plus the cursor metadata.
    ``next_offset`` is the cursor for the next page, or None when this is the last
    page (``has_more`` False)."""
    nxt = offset + limit
    has_more = nxt < total
    return Response({
        "results": rows,
        "has_more": has_more,
        "next_offset": nxt if has_more else None,
        "total_count": total,
    })


def _paginate(request, qs, serialize_fn, partner):
    """Paginate a queryset: count, slice to the page, run each row through the firewall.

    Used by the queryset-backed endpoints (events/stages/matches/teams). Uses .count()
    + a sliced queryset so only the page's rows are ever fetched from the DB."""
    limit, offset = _page_params(request)
    total = qs.count()
    rows = [serialize_fn(obj, partner) for obj in qs[offset:offset + limit]]
    return _envelope(rows, total, offset, limit)


def _paginate_list(request, rows):
    """Paginate an already-serialized Python list with the same envelope as _paginate.

    Used by standings/players, where the rows are computed (ranked / per-event-folded)
    rather than coming straight from a queryset slice. Same limit/offset rules apply."""
    limit, offset = _page_params(request)
    total = len(rows)
    return _envelope(rows[offset:offset + limit], total, offset, limit)


def _visible_event_or_404(partner, slug):
    """Resolve a slug to an Event the partner is scoped to, or None.

    Goes through partner_visible_events so the publish gate + grant rules apply: an
    unpublished or un-granted event resolves to None here, which each caller turns into
    a 404 (never confirming the event exists — see the module header)."""
    return partner_visible_events(partner).filter(slug=slug).first()


# ── 1. list events ──────────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_events")
def list_events(request, partner):
    """GET /events/ — paginated list of the published events this partner may read,
    newest first."""
    qs = partner_visible_events(partner).order_by("-start_date", "-event_id")
    return _paginate(request, qs, serialize.serialize_event, partner)


# ── 2. event detail ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_events")
def event_detail(request, partner, event_slug):
    """GET /events/<slug>/ — one event's public card, or 404 if out of scope."""
    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)
    return Response(serialize.serialize_event(event, partner))


# ── 3. event stages (groups nested) ─────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_stages")
def event_stages(request, partner, event_slug):
    """GET /events/<slug>/stages/ — paginated stages of the event, each with its groups
    nested. Resolve the event first so an out-of-scope slug 404s before we read stages."""
    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)
    # stage_id order == creation order, which is also the order serialize_stage numbers.
    qs = event.stages.order_by("stage_id")

    def _serialize_stage_with_groups(stage, p):
        # A stage's public row plus its groups (groups are part of the stage resource,
        # gated by the same can_read_stages toggle — they carry no extra stat fields).
        out = serialize.serialize_stage(stage, p)
        out["groups"] = [serialize.serialize_group(g, p) for g in stage.groups.order_by("group_id")]
        return out

    return _paginate(request, qs, _serialize_stage_with_groups, partner)


# ── 4. event matches ────────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_matches")
def event_matches(request, partner, event_slug):
    """GET /events/<slug>/matches/ — paginated matches of the event (room creds stripped
    by the serializer firewall)."""
    from afc_tournament_and_scrims.models import Match

    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)
    # A match belongs to the event through group -> stage -> event. Filter on that chain
    # so we only ever return THIS event's matches.
    qs = (Match.objects
          .filter(group__stage__event=event)
          .order_by("group__stage__stage_id", "match_number", "match_id"))
    return _paginate(request, qs, serialize.serialize_match, partner)


# ── 5. event standings ──────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_standings")
def event_standings(request, partner, event_slug):
    """GET /events/<slug>/standings/ — the event's final ranked standings.

    serialize_standings returns a fully-ranked LIST (not a queryset), so we paginate it
    in memory with the same envelope. The whole event-wide fold is computed once; we
    just slice the ranked rows for the requested page.
    """
    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)
    rows = serialize.serialize_standings(event, partner)  # already ranked + firewalled
    return _paginate_list(request, rows)


# ── 6. event teams ──────────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_teams")
def event_teams(request, partner, event_slug):
    """GET /events/<slug>/teams/ — paginated tournament teams of the event with their
    event-wide aggregated, toggled stats."""
    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)
    # Deterministic, handle-sorted order so pagination is stable across pages.
    qs = event.tournament_teams.order_by("team__team_name", "tournament_team_id")
    return _paginate(request, qs, serialize.serialize_team, partner)


# ── 7. event players ────────────────────────────────────────────────────────────
@api_view(["GET"])
@partner_endpoint("can_read_players")
def event_players(request, partner, event_slug):
    """GET /events/<slug>/players/ — paginated list of the players who recorded stats in
    the event, each with their per-event (NOT lifetime) toggled stats.

    Players are reached through their tournament team so each player's stats fold scoped
    to THIS event (serialize_player needs the tournament_team for the per-event fold —
    see serialize.py). We flatten (player, team) pairs into one list and paginate it,
    because the per-event scoping is keyed on the team, not the user.
    """
    event = _visible_event_or_404(partner, event_slug)
    if not event:
        return Response({"error": "not_found"}, status=404)

    # Build (player, tournament_team) pairs sorted by team then username, then serialize
    # each scoped to the team they played for in THIS event. Stable order => stable paging.
    rows = [
        serialize.serialize_player(player, partner, tournament_team=tteam)
        for tteam in event.tournament_teams.order_by("team__team_name", "tournament_team_id")
        for player in serialize._team_players(tteam)
    ]
    return _paginate_list(request, rows)
