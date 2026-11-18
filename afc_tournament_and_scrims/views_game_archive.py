# ── GAME FILE ARCHIVE: the endpoints (owner 2026-11-18, inbox #603 #606 #611 "save everything") ────────────────
# The store itself (allowlist, dedupe, map links, space guard) is game_archive.py; this module is the door.
# Plan: tasks/game-archive-plan-2026-11-18.md, Task 2.
#
# SENDING (AFC Capture 1.7.0, afc_capture/archive.py, with the event's X-Upload-Token; a signed-in admin may too):
#   POST events/<event_id>/archive/files/check/   {sha256: [hex64, ... at most 200]}  -> {stored: [sha256 ...]}
#        asked before any bytes move, so a file already kept is never sent twice
#   POST events/<event_id>/archive/files/         multipart file (gzip, or plain), name (path relative to the
#        game's Free Fire_64_Data folder), sha256 (of the ORIGINAL bytes), optional stage, group (Capture's set
#        stage/group, the filing used until the file's map is known)
#        -> {file_id, result: stored|duplicate|kept_longer|replaced, kind, maps: [match_id ...]}
#
# READING (the Archive tab, AFC_Frontend app/(a)/a/leaderboards/_components/GameArchiveTab.tsx, Bearer only):
#   GET  events/<event_id>/archive/                       the tree: stages > groups > maps with file counts and
#        sizes, the "not matched" pile, totals and the disk state
#   GET  events/<event_id>/archive/files/?stage=|group=|match=|unmatched=1&limit=50&offset=0   one page of files
#   GET  events/<event_id>/archive/files/<file_id>/download/   the ORIGINAL bytes, an attachment, nosniff
#
# WHO: an upload token of THIS event may only check and send (it is the observer PC's key). Signed in: AFC event
# admins for every event, an organizer holding can_upload_results for their org's events (the same people who
# may upload results). A file id of another event answers exactly like a missing one (R88).

import gzip
import hashlib
import io
import re
import time

from django.core.cache import cache
from django.db.models import Count, Sum
from django.http import StreamingHttpResponse
from django.utils.http import content_disposition_header
from rest_framework import serializers
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from . import game_archive as ga
from . import game_files as gf
from .models import Event, GameFile, Match, StageGroups, Stages
from .views import _is_event_admin, _resolve_event_upload_token

SENDS_PER_MINUTE = 120        # per upload token or signed-in user; an evening's backfill is a few hundred files
PAGE_DEFAULT, PAGE_MAX = 50, 200
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _refuse(message, code, status=400):
    return Response({"message": message, "code": code}, status=status)


def _archive_gate(request, event_id, *, sending):
    """(event, user, error). An upload token must belong to THIS event and may only send; a signed-in user must
    be an AFC event admin or hold can_upload_results on the event's org. Checked before anything is read."""
    value, token = _resolve_event_upload_token(request)
    if value:
        if token is None or token.event_id != event_id:
            return None, None, _refuse("Invalid or revoked upload token.", "invalid_revoked_upload_token", 403)
        if not sending:
            return None, None, _refuse("The capture key can only send files.", "archive_token_send_only", 403)
        return token.event, token.created_by, None
    auth = request.headers.get("Authorization") or ""
    user = validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") and " " in auth else None
    if not user:
        return None, None, _refuse("Invalid or expired session token.", "invalid_expired_session_token", 401)
    event = Event.objects.select_related("organization").filter(event_id=event_id).first()
    if not event:
        return None, None, _refuse("Event not found.", "event_not_found", 404)
    if not (_is_event_admin(user) or org_can_event(user, "can_upload_results", event)):
        return None, None, _refuse("You do not have permission for this event.", "not_permission_event", 403)
    return event, user, None


def _rate_limited(request, user):
    """True once this sender passed SENDS_PER_MINUTE in the current minute."""
    _value, token = _resolve_event_upload_token(request)
    who = f"t{token.pk}" if token else f"u{getattr(user, 'pk', 0)}"
    key = f"gamearchive:rate:{who}:{int(time.time() // 60)}"
    cache.add(key, 0, 90)
    try:
        return cache.incr(key) > SENDS_PER_MINUTE
    except ValueError:
        return False


def _unpack(data, cap):
    """The original bytes of an upload (gunzipped when it is gzip), never more than `cap`. ValueError when over."""
    if data[:2] != b"\x1f\x8b":
        if len(data) > cap:
            raise ValueError("too_large")
        return data
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            if out.tell() > cap:
                raise ValueError("too_large")
    return out.getvalue()


class CheckBody(serializers.Serializer):
    sha256 = serializers.ListField(child=serializers.RegexField(_HEX64), max_length=200, allow_empty=True)


class SendForm(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    sha256 = serializers.RegexField(_HEX64)
    stage = serializers.IntegerField(min_value=1, required=False)
    group = serializers.IntegerField(min_value=1, required=False)


@api_view(["POST"])
@parser_classes([JSONParser])
def archive_check(request, event_id):
    """Which of these files (by sha256 of their original bytes) the archive already keeps for this event."""
    event, _user, err = _archive_gate(request, event_id, sending=True)
    if err:
        return err
    body = CheckBody(data=request.data)
    if not body.is_valid():
        return _refuse("Send a list of up to 200 sha256 values.", "archive_bad_check")
    wanted = body.validated_data["sha256"]
    stored = set(GameFile.objects.filter(event=event, sha256__in=wanted).values_list("sha256", flat=True))
    return Response({"stored": [s for s in wanted if s in stored]})


def _send(request, event, user):
    form = SendForm(data={k: request.data.get(k) for k in ("name", "sha256", "stage", "group")
                          if request.data.get(k) not in (None, "")})
    if not form.is_valid():
        return _refuse("Send the file's name and the sha256 of its bytes.", "archive_bad_send")
    v = form.validated_data
    named = ga.classify(v["name"])
    if named is None:
        return _refuse("That file is not one the archive keeps.", "archive_name_not_allowed")
    stage = group = None
    if v.get("stage"):
        stage = Stages.objects.filter(stage_id=v["stage"], event=event).first()
    if v.get("group"):
        group = StageGroups.objects.filter(group_id=v["group"], stage__event=event).first()
    if (v.get("stage") and stage is None) or (v.get("group") and group is None):
        return _refuse("That stage or group is not part of this event.", "archive_bad_stage_group")
    up = request.FILES.get("file")
    if not up:
        return _refuse("Attach the file.", "archive_file_required")
    if up.size > gf.MAX_UPLOAD_BYTES:
        return _refuse("That file is larger than the archive keeps for its kind.", "archive_file_too_large", 413)
    try:
        data = _unpack(up.read(), ga.KIND_CAPS.get(named[0], ga.DEFAULT_CAP))
    except ValueError:
        return _refuse("That file is larger than the archive keeps for its kind.", "archive_file_too_large", 413)
    except (OSError, EOFError):
        return _refuse("That file arrived damaged. It will be sent again.", "archive_file_damaged")
    if hashlib.sha256(data).hexdigest() != v["sha256"]:
        return _refuse("The file does not match its sha256. It will be sent again.", "archive_sha_mismatch")
    try:
        row, result = ga.store_bytes(event, v["name"], data, source="capture", user=user, stage=stage, group=group)
    except ga.ArchiveRefused as refused:
        return _refuse(refused.message, refused.code, refused.status)
    return Response({"file_id": row.token, "result": result, "kind": row.kind,
                     "maps": list(row.matches.values_list("match_id", flat=True))})


def _page(request):
    def num(key, default, top):
        try:
            return max(0, min(int(request.query_params.get(key, default)), top))
        except (TypeError, ValueError):
            return default
    return num("limit", PAGE_DEFAULT, PAGE_MAX) or PAGE_DEFAULT, num("offset", 0, 10 ** 9)


def _list(request, event):
    """One page of the event's files, optionally one stage, group, map or the "not matched" pile."""
    qs = GameFile.objects.filter(event=event)
    p = request.query_params
    scope = None
    if p.get("match"):
        scope = Match.objects.filter(match_id=_int(p["match"]), group__stage__event=event).first()
        qs = qs.filter(matches=scope)
    elif p.get("group"):
        scope = StageGroups.objects.filter(group_id=_int(p["group"]), stage__event=event).first()
        qs = qs.filter(matches__group=scope)
    elif p.get("stage"):
        scope = Stages.objects.filter(stage_id=_int(p["stage"]), event=event).first()
        qs = qs.filter(matches__group__stage=scope)
    elif p.get("unmatched"):
        qs = qs.filter(matches__isnull=True)
        scope = True
    else:
        scope = True
    if scope is None:
        return _refuse("That part of the event was not found.", "archive_scope_not_found", 404)
    qs = qs.distinct().order_by("created_at", "pk")
    limit, offset = _page(request)
    total = qs.count()
    rows = qs[offset:offset + limit]
    return Response({
        "results": [{"file_id": r.token, "kind": r.kind, "name": r.original_name, "size_bytes": r.size_bytes,
                     "created_at": r.created_at.isoformat(), "source": r.source,
                     "game_match_id": r.game_match_id} for r in rows],
        "total_count": total,
        "has_more": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
    })


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
def archive_files(request, event_id):
    """POST: send one file (Capture). GET: one page of the event's files (the Archive tab)."""
    sending = request.method == "POST"
    event, user, err = _archive_gate(request, event_id, sending=sending)
    if err:
        return err
    if not sending:
        return _list(request, event)
    if _rate_limited(request, user):
        return _refuse("Too many files at once. The rest will follow in a minute.", "archive_rate_limited", 429)
    return _send(request, event, user)


@api_view(["GET"])
def archive_tree(request, event_id):
    """The event's archive as stages > groups > maps, each with its file count and size."""
    event, _user, err = _archive_gate(request, event_id, sending=False)
    if err:
        return err
    files = GameFile.objects.filter(event=event)
    totals = files.aggregate(n=Count("pk"), b=Sum("size_bytes"))
    loose = files.filter(matches__isnull=True).aggregate(n=Count("pk"), b=Sum("size_bytes"))
    links = list(GameFile.matches.through.objects.filter(gamefile__event=event)
                 .values_list("gamefile_id", "match_id"))
    sizes = dict(files.values_list("pk", "size_bytes"))
    by_map = {}
    for file_id, match_id in links:
        by_map.setdefault(match_id, set()).add(file_id)

    def summary(file_ids):
        return {"files": len(file_ids), "bytes": sum(sizes.get(f, 0) for f in file_ids)}

    stages = []
    for st in Stages.objects.filter(event=event).order_by("stage_order", "start_date", "stage_id"):
        stage_files, groups = set(), []
        for g in st.groups.all().order_by("group_order", "playing_date", "playing_time", "group_id"):
            group_files, maps = set(), []
            for m in Match.objects.filter(group=g).order_by("match_number", "match_id"):
                ids = by_map.get(m.match_id, set())
                group_files |= ids
                maps.append({"match_id": m.match_id, "map_number": m.match_number, **summary(ids)})
            stage_files |= group_files
            groups.append({"group_id": g.group_id, "group_name": g.group_name, **summary(group_files),
                           "maps": maps})
        stages.append({"stage_id": st.stage_id, "stage_name": st.stage_name, **summary(stage_files),
                       "groups": groups})
    return Response({
        "space": ga.space_state(),
        "totals": {"files": totals["n"] or 0, "bytes": totals["b"] or 0},
        "unmatched": {"files": loose["n"] or 0, "bytes": loose["b"] or 0},
        "stages": stages,
    })


@api_view(["GET"])
def archive_download(request, event_id, file_token):
    """One archived file, gunzipped back to its original bytes, as a download. Never shown inline."""
    event, _user, err = _archive_gate(request, event_id, sending=False)
    if err:
        return err
    row = GameFile.objects.filter(event=event, token=str(file_token)[:32]).first()
    if row is None:
        return _refuse("That file was not found.", "archive_file_not_found", 404)
    # Streamed, not FileResponse: FileResponse measures a file by seeking to its end, which a gzip stream refuses.
    resp = StreamingHttpResponse(_original_bytes(row), content_type="application/octet-stream")
    resp["Content-Disposition"] = content_disposition_header(True, row.original_name)
    resp["Content-Length"] = str(row.size_bytes)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def _original_bytes(row, chunk=256 * 1024):
    """The stored gzip, unpacked as it streams; the file is closed when the last piece has gone."""
    with row.file.open("rb") as fh, gzip.GzipFile(fileobj=fh) as gz:
        while True:
            piece = gz.read(chunk)
            if not piece:
                return
            yield piece
