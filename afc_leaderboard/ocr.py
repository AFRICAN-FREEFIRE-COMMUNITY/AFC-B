"""
afc_leaderboard.ocr - the OCR-engine layer for the standalone-leaderboard multi-image batch (Phase 2.6).

WHY THIS MODULE
    The actual screenshot-reading work is kept here as plain functions (not in the Celery task or the
    views) so it is unit-testable and import-cheap. The Celery task (afc_leaderboard.tasks
    .process_leaderboard_ocr_job) is a thin wrapper that just calls process_job(job); the views call the
    row builders for the legacy single-shot endpoint. One place owns "screenshot bytes -> review rows".

WHAT IT DOES
    process_job(job): read every LeaderboardOcrImage on a job via the SHARED extractor
    (afc_ocr.services.extract.extract_rows - local student first, Gemini teacher fallback, exactly the
    engine the event OCR flow uses), MERGE the placements read from a map's several screenshots into one
    ordered standings list, match the read names against the WHOLE platform (every Team / every User),
    and store the resulting review rows + engine on the job. It never raises into the worker: any failure
    is recorded on the job as status="failed" + error, so the FE poll shows a clean message.

HOW IT CONNECTS
    - Reads: afc_ocr.services.extract.extract_rows + afc_ocr.services.matching (all_platform_teams /
      all_platform_players / match_team_name / match_name) - the un-gated, platform-wide matchers
      (a standalone leaderboard has no event roster to scope to).
    - Writes: LeaderboardOcrJob.rows / .engine / .status and each LeaderboardOcrImage.raw_output.
    - Row builders (build_team_ocr_rows / build_solo_ocr_rows) are ALSO imported by
      afc_leaderboard.views for the legacy single-shot ocr_extract, so the row shape lives in one place.
"""
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from afc_ocr.services import extract
from afc_ocr.services.matching import (
    all_platform_players, all_platform_teams_with_ghosts, match_team_name, match_name,
    derive_team_tag, REASON_TEAM_CONFLICT,
)
from utils.search_utils import normalize_search_text

logger = logging.getLogger(__name__)

# A player match is only trusted as a TEAM signal at or above this score. Mirrors the frontend's
# auto-pick gate (OcrReviewTable.tsx: `p.confidence >= 0.75 ? ... : null`) so the team we infer from
# the players is inferred from exactly the players the reviewer sees pre-selected.
PLAYER_TRUST = 0.75


# ── team identity from the PLAYERS that were read ────────────────────────────────────────────────
def team_from_players(players_detail):
    """Infer a placement's team from the platform teams its READ PLAYERS belong to.

    WHY: a Free Fire result screen frequently shows a logo, a 2-4 character clan tag, or nothing at
    all where the team name belongs, so matching on the read team string alone is thin evidence. The
    players, however, are named in full. If several of them resolve confidently to AFC users who are
    all on the same platform team, that team is the placement - independently of what the team cell
    said. Real clone data: a row whose team cell read "KN" name-matched "KNIGHTS X4" @0.90, while its
    players ("DANTE", "SABATH24") are members of "KNIGHTS E-SPORTS". The players were right.

    Only players matched at or above PLAYER_TRUST vote, and only when they have a platform team.
    Returns (team_id, team_name, votes, voters) where `voters` is how many trusted players had ANY
    team, so the caller can tell a 3-of-3 agreement from a 2-of-5 split. Returns (None, None, 0, 0)
    when nothing votes.

    Consumed by build_team_ocr_rows / build_rows_from_match_log below; the values only ever feed the
    review row's suggestion + confidence, never a direct write.
    """
    votes = Counter()
    names = {}
    voters = 0
    for p in players_detail or []:
        if (p.get("confidence") or 0) < PLAYER_TRUST:
            continue
        team_id = p.get("matched_team_id")
        if not team_id:
            continue
        voters += 1
        votes[team_id] += 1
        names[team_id] = p.get("matched_team_name")
    if not votes:
        return None, None, 0, 0
    team_id, count = votes.most_common(1)[0]
    return team_id, names.get(team_id), count, voters


def trusted_player_team_ids(players_detail):
    """Every platform team a CONFIDENTLY matched player of this placement belongs to.

    The corroboration input for afc_ocr.services.matching's short-tag guard (SHORT_TAG_MAX_LEN): a
    bare 2-4 character team cell only binds when something outside that cell agrees, and "one of the
    players we read is actually on this team" is the strongest form of that agreement. Reuses the
    same PLAYER_TRUST gate team_from_players votes on, so the two signals are drawn from exactly the
    same set of players the reviewer sees pre-selected.

    A SET, not a plurality: for corroboration we only need to know whether the tag's team appears at
    all, and demanding a plurality here would throw away the very case the guard exists to allow (a
    placement where the OCR only managed to match one of the four players).

    Passed to match_team_name by build_team_ocr_rows / build_rows_from_match_log below.
    """
    return {
        p["matched_team_id"]
        for p in players_detail or []
        if (p.get("confidence") or 0) >= PLAYER_TRUST and p.get("matched_team_id")
    }


def _resolve_row_team(name_match, players_detail):
    """Combine the team-NAME match with the team-FROM-PLAYERS signal into the row's final identity.

    The two signals are independent, so we treat them as votes rather than letting the name match win
    by default (it used to be the only input; `players_detail` was computed and then discarded):

      agree            -> confidence lifted to >= 0.9. Two independent signals on the same team.
      name weak/absent -> adopt the players' team at 0.8 when at least 2 trusted players agree AND
                          they are a majority of the players that had a team. Above the review gate,
                          because "two roster-confirmed teammates" is stronger evidence than a
                          3-character tag fuzz, but never 1.0 - it is still an inference.
      CONFLICT         -> both signals are confident and they DISAGREE. We do not pick. The name
                          match stays as the suggestion but its confidence is dropped below the
                          review gate and the players' team is inserted as the top alternative, so
                          the row surfaces for a human instead of silently binding a wrong team to a
                          tournament standing.

    Returns (matched_team_id, matched_team_name, confidence, top_candidates, unmatched_reason).
    `unmatched_reason` is "" when the row ends up bound, else the REASON_* string the review table
    renders as its "why not" line: it is inherited from match_team_name (below the floor / a tie /
    an uncorroborated short tag) and CLEARED here when the players end up carrying the row, because
    a row that binds has nothing to explain.
    """
    p_team_id, p_team_name, votes, voters = team_from_players(players_detail)
    name_id = name_match["matched_team_id"]
    name_conf = name_match["confidence"]
    name_reason = name_match.get("unmatched_reason", "")
    candidates = list(name_match["top_candidates"])

    # No player signal at all -> the name match is all we have, unchanged.
    if not p_team_id or votes < 2:
        return name_id, name_match["matched_team_name"], name_conf, candidates, name_reason

    players_majority = votes * 2 >= voters
    player_cand = {"team_id": p_team_id, "team_name": p_team_name, "confidence": 0.8}

    if name_id == p_team_id:
        # Both signals point at the same team.
        conf = max(name_conf, 0.9)
        candidates = [dict(c, confidence=conf) if c.get("team_id") == p_team_id else c
                      for c in candidates] or [dict(player_cand, confidence=conf)]
        return name_id, name_match["matched_team_name"], conf, candidates, ""

    if name_conf < PLAYER_TRUST and players_majority:
        # The team cell was unreadable, matched weakly, or was withheld by one of match_team_name's
        # guards; the players carry the row, so whatever that guard objected to no longer applies.
        rest = [c for c in candidates if c.get("team_id") != p_team_id]
        return p_team_id, p_team_name, 0.8, [player_cand] + rest, ""

    if name_conf >= PLAYER_TRUST and players_majority:
        # CONFLICT: two confident, disagreeing signals. Surface both, auto-resolve neither.
        rest = [c for c in candidates if c.get("team_id") != p_team_id]
        return (name_id, name_match["matched_team_name"], min(name_conf, 0.7),
                [player_cand] + rest, REASON_TEAM_CONFLICT)

    return name_id, name_match["matched_team_name"], name_conf, candidates, name_reason


# ── row builders (one review row per competitor) ─────────────────────────────────────────────────
def build_team_ocr_rows(raw_output, teams, players_pool=None):
    """Turn the extractor's raw {placements:[...]} into review rows for a TEAM leaderboard.

    For each placement we read the team_name (the team_standings prompt asks Gemini for it) and match it
    against the platform team pool via afc_ocr.matching.match_team_name. kills is the placement-level
    summed team kills when present, else the sum of the placement's players' kills (tolerant fallback
    when Gemini omitted the placement total). Returns the team-shaped rows:
      {row_id, raw_name, players_read, players_detail, placement, kills, matched_team_id, matched_name,
       confidence, unmatched_reason, top_candidates, is_unmatched}.

    unmatched_reason is "" on a bound row and otherwise one of afc_ocr.services.matching's REASON_*
    strings ("below_floor" / "ambiguous" / "tag_needs_corroboration" / "team_conflict" /
    "no_candidates"), which the review table renders as the one-line explanation of why it is asking
    the admin to pick. It is the ONLY place that answer exists, so it must ride on every row.

    players_pool (optional): the platform user pool from afc_ocr.matching.all_platform_players. When
    given, EACH read player is also matched against it (owner 2026-06-12: "like it brings out
    suggestions for teams it should also try to find matches for the players") and the row carries
    players_detail = [{name, kills, matched_user_id, matched_username, confidence, unmatched_reason,
    top_candidates, is_unmatched}] for the FE per-player approve/search/ghost controls
    (OcrReviewTable). Those per-player matches are ALSO what corroborates a short team-cell tag (see
    trusted_player_team_ids). When None (older callers), only the plain players_read name list is
    produced, exactly as before - and a short tag then has no corroboration available, so it surfaces.
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
        # Per-player platform matching (the FE shows each player's kills + match suggestions and lets
        # the admin approve/correct each one, mirroring the team-level candidate flow).
        players_detail = []
        if players_pool is not None:
            for p in entry.get("players", []):
                pname = (p.get("name") or "").strip()
                if not pname:
                    continue
                pm = match_name(pname, players_pool)
                players_detail.append({
                    "name": pname,
                    "kills": int(p.get("kills", 0) or 0),
                    "matched_user_id": pm["matched_user_id"],
                    "matched_username": pm["matched_username"],
                    # The matched player's CURRENT platform team (owner 2026-06-12: show which
                    # team each suggested player is in, not just the username). The id rides along
                    # so team_from_players can tally teams without re-querying (the FE only renders
                    # the name).
                    "matched_team_id": pm.get("matched_team_id"),
                    "matched_team_name": pm.get("matched_team_name"),
                    "confidence": pm["confidence"],
                    # Why this player did not auto-bind (afc_ocr.services.matching REASON_*), so the
                    # players panel can say "two accounts matched" instead of a bare "not on platform".
                    "unmatched_reason": pm.get("unmatched_reason", ""),
                    "top_candidates": pm["top_candidates"],
                    "is_unmatched": pm["matched_user_id"] is None,
                })
        if entry.get("kills") is not None:
            kills = int(entry.get("kills") or 0)
        else:
            kills = sum(int(p.get("kills", 0) or 0) for p in entry.get("players", []))
        # The clan tag the placement's player IGNs wear ("AE.John" + "AE.Mike" -> "AE"). It has TWO
        # jobs: it stands in as the read when the team cell is blank (FF often shows only a logo),
        # and it CORROBORATES a short team-cell tag against each team's registered team_tag. This is
        # the owner's "team tags help when searching for teams through the tags on the players names".
        derived_tag = derive_team_tag(players_read)
        m = match_team_name(
            raw_name or derived_tag, teams,
            # Corroboration for the short-tag guard (matching.SHORT_TAG_MAX_LEN). players_tag is only
            # passed when the team cell WAS read: if the read is itself the derived tag, handing it
            # back as corroboration would let one observation vouch for itself.
            player_team_ids=trusted_player_team_ids(players_detail),
            players_tag=derived_tag if raw_name else "",
        )
        # Fold in the team the PLAYERS resolve to (see _resolve_row_team): agreement raises the
        # confidence, an unreadable team cell is carried by the players, and a genuine disagreement
        # drops below the review gate instead of silently binding the wrong team.
        team_id, team_name, confidence, candidates, reason = _resolve_row_team(m, players_detail)
        rows.append({
            "row_id": m["row_id"],
            # Display the team name when read, else the tag we inferred from the players (so the row is
            # never blank), else nothing (the FE shows "team name not read" + the players it saw).
            "raw_name": raw_name or derived_tag,
            "players_read": players_read,
            "players_detail": players_detail,
            "placement": placement,
            "kills": kills,
            "matched_team_id": team_id,
            "matched_name": team_name,
            "confidence": confidence,
            # Why this row did not auto-bind (one of afc_ocr.services.matching's REASON_* strings),
            # "" when it did. The review table turns it into a one-line explanation next to the picker.
            "unmatched_reason": reason,
            "top_candidates": candidates,
            "is_unmatched": team_id is None,
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
                # Why this row did not auto-bind (afc_ocr.services.matching REASON_*), "" when it did.
                "unmatched_reason": m.get("unmatched_reason", ""),
                "top_candidates": m["top_candidates"],
                "is_unmatched": m["matched_user_id"] is None,
            })
    return rows


# ── match-log file rows (the "upload result file" option) ────────────────────────────────────────
def build_rows_from_match_log(parsed_teams, teams, players_pool):
    """Turn utils.match_log.parse_team_match_log output into the SAME review rows the OCR flows
    produce, so the FE reuses one review table + one apply pipeline for screenshots AND files.

    The file format carries each player's UID, so players match the platform EXACTLY by User.uid
    (confidence 1.0) and only fall back to the fuzzy match_name when the UID is unknown. Team
    matching is identical to the OCR path (match_team_name over the real+ghost pool). Consumed by
    afc_leaderboard.views.results_file_extract; row shape documented on build_team_ocr_rows."""
    from afc_auth.models import User

    # One query resolves every UID in the file to a platform user (incl. their current team,
    # which the review panel shows next to each suggestion).
    all_uids = [p["uid"] for t in parsed_teams for p in t.get("players", []) if p.get("uid")]
    # NOTE: there is no User.team FK - membership lives on afc_team.TeamMembers (member FK,
    # unique_member_one_team), so the current team comes via the reverse `teammembers` join
    # (LEFT JOIN: NULL for free agents, one row per user thanks to the unique constraint).
    uid_to_user = {
        u["uid"]: u
        for u in User.objects.filter(uid__in=all_uids).values(
            "uid", "user_id", "username", "teammembers__team_id", "teammembers__team__team_name",
        )
    }

    rows = []
    for entry in parsed_teams:
        players_read = [p["name"] for p in entry.get("players", []) if p.get("name")]
        players_detail = []
        for p in entry.get("players", []):
            pname = (p.get("name") or "").strip()
            if not pname:
                continue
            hit = uid_to_user.get(p.get("uid"))
            if hit:
                # UID hit: exact identity, no fuzzying needed.
                players_detail.append({
                    "name": pname,
                    "kills": int(p.get("kills", 0) or 0),
                    "matched_user_id": hit["user_id"],
                    "matched_username": hit["username"],
                    "matched_team_id": hit["teammembers__team_id"],
                    "matched_team_name": hit["teammembers__team__team_name"],
                    "confidence": 1.0,
                    "unmatched_reason": "",
                    "top_candidates": [
                        {
                            "user_id": hit["user_id"],
                            "username": hit["username"],
                            "team_name": hit["teammembers__team__team_name"],
                            "confidence": 1.0,
                        }
                    ],
                    "is_unmatched": False,
                })
                continue
            pm = match_name(pname, players_pool)
            players_detail.append({
                "name": pname,
                "kills": int(p.get("kills", 0) or 0),
                "matched_user_id": pm["matched_user_id"],
                "matched_username": pm["matched_username"],
                "matched_team_id": pm.get("matched_team_id"),
                "matched_team_name": pm.get("matched_team_name"),
                "confidence": pm["confidence"],
                "unmatched_reason": pm.get("unmatched_reason", ""),
                "top_candidates": pm["top_candidates"],
                "is_unmatched": pm["matched_user_id"] is None,
            })

        raw_name = (entry.get("team_name") or "").strip()
        derived_tag = derive_team_tag(players_read)
        # Same corroborated team match as the screenshot path (see build_team_ocr_rows). The
        # membership signal is STRONGER here: a UID hit is a certain identity, so "this player is on
        # that team" is a fact rather than an inference.
        m = match_team_name(
            raw_name or derived_tag, teams,
            player_team_ids=trusted_player_team_ids(players_detail),
            players_tag=derived_tag if raw_name else "",
        )
        # Same player-plurality fold as the screenshot path. It matters MORE here: every UID hit is a
        # certain identity, so the players' team is near-proof of which team the row is.
        team_id, team_name, confidence, candidates, reason = _resolve_row_team(m, players_detail)
        rows.append({
            "row_id": m["row_id"],
            "raw_name": raw_name or derived_tag,
            "players_read": players_read,
            "players_detail": players_detail,
            "placement": int(entry.get("placement", 0) or 0),
            # The file states the team's KillScore directly; fall back to the players' sum.
            "kills": int(entry.get("team_kills") or 0)
            or sum(int(p.get("kills", 0) or 0) for p in entry.get("players", [])),
            "matched_team_id": team_id,
            "matched_name": team_name,
            "confidence": confidence,
            "unmatched_reason": reason,
            "top_candidates": candidates,
            "is_unmatched": team_id is None,
        })
    return rows


# ── multi-image merge ─────────────────────────────────────────────────────────────────────────────
def _norm(s):
    """Loose key for dedupe: the SHARED normalizer (so 'V-ENT' and 'vent' collide, and so do two
    reads of the same name that differ only in decoration - 'NP.BLOOD彡' vs 'NP. BLOOD' - which the
    old `[^a-z0-9]`-only version treated as different placements and duplicated into the merge)."""
    return normalize_search_text(s)


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

        # ── read the images CONCURRENTLY ──
        # A map's screenshots used to be read one after another, so a 3-screenshot map paid
        # 3 full extractions in series (each = local-student attempt + a ~10-25s Gemini HTTP
        # call) and a batch took the owner 5-10 minutes on prod. The extraction is I/O-bound
        # on Gemini, so we overlap the reads in threads; the shared local student stays
        # serialized behind extract._STUDENT_LOCK. The threads touch NO Django ORM (file
        # read + HTTP only) - all DB writes happen back on this thread, in image order, so
        # merge_placements sees the same ordering the sequential loop produced. One image
        # failing raises out of ex.map and fails the whole job, exactly as before.
        def _read_one(img):
            data = img.image.read()           # FieldFile.read() opens lazily
            try:
                img.image.close()
            except Exception:
                pass
            started = time.monotonic()
            raw, eng = extract.extract_rows(
                data, _guess_mime(img.image.name), event_type, prompt_kind=prompt_kind,
            )
            # Per-image wall time, persisted in raw_output so prod slowness is diagnosable
            # from the DB ("which engine, how long, per screenshot") without box access.
            if isinstance(raw, dict):
                raw.setdefault("_elapsed_ms", int((time.monotonic() - started) * 1000))
            return raw, eng

        with ThreadPoolExecutor(max_workers=min(4, len(images))) as ex:
            outputs = list(ex.map(_read_one, images))  # ex.map preserves image order

        placement_lists = []
        engine = ""
        for img, (raw, eng) in zip(images, outputs):
            img.raw_output = raw
            img.save(update_fields=["raw_output"])
            placement_lists.append(raw.get("placements", []) or [])
            engine = eng or engine

        merged = merge_placements(placement_lists, is_team)
        if is_team:
            # Team pool INCLUDES ghost teams (suggest the ghost an earlier map created instead of
            # duplicating it); players_pool ALSO matches each read player against the platform user
            # pool so the review table can suggest per-player matches (see build_team_ocr_rows).
            rows = build_team_ocr_rows(
                {"placements": merged},
                all_platform_teams_with_ghosts(),
                players_pool=all_platform_players(),
            )
        else:
            rows = build_solo_ocr_rows({"placements": merged}, all_platform_players())

        job.rows = rows
        job.engine = engine
        job.status = "done"
        job.error = ""
        job.save(update_fields=["rows", "engine", "status", "error", "updated_at"])
    except Exception as e:  # noqa: BLE001 - a failed read must mark the job, never crash the worker
        logger.exception("afc_leaderboard.ocr.process_job failed for job %s", getattr(job, "id", "?"))
        job.status = "failed"
        job.error = str(e)[:2000]
        job.save(update_fields=["status", "error", "updated_at"])
