# ── Broadcast Kit endpoints (owner 2026-07-03) ──────────────────────────────────
# Gate: _broadcast_gate (AFC event admin OR organizer with can_edit_events on the event) - the same
# gate the overlay/media surfaces use, so organizers can pull the kit for their own events.
#
#   GET  events/<id>/broadcast-kit/            -> readiness summary (per team: assigned letter,
#                                                 headpic id, has-logo, player uid/image coverage).
#                                                 Optional ?stage_id= to scope to a stage.
#   POST events/<id>/broadcast-kit/download/   -> the zip (attachment). multipart body:
#                                                 caster_name?, caster_uid?, stage_id?,
#                                                 billboard? (file), skybox? (file).
# Frontend consumer: components/overlay/BroadcastKitCard.tsx.

from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .views import _broadcast_gate
from .broadcast_kit import build_broadcast_kit, build_kit_summary


def _resolve_stage(event, stage_id):
    if not stage_id:
        return None
    from .models import Stages
    try:
        return Stages.objects.get(stage_id=stage_id, event=event)
    except (Stages.DoesNotExist, ValueError, TypeError):
        return None


@api_view(["GET"])
def broadcast_kit_summary(request, event_id):
    """Readiness report for the Broadcast Kit UI (what will/won't be in the zip)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    stage = _resolve_stage(event, request.GET.get("stage_id"))
    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "stage_id": stage.stage_id if stage else None,
        "teams": build_kit_summary(event, stage=stage),
    })


@api_view(["POST"])
def broadcast_kit_download(request, event_id):
    """Build + return the kit zip for this event (optionally scoped to a stage)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    stage = _resolve_stage(event, request.data.get("stage_id"))
    caster_name = (request.data.get("caster_name") or "").strip()
    caster_uid = (request.data.get("caster_uid") or "").strip() or None

    billboard = request.FILES.get("billboard")
    skybox = request.FILES.get("skybox")
    billboard_bytes = billboard.read() if billboard else None
    skybox_bytes = skybox.read() if skybox else None

    data = build_broadcast_kit(
        event, stage=stage, caster_name=caster_name, caster_uid=caster_uid,
        billboard_bytes=billboard_bytes, skybox_bytes=skybox_bytes,
    )

    slug = (getattr(event, "slug", None) or ("event-%d" % event.event_id))
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="broadcast-kit-%s.zip"' % slug
    return resp
