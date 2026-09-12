"""
afc_draws/views.py - the HTTP face of the group draw (owner 2026-09-12, Phase 1).

Routes (afc_draws/urls.py, mounted at draws/):
    POST draws/stages/<stage_id>/create/     organizer/admin  deal + seal the cards      -> board
    POST draws/<draw_id>/open/               organizer/admin  {closes_at: ISO}           -> board
    POST draws/<draw_id>/close/              organizer/admin  deal stragglers, publish   -> board
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
    """Open the window. Body: {closes_at: ISO 8601 datetime, in the future}."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    raw = request.data.get("closes_at")
    closes_at = parse_datetime(str(raw)) if raw else None
    if closes_at is None:
        return Response({"message": "closes_at must be an ISO 8601 datetime."}, status=400)
    if timezone.is_naive(closes_at):
        closes_at = timezone.make_aware(closes_at, timezone.get_current_timezone())
    draw, err = _run(services.open_draw, draw, closes_at)
    if err:
        return err
    return Response(services.serialize_board(draw, user))


@api_view(["POST"])
def close_draw(request, draw_id):
    """Close now: everyone unpicked is dealt into the remaining cards."""
    user, err = _auth_user(request)
    if err:
        return err
    draw = get_object_or_404(StageDraw, draw_id=draw_id)
    denied = _manager_or_403(user, draw.stage)
    if denied:
        return denied
    draw, err = _run(services.close_draw, draw)
    if err:
        return err
    return Response(services.serialize_board(draw, user))


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
