"""
afc_draws/views.py - the HTTP face of the group draw (owner 2026-09-12, Phase 1).

Routes (afc_draws/urls.py, mounted at draws/):
    POST draws/stages/<stage_id>/create/     organizer/admin  deal + seal the cards      -> board
    POST draws/<draw_id>/open/               organizer/admin  {closes_at: ISO, auto_place_at_close?} -> board
    POST draws/<draw_id>/window/             organizer/admin  {closes_at?: ISO, auto_place_at_close?} while open -> board
    POST draws/<draw_id>/close/              organizer/admin  {place_rest?: bool} publish; stragglers dealt or left -> board
    POST draws/<draw_id>/remind/             organizer/admin  in-app + email to everyone unpicked -> board + {reminded}
    POST draws/<draw_id>/reset/              organizer/admin  delete the draw            -> {message}
    GET  draws/<draw_id>/board/              public (Bearer optional: adds "viewer")     -> board
    GET  draws/events/<event_id>/            public (Bearer optional)                    -> {draws:[board...]}
    POST draws/<draw_id>/pick/               captain / solo player {card_number, tournament_team_id?} -> board

Auth: Bearer session token, the same validate_token everything else uses. Reads are public because
the board is meant to be watched by everyone in the lobby; a token only adds what the viewer may
do. Rules and errors live in afc_draws/services.py (DrawError carries the HTTP status).

Consumed by: frontend lib/draws.ts -> GroupDrawBoard (event page) and GroupDrawCard (organizer /
admin edit page, Actions tab).
"""
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_tournament_and_scrims.models import Event, Stages

from . import services
from .models import StageDraw


def _auth_user(request, required=True):
    """Resolve the Bearer caller. Returns (user, error_response). With required=False a missing
    header is fine (public read) but a BAD token is still refused, so a stale cookie is noticed."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        if required:
            return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
        return None, None
    user = validate_token(auth.split(" ", 1)[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _manager_or_403(user, stage):
    if not services.user_may_run_draw(user, stage):
        return Response({"message": "You do not have permission to run this stage's draw."}, status=403)
    return None


def _parse_when(raw):
    """An ISO 8601 datetime from the body, made aware in the server zone when it comes naive."""
    when = parse_datetime(str(raw)) if raw else None
    if when is not None and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return when


def _as_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _run(fn, *args, **kwargs):
    """Call a service; a DrawError becomes its HTTP answer."""
    try:
        return fn(*args, **kwargs), None
    except services.DrawError as exc:
        return None, Response({"message": exc.message}, status=exc.status)


@api_view(["POST"])
def create_draw(request, stage_id):
    """Deal and seal the cards for a stage. Nobody can pick until open."""
    user, err = _auth_user(request)
    if err:
        return err
    stage = get_object_or_404(Stages, stage_id=stage_id)
    denied = _manager_or_403(user, stage)
    if denied:
        return denied
    draw, err = _run(services.deal, stage, user)
    if err:
        return err
    return Response(services.serialize_board(draw, user), status=201)


@api_view(["POST"])
def open_draw(request, draw_id):
    """Open the window. Body: {closes_at: ISO 8601 datetime in the future, auto_place_at_close?:
    bool (default true) - whether whoever has not picked by then is dealt in automatically}."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    closes_at = _parse_when(request.data.get("closes_at"))
    if closes_at is None:
        return Response({"message": "closes_at must be an ISO 8601 datetime."}, status=400)
    auto_place = _as_bool(request.data.get("auto_place_at_close"), default=True)
    draw, err = _run(services.open_draw, draw, closes_at, auto_place)
    if err:
        return err
    return Response(services.serialize_board(draw, user))


@api_view(["POST"])
def update_window(request, draw_id):
    """Change an OPEN draw's close time and/or straggler choice (owner 2026-09-12). Body:
    {closes_at?: ISO 8601 in the future, auto_place_at_close?: bool}; at least one of them."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    raw_when = request.data.get("closes_at")
    closes_at = _parse_when(raw_when)
    if raw_when and closes_at is None:
        return Response({"message": "closes_at must be an ISO 8601 datetime."}, status=400)
    auto_place = _as_bool(request.data.get("auto_place_at_close"))
    draw, err = _run(services.update_window, draw, closes_at, auto_place)
    if err:
        return err
    return Response(services.serialize_board(draw, user))


@api_view(["POST"])
def close_draw(request, draw_id):
    """Close now. Body: {place_rest?: bool}. Everyone unpicked is dealt into the remaining cards
    when place_rest is true (default: the choice made at open), otherwise left for the organizer."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    place_rest = _as_bool(request.data.get("place_rest"))
    draw, err = _run(services.close_draw, draw, place_rest)
    if err:
        return err
    return Response(services.serialize_board(draw, user))


@api_view(["POST"])
def remind(request, draw_id):
    """Tell everyone who has not picked, in-app and by email. One per 10 minutes per draw."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    count, err = _run(services.remind, draw, user)
    if err:
        return err
    draw.refresh_from_db()
    out = services.serialize_board(draw, user)
    out["reminded"] = count
    return Response(out)


@api_view(["POST"])
def reset_draw(request, draw_id):
    """Delete the draw and the group rows it wrote. Refused once the stage has a result."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    _, err = _run(services.reset, draw)
    if err:
        return err
    return Response({"message": "Draw reset."})


@api_view(["GET"])
def board(request, draw_id):
    """The public board. A Bearer token adds `viewer` (what this person may pick for)."""
    user, err = _auth_user(request, required=False)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    return Response(services.serialize_board(draw, user))


@api_view(["GET"])
def event_draws(request, event_id):
    """Every draw of an event, one board per stage that has one. The event page reads this once
    and renders a board per stage."""
    user, err = _auth_user(request, required=False)
    if err:
        return err
    event = get_object_or_404(Event, event_id=event_id)
    draws = StageDraw.objects.filter(stage__event=event).select_related("stage").order_by("stage_id")
    return Response({"draws": [services.serialize_board(d, user) for d in draws]})


@api_view(["POST"])
def pick_card(request, draw_id):
    """Turn a card over. Body: {card_number, tournament_team_id?}. Answers with the whole board so
    the caller can render the reveal and the updated table in one go."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    _, err = _run(
        services.pick, draw, user, request.data.get("card_number"), request.data.get("tournament_team_id"),
    )
    if err:
        return err
    draw.refresh_from_db()
    return Response(services.serialize_board(draw, user))
