# ── EVENT OVERLAYS — saved, named broadcast overlays (owner 2026-07-02, studio v2) ──
# An overlay is a persistent per-event entity: created from a design (kind="leaderboard") or as a
# scene (kind="timer"), named/renamed, duplicated, deleted. Its public link NEVER changes:
# /overlay/view/<Event.overlay_token>/<overlay_id> polls overlay_config below, so edits from the
# studio (design, stage/group, animations, timer trigger) update what the SAME link renders live.
#
# ENDPOINTS (Bearer via _broadcast_gate — AFC event admin OR org that can_edit_events):
#   GET  events/<event_id>/overlays/                    -> list
#   POST events/<event_id>/overlays/create/             -> {name, kind, config} -> row
#   POST events/<event_id>/overlays/<overlay_id>/update/    -> {name?, config?, active?} -> row
#   POST events/<event_id>/overlays/<overlay_id>/duplicate/ -> copy (name + " copy")
#   POST events/<event_id>/overlays/<overlay_id>/delete/    -> gone
# PUBLIC (the overlay token is the read capability, mirrors overlay_feed):
#   GET  events/overlay/config/?token=&overlay=<id>     -> {kind, name, config, active, server_time}
#
# CONSUMED BY: FE lib/overlay.ts overlaysApi/overlayConfigApi -> studio app/(a)/a/overlays/[eventId]
# (cards) + the stable renderer app/overlay/view/[token]/[overlayId]/page.tsx.

from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Event, EventOverlay
from .views import _broadcast_gate, _org_hidden

VALID_KINDS = {k for k, _ in EventOverlay.KINDS}


def _serialize(row):
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "config": row.config or {},
        "active": row.active,
        "updated_at": row.updated_at,
    }


@api_view(["GET"])
def list_overlays(request, event_id):
    """GET events/<event_id>/overlays/ — every saved overlay, in creation order (the studio's cards)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    return Response(
        {"overlays": [_serialize(r) for r in EventOverlay.objects.filter(event=event)]},
        status=200,
    )


@api_view(["POST"])
def create_overlay(request, event_id):
    """POST events/<event_id>/overlays/create/ {name, kind, config} — new overlay (e.g. picked a
    design -> a leaderboard overlay preconfigured with it; or a fresh timer scene)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    kind = (request.data.get("kind") or "leaderboard").strip().lower()
    if kind not in VALID_KINDS:
        return Response({"message": f"Unknown overlay kind '{kind}'."}, status=400)
    name = (request.data.get("name") or "").strip()[:80] or kind.title()
    config = request.data.get("config") if isinstance(request.data.get("config"), dict) else {}
    row = EventOverlay.objects.create(
        event=event, name=name, kind=kind, config=config,
        # Scenes start hidden (transparent) until triggered; leaderboards render immediately.
        active=(kind != "timer"),
    )
    return Response(_serialize(row), status=201)


def _get_row(event, overlay_id):
    return EventOverlay.objects.filter(event=event, id=overlay_id).first()


@api_view(["POST"])
def update_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/update/ {name?, config?, active?} — partial edit.
    config REPLACES wholesale when given (the FE always sends the full config object); rename via
    name; scenes trigger/hide via active. The public link keeps rendering the new state."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    if "name" in request.data:
        name = (request.data.get("name") or "").strip()[:80]
        if name:
            row.name = name
    if isinstance(request.data.get("config"), dict):
        row.config = request.data["config"]
    if "active" in request.data:
        row.active = bool(request.data.get("active"))
    row.save()
    return Response(_serialize(row), status=200)


@api_view(["POST"])
def duplicate_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/duplicate/ — copy config+kind under "<name> copy"
    (a fresh id = a fresh stable link, so the copy can diverge without touching the original)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    copy = EventOverlay.objects.create(
        event=event, name=f"{row.name} copy"[:80], kind=row.kind,
        config=dict(row.config or {}), active=row.active if row.kind != "timer" else False,
    )
    return Response(_serialize(copy), status=201)


@api_view(["POST"])
def delete_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/delete/ — remove it (its link then 404s)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    row.delete()
    return Response({"message": "Overlay deleted."}, status=200)


@api_view(["GET"])
def overlay_config(request):
    """GET events/overlay/config/?token=&overlay=<id> — PUBLIC config the stable renderer polls.
    Token = Event.overlay_token (same capability as overlay_feed); a hidden org's event 404s.
    server_time lets the timer correct client-clock drift. A deleted overlay 404s (OBS shows blank)."""
    token = (request.query_params.get("token") or "").strip()
    try:
        overlay_id = int(request.query_params.get("overlay") or 0)
    except (TypeError, ValueError):
        overlay_id = 0
    if not token or not overlay_id:
        return Response({"message": "token and overlay are required."}, status=400)
    event = Event.objects.select_related("organization").filter(overlay_token=token).first()
    if not event or _org_hidden(event):
        return Response({"message": "Not found."}, status=404)
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Not found."}, status=404)
    return Response({
        "kind": row.kind,
        "name": row.name,
        "config": row.config or {},
        "active": row.active,
        "event_id": event.event_id,
        "server_time": timezone.now(),
    }, status=200)
