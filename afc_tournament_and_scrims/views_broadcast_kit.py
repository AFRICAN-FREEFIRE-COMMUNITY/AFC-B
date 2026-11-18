# ── Broadcast Kit endpoints (owner 2026-07-03; client folder pieces 2026-11-18, inbox #618) ───────────
# Gate: _broadcast_gate (AFC event admin OR organizer with can_edit_events on the event) - the same
# gate the overlay/media surfaces use, so organizers can pull the kit for their own events.
#
#   GET  events/<id>/broadcast-kit/            -> readiness summary (per team: slot, assigned letter,
#                                                 headpic id, flag letter yes/no, has-logo + logo_url,
#                                                 what the name file carries per nation mode, player
#                                                 uid/image/role coverage), `one_room`, and the game's
#                                                 own name lists the card matches uploads against.
#                                                 Optional ?stage_id= to scope to a stage, and
#                                                 ?group_id= to one lobby of it (inbox #602).
#   POST events/<id>/broadcast-kit/download/   -> the zip (attachment). multipart body:
#                                                 caster_name?, caster_uid?, stage_id?, group_id?,
#                                                 nation_mode? (team | country | role),
#                                                 flag_banner? (file), flag_logo_size? (30..95),
#                                                 flag_logo_height? (10..90),
#                                                 team_billboard? (file), billboard_place? (left |
#                                                 centre | right), skybox? (file), sky_shape? (title |
#                                                 logo), hud_pics? (files, many), map_skins? (files,
#                                                 many), billboard? (file: the older card's field, now
#                                                 the BillBoard.png map skin).
#                                                 Refusals: 400 {message, code, field} with code
#                                                 not_an_image | image_too_large | too_many_files.
# Letters per room (inbox #602): two lobbies of one stage may now share a letter, and each team's flag
# file is NAMED after its letter, so a stage-wide zip could hold two logos under one name (the last
# one wins). The group_id scope builds one lobby's zip, with that stage's letters, so nothing collides.
# Frontend consumer: components/overlay/BroadcastKitCard.tsx.

import io

from django.http import HttpResponse
from PIL import Image
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .views import _broadcast_gate
from .broadcast_kit import build_broadcast_kit, build_kit_summary, is_one_room, kit_names
from . import client_files as cf

# Uploads (R70): every file is decoded by Pillow before use, only raster formats, 10 MB each. The kit
# draws on the decoded pixels and writes fresh PNGs, so nothing the organizer sent reaches the zip as-is.
MAX_HUD_FILES = len(cf.HUD_PANELS)
MAX_SKIN_FILES = len(cf.MAP_SKINS)


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


def _image_bytes(uploaded):
    """(raw bytes, None) when `uploaded` is a raster picture under the cap, else (None, code). The same
    test as afc_auth.image_utils.require_image_upload, without its re-encode: a panel or a skin keeps
    its own pixels (that helper would turn a picture without transparency into a JPEG)."""
    from afc_auth.image_utils import IMAGE_FORMATS, IMAGE_TOO_LARGE, MAX_IMAGE_BYTES, NOT_AN_IMAGE
    size = getattr(uploaded, "size", None)
    if size is not None and size > MAX_IMAGE_BYTES:
        return None, IMAGE_TOO_LARGE
    try:
        uploaded.seek(0)
        raw = uploaded.read()
    except Exception:
        return None, NOT_AN_IMAGE
    if len(raw) > MAX_IMAGE_BYTES:
        return None, IMAGE_TOO_LARGE
    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = (img.format or "").upper()
            img.verify()
    except Exception:
        return None, NOT_AN_IMAGE
    if fmt not in IMAGE_FORMATS:
        return None, NOT_AN_IMAGE
    return raw, None


_REFUSAL = {
    "not_an_image": "%s is not a picture the kit can use (JPEG, PNG, WEBP or GIF).",
    "image_too_large": "%s is bigger than 10 MB.",
}


def _refuse(code, field, name):
    return Response({"message": _REFUSAL[code] % (name or field), "code": code, "field": field}, status=400)


def _int_in(value, low, high, default):
    try:
        return min(high, max(low, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


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
        # Billboards and the seating sheet only mean something for one room (inbox #617).
        "one_room": is_one_room(event, stage, group),
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
        "teams": build_kit_summary(event, stage=stage, group=group, request=request),
        # The game's own names (client_files.py): the card shows which uploads will be used.
        "names": kit_names(),
    })


@api_view(["POST"])
def broadcast_kit_download(request, event_id):
    """Build + return the kit zip for this event (optionally scoped to a stage or one lobby)."""
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

    # Single pictures: each decoded before use (R70, 2026-09-22).
    singles = {}
    for field in ("flag_banner", "team_billboard", "skybox", "billboard"):
        f = request.FILES.get(field)
        if f is None:
            continue
        raw, code = _image_bytes(f)
        if code:
            return _refuse(code, field, getattr(f, "name", ""))
        singles[field] = raw

    # Named sets: only the game's own names reach the zip; anything else is listed in HOW_TO_USE.
    hud_list, skin_list = request.FILES.getlist("hud_pics"), request.FILES.getlist("map_skins")
    if len(hud_list) > MAX_HUD_FILES or len(skin_list) > MAX_SKIN_FILES:
        return Response({"message": "At most %d panel pictures and %d map skins in one kit."
                         % (MAX_HUD_FILES, MAX_SKIN_FILES), "code": "too_many_files",
                         "field": "hud_pics" if len(hud_list) > MAX_HUD_FILES else "map_skins"}, status=400)
    hud_files, skin_files, unknown = {}, {}, []
    for field, files, match in (("hud_pics", hud_list, cf.hud_panel_name),
                                ("map_skins", skin_list, cf.map_skin_file)):
        target_of = hud_files if field == "hud_pics" else skin_files
        for f in files:
            name = getattr(f, "name", "") or ""
            target = match(name)
            if not target:
                unknown.append(name)
                continue
            raw, code = _image_bytes(f)
            if code:
                return _refuse(code, field, name)
            target_of[target] = raw

    data = build_broadcast_kit(
        event, stage=stage, group=group, caster_name=caster_name, caster_uid=caster_uid,
        billboard_bytes=singles.get("billboard"), skybox_bytes=singles.get("skybox"),
        nation_mode=(request.data.get("nation_mode") or "team").strip(),
        flag_banner_bytes=singles.get("flag_banner"),
        flag_logo_size=_int_in(request.data.get("flag_logo_size"), 30, 95, 62),
        flag_logo_height=_int_in(request.data.get("flag_logo_height"), 10, 90, 34),
        billboard_art_bytes=singles.get("team_billboard"),
        billboard_place=(request.data.get("billboard_place") or "right").strip(),
        sky_shape=(request.data.get("sky_shape") or "title").strip(),
        hud_files=list(hud_files.items()), skin_files=list(skin_files.items()),
        extra_notes=["The game reads no file named %s, so it was left out." % n for n in unknown],
    )

    slug = (getattr(event, "slug", None) or ("event-%d" % event.event_id))
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="broadcast-kit-%s.zip"' % slug
    return resp
