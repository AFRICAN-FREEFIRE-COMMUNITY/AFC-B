"""
afc_leaderboard.ocr — the OCR-engine layer for the standalone-leaderboard multi-image batch (Phase 2.6).

WHY THIS MODULE
    The actual screenshot-reading work is kept here as plain functions (not in the Celery task or the
    views) so it is unit-testable and import-cheap. The Celery task (afc_leaderboard.tasks
    .process_leaderboard_ocr_job) is a thin wrapper that just calls process_job(job); the views call the
    row builders for the legacy single-shot endpoint. One place owns "screenshot bytes -> review rows".

WHAT IT DOES
    process_job(job): read every LeaderboardOcrImage on a job via the SHARED extractor
    (afc_ocr.services.extract.extract_rows — local student first, Gemini teacher fallback, exactly the
    engine the event OCR flow uses), MERGE the placements read from a map's several screenshots into one
    ordered standings list, match the read names against the WHOLE platform (every Team / every User),
    and store the resulting review rows + engine on the job. It never raises into the worker: any failure
    is recorded on the job as status="failed" + error, so the FE poll shows a clean message.

HOW IT CONNECTS
    - Reads: afc_ocr.services.extract.extract_rows + afc_ocr.services.matching (all_platform_teams /
      all_platform_players / match_team_name / match_name) — the un-gated, platform-wide matchers
      (a standalone leaderboard has no event roster to scope to).
    - Writes: LeaderboardOcrJob.rows / .engine / .status and each LeaderboardOcrImage.raw_output.
    - Row builders (build_team_ocr_rows / build_solo_ocr_rows) are ALSO imported by
      afc_leaderboard.views for the legacy single-shot ocr_extract, so the row shape lives in one place.
"""
import logging
import re

from afc_ocr.services import extract
from afc_ocr.services.matching import (
    all_platform_players, all_platform_teams, match_team_name, match_name, derive_team_tag,
)

logger = logging.getLogger(__name__)


# ── row builders (one review row per competitor) ─────────────────────────────────────────────────
def build_team_ocr_rows(raw_output, teams):
    """Turn the extractor's raw {placements:[...]} into review rows for a TEAM leaderboard.

    For each placement we read the team_name (the team_standings prompt asks Gemini for it) and match it
    against the platform team pool via afc_ocr.matching.match_team_name. kills is the placement-level
    summed team kills when present, else the sum of the placement's players' kills (tolerant fallback
    when Gemini omitted the placement total). Returns the team-shaped rows:
      {row_id, raw_name, placement, kills, matched_team_id, matched_name, confidence, top_candidates,
       is_unmatched}.
    """
    rows = []
    for entry in raw_output.get("placements", []):
        placement = int(entry.get("placement", 0) or 0)
        raw_name = (entry.get("team_name") or "").strip()
        # The player names the OCR read inside this placement (owner 2026-06-11: "display the full name
        # it sees"). Surfaced to the FE so the admin can identify the team even when only a short tag (or
        # nothing) was read for the team name, and so a created ghost team inherits this roster.
        players_read = [
            (p.get("name") or "").strip()
            for p in entry.get("players", [])
            if (p.get("name") or "").strip()
        ]
        if entry.get("kills") is not None:
            kills = int(entry.get("kills") or 0)
        else:
            kills = sum(int(p.get("kills", 0) or 0) for p in entry.get("players", []))
        # Match on the read team name; if it is blank (FF often shows only a logo for the team), fall
        # back to the tag shared across the placement's player IGNs (e.g. "AE.John" + "AE.Mike" -> "AE"),
        # which match_team_name scores against each team's team_tag. This is the owner's "team tags help
        # when searching for teams through the tags on the players names".
        derived_tag = derive_team_tag(players_read) if not raw_name else ""
        m = match_team_name(raw_name or derived_tag, teams)
        rows.append({
            "row_id": m["row_id"],
            # Display the team name when read, else the tag we inferred from the players (so the row is
            # never blank), else nothing (the FE shows "team name not read" + the players it saw).
            "raw_name": raw_name or derived_tag,
            "players_read": players_read,
            "placement": placement,
            "kills": kills,
            "matched_team_id": m["matched_team_id"],
            "matched_name": m["matched_team_name"],
            "confidence": m["confidence"],
            "top_candidates": m["top_candidates"],
            "is_unmatched": m["matched_team_id"] is None,
        })
    return rows


def build_solo_ocr_rows(raw_output, players):
    """Turn the extractor's raw {placements:[...]} into review rows for a SOLO leaderboard.

    Each placement holds one (or more) player rows; we match each read player name against the platform
    user pool via afc_ocr.matching.match_name (reused as-is from the event flow). Returns the user-shaped
    rows: {row_id, raw_name, placement, kills, matched_user_id, matched_name, confidence, top_candidates,
    is_unmatched}.
    """
    rows = []
    for entry in raw_output.get("placements", []):
        placement = int(entry.get("placement", 0) or 0)
        for player in entry.get("players", []):
            raw_name = (player.get("name") or "").strip()
            kills = int(player.get("kills", 0) or 0)
            m = match_name(raw_name, players)
            rows.append({
                "row_id": m["row_id"],
                "raw_name": raw_name,
                "placement": placement,
                "kills": kills,
                "matched_user_id": m["matched_user_id"],
                "matched_name": m["matched_username"],
                "confidence": m["confidence"],
                "top_candidates": m["top_candidates"],
                "is_unmatched": m["matched_user_id"] is None,
            })
    return rows


# ── multi-image merge ─────────────────────────────────────────────────────────────────────────────
def _norm(s):
    """Loose key for dedupe: lowercased, alphanumerics only (so 'V-ENT' and 'vent' collide)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def merge_placements(placement_lists, is_team):
    """Merge the placement entries read from SEVERAL screenshots of ONE map into a single ordered list.

    A map's standings are often split across more than one screenshot (e.g. placements 1-6 on one screen,
    7-12 on the next, or a top/bottom half). We concatenate every image's placements, drop EXACT
    duplicates (a team/player set already seen at the same placement, in case two shots overlap), and
    sort by placement. Deliberately tolerant, not clever: the admin reviews + corrects the merged rows
    afterwards, so a stray double is a quick delete rather than a silent miscount.

    `placement_lists` is a list (per image) of placement-entry lists. Returns one flat, ordered list.
    """
    seen = set()
    merged = []
    for plist in placement_lists:
        for entry in plist or []:
            placement = entry.get("placement")
            if is_team:
                key = (placement, _norm(entry.get("team_name", "")))
            else:
                names = tuple(sorted(_norm(p.get("name", "")) for p in entry.get("players", [])))
                key = (placement, names)
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    merged.sort(key=lambda e: (e.get("placement") or 0))
    return merged


def _guess_mime(name):
    """Best-effort mime from the stored file name (ImageField does not keep content_type). Only matters
    for Gemini's inline_data; the local student sniffs the bytes regardless."""
    n = (name or "").lower()
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


# ── the background worker body ──────────────────────────────────────────────────────────────────
def process_job(job):
    """Read every image on `job`, merge their placements, match against the platform, store review rows.

    Sets job.status pending -> processing -> done | failed. NEVER raises (a failure is captured on the
    job so the FE poll surfaces it). Called by the Celery task afc_leaderboard.tasks
    .process_leaderboard_ocr_job; also callable inline in tests / eager mode.
    """
    job.status = "processing"
    job.save(update_fields=["status", "updated_at"])
    try:
        lb = job.leaderboard
        is_team = lb.format == "team"
        prompt_kind = "team_standings" if is_team else None
        event_type = "team" if is_team else "solo"

        images = list(job.images.all())
        if not images:
            raise RuntimeError("No screenshots were attached to this map.")

        placement_lists = []
        engine = ""
        for img in images:
            data = img.image.read()           # FieldFile.read() opens lazily
            try:
                img.image.close()
            except Exception:
                pass
            raw, eng = extract.extract_rows(
                data, _guess_mime(img.image.name), event_type, prompt_kind=prompt_kind,
            )
            img.raw_output = raw
            img.save(update_fields=["raw_output"])
            placement_lists.append(raw.get("placements", []) or [])
            engine = eng or engine

        merged = merge_placements(placement_lists, is_team)
        if is_team:
            rows = build_team_ocr_rows({"placements": merged}, all_platform_teams())
        else:
            rows = build_solo_ocr_rows({"placements": merged}, all_platform_players())

        job.rows = rows
        job.engine = engine
        job.status = "done"
        job.error = ""
        job.save(update_fields=["rows", "engine", "status", "error", "updated_at"])
    except Exception as e:  # noqa: BLE001 — a failed read must mark the job, never crash the worker
        logger.exception("afc_leaderboard.ocr.process_job failed for job %s", getattr(job, "id", "?"))
        job.status = "failed"
        job.error = str(e)[:2000]
        job.save(update_fields=["status", "error", "updated_at"])
