"""
afc_tournament_and_scrims.views_capture_pending — PENDING CAPTURE BUCKET ("decide later").

PURPOSE (owner 2026-07-05, complaint D)
    The desktop AFC Capture client posts each round with a stage + group but NO match_id. When every
    configured map slot for that group is already scored and an EXTRA game lands, the backend no longer
    silently invents a new "map" (the complaint-D bug). Instead upload_team_match_result returns a 409
    asking the operator to decide, and the desktop prompt offers three choices:
        [Attribute as new map]  ->  re-upload with attribution="new"
        [Replace map: <slot>]   ->  re-upload with attribution="replace:<match_id>"
        [Decide later]          ->  re-upload with attribution="pending"  ->  PARKED HERE
    "Decide later" reliably parks the raw upload in a PendingCaptureUpload row (NEVER dropped) so an
    admin/organizer resolves it later from the website. This module is the web side of that bucket:
    create (helper), list, resolve, discard.

WHY A SEPARATE MODULE
    Same isolation rationale as event_links.py / seeding_management.py — keep the 20k-line views.py from
    growing. The heavy scoring lives in views.upload_team_match_result; RESOLVE deliberately re-invokes
    that SAME view (via a synthetic request) so a parked upload is scored by the identical code path a
    live upload uses — no second scoring implementation to drift.

HOW IT CONNECTS
    - Model: PendingCaptureUpload (afc_tournament_and_scrims.models) — stores {file_text, file_name,
      file_type, stage_id, group_id} + a small parsed summary + status (pending|resolved|discarded).
    - Intake: views.upload_team_match_result calls _create_pending_capture() on attribution="pending".
    - Scoring: resolve_pending_capture() rebuilds the file and calls views.upload_team_match_result with
      attribution="new" (a fresh slot) or match_id=<mid> (overwrite), forwarding the operator's Bearer.
    - Permissions: AFC event admin OR an organizer with can_upload_results on the event's owning org —
      the SAME gate the other result endpoints use.
    - Consumed by: frontend lib/pendingCaptures.ts -> the admin event leaderboard editor's Flagging tab
      (PendingCapturesPanel).

ENDPOINTS (mounted under events/ via afc_tournament_and_scrims/urls.py)
    GET    events/<event_id>/pending-captures/                       list_pending_captures
    POST   events/<event_id>/pending-captures/<pending_id>/resolve/  resolve_pending_capture
    POST   events/<event_id>/pending-captures/<pending_id>/discard/  discard_pending_capture
"""

from __future__ import annotations

from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from .models import Event, Match, PendingCaptureUpload, StageGroups


# --------------------------------------------------------------------------- #
# Intake helper — called by views.upload_team_match_result on attribution="pending"
# --------------------------------------------------------------------------- #
def _parse_capture_summary(text: str) -> dict:
    """Build a small human-readable digest of a MatchResult file for the resolve UI, WITHOUT scoring.

    Reuses the SAME parser regexes the real scoring path uses (views.TEAM_BLOCK_RE / PLAYER_RE), so what
    the operator previews in the pending list matches what will be scored on resolve. Best-effort: a file
    that doesn't parse yields an empty summary (the raw text is still stored for a manual look)."""
    from .views import TEAM_BLOCK_RE, PLAYER_RE  # lazy: avoid a load-time circular import with views.py

    teams = []
    total_players = 0
    for block in TEAM_BLOCK_RE.finditer(text or ""):
        players_block = block.group("players_block")
        players = list(PLAYER_RE.finditer(players_block))
        kills = sum(int(p.group("kills")) for p in players)
        total_players += len(players)
        teams.append({
            "team_name": block.group("team_name").strip(),
            "placement": int(block.group("placement")),
            "players": len(players),
            "kills": kills,
        })
    return {"teams": teams, "team_count": len(teams), "player_count": total_players}


def _create_pending_capture(*, event, stage, group, upload_token, uploaded_by,
                            file_text, file_name, file_type) -> PendingCaptureUpload:
    """Persist a "decide later" capture. Stores the raw file text + the client's set stage/group so
    resolve_pending_capture can re-score it verbatim, plus a parsed digest for the list UI. Returns the
    created row. Called from views.upload_team_match_result's attribution="pending" branch."""
    return PendingCaptureUpload.objects.create(
        event=event,
        stage=stage,
        group=group,
        upload_token=upload_token,
        uploaded_by=uploaded_by,
        file_name=file_name or "",
        raw_payload={
            "file_text": file_text,
            "file_name": file_name or "",
            "file_type": file_type or "",
            "stage_id": stage.stage_id if stage else None,
            "group_id": group.group_id if group else None,
        },
        parsed_summary=_parse_capture_summary(file_text),
        status="pending",
    )


# --------------------------------------------------------------------------- #
# Shared gate — same authority as the other result endpoints
# --------------------------------------------------------------------------- #
def _pending_gate(request, event_id):
    """(user, event, error_response) — exactly one of (user+event) / error is set. Bearer auth + the
    standard result-endpoint gate: an AFC event admin OR an organizer with can_upload_results on the
    event's owning org (native org=None events stay admin-only via org_can_event)."""
    from .views import _is_event_admin  # lazy: avoid a load-time circular import with views.py

    auth = request.headers.get("Authorization") or ""
    user = validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") else None
    if not user:
        return None, None, Response({"message": "Invalid or expired session token."}, status=401)
    event = Event.objects.filter(event_id=event_id).first()
    if not event:
        return None, None, Response({"message": "Event not found."}, status=404)
    if not (_is_event_admin(user) or org_can_event(user, "can_upload_results", event)):
        return None, None, Response({"message": "You do not have permission for this event."}, status=403)
    return user, event, None


def _serialize_pending(p: PendingCaptureUpload) -> dict:
    """One pending row for the list UI. Echoes the parsed digest + the client's set stage/group (a hint
    for the resolver's default target) + who/when. NEVER returns the raw file text (kept server-side)."""
    return {
        "id": p.id,
        "status": p.status,
        "file_name": p.file_name,
        "stage_id": p.stage_id,
        "group_id": p.group_id,
        "stage_name": p.stage.stage_name if p.stage else None,
        "group_name": p.group.group_name if p.group else None,
        "summary": p.parsed_summary or {},
        "uploaded_by": (p.uploaded_by.username if p.uploaded_by else None),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "resolution": p.resolution or "",
        "resolved_match_id": p.resolved_match_id,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@api_view(["GET"])
def list_pending_captures(request, event_id):
    """GET events/<event_id>/pending-captures/ — the event's UNRESOLVED parked captures (status=pending),
    plus the event's stage/group structure so the resolve dialog can offer a target picker. Consumed by
    the admin leaderboard editor's PendingCapturesPanel (frontend lib/pendingCaptures.ts)."""
    user, event, err = _pending_gate(request, event_id)
    if err:
        return err

    pendings = list(PendingCaptureUpload.objects.filter(event=event, status="pending")
                    .select_related("stage", "group", "uploaded_by"))

    # Stage/group structure for the resolve dialog's target picker (mirrors _event_stage_structure).
    from .models import Stages
    stages_out = []
    for st in Stages.objects.filter(event=event).order_by("stage_order", "start_date", "stage_id"):
        stages_out.append({
            "stage_id": st.stage_id,
            "stage_name": st.stage_name,
            "groups": [
                {
                    "group_id": g.group_id,
                    "group_name": g.group_name,
                    # Existing slots so the dialog can build the "replace which map" dropdown per group.
                    "match_slots": [
                        {"match_id": m.match_id, "map": m.match_map or "", "scored": bool(m.result_inputted)}
                        for m in g.matches.all().order_by("match_number", "match_id")
                    ],
                }
                for g in st.groups.all().order_by("group_order", "playing_date", "playing_time", "group_id")
            ],
        })

    return Response({
        "event_id": event.event_id,
        "pending": [_serialize_pending(p) for p in pendings],
        "pending_count": len(pendings),
        "stages": stages_out,
    }, status=200)


@api_view(["POST"])
def resolve_pending_capture(request, event_id, pending_id):
    """POST events/<event_id>/pending-captures/<pending_id>/resolve/ — score a parked capture into a
    match slot, then mark it resolved. Body:
        { attribution: "new" | "replace:<match_id>", group_id, stage_id? }
    "new"     -> create + score a fresh extra map slot in <group_id>.
    "replace" -> overwrite match <match_id>'s result.

    RUNS THE SAME SCORING PATH: this rebuilds the stored file and re-invokes views.upload_team_match_result
    (forwarding the operator's Bearer token) so the parked upload is scored by the identical code — flag
    derivation, name-matching, recompute, auto-complete, all included. No second scoring implementation."""
    from .views import upload_team_match_result  # lazy: avoid a load-time circular import

    user, event, err = _pending_gate(request, event_id)
    if err:
        return err

    pending = PendingCaptureUpload.objects.filter(id=pending_id, event=event).first()
    if not pending:
        return Response({"message": "Pending capture not found."}, status=404)
    if pending.status != "pending":
        return Response({"message": f"This capture is already {pending.status}."}, status=400)

    data = request.data or {}
    attribution = str(data.get("attribution") or "").strip().lower()
    file_text = (pending.raw_payload or {}).get("file_text") or ""
    file_name = (pending.raw_payload or {}).get("file_name") or "MatchResult.log"
    file_type = (pending.raw_payload or {}).get("file_type") or ""

    # ── Resolve WHERE to score, validating everything belongs to THIS event (never cross-event) ──
    upload_data = {"file_type": file_type}
    if attribution == "new":
        group_id = data.get("group_id") or (pending.raw_payload or {}).get("group_id")
        grp = StageGroups.objects.filter(group_id=group_id, stage__event=event).first() if group_id else None
        if grp is None:
            return Response({"message": "Choose a valid group for the new map."}, status=400)
        # The inner view creates + scores a brand-new slot in this group (attribution="new").
        upload_data["group"] = grp.group_id
        upload_data["attribution"] = "new"
    elif attribution.startswith("replace:"):
        rid = attribution.split(":", 1)[1].strip()
        if not rid.isdigit():
            return Response({"message": "replace target match_id must be numeric."}, status=400)
        match = Match.objects.filter(match_id=int(rid), group__stage__event=event).first()
        if match is None:
            return Response({"message": "Replacement match not found for this event."}, status=400)
        # An explicit match_id makes the inner view overwrite that slot (idempotent re-derive).
        upload_data["match_id"] = match.match_id
    else:
        return Response({"message": "attribution must be 'new' or 'replace:<match_id>'."}, status=400)

    # ── Re-invoke the real scoring view with a synthetic multipart request ──
    factory = APIRequestFactory()
    file_obj = SimpleUploadedFile(file_name, file_text.encode("utf-8"), content_type="text/plain")
    upload_data["file"] = file_obj
    synthetic = factory.post("/events/upload-team-match-result/", upload_data, format="multipart")
    # Forward the operator's Bearer so the inner view authorizes as the SAME admin/organizer (the gate
    # above already confirmed they may upload for this event). We do NOT forward any capture token.
    synthetic.META["HTTP_AUTHORIZATION"] = request.META.get("HTTP_AUTHORIZATION", "")

    resp = upload_team_match_result(synthetic)
    status_code = getattr(resp, "status_code", 500)
    body = getattr(resp, "data", {}) or {}
    if not (200 <= status_code < 300):
        # Scoring rejected the file (bad parse, permission, ...). Leave the pending row PENDING so the
        # operator can retry / discard — never lose it. Surface the inner error verbatim.
        return Response({"message": body.get("message", "Could not score this capture."), "detail": body},
                        status=status_code)

    scored_match_id = body.get("match_id")
    pending.status = "resolved"
    pending.resolution = attribution
    pending.resolved_match_id = scored_match_id
    pending.resolved_by = user
    pending.resolved_at = timezone.now()
    pending.save(update_fields=["status", "resolution", "resolved_match", "resolved_by", "resolved_at"])

    return Response({
        "message": "Pending capture scored.",
        "pending_id": pending.id,
        "match_id": scored_match_id,
        "result": body,
    }, status=200)


@api_view(["POST"])
def discard_pending_capture(request, event_id, pending_id):
    """POST events/<event_id>/pending-captures/<pending_id>/discard/ — drop a parked capture the operator
    judged a genuine mis-capture (wrong event, duplicate run). Marks it discarded (kept for the audit
    trail, not scored). Same gate as list/resolve."""
    user, event, err = _pending_gate(request, event_id)
    if err:
        return err

    pending = PendingCaptureUpload.objects.filter(id=pending_id, event=event).first()
    if not pending:
        return Response({"message": "Pending capture not found."}, status=404)
    if pending.status != "pending":
        return Response({"message": f"This capture is already {pending.status}."}, status=400)

    pending.status = "discarded"
    pending.resolution = "discarded"
    pending.resolved_by = user
    pending.resolved_at = timezone.now()
    pending.save(update_fields=["status", "resolution", "resolved_by", "resolved_at"])
    return Response({"message": "Pending capture discarded.", "pending_id": pending.id}, status=200)
