from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db.models import Q  # org-scoped session filter in list_ocr_sessions

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event, is_platform_org_admin
from afc_tournament_and_scrims.models import Event, Match, MatchResultImage

from .models import OCRSession
from .services.gemini import call_gemini, get_prompt_context
from .services.matching import get_registered_players, match_name, detect_team_mismatches
# A8/A5 per-request image cap. Shared server-side validator (mime + per-file size + count)
# used by the OCR upload views ONLY (upload_ocr_session below). Returns a client-safe error
# string or None; rejecting garbage/oversize/HEIC uploads BEFORE any Gemini call keeps the
# synchronous read under the gateway budget. Defined in afc_ocr/services/image_validate.py.
from .services.image_validate import validate_ocr_images

import logging
import re  # A7: leading-int parse in _safe_int (Gemini can emit "3 kills" for a kills cell)

logger = logging.getLogger(__name__)


def _safe_int(x, default=0):
    """Coerce a Gemini-emitted value to int, defaulting on null/blank/garbage.

    Gemini occasionally returns None, "", "3 kills" or a float for a kills/placement cell;
    a bare int() there 500s the whole upload. Used at every int() call site that consumes
    raw model output (upload_ocr_session + ocr_from_stored_image draft build below, and the
    solo commit path in services/commit.py). Kept module-level + pure so it is trivially
    unit-testable (afc_ocr/tests/test_safe_int.py).
    """
    try:
        if x is None:
            return default
        if isinstance(x, bool):        # avoid True -> 1 surprises
            return default
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x).strip()
        if not s:
            return default
        m = re.match(r"-?\d+", s)       # leading integer run ("3 kills" -> 3)
        return int(m.group()) if m else default
    except (TypeError, ValueError):
        return default


def _extract_with_router(image_bytes, mime_type, aliases, team_notes, event_type):
    """Thin delegate to the shared OCR extraction service (afc_ocr.services.extract.extract_rows).

    The local-first-then-Gemini routing body was lifted into services/extract.py (P2) so the
    standalone-leaderboard OCR endpoints can reuse the EXACT same extraction without the event
    commit machinery. This wrapper preserves the event upload paths' call signature + behavior
    1:1 (default prompt_kind => the unchanged event prompt). Returns (raw_output, engine).
    Called by upload_ocr_session + ocr_from_stored_image below.
    """
    from .services.extract import extract_rows
    return extract_rows(image_bytes, mime_type, event_type, aliases=aliases, team_notes=team_notes)


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
    # NOTE (fix, owner 2026-07-02 organizer parity): UserRoles has no `role_name` field - the
    # granular role name lives on the related Roles row, so the correct lookup is
    # `role__role_name__in` (same fix as afc_tournament_and_scrims.views._is_event_admin).
    # The old `role_name__in` lookup raised FieldError (a 500) for EVERY non-staff caller,
    # which also meant granular-only event_admins could never pass this gate.
    if not (
        getattr(user, "role", None) in ["admin", "moderator", "support"]
        or (hasattr(user, "userroles") and user.userroles.filter(
            role__role_name__in=["event_admin", "head_admin"]
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
    # Preserve the existing admin path exactly: if _require_admin passes, so does this.
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
    Upload ONE OR MORE result screenshots for a single map, OCR each, merge their placements,
    and return one draft session for review. Consumed by MapSelectionStep.tsx (the per-match
    "Upload Results" drawer) and ImageUploadStep.tsx.

    Form fields:
      match_id   (int)
      map_index  (int, 1-indexed)
      screenshot (file: PNG / JPG / WEBP), repeatable; send the field once per screenshot when a
                 map's standings are split across several shots. A single file still works.
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the match is resolved (org members with
    # can_upload_results may upload for THEIR org's events; AFC admins always pass).

    match_id  = request.data.get("match_id")
    map_index = request.data.get("map_index")
    # Accept ONE OR MORE screenshots for this map under the same "screenshot" field. A single
    # map's standings are often split across several screenshots (placements 1-6 on one screen,
    # 7-12 on the next, or a top/bottom half), so the per-match upload now mirrors the batch
    # worker (afc_leaderboard.ocr.process_job): read every image, then union their placements
    # with merge_placements below. getlist() also returns a 1-element list for the legacy
    # single-file callers (older clients / ImageUploadStep), so this stays backward compatible.
    screenshots = request.FILES.getlist("screenshot")

    if not match_id or not map_index or not screenshots:
        return Response(
            {"message": "match_id, map_index, and at least one screenshot are required."},
            status=400,
        )

    # A5/A8: per-request image cap BEFORE any Gemini call. validate_ocr_images rejects a wrong
    # mime (incl. HEIC), an oversize file, or too many screenshots at once, returning a
    # client-safe message. Bounding the file count keeps the synchronous multi-image read
    # under the ~30s prod gateway budget (the Gemini socket timeout does the rest, see A5).
    if (image_err := validate_ocr_images(screenshots)):
        return Response({"message": image_err}, status=400)

    # A5 (sync path stays under the ~30s prod gateway budget): the synchronous per-map read fans
    # a SEPARATE Gemini call out per screenshot (ThreadPoolExecutor with max_workers=min(4, N)
    # below, each worker calls _extract_with_router -> Gemini). So N>4 screenshots serialize into
    # multiple waves and can blow past the gateway budget. Cap the SYNC path at 4 here; the async
    # standalone batch flow (afc_leaderboard.ocr.process_job) is the path for larger uploads.
    # validate_ocr_images already bounded mime/size/count above; this bounds the wall-time.
    if len(screenshots) > 4:
        return Response(
            {"message": "You can upload up to 4 screenshots per map at once. Add the rest as a second read."},
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
    # Read the bytes + persist each screenshot up front. We persist EVERY uploaded image as a
    # MatchResultImage (the same model manual result uploads use, see afc_tournament_and_scrims
    # .views) so the pixels survive to commit-time; the session links the FIRST one below
    # (OCRSession.image is a single FK) which is what the OCR learning loop
    # (capture_training_pair) currently re-reads. Persisting is best-effort: a storage failure
    # must never block OCR, so we swallow it and keep the bytes we already read. We rewind each
    # file (.seek(0)) before create() because .read() above consumed it (image= re-reads from 0).
    payloads = []            # (image_bytes, mime_type) per screenshot, in upload order
    first_result_image = None
    for shot in screenshots:
        data = shot.read()
        mime = shot.content_type or "image/jpeg"
        payloads.append((data, mime))
        try:
            shot.seek(0)
            img = MatchResultImage.objects.create(
                match=match,
                image=shot,
                uploaded_by=user,
                note="OCR upload",
            )
            if first_result_image is None:
                first_result_image = img
        except Exception:
            pass

    # Extract each screenshot, then MERGE. We read the images CONCURRENTLY exactly like the batch
    # worker (afc_leaderboard.ocr.process_job): extraction is I/O-bound on the Gemini fallback, so
    # overlapping the calls keeps a multi-screenshot upload snappy, while the shared local student
    # stays serialized behind afc_ocr.services.extract._STUDENT_LOCK. A single failing image raises
    # out of ex.map and 503s the whole upload (same all-or-nothing the single-file path had).
    # merge_placements unions every image's placements and drops exact duplicates, keyed by
    # placement (placement is unique within one map, so overlapping top/bottom shots de-dupe
    # cleanly); the admin reviews + corrects the merged rows in OCRReviewTable afterwards.
    from concurrent.futures import ThreadPoolExecutor
    from afc_leaderboard.ocr import merge_placements

    def _read_one(payload):
        data, mime = payload
        return _extract_with_router(data, mime, aliases, team_notes, event_type)

    try:
        with ThreadPoolExecutor(max_workers=min(4, len(payloads))) as ex:
            outputs = list(ex.map(_read_one, payloads))   # ex.map preserves upload order
    except RuntimeError as exc:
        # A5/A9/A10: services/gemini.call_gemini raises FRIENDLY, key-free RuntimeErrors for the
        # known-safe cases (a Gemini timeout -> "took too long, try again", a safety block, an
        # empty/unreadable result). The Gemini API key is stripped at the gemini.py layer, so
        # these messages carry no secret and are safe to show the reviewer verbatim.
        logger.warning("OCR extraction could not read screenshots for match %s: %s", match_id, exc)
        return Response({"message": str(exc)}, status=503)
    except Exception:
        # A10: anything unexpected -> log the real detail server-side, return a GENERIC message.
        # Never interpolate {exc} into the client body (avoids leaking internals / the API key).
        logger.exception("OCR extraction failed for match %s", match_id)
        return Response({"message": "Could not read that screenshot. Please try again."}, status=503)

    placement_lists = [(raw.get("placements", []) or []) for raw, _eng in outputs]
    # Record the last non-empty engine for the FE "which engine" badge (mirrors process_job).
    engine = next((eng for _raw, eng in reversed(outputs) if eng), "")
    merged = merge_placements(placement_lists, is_team=(event_type == "team"))

    # Canonical draft shape the rest of the flow expects: {"placements": [...]}. We store the
    # MERGED placements (not any single image's), so the draft-row build / match_name / commit
    # path below is byte-for-byte unchanged whether one or many screenshots were uploaded.
    raw_output = {"placements": merged, "_engine": engine}

    # Build draft rows from the extracted output (same shape whether local student or Gemini)
    draft_rows = []
    for placement_entry in raw_output.get("placements", []):
        # A7: _safe_int guards raw Gemini output (null / "" / "3 kills" / a float) so a garbage
        # cell yields 0 instead of 500-ing the whole upload with ValueError/TypeError.
        placement = _safe_int(placement_entry.get("placement", 0))
        for player_data in placement_entry.get("players", []):
            raw_name = player_data.get("name", "").strip()
            kills    = _safe_int(player_data.get("kills", 0))
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
        # Link the FIRST persisted screenshot so commit_ocr_session -> capture_training_pair
        # can re-read the exact pixels for the OCR learning loop. None if every save above
        # failed (capture then skips this session, OCR is unaffected). When several screenshots
        # were merged, only this first image feeds the single-image training capture today;
        # all of them are still persisted as MatchResultImage rows for the record.
        image=first_result_image,
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
    GET    /events/ocr-session/<id>/  : fetch draft
    PATCH  /events/ocr-session/<id>/  : update one row
    DELETE /events/ocr-session/<id>/  : discard session

    PATCH body:
      row_id             (str, required)
      matched_user_id    (int, optional)
      matched_username   (str, optional)
      matched_team_id    (int, optional)
      kills              (int, optional)
      admin_confirmed_sub (bool, optional)
      corrected_text     (str, optional): the corrected ON-SCREEN read name. This is
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
    # Permission is gated below, after the session (and its match) is resolved (org members
    # with can_upload_results may act on THEIR org's events; AFC admins always pass).

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
      final_rows  (list): if omitted, uses the session's current draft_rows
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the session (and its match) is resolved (org members
    # with can_upload_results may commit for THEIR org's events; AFC admins always pass).

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

    # Validate: every row must either have a matched_user_id or be removed.
    # A10: use .get("raw_name","") (NOT r["raw_name"]). This runs BEFORE the try below, so a
    # hand-built or older final_rows row missing raw_name would otherwise be a raw 500 here.
    unresolved = [
        r.get("raw_name", "") for r in final_rows
        if not r.get("matched_user_id")
    ]
    if unresolved:
        return Response({
            "message": "Some rows have no matched player. Resolve or remove them before committing.",
            "unresolved": unresolved,
        }, status=400)

    unacknowledged = [
        r.get("raw_name", "") for r in final_rows
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

        # #14: commit_team_result returns (lb, unmatched_blocks) so unmatched OCR team blocks
        # are surfaced to the reviewer instead of silently dropped.
        # A3: commit_solo_result now ALSO returns (lb, skipped): solo rows whose matched user is
        # not a RegisteredCompetitors for this event, which the old code dropped silently. We keep
        # the two channels DISTINCT (team skips are teams -> missing_teams; solo skips are players
        # -> skipped_rows) so the FE can label each correctly.
        unmatched_blocks = []   # team path: OCR team blocks not matched to a registered team
        skipped_rows = []       # solo path: matched players not on the event roster (A3)
        if session.event_type == "team":
            lb, unmatched_blocks = commit_team_result(session.match, final_rows)
        else:
            lb, skipped_rows = commit_solo_result(session.match, final_rows)

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
            # #14: team blocks the OCR could not match to a registered team, so the reviewer can
            # attribute them from the flagged-teams resolver instead of losing them silently. [] = clean.
            "missing_teams": unmatched_blocks,
            # A3: solo rows dropped because the matched player is not on this event's roster.
            # DISTINCT from missing_teams (players, not teams) so OCRReviewTable.handleCommit can
            # warn "N players were not on the event roster and were skipped." [] = clean / team event.
            "skipped_rows": skipped_rows,
        })

    except Exception:
        # A10: log the real exception server-side, return a GENERIC client message (no {exc}
        # interpolation -> avoids leaking internals / any secret into the response body).
        logger.exception("OCR commit failed for session %s", session_id)
        return Response({"message": "Could not commit the results. Please try again."}, status=500)


@api_view(["GET"])
def ocr_session_roster(request, session_id):
    """
    GET /events/ocr-session/<id>/roster/
    Return the players REGISTERED for this OCR session's event, so the review table's
    "matched player" picker can be scoped to the roster instead of the whole platform.

    WHY this exists / how it connects
      OCRReviewTable.tsx lets an admin re-point a read row at the correct player. Its
      searchable picker was populating from each row's fuzzy `top_candidates` only, which
      misses a registered player the OCR never guessed. This endpoint hands the FE the full
      event roster once, so the picker offers every legitimate participant (and nobody who
      is not registered) for that event. It reuses the SAME roster resolver the draft build
      uses (services/matching.py::get_registered_players), so the picker list and the
      auto-match candidate pool are drawn from one source of truth.

    Request:
      path session_id (uuid) - the OCRSession to scope the roster to.

    Response 200 JSON:
      {
        "players": [{"user_id": int, "username": str,
                     "team_id": int|null, "team_name": str|null}],
        "event_type": "solo" | "team"
      }
      (players is get_registered_players' list verbatim: team_id/team_name are null on solo
       events, and carry the tournament team on team events.)

    Auth: Bearer token via _auth (400 missing/garbled header, 401 bad/expired token), then
      the SAME event-scoped gate every other OCR session view uses, _require_results_access
      (403 unless AFC admin OR an organizer holding can_upload_results on the event's org).

    Errors: 401 bad/missing token; 403 no results access; 404 session or event not found.

    Frontend consumer: OCRReviewTable.tsx via lib/api/ocr.ts getSessionRoster(), which calls
      this to populate the roster-scoped "matched player" searchable picker.
    """
    user, err = _auth(request)
    if err:
        return err
    # Permission is gated below, after the session (and its match) is resolved (org members
    # with can_upload_results may read THEIR org's roster; AFC admins always pass). Mirrors
    # ocr_session_detail / commit_ocr_session above exactly.

    try:
        session = OCRSession.objects.select_related(
            "match__leaderboard__event",
            "match__group__stage__event",
        ).get(session_id=session_id)
    except OCRSession.DoesNotExist:
        return Response({"message": "Session not found."}, status=404)

    # Event-scoped access check, resolving the event via the session's match (identical gate
    # to the sibling session views, so admin AND organizer parity is preserved here).
    if (deny := _require_results_access(user, session.match)):
        return deny

    # Resolve the event the SAME way the draft build does (_get_event: a match hangs off either
    # a standalone leaderboard or a stage group). A session with no resolvable event is a 404.
    event = _get_event(session.match)
    if not event:
        return Response({"message": "Cannot determine event for this session."}, status=404)

    # solo events carry no team context (team_id/team_name come back null from the resolver);
    # anything else is a team event. Same derivation the upload/commit paths use.
    event_type = "solo" if event.participant_type == "solo" else "team"

    # The single source of truth for "who is allowed on this event's rows" (services/matching.py).
    # Reusing it keeps the picker list identical to the auto-match candidate pool.
    players = get_registered_players(session.match, event, event_type)

    return Response({
        "players":    players,
        "event_type": event_type,
    })


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
    # Permission is gated below, after the match is resolved (org members with
    # can_upload_results may run OCR for THEIR org's events; AFC admins always pass).

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

    # #12 (2026-07-06): bind the stored image to the AUTHORIZED match. The access check above gates on
    # match_id, but image_id was fetched independently, so without this an organizer with results access
    # on match A could OCR a screenshot belonging to another event's match by passing a foreign image_id.
    if getattr(stored, "match_id", None) and stored.match_id != match.match_id:
        return Response({"message": "That image does not belong to this match."}, status=400)

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
    except Exception:
        # A10: log the real open error server-side, return a GENERIC client-safe message. No
        # {exc} interpolation -> a storage path / backend detail never leaks into the response
        # body (mirrors the extraction error paths above + the commit guard).
        logger.exception("Could not open stored image %s for match %s", image_id, match_id)
        return Response({"message": "Could not open that stored image. Please try again."}, status=500)

    try:
        raw_output, engine = _extract_with_router(image_bytes, mime_type, aliases, team_notes, event_type)
    except RuntimeError as exc:
        # A5/A9/A10: friendly, key-free RuntimeError from services/gemini (timeout / safety block /
        # unreadable result). Safe to surface verbatim; the API key is stripped at the gemini layer.
        logger.warning("OCR extraction could not read stored image %s for match %s: %s", image_id, match_id, exc)
        return Response({"message": str(exc)}, status=503)
    except Exception:
        # A10: unexpected failure -> log detail server-side, return a GENERIC message (no {exc}).
        logger.exception("OCR extraction failed for stored image %s match %s", image_id, match_id)
        return Response({"message": "Could not read that screenshot. Please try again."}, status=503)
    raw_output.setdefault("_engine", engine)  # persisted for the FE badge + training corpus

    draft_rows = []
    for placement_entry in raw_output.get("placements", []):
        # A7: _safe_int guards raw Gemini output so a null/blank/garbage cell yields 0, not a 500.
        placement = _safe_int(placement_entry.get("placement", 0))
        for player_data in placement_entry.get("players", []):
            raw_name = player_data.get("name", "").strip()
            kills    = _safe_int(player_data.get("kills", 0))
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
    GET /events/ocr-sessions/?match_id=<id>&event_id=<id>
    List OCR sessions, optionally filtered by match. Consumed by the admin OCR page
    (app/(a)/a/events/[slug]/ocr/page.tsx) via lib/api/ocr.ts listSessions().

    Auth (organizer parity, owner 2026-07-02):
      - AFC admins (_require_admin): unscoped platform-wide list by default. Optional match_id
        filter, and (O4, 2026-07-14) an OPTIONAL event_id filter: passing ?event_id= scopes the
        list to that one event so the rebuilt admin event OCR page can show only THIS event's
        sessions. Omitting event_id preserves the historical unscoped behavior.
      - Organizers: NEW branch - allowed when they hold can_upload_results on the event's
        owning org (org_can_event, same grant as every other OCR surface, see
        _require_results_access above). An organizer MUST pass event_id and only ever gets
        THAT event's sessions back (org-scoped, never the platform-wide list); match_id then
        narrows within it. Native (org=None) events stay admin-only via org_can_event.
    """
    user, err = _auth(request)
    if err:
        return err

    qs = OCRSession.objects.select_related("match", "created_by").order_by("-created_at")

    if _require_admin(user) is not None:
        # Not an AFC admin -> organizer branch. Sessions hang off matches, and a match's
        # event resolves via either a standalone leaderboard or a stage group (same paths
        # as _get_event above), so the event scope filter must cover both FKs.
        event_id = request.query_params.get("event_id")
        if not event_id:
            return Response({"message": "event_id is required."}, status=400)
        try:
            event = Event.objects.get(event_id=event_id)
        except Event.DoesNotExist:
            return Response({"message": "Event not found."}, status=404)
        if not org_can_event(user, "can_upload_results", event):
            return Response({"message": "Unauthorized. Admins only."}, status=403)
        qs = qs.filter(
            Q(match__leaderboard__event=event) | Q(match__group__stage__event=event)
        )
    else:
        # O4: AFC admin. Default list is platform-wide, but the rebuilt admin event OCR page
        # (app/(a)/a/events/[slug]/ocr/page.tsx) passes ?event_id= to see only THIS event's
        # sessions. Reuse the SAME event-scope filter the organizer branch uses (session ->
        # match -> event via a standalone leaderboard OR a stage group; both FKs covered).
        # No event_id -> unscoped, so any other admin caller is unaffected.
        admin_event_id = request.query_params.get("event_id")
        if admin_event_id:
            try:
                event = Event.objects.get(event_id=admin_event_id)
            except Event.DoesNotExist:
                return Response({"message": "Event not found."}, status=404)
            qs = qs.filter(
                Q(match__leaderboard__event=event) | Q(match__group__stage__event=event)
            )

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


# ═══════════════════════════════════════════════════════════════════════════════
# OCR MLOps ENDPOINTS - the self-hosted student-model control surface
# ═══════════════════════════════════════════════════════════════════════════════
#
# WHAT THIS BLOCK IS
#   Five admin/staff-gated endpoints that wire the OCR learning loop (capture ->
#   assemble -> train off-box -> ship) to two consumers:
#     • the "OCR Model" admin dashboard page (the FE surface that shows corpus size,
#       weekly local-vs-Gemini share, retrain history, and the Promote / Rollback /
#       Download-dataset buttons), and
#     • train_cycle.py, the off-box trainer script on the GPU PC that POLLS
#       retrain-status, DOWNLOADS the dataset zip, trains, then PUSHES the bundle back
#       with upload-model.
#
#   So the FE reads model-stats (dashboard render) + drives promote/rollback (buttons),
#   while train_cycle.py drives retrain-status (poll) + dataset-export (download) +
#   upload-model (push). model-stats + dataset-export serve BOTH (the dashboard also has
#   a "Download dataset" button that hits the same export endpoint train_cycle.py uses).
#
# WHAT IT READS (no new models - pure aggregation over what the loop already captured)
#   • OCRTrainingPair  - corpus counts by source (gold/silver/synthetic) + is_clean, the
#     zero-touch rate (num_corrections == 0), and the gold-growth-over-time series.
#   • OCRSession       - weekly scan volume + which engine read each (raw_output['_engine']
#     -> local_student_* vs gemini-2.5-pro vs hybrid/local_best_effort).
#   • services.dataset.assemble_rec_dataset - builds the on-disk rec dataset we zip up.
#   • services.model_registry - active_version / promote / rollback / recent_shadow_stats.
#   • services.eval_gate (indirectly, via the bundle's eval_report.json 'ship' flag).
#   • media/ocr_retrain/*.json markers + media/models/student_v*/model_card.json - the
#     retrain history the dashboard timelines.
#   • tasks.check_retrain_trigger's thresholds - replicated read-only for retrain-status.
#
# AUTH GATE (the load-bearing rule)
#   Every endpoint here is Bearer-token + staff gated via _is_ocr_admin (below), which
#   mirrors afc_player_market.views_moderation._is_market_moderator: coarse user.role in
#   {admin, moderator, support} OR a granular OCR/event role in the UserRoles table. Bad
#   auth -> clean 400/401/403; bad INPUT -> clean 4xx, never a 500 (each view validates +
#   degrades defensively, exactly like the rest of this app).
#
# EVERYTHING THESE TOUCH ON DISK IS LOCAL + GITIGNORED (media/models, media/ocr_retrain,
# media/ocr_training, the dataset temp dirs). Nothing here is ever pushed.
# ═══════════════════════════════════════════════════════════════════════════════

import io
import json as _json
import os
import shutil
import tempfile
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import TruncWeek
from django.http import HttpResponse
from django.utils.timezone import now            # used by ocr_retrain_status (cadence days)

from .models import OCRTrainingPair
from .services import dataset as ocr_dataset
from .services import model_registry


# Granular UserRoles role_names that count as OCR admins, in addition to the coarse
# user.role in {admin, moderator, support}. event_admin owns the tournament-results
# surface (which is where OCR lives); head_admin is the platform super-role. We read
# them the SAME way afc_player_market reads its moderator roles
# (user.userroles.filter(role__role_name__in=...)) so the gate logic is identical across
# the codebase. (Note: the older _require_admin above filters on `role_name` directly,
# which does not traverse the Roles FK; we deliberately use the correct `role__role_name`
# join here, matching _is_market_moderator, so granular OCR admins actually pass.)
OCR_ADMIN_ROLES = ("head_admin", "event_admin")


def _is_ocr_admin(user) -> bool:
    """True for users allowed to operate the OCR model dashboard + MLOps endpoints.

    Mirrors afc_player_market.views_moderation._is_market_moderator exactly:
      coarse gate  -> user.role in {admin, moderator, support}, PLUS
      granular gate-> any UserRoles row whose role.role_name is in OCR_ADMIN_ROLES.
    Returns a plain bool so callers do `if not _is_ocr_admin(user): return 403`.
    """
    if not user:
        return False
    if getattr(user, "role", None) in ("admin", "moderator", "support"):
        return True
    # Reverse relation is `userroles` (afc_auth.UserRoles.related_name), and the role
    # name lives on the related Roles row -> traverse with role__role_name.
    return user.userroles.filter(role__role_name__in=OCR_ADMIN_ROLES).exists()


def _require_ocr_admin(request):
    """Auth + staff gate for every MLOps endpoint, returning (user, error_response).

    (user, None)  -> authenticated OCR admin, proceed.
    (None, resp)  -> stop and return resp: 400 missing/garbled header, 401 bad/expired
                     token, 403 authenticated-but-not-staff. Same wording/shape as the
                     rest of afc_ocr (_auth) and afc_player_market (_authenticate).
    """
    user, err = _auth(request)          # reuse this module's Bearer handshake (400/401)
    if err:
        return None, err
    if not _is_ocr_admin(user):
        return None, Response(
            {"message": "You do not have permission to manage the OCR model."},
            status=403,
        )
    return user, None


# ──────────────────────────────────────────────────────────────────────────────
# Small shared helpers for the dashboard aggregation. Kept pure + defensive so an
# empty corpus / missing marker yields zeros, never a 500.
# ──────────────────────────────────────────────────────────────────────────────

def _iso_week_label(dt) -> str:
    """A 'YYYY-Www' ISO-week label for a datetime (e.g. '2026-W23'). Used to bucket
    OCRSession scans + gold growth by week on the dashboard timeline. Returns '' for a
    falsy input so a stray null date never crashes the grouping."""
    if not dt:
        return ""
    iso = dt.isocalendar()        # (iso_year, iso_week, iso_weekday); works on date+datetime
    return f"{iso[0]}-W{iso[1]:02d}"


def _classify_engine(engine: str) -> str:
    """Bucket a raw_output['_engine'] string into one of 'local' / 'gemini' / 'hybrid'.

    The router (views._extract_with_router) records the engine as:
      • 'local_student_vN'      -> the student served it with ZERO Gemini calls  -> local
      • 'gemini-2.5-pro'        -> the teacher served it                          -> gemini
      • 'local_best_effort_vN'  -> Gemini was off, student draft shown for review -> hybrid
    Anything else (legacy / unknown) is counted as 'gemini' so we never over-credit the
    local engine. This is the single place the engine taxonomy lives for the dashboard."""
    e = (engine or "").lower()
    if e.startswith("local_student"):
        return "local"
    if e.startswith("local_best_effort") or e.startswith("hybrid"):
        return "hybrid"
    return "gemini"


def _read_json_file(path: str) -> dict:
    """Read+parse a JSON file, returning {} on any error (missing / torn / not-json).
    Used to slurp retrain markers + model_card.json without ever raising into a view."""
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError):
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# 1. GET /events/ocr/model-stats/ - the dashboard payload
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def ocr_model_stats(request):
    """GET /events/ocr/model-stats/ -> the OCR Model dashboard's single render payload.

    Consumer: the admin "OCR Model" dashboard page. One call populates the whole page:
    the active-model header, the corpus donut, the weekly local-vs-Gemini chart, the
    retrain-history timeline, the shadow-model card, and the gold-growth sparkline.

    Reads (all defensive; empty corpus -> zeros, never a crash):
      • active_model  <- model_registry.active_version() + the promoted_at from the
                         active bundle's model_card.json (when present).
      • corpus        <- OCRTrainingPair grouped by source (gold=admin_review,
                         silver=gemini_autolabel, synthetic) + clean count (is_clean).
      • weekly        <- OCRSession grouped by ISO week (last ~12), split by engine from
                         raw_output['_engine']; local_share + zero_touch + gemini_calls.
      • retrain_history <- media/ocr_retrain/*.json markers joined with any
                         media/models/student_v*/model_card.json (eval_report 'ship').
      • shadow        <- model_registry.recent_shadow_stats().
      • dataset_growth<- cumulative gold pairs per week (the corpus-growth sparkline).

    Auth: Bearer + _is_ocr_admin (400/401/403). Never 500 on data shape.
    """
    user, err = _require_ocr_admin(request)
    if err:
        return err

    # Whole body is wrapped: a dashboard read must degrade to a usable (possibly-empty)
    # payload rather than 500. Each section is also individually guarded below.
    try:
        # ── active model header ────────────────────────────────────────────────
        active_version = model_registry.active_version()
        promoted_at = None
        try:
            active_dir = model_registry.active_model_dir()
            if active_dir:
                card = _read_json_file(os.path.join(active_dir, "model_card.json"))
                # model_card.json may record when this bundle was promoted; fall back to
                # the bundle dir's mtime (when the pointer was last flipped onto it).
                promoted_at = card.get("promoted_at") or card.get("created_at")
                if not promoted_at:
                    try:
                        ts = os.path.getmtime(active_dir)
                        promoted_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    except OSError:
                        promoted_at = None
        except Exception:  # noqa: BLE001 - header is best-effort; never block the page
            promoted_at = None

        active_model = {"version": active_version, "promoted_at": promoted_at}

        # ── corpus counts (one grouped query, no N+1) ──────────────────────────
        # Map the model's source choices to the dashboard's gold/silver/synthetic names.
        corpus = {"gold": 0, "silver": 0, "synthetic": 0, "total": 0, "clean": 0}
        source_to_key = {
            "admin_review": "gold",
            "gemini_autolabel": "silver",
            "synthetic": "synthetic",
        }
        for row in (
            OCRTrainingPair.objects.values("source").annotate(n=Count("pair_id"))
        ):
            key = source_to_key.get(row["source"])
            if key:
                corpus[key] = row["n"]
        corpus["total"] = corpus["gold"] + corpus["silver"] + corpus["synthetic"]
        corpus["clean"] = OCRTrainingPair.objects.filter(is_clean=True).count()

        # ── weekly scan volume + engine split (last ~12 weeks) ─────────────────
        # We bucket OCRSession by ISO week in Python (the engine lives in a JSONField,
        # which we cannot group on portably across DBs). We pull a bounded, recent window
        # ordered newest-first, accumulate per week, then keep the most recent 12 weeks.
        weekly_map: "OrderedDict[str, dict]" = OrderedDict()
        # Cap the scan rows we walk so a huge history can never blow memory; 12 weeks of
        # real scans is tiny, and we only need engine + created_at per row.
        recent_sessions = (
            OCRSession.objects
            .order_by("-created_at")
            .values("created_at", "raw_output")[:5000]
        )
        for s in recent_sessions:
            wk = _iso_week_label(s["created_at"])
            if not wk:
                continue
            bucket = weekly_map.setdefault(
                wk,
                {"week": wk, "scans": 0, "local": 0, "gemini": 0, "hybrid": 0},
            )
            bucket["scans"] += 1
            engine = (s.get("raw_output") or {}).get("_engine", "")
            bucket[_classify_engine(engine)] += 1

        # Sort weeks ascending, keep the last 12, and derive the rate fields.
        weekly = []
        for wk in sorted(weekly_map.keys())[-12:]:
            b = weekly_map[wk]
            local, gemini, hybrid = b["local"], b["gemini"], b["hybrid"]
            denom = local + gemini + hybrid
            # local_share = of the reads that an engine actually served, what fraction the
            # student served end-to-end (the cost-win metric). zero_touch is filled below
            # from the training corpus (sessions whose pair needed zero corrections).
            weekly.append({
                "week": wk,
                "scans": b["scans"],
                "local": local,
                "gemini": gemini,
                "hybrid": hybrid,
                "local_share": round(local / denom, 4) if denom else 0.0,
                "zero_touch": 0.0,                 # populated in the join below
                "gemini_calls": gemini,            # one Gemini read == one billable call
            })

        # ── zero_touch per week: fraction of that week's COMMITTED sessions whose captured
        # OCRTrainingPair needed zero admin corrections (num_corrections == 0). We join the
        # training pairs (which carry num_corrections) back to their session's week. A pair
        # links to its session via OCRTrainingPair.session; we read the session's
        # created_at to place it in the same ISO week as the weekly scan buckets above.
        zt_total: dict[str, int] = {}
        zt_zero: dict[str, int] = {}
        for p in (
            OCRTrainingPair.objects
            .filter(source="admin_review", session__isnull=False)
            .values("num_corrections", "session__created_at")[:5000]
        ):
            wk = _iso_week_label(p["session__created_at"])
            if not wk:
                continue
            zt_total[wk] = zt_total.get(wk, 0) + 1
            if (p["num_corrections"] or 0) == 0:
                zt_zero[wk] = zt_zero.get(wk, 0) + 1
        for row in weekly:
            wk = row["week"]
            tot = zt_total.get(wk, 0)
            row["zero_touch"] = round(zt_zero.get(wk, 0) / tot, 4) if tot else 0.0

        # ── retrain history (markers joined with bundle model cards) ───────────
        retrain_history = _collect_retrain_history()

        # ── shadow-model stats (running promotion evidence) ────────────────────
        try:
            shadow = model_registry.recent_shadow_stats() or {}
        except Exception:  # noqa: BLE001
            shadow = {}

        # ── dataset growth: cumulative gold pairs per ISO week (sparkline) ──────
        dataset_growth = _gold_growth_by_week()

        return Response({
            "active_model": active_model,
            "corpus": corpus,
            "weekly": weekly,
            "retrain_history": retrain_history,
            "shadow": shadow,
            "dataset_growth": dataset_growth,
        }, status=200)

    except Exception as exc:  # noqa: BLE001 - dashboard must not 500 on a data hiccup
        logger.exception("ocr_model_stats failed: %s", exc)
        return Response(
            {"message": "Could not assemble OCR model stats.", "detail": str(exc)},
            status=500,
        )


def _collect_retrain_history(limit: int = 50) -> list:
    """Build the dashboard's retrain-history timeline.

    Sources, merged into one newest-first list:
      • media/ocr_retrain/retrain_requested_*.json  - every retrain REQUEST the weekly
        trigger emitted (reason = volume/cadence, suggested_next_version, requested_at).
        These are 'requested but not yet shipped' rows (shipped=False) unless a matching
        bundle that shipped exists.
      • media/models/student_v*/model_card.json     - every deployed bundle's card; we read
        its eval_report.json 'ship' flag (or the card's own 'ship'/'gate_passed') to mark
        whether that version actually shipped.

    Each row: {"at": iso, "shipped": bool, "reason": str, "version": str}. Capped to
    `limit` rows. Fully defensive: a missing dir or torn file just yields fewer rows."""
    rows: list[dict] = []

    # 1. Retrain REQUEST markers (the 'a retrain was asked for' events).
    try:
        retrain_dir = os.path.join(settings.MEDIA_ROOT, "ocr_retrain")
        if os.path.isdir(retrain_dir):
            for fname in os.listdir(retrain_dir):
                if not (fname.startswith("retrain_requested_") and fname.endswith(".json")):
                    continue
                data = _read_json_file(os.path.join(retrain_dir, fname))
                rows.append({
                    "at": data.get("requested_at"),
                    # A request marker is a 'due' signal, not a ship; the bundle card below
                    # is the authority on whether a version actually shipped.
                    "shipped": False,
                    "reason": data.get("reason") or "requested",
                    "version": str(data.get("suggested_next_version") or ""),
                })
    except OSError:
        pass

    # 2. Deployed bundle model cards (the 'a version shipped' events).
    try:
        models_dir = os.path.join(settings.MEDIA_ROOT, "models")
        if os.path.isdir(models_dir):
            for entry in os.listdir(models_dir):
                bundle = os.path.join(models_dir, entry)
                if not (entry.startswith("student_v") and os.path.isdir(bundle)):
                    continue
                card = _read_json_file(os.path.join(bundle, "model_card.json"))
                report = _read_json_file(os.path.join(bundle, "eval_report.json"))
                # 'shipped' = the eval gate said ship. Prefer the eval_report flag (the
                # gate's own verdict); fall back to anything the card recorded.
                shipped = bool(
                    report.get("ship", card.get("ship", card.get("gate_passed", False)))
                )
                version = str(card.get("version") or entry[len("student_v"):])
                at = card.get("promoted_at") or card.get("created_at")
                if not at:
                    try:
                        at = datetime.fromtimestamp(
                            os.path.getmtime(bundle), tz=timezone.utc
                        ).isoformat()
                    except OSError:
                        at = None
                rows.append({
                    "at": at,
                    "shipped": shipped,
                    "reason": card.get("reason") or "deployed",
                    "version": version,
                })
    except OSError:
        pass

    # Newest first; rows with no timestamp sink to the bottom. Cap to `limit`.
    rows.sort(key=lambda r: (r.get("at") is None, r.get("at") or ""), reverse=True)
    return rows[:limit]


def _gold_growth_by_week(weeks: int = 12) -> list:
    """Cumulative GOLD (admin_review) pair count per ISO week, last `weeks` weeks.

    Powers the corpus-growth sparkline on the dashboard ('how fast is our human-confirmed
    truth growing'). We group gold pairs by their created week, then run a running total
    so each point is the cumulative corpus size as of that week. Defensive: empty corpus
    -> empty list."""
    try:
        per_week = (
            OCRTrainingPair.objects
            .filter(source="admin_review")
            .annotate(wk=TruncWeek("created_at"))
            .values("wk")
            .annotate(n=Count("pair_id"))
            .order_by("wk")
        )
    except Exception:  # noqa: BLE001
        return []

    cumulative = 0
    points = []
    for row in per_week:
        cumulative += row["n"]
        points.append({"week": _iso_week_label(row["wk"]), "cumulative_gold": cumulative})
    # Keep the tail (most recent `weeks` points) but preserve the running cumulative value.
    return points[-weeks:]


# ──────────────────────────────────────────────────────────────────────────────
# 2. GET /events/ocr/dataset-export/ - the downloadable training dataset zip
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def ocr_dataset_export(request):
    """GET /events/ocr/dataset-export/?splits=train,eval&include_synthetic=true
    -> a downloadable application/zip of the assembled PaddleOCR recognition dataset.

    Consumers:
      • train_cycle.py (off-box trainer) downloads this before a training run, OR
      • the dashboard's "Download dataset" button (same endpoint, same bytes).

    Builds the dataset by calling services.dataset.assemble_rec_dataset into a TEMP dir
    (rec_gt.txt + crops/ + rec_keys.txt), then zips that temp dir plus a manifest.json
    carrying the dataset_version + per-split counts. The temp dir is always cleaned up.

    Query params (all optional, defensively parsed -> never 500 on junk):
      • splits=train,eval         comma list of split buckets (default 'train'). Only
                                  train/eval/holdout are honoured; unknown tokens dropped.
      • include_synthetic=true    whether synthetic crops are included (default true).

    Zero crops -> a VALID empty-but-well-formed zip (rec_gt.txt empty, rec_keys.txt, the
    manifest) returned 200, not an error (this is the normal cold-corpus state). Auth:
    Bearer + _is_ocr_admin (400/401/403)."""
    user, err = _require_ocr_admin(request)
    if err:
        return err

    # ── parse query params defensively (bad input -> sane defaults, never a 500) ──
    raw_splits = (request.query_params.get("splits") or "train").strip()
    allowed_splits = {"train", "eval", "holdout"}
    splits = tuple(
        s for s in (tok.strip() for tok in raw_splits.split(",")) if s in allowed_splits
    ) or ("train",)   # fall back to train if the caller sent only junk tokens

    inc_raw = (request.query_params.get("include_synthetic") or "true").strip().lower()
    include_synthetic = inc_raw not in ("false", "0", "no", "off")

    tmp_dir = None
    try:
        # Assemble into a throwaway temp dir (gitignored by nature; cleaned in finally).
        tmp_dir = tempfile.mkdtemp(prefix="ocr_dataset_export_")
        manifest = ocr_dataset.assemble_rec_dataset(
            out_dir=tmp_dir,
            splits=splits,
            include_synthetic=include_synthetic,
        )

        # Build the zip IN MEMORY so we can stream the bytes back without a second temp
        # file. The dataset is small (cropped cells), so this is cheap and avoids leaving
        # an artifact on disk. We walk the assembled out_dir and add every file under it.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(tmp_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    # arcname = path relative to out_dir so the zip unpacks to
                    # rec_gt.txt / rec_keys.txt / crops/... at its root.
                    arcname = os.path.relpath(abs_path, tmp_dir)
                    zf.write(abs_path, arcname)

            # manifest.json: the dataset_version + counts train_cycle.py needs to tag its
            # run. assemble_rec_dataset returns absolute paths in the manifest; we strip
            # those to keep the zip relocatable + free of server paths.
            manifest_public = {
                "dataset_version": manifest.get("dataset_version"),
                "splits": manifest.get("splits"),
                "counts": manifest.get("counts"),
                "by_source": manifest.get("by_source"),
                "num_pairs": manifest.get("num_pairs"),
                "num_chars": manifest.get("num_chars"),
                "skipped_no_crop": manifest.get("skipped_no_crop"),
                "include_synthetic": include_synthetic,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }
            zf.writestr("manifest.json", _json.dumps(manifest_public, indent=2))

            # Guarantee a well-formed zip even when assemble wrote nothing: if rec_gt.txt
            # somehow was not emitted (it always is, but be safe), add an empty one so the
            # archive is always a valid dataset shell.
            if not os.path.exists(os.path.join(tmp_dir, ocr_dataset.REC_GT_FILENAME)):
                zf.writestr(ocr_dataset.REC_GT_FILENAME, "")

        zip_bytes = buf.getvalue()
        version_tag = manifest.get("dataset_version") or "v_empty"
        resp = HttpResponse(zip_bytes, content_type="application/zip")
        resp["Content-Disposition"] = (
            f'attachment; filename="ocr_dataset_{version_tag}.zip"'
        )
        resp["Content-Length"] = str(len(zip_bytes))
        return resp

    except Exception as exc:  # noqa: BLE001 - never leak a stack to the client
        logger.exception("ocr_dataset_export failed: %s", exc)
        return Response(
            {"message": "Could not build the dataset export.", "detail": str(exc)},
            status=500,
        )
    finally:
        # Always remove the temp dir (success or failure). Best-effort: a cleanup error
        # must not turn a successful export into a 500.
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# 3. GET /events/ocr/retrain-status/ - "is a retrain due?" for train_cycle.py to poll
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def ocr_retrain_status(request):
    """GET /events/ocr/retrain-status/ -> {"due", "new_gold_since_last",
    "last_dataset_version", "reason"}.

    Consumer: train_cycle.py POLLS this before kicking off an (expensive) off-box training
    run, so the GPU only spins up when enough new human-confirmed data has accumulated. It
    is the read-only twin of tasks.check_retrain_trigger (which is the WRITE side that
    actually drops the marker on the weekly beat); here we only REPORT the same decision
    using the same hysteresis thresholds, without writing anything.

    Logic (mirrors tasks.check_retrain_trigger, read-only):
      • new_gold = OCRTrainingPair(source='admin_review') created AFTER the last retrain
        marker's timestamp (or all-time gold if no marker yet).
      • due = VOLUME (new_gold >= N_NEW) OR CADENCE (>= MIN_DAYS_BETWEEN days since last
        marker AND new_gold >= N_MIN). Same env-tunable knobs the task reads.
      • last_dataset_version = the suggested_next_version recorded in the newest marker
        (what the off-box run should call its output), or None on a cold corpus.

    Defensive: empty corpus / no marker -> due False, reason 'cold' or 'below_threshold',
    never a crash. Auth: Bearer + _is_ocr_admin (400/401/403)."""
    user, err = _require_ocr_admin(request)
    if err:
        return err

    try:
        # Reuse the task's OWN private helpers + thresholds so the poll answer can never
        # drift from the marker the weekly task writes. These are pure reads (no marker
        # is written here). If the task module signature ever changes, this import is the
        # single coupling point to update.
        from . import tasks as ocr_tasks

        last_request_at = ocr_tasks._last_retrain_request_at()

        gold_qs = OCRTrainingPair.objects.filter(source="admin_review")
        if last_request_at:
            gold_qs = gold_qs.filter(created_at__gt=last_request_at)
        new_gold = gold_qs.count()

        days_since = (now() - last_request_at).days if last_request_at else None

        # Replicate the hysteresis decision (volume OR cadence), read-only.
        n_new = ocr_tasks._RETRAIN_N_NEW
        n_min = ocr_tasks._RETRAIN_N_MIN
        min_days = ocr_tasks._RETRAIN_MIN_DAYS_BETWEEN

        due = False
        reason = "below_threshold"
        if new_gold >= n_new:
            due, reason = True, "volume"
        elif new_gold >= n_min and (
            last_request_at is None or (days_since is not None and days_since >= min_days)
        ):
            due, reason = True, "cadence"
        elif last_request_at is None and new_gold == 0:
            reason = "cold"

        # last_dataset_version = the next-version the most recent marker suggested (so the
        # trainer knows what to name its output). None until the first marker exists.
        last_dataset_version = None
        try:
            marker_dir = ocr_tasks._retrain_dir()
            markers = sorted(
                f for f in os.listdir(marker_dir)
                if f.startswith("retrain_requested_") and f.endswith(".json")
            )
            if markers:
                data = _read_json_file(os.path.join(marker_dir, markers[-1]))
                last_dataset_version = data.get("suggested_next_version")
        except OSError:
            last_dataset_version = None

        return Response({
            "due": due,
            "new_gold_since_last": new_gold,
            "last_dataset_version": last_dataset_version,
            "reason": reason,
        }, status=200)

    except Exception as exc:  # noqa: BLE001
        logger.exception("ocr_retrain_status failed: %s", exc)
        return Response(
            {"message": "Could not compute retrain status.", "detail": str(exc)},
            status=500,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 4. POST /events/ocr/upload-model/ - the off-box PC pushes a trained bundle back
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def ocr_upload_model(request):
    """POST /events/ocr/upload-model/?promote=true|false  (multipart: file=<bundle.zip>)
    -> store the trained student bundle on the server, optionally promoting it live.

    Consumer: train_cycle.py on the GPU PC, AFTER a training run that the eval gate
    cleared. This is the return leg of the loop: dataset-export goes out, upload-model
    comes back. The dashboard never calls this (it only promotes ALREADY-uploaded
    versions via /promote/).

    The uploaded zip MUST contain, at its root:
      • rec.onnx          the fine-tuned recognizer (local_ocr loads this),
      • rec_keys.txt      the char dictionary baked into that onnx,
      • VERSION           the human version string (e.g. '12'),
      • model_card.json   provenance (dataset_version, metrics, etc.),
      • eval_report.json  the eval-gate verdict - MUST say ship==true or we reject 400.

    Flow:
      1. Read VERSION from the zip -> the target dir media/models/student_v<VERSION>/.
      2. Validate every required file is present AND eval_report.json 'ship' is true. A
         model the gate did not clear is REJECTED 400 (we never store/serve a non-shipping
         model). Path-traversal in zip entry names is rejected (security: ZipSlip).
      3. Extract into media/models/student_v<VERSION>/ (gitignored).
      4. If ?promote=true -> model_registry.promote(VERSION) flips it live (blue-green).

    Returns {"version", "stored": true, "promoted": bool, "gate_passed": bool}. Bad
    input (no file, not a zip, missing files, gate fail) -> 400, never 500. Auth: Bearer +
    _is_ocr_admin (400/401/403)."""
    user, err = _require_ocr_admin(request)
    if err:
        return err

    upload = request.FILES.get("file")
    if not upload:
        return Response({"message": "A bundle zip file is required (field 'file')."}, status=400)

    # ?promote may arrive as a query param OR a form field; accept either, default false.
    promote_raw = (
        request.query_params.get("promote")
        or request.data.get("promote")
        or "false"
    )
    want_promote = str(promote_raw).strip().lower() in ("true", "1", "yes", "on")

    # Required members the bundle must carry (mirrors what local_ocr + the gate expect).
    required = {"rec.onnx", "rec_keys.txt", "VERSION", "model_card.json", "eval_report.json"}

    try:
        # Read the whole upload into memory (bundles are a few MB) and open as a zip. A
        # non-zip payload raises BadZipFile -> clean 400 below.
        data = upload.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return Response({"message": "Uploaded file is not a valid zip bundle."}, status=400)

        with zf:
            # Map basename -> entry so the bundle may be zipped flat OR inside a top folder.
            # We also guard against ZipSlip: reject any entry whose name escapes via .. or
            # an absolute path (we only ever read by basename, but validate to be safe).
            names = zf.namelist()
            for n in names:
                norm = os.path.normpath(n)
                if norm.startswith("..") or os.path.isabs(norm):
                    return Response(
                        {"message": f"Unsafe path in bundle: {n}"}, status=400
                    )
            by_base = {}
            for n in names:
                base = os.path.basename(n)
                if base and base not in by_base:  # first wins; ignore dir entries (base='')
                    by_base[base] = n

            missing = sorted(required - set(by_base.keys()))
            if missing:
                return Response(
                    {"message": f"Bundle is missing required files: {missing}"},
                    status=400,
                )

            # ── read VERSION (decides the target dir name) ─────────────────────
            version = zf.read(by_base["VERSION"]).decode("utf-8", "replace").strip()
            if not version:
                return Response({"message": "Bundle VERSION file is empty."}, status=400)
            # Keep the version filesystem-safe (it becomes a dir name student_v<version>).
            safe_version = "".join(
                c for c in version if c.isalnum() or c in ("_", "-", ".")
            )
            if not safe_version:
                return Response({"message": "Bundle VERSION is not a usable version string."}, status=400)

            # ── eval-gate verdict MUST be ship==true ───────────────────────────
            try:
                report = _json.loads(zf.read(by_base["eval_report.json"]).decode("utf-8", "replace"))
            except ValueError:
                return Response({"message": "eval_report.json in bundle is not valid JSON."}, status=400)
            gate_passed = bool(report.get("ship", False))
            if not gate_passed:
                # We never store/serve a model the gate did not clear (the safety spine).
                return Response(
                    {
                        "message": "Bundle rejected: eval_report.json ship is not true "
                                   "(the eval gate did not clear this model).",
                        "gate_passed": False,
                    },
                    status=400,
                )

            # ── extract into media/models/student_v<version>/ ──────────────────
            target_dir = model_registry.bundle_dir_for(safe_version)
            # Fresh dir: clear any prior partial of the same version so we never mix files.
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            os.makedirs(target_dir, exist_ok=True)
            # Write only the required members (flat), reading each by its in-zip name. This
            # also means a bundle nested in a folder still lands flat in target_dir.
            for base in required:
                with open(os.path.join(target_dir, base), "wb") as out:
                    out.write(zf.read(by_base[base]))

        # ── optional promote (blue-green flip) ─────────────────────────────────
        promoted = False
        if want_promote:
            try:
                model_registry.promote(safe_version)
                promoted = True
            except FileNotFoundError as exc:
                # Should not happen (we just wrote the dir), but surface cleanly if it does.
                return Response(
                    {"message": f"Stored but could not promote: {exc}",
                     "version": safe_version, "stored": True, "promoted": False,
                     "gate_passed": gate_passed},
                    status=400,
                )

        return Response({
            "version": safe_version,
            "stored": True,
            "promoted": promoted,
            "gate_passed": gate_passed,
        }, status=200)

    except Exception as exc:  # noqa: BLE001
        logger.exception("ocr_upload_model failed: %s", exc)
        return Response(
            {"message": "Could not store the uploaded model bundle.", "detail": str(exc)},
            status=500,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 5. POST /events/ocr/promote/  +  POST /events/ocr/rollback/ - dashboard buttons
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def ocr_promote_model(request):
    """POST /events/ocr/promote/  body {"version": "<v>"} -> make student_v<v> the live
    model (blue-green green-cut) and return the new active version.

    Consumer: the "Promote" button on the OCR Model dashboard (operates on a version that
    was ALREADY uploaded via /upload-model/). Thin wrapper over model_registry.promote.

    Returns {"active_version": <v>, "promoted": true}. A version whose bundle is not on
    disk -> 400 (FileNotFoundError from promote), never 500. Auth: Bearer + _is_ocr_admin."""
    user, err = _require_ocr_admin(request)
    if err:
        return err

    version = request.data.get("version")
    if version in (None, ""):
        return Response({"message": "version is required."}, status=400)

    try:
        model_registry.promote(version)
        return Response(
            {"active_version": model_registry.active_version(), "promoted": True},
            status=200,
        )
    except FileNotFoundError as exc:
        # Bundle not deployed at media/models/student_v<version>/ - caller error, not a 500.
        return Response({"message": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ocr_promote_model failed: %s", exc)
        return Response(
            {"message": "Could not promote the model.", "detail": str(exc)},
            status=500,
        )


@api_view(["POST"])
def ocr_rollback_model(request):
    """POST /events/ocr/rollback/ -> revert to the previously live model (blue-green
    blue-cut) and return the now-active version.

    Consumer: the "Rollback" button on the OCR Model dashboard. Thin wrapper over
    model_registry.rollback. When there is no valid previous bundle, the registry clears
    the pointer (cold start / Gemini-only) and active_version becomes null - we report
    that honestly rather than erroring.

    Returns {"active_version": <v|null>, "rolled_back": true}. Auth: Bearer +
    _is_ocr_admin (400/401/403)."""
    user, err = _require_ocr_admin(request)
    if err:
        return err

    try:
        model_registry.rollback()   # returns the new active dir or None (cold start)
        return Response(
            {"active_version": model_registry.active_version(), "rolled_back": True},
            status=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ocr_rollback_model failed: %s", exc)
        return Response(
            {"message": "Could not roll back the model.", "detail": str(exc)},
            status=500,
        )
