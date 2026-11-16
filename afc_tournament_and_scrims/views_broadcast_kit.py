# ── Broadcast Kit endpoints (owner 2026-07-03) ──────────────────────────────────
# Gate: _broadcast_gate (AFC event admin OR organizer with can_edit_events on the event) - the same
# gate the overlay/media surfaces use, so organizers can pull the kit for their own events.
#
#   GET  events/<id>/broadcast-kit/            -> readiness summary (per team: assigned letter,
#                                                 headpic id, has-logo, player uid/image coverage).
#                                                 Optional ?stage_id= to scope to a stage, and
#                                                 ?group_id= to one lobby of it (inbox #602).
#   POST events/<id>/broadcast-kit/download/   -> the zip (attachment). multipart body:
#                                                 caster_name?, caster_uid?, stage_id?, group_id?,
#                                                 billboard? (file), skybox? (file).
# Letters per room (inbox #602): two lobbies of one stage may now share a letter, and each team's flag
# file is NAMED after its letter, so a stage-wide zip could hold two logos under one name (the last
# one wins). The group_id scope builds one lobby's zip, with that stage's letters, so nothing collides.
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


def _resolve_group(stage, group_id):
    """The lobby `group_id` of `stage`, or None (no stage, no id, or not a lobby of that stage)."""
    if not stage or not group_id:
        return None
    from .models import StageGroups
    try:
        return StageGroups.objects.get(group_id=group_id, stage=stage)
    except (StageGroups.DoesNotExist, ValueError, TypeError):
        return None


@api_view(["GET"])
def broadcast_kit_summary(request, event_id):
    """Readiness report for the Broadcast Kit UI (what will/won't be in the zip)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    stage = _resolve_stage(event, request.GET.get("stage_id"))
    group = _resolve_group(stage, request.GET.get("group_id"))
    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "stage_id": stage.stage_id if stage else None,
        "group_id": group.group_id if group else None,
        # The stage + lobby pickers on BroadcastKitCard (inbox #602): every stage with its lobbies.
        "stages": [
            {
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "groups": [
                    {"group_id": g.group_id, "group_name": g.group_name}
                    for g in s.groups.all().order_by("group_id")
                ],
            }
            for s in event.stages.all()
        ],
        "teams": build_kit_summary(event, stage=stage, group=group),
    })


@api_view(["POST"])
def broadcast_kit_download(request, event_id):
    """Build + return the kit zip for this event (optionally scoped to a stage)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    stage = _resolve_stage(event, request.data.get("stage_id"))
    group = _resolve_group(stage, request.data.get("group_id"))
    caster_name = (request.data.get("caster_name") or "").strip()
    caster_uid = (request.data.get("caster_uid") or "").strip() or None
    # The caster's Free Fire UID goes into the observer config, so digits only (inbox #89).
    from afc_auth.identifiers import uid_format_error
    uid_error, uid_code = uid_format_error(caster_uid)
    if uid_error:
        return Response({"message": uid_error, "code": uid_code}, status=400)

    billboard = request.FILES.get("billboard")
    # Broadcast art is rendered into the stream overlay, so decode the bytes (R70, 2026-09-22).
    if billboard is not None:
        from afc_auth.image_utils import require_image_upload
        billboard, bad_image = require_image_upload(billboard)
        if bad_image:
            return Response({"message": "The billboard must be a JPEG, PNG, WEBP or GIF under 10 MB.",
                             "code": bad_image}, status=400)
    skybox = request.FILES.get("skybox")
    billboard_bytes = billboard.read() if billboard else None
    skybox_bytes = skybox.read() if skybox else None

    data = build_broadcast_kit(
        event, stage=stage, group=group, caster_name=caster_name, caster_uid=caster_uid,
        billboard_bytes=billboard_bytes, skybox_bytes=skybox_bytes,
    )

    slug = (getattr(event, "slug", None) or ("event-%d" % event.event_id))
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="broadcast-kit-%s.zip"' % slug
    return resp
