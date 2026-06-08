from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event, is_platform_org_admin
from afc_tournament_and_scrims.models import Match, MatchResultImage

from .models import OCRSession
from .services.gemini import call_gemini, get_prompt_context
from .services.matching import get_registered_players, match_name, detect_team_mismatches

import logging

logger = logging.getLogger(__name__)


def _extract_with_router(image_bytes, mime_type, aliases, team_notes, event_type):
    """LOCAL-FIRST OCR extraction with Gemini fallback (the self-hosted OCR student, P3).

    Returns (raw_output: dict, engine: str). This is the ONE place the upload paths get their
    extraction, so the local-vs-Gemini routing lives in a single auditable spot. Flow:
      1. If local-first is enabled and the local engine is available, run the student
         (services/local_ocr) and ask the confidence gate (services/ocr_confidence) whether
         to trust it.
      2. gate == "local"  -> use the student's output, ZERO Gemini calls (the cost win).
      3. otherwise         -> escalate to Gemini (the teacher), exactly as before.
    Graceful degradation (mirrors the old 503 handling): if the local engine errors or is
    absent we go to Gemini; if Gemini is disabled/unavailable we serve the student's
    best-effort draft so the admin can still review (never a hard fail when SOME engine ran).
    `engine` is returned + persisted (raw_output["_engine"]) so the FE "which engine" badge
    and the training corpus (teacher_model) know the source. raw_output keeps Gemini's exact
    shape {"placements": [...]} so the draft build / match_name / commit path are untouched.
    """
    from django.conf import settings
    from .services import local_ocr, ocr_confidence

    gemini_enabled = getattr(settings, "OCR_GEMINI_FALLBACK", True) and bool(getattr(settings, "GEMINI_API_KEY", None))

    student_json, conf, decision = None, None, "gemini"
    if getattr(settings, "OCR_LOCAL_FIRST", True) and local_ocr.is_available():
        try:
            student_json, conf = local_ocr.get_engine().run(image_bytes, mime_type, aliases, team_notes, event_type)
            decision = ocr_confidence.gate(student_json, conf)["decision"]
        except Exception:
            logger.exception("local OCR student failed; escalating to Gemini")
            student_json, decision = None, "gemini"

    if decision == "local" and student_json is not None:
        return student_json, f"local_student_{(conf or {}).get('model_version', 'v0')}"

    if gemini_enabled:
        return call_gemini(image_bytes, mime_type, aliases, team_notes), "gemini-2.5-pro"

    if student_json is not None:  # Gemini off/unavailable: best-effort local draft for review
        return student_json, f"local_best_effort_{(conf or {}).get('model_version', 'v0')}"

    raise RuntimeError("No OCR engine available (local unavailable and Gemini disabled).")


def _auth(request):
    """Returns (user, error_response). If error_response is not None, return it immediately."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid session."}, status=401)
    return user, None


def _require_admin(user):
    if not (
        getattr(user, "role", None) in ["admin", "moderator", "support"]
        or (hasattr(user, "userroles") and user.userroles.filter(
            role_name__in=["event_admin", "head_admin"]
        ).exists())
    ):
        return Response({"message": "Unauthorized. Admins only."}, status=403)
    return None


def _get_event(match):
    if match.leaderboard_id:
        return match.leaderboard.event
    if match.group_id:
        return match.group.stage.event
    return None


def _require_results_access(user, match):
    """Event-scoped gate for OCR endpoints that act on a specific match.

    Returns None when the user is allowed (so callers do `if (deny := ...): return deny`),
    otherwise a 403 Response. Allows AFC event admins (existing behaviour, unchanged) OR
    org members holding can_upload_results on the match's owning org. org_can_event treats
    native (org=None) events as admin-only, so organizers never touch events outside their org.
    """
    # Preserve the existing admin path exactly — if _require_admin passes, so does this.
    if _require_admin(user) is None:
        return None
    event = _get_event(match)
    if event and org_can_event(user, "can_upload_results", event):
        return None
    return Response({"message": "Unauthorized. Admins only."}, status=403)


@api_view(["POST"])
def upload_ocr_session(request):
    """
    POST /events/ocr-match-result/
    Upload a match result screenshot, run Gemini, return draft session.

    Form fields:
      match_id   (int)
      map_index  (int, 1-indexed)
      screenshot (file: PNG / JPG / WEBP)
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the match is resolved — org members with
    # can_upload_results may upload for THEIR org's events (AFC admins always pass).

    match_id  = request.data.get("match_id")
    map_index = request.data.get("map_index")
    screenshot = request.FILES.get("screenshot")

    if not match_id or not map_index or not screenshot:
        return Response(
            {"message": "match_id, map_index, and screenshot are required."},
            status=400,
        )

    try:
        match = Match.objects.select_related(
            "leaderboard__event",
            "group__stage__event",
        ).get(match_id=match_id)
    except Match.DoesNotExist:
        return Response({"message": "Match not found."}, status=404)

    # Event-scoped access check (admins, or org members with can_upload_results on this match).
    if (deny := _require_results_access(user, match)):
        return deny

    event = _get_event(match)
    if not event:
        return Response({"message": "Cannot determine event for this match."}, status=400)

    event_type = "solo" if event.participant_type == "solo" else "team"

    # Gather Gemini context (aliases + sub notes from past matches)
    aliases, team_notes = get_prompt_context(match)

    # Load registered players for matching
    registered = get_registered_players(match, event, event_type)

    # Call Gemini
    image_bytes = screenshot.read()
    mime_type   = screenshot.content_type or "image/jpeg"

    # Persist the uploaded screenshot so it survives to commit-time. Previously these
    # bytes were read, sent to Gemini, and then DISCARDED, which left the OCR learning
    # loop with no pixels to learn from. We reuse the existing MatchResultImage model
    # (the same one manual result uploads use, see afc_tournament_and_scrims.views)
    # and stash it on the session so capture_training_pair can re-read the exact bytes
    # at commit. We rewind the file first because we already consumed it with .read()
    # above (image= re-reads the file object from the start). Best-effort: a storage
    # failure here must not block OCR, so we swallow it and continue with image=None
    # (capture simply skips later if no image was persisted).
    result_image = None
    try:
        screenshot.seek(0)
        result_image = MatchResultImage.objects.create(
            match=match,
            image=screenshot,
            uploaded_by=user,
            note="OCR upload",
        )
    except Exception:
        result_image = None

    try:
        raw_output, engine = _extract_with_router(image_bytes, mime_type, aliases, team_notes, event_type)
    except Exception as exc:
        return Response({"message": f"OCR extraction failed: {exc}"}, status=503)
    raw_output.setdefault("_engine", engine)  # persisted for the FE badge + training corpus

    # Build draft rows from the extracted output (same shape whether local student or Gemini)
    draft_rows = []
    for placement_entry in raw_output.get("placements", []):
        placement = int(placement_entry.get("placement", 0))
        for player_data in placement_entry.get("players", []):
            raw_name = player_data.get("name", "").strip()
            kills    = int(player_data.get("kills", 0))
            row = match_name(raw_name, registered)
            row["placement"] = placement
            row["kills"]     = kills
            draft_rows.append(row)

    # Detect team mismatches (team events only)
    if event_type == "team":
        draft_rows = detect_team_mismatches(draft_rows)
    else:
        for row in draft_rows:
            row.update({"team_mismatch": False, "admin_confirmed_sub": False, "expected_team_id": None})

    session = OCRSession.objects.create(
        match=match,
        map_index=int(map_index),
        created_by=user,
        event_type=event_type,
        raw_output=raw_output,
        draft_rows=draft_rows,
        # Link the persisted screenshot so commit_ocr_session -> capture_training_pair
        # can re-read the exact pixels for the OCR learning loop. None if the save above
        # failed (capture then skips this session, OCR is unaffected).
        image=result_image,
    )

    return Response({
        "session_id": str(session.session_id),
        "status":     session.status,
        "event_type": session.event_type,
        "draft_rows": session.draft_rows,
        # Which engine produced this draft (local student vN / gemini-2.5-pro / best-effort).
        # The review UI shows it as the "Engine" badge so the admin sees how it was read.
        "engine":     engine,
    }, status=201)


@api_view(["GET", "PATCH", "DELETE"])
def ocr_session_detail(request, session_id):
    """
    GET    /events/ocr-session/<id>/  — fetch draft
    PATCH  /events/ocr-session/<id>/  — update one row
    DELETE /events/ocr-session/<id>/  — discard session

    PATCH body:
      row_id             (str, required)
      matched_user_id    (int, optional)
      matched_username   (str, optional)
      matched_team_id    (int, optional)
      kills              (int, optional)
      admin_confirmed_sub (bool, optional)
      corrected_text     (str, optional) — the corrected ON-SCREEN read name. This is
                          recognition-truth (what the pixels say), kept separate from
                          matched_user_id (identity-truth, who it resolves to). Feeds
                          the OCR learning loop: at commit, capture_training_pair uses
                          corrected_text (when set) as the name cell's label, else the
                          original raw_name. Fully optional and backward-compatible;
                          rows the admin does not touch keep raw_name as the read text.
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the session (and its match) is resolved — org members
    # with can_upload_results may act on THEIR org's events (AFC admins always pass).

    try:
        session = OCRSession.objects.select_related(
            "match__leaderboard__event",
            "match__group__stage__event",
        ).get(session_id=session_id)
    except OCRSession.DoesNotExist:
        return Response({"message": "Session not found."}, status=404)

    # Event-scoped access check, resolving the event via the session's match.
    if (deny := _require_results_access(user, session.match)):
        return deny

    # ── GET ──────────────────────────────────────────────────────────────────
    if request.method == "GET":
        return Response({
            "session_id": str(session.session_id),
            "status":     session.status,
            "event_type": session.event_type,
            "match_id":   session.match_id,
            "map_index":  session.map_index,
            "draft_rows": session.draft_rows,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        })

    # ── PATCH ────────────────────────────────────────────────────────────────
    if request.method == "PATCH":
        if session.status != "pending_review":
            return Response({"message": f"Session is already {session.status}."}, status=400)

        row_id = request.data.get("row_id")
        if not row_id:
            return Response({"message": "row_id is required."}, status=400)

        rows = session.draft_rows
        for row in rows:
            if row.get("row_id") == row_id:
                for field in ("matched_user_id", "matched_username", "matched_team_id"):
                    if field in request.data:
                        row[field] = request.data[field]
                if "kills" in request.data:
                    row["kills"] = int(request.data["kills"])
                if "admin_confirmed_sub" in request.data:
                    val = request.data["admin_confirmed_sub"]
                    row["admin_confirmed_sub"] = val if isinstance(val, bool) else str(val).lower() == "true"
                # corrected_text = the admin-fixed ON-SCREEN read name (recognition-truth).
                # Optional and independent of identity (matched_user_id): an admin may
                # correct only WHO the row is, only WHAT it reads, or both. We store the
                # raw string verbatim (the pixels may legitimately contain odd casing /
                # symbols). Only the OCR learning loop reads this field; the leaderboard
                # commit ignores it, so adding it changes no scoring behavior.
                if "corrected_text" in request.data:
                    row["corrected_text"] = request.data["corrected_text"]
                break
        else:
            return Response({"message": "Row not found."}, status=404)

        session.draft_rows = rows
        session.save(update_fields=["draft_rows", "updated_at"])
        return Response({"message": "Row updated.", "draft_rows": session.draft_rows})

    # ── DELETE ───────────────────────────────────────────────────────────────
    if request.method == "DELETE":
        session.status = "discarded"
        session.save(update_fields=["status"])
        return Response({"message": "Session discarded."})


@api_view(["POST"])
def commit_ocr_session(request, session_id):
    """
    POST /events/ocr-session/<id>/commit/
    Commit the OCR session: save aliases, save sub notes, write match stats.

    Body (optional):
      final_rows  (list) — if omitted, uses the session's current draft_rows
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the session (and its match) is resolved — org members
    # with can_upload_results may commit for THEIR org's events (AFC admins always pass).

    try:
        session = OCRSession.objects.select_related(
            "match__leaderboard__event",
            "match__group__stage__event",
        ).get(session_id=session_id)
    except OCRSession.DoesNotExist:
        return Response({"message": "Session not found."}, status=404)

    # Event-scoped access check, resolving the event via the session's match.
    if (deny := _require_results_access(user, session.match)):
        return deny

    if session.status != "pending_review":
        return Response({"message": f"Session is already {session.status}."}, status=400)

    final_rows = request.data.get("final_rows") or session.draft_rows

    # Validate: every row must either have a matched_user_id or be removed
    unresolved = [
        r["raw_name"] for r in final_rows
        if not r.get("matched_user_id")
    ]
    if unresolved:
        return Response({
            "message": "Some rows have no matched player. Resolve or remove them before committing.",
            "unresolved": unresolved,
        }, status=400)

    unacknowledged = [
        r["raw_name"] for r in final_rows
        if r.get("team_mismatch") and not r.get("admin_confirmed_sub")
    ]
    if unacknowledged:
        return Response({
            "message": "Some team mismatches have not been acknowledged. Confirm or reassign before committing.",
            "unacknowledged": unacknowledged,
        }, status=400)

    try:
        from .services.commit import (
            commit_team_result,
            commit_solo_result,
            save_name_corrections,
            save_team_notes,
        )

        save_name_corrections(final_rows, session.draft_rows)
        save_team_notes(final_rows, session.match, user)

        if session.event_type == "team":
            lb = commit_team_result(session.match, final_rows)
        else:
            lb = commit_solo_result(session.match, final_rows)

        # Persist final state BEFORE capturing training data, so capture diffs the
        # original Gemini draft (session.draft_rows still holds it here) against
        # final_rows. We snapshot draft for the diff, then overwrite.
        original_draft = session.draft_rows
        session.status = "committed"
        session.draft_rows = final_rows  # persist final state
        session.save(update_fields=["status", "draft_rows"])

        # ── OCR learning loop (Phase 1): capture this committed review as training
        # data. The leaderboard write above already succeeded and is committed; this
        # capture runs LAST and in its OWN try/except so a capture failure can never
        # roll back or break the real commit (the function is also internally guarded).
        # image_bytes is NOT in scope here (commit may be a separate request from
        # upload), so capture re-reads the exact pixels from session.image. We pass a
        # lightweight object carrying the ORIGINAL draft so num_corrections diffs
        # against the Gemini draft, not the already-overwritten final_rows.
        try:
            from .services.training_capture import capture_training_pair
            session.draft_rows = original_draft  # restore in-memory for the diff only
            capture_training_pair(session, final_rows, image_bytes=None)
            session.draft_rows = final_rows      # leave the in-memory state consistent
        except Exception:
            # Defense in depth: capture_training_pair already swallows its own errors,
            # but never let anything here disturb the successful commit response.
            session.draft_rows = final_rows

        return Response({
            "message":       "OCR session committed successfully.",
            "leaderboard_id": lb.leaderboard_id if lb else None,
        })

    except Exception as exc:
        return Response({"message": f"Commit failed: {exc}"}, status=500)


@api_view(["POST"])
def ocr_from_stored_image(request):
    """
    POST /events/ocr-from-image/
    Run OCR on an already-uploaded MatchResultImage (by image_id).

    Body: { image_id (int), match_id (int), map_index (int, default 1) }
    """
    from afc_tournament_and_scrims.models import MatchResultImage

    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the match is resolved — org members with
    # can_upload_results may run OCR for THEIR org's events (AFC admins always pass).

    image_id  = request.data.get("image_id")
    match_id  = request.data.get("match_id")
    map_index = request.data.get("map_index", 1)

    if not image_id or not match_id:
        return Response({"message": "image_id and match_id are required."}, status=400)

    try:
        stored = MatchResultImage.objects.get(image_id=image_id)
    except MatchResultImage.DoesNotExist:
        return Response({"message": "Image not found."}, status=404)

    try:
        match = Match.objects.select_related(
            "leaderboard__event",
            "group__stage__event",
        ).get(match_id=match_id)
    except Match.DoesNotExist:
        return Response({"message": "Match not found."}, status=404)

    # Event-scoped access check (admins, or org members with can_upload_results on this match).
    if (deny := _require_results_access(user, match)):
        return deny

    event = _get_event(match)
    if not event:
        return Response({"message": "Cannot determine event for this match."}, status=400)

    event_type = "solo" if event.participant_type == "solo" else "team"

    aliases, team_notes = get_prompt_context(match)
    registered = get_registered_players(match, event, event_type)

    try:
        with stored.image.open("rb") as f:
            image_bytes = f.read()
        name = stored.image.name.lower()
        if name.endswith(".png"):
            mime_type = "image/png"
        elif name.endswith(".webp"):
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"
    except Exception as exc:
        return Response({"message": f"Failed to read stored image: {exc}"}, status=500)

    try:
        raw_output, engine = _extract_with_router(image_bytes, mime_type, aliases, team_notes, event_type)
    except Exception as exc:
        return Response({"message": f"OCR extraction failed: {exc}"}, status=503)
    raw_output.setdefault("_engine", engine)  # persisted for the FE badge + training corpus

    draft_rows = []
    for placement_entry in raw_output.get("placements", []):
        placement = int(placement_entry.get("placement", 0))
        for player_data in placement_entry.get("players", []):
            raw_name = player_data.get("name", "").strip()
            kills    = int(player_data.get("kills", 0))
            row = match_name(raw_name, registered)
            row["placement"] = placement
            row["kills"]     = kills
            draft_rows.append(row)

    if event_type == "team":
        draft_rows = detect_team_mismatches(draft_rows)
    else:
        for row in draft_rows:
            row.update({"team_mismatch": False, "admin_confirmed_sub": False, "expected_team_id": None})

    session = OCRSession.objects.create(
        match=match,
        map_index=int(map_index),
        created_by=user,
        event_type=event_type,
        raw_output=raw_output,
        draft_rows=draft_rows,
        # This path OCRs an ALREADY-persisted MatchResultImage (the `stored` row,
        # looked up by image_id). Link that same row so the OCR learning loop can
        # re-read its exact pixels at commit-time, no second copy needed.
        image=stored,
    )

    return Response({
        "session_id": str(session.session_id),
        "status":     session.status,
        "event_type": session.event_type,
        "draft_rows": session.draft_rows,
        # Which engine produced this draft (local student vN / gemini-2.5-pro / best-effort).
        # The review UI shows it as the "Engine" badge so the admin sees how it was read.
        "engine":     engine,
    }, status=201)


@api_view(["GET"])
def list_ocr_sessions(request):
    """
    GET /events/ocr-sessions/?match_id=<id>
    List all OCR sessions, optionally filtered by match.
    """
    user, err = _auth(request)
    if err:
        return err
    if (deny := _require_admin(user)):
        return deny

    qs = OCRSession.objects.select_related("match", "created_by").order_by("-created_at")
    match_id = request.query_params.get("match_id")
    if match_id:
        qs = qs.filter(match_id=match_id)

    data = [
        {
            "session_id": str(s.session_id),
            "match_id":   s.match_id,
            "map_index":  s.map_index,
            "event_type": s.event_type,
            "status":     s.status,
            "created_by": s.created_by.username,
            "created_at": s.created_at,
        }
        for s in qs[:100]
    ]

    return Response(data)
