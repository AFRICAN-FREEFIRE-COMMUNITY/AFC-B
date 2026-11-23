# ── GAME FILE ARCHIVE: the store (owner 2026-11-18, inbox #603 #606 #610 #611 "save everything") ─────────────
# Every file the Free Fire client writes for a map is kept on our server, gzipped, once per content, filed under
# its event and linked to the site map(s) it belongs to, so stats nobody computed at the time can be worked out
# later from the same bytes. Design: tasks/game-archive-design-2026-11-18.md. Plan: tasks/game-archive-plan-
# 2026-11-18.md (Task 1).
#
# THE ALLOWLIST (one table, the same one AFC Capture 1.7.0 carries in afc_capture/archive.py): a path relative to
# the game's "Free Fire_64_Data" folder, or a bare file name. Anything else is refused, above all the observer
# account's GuestCacheProduction.json (its UID and password in plain text) and anything under StreamingAssets.
#
# CALLERS:
#   views_game_archive.archive_send       AFC Capture 1.7.0 sends one gzipped file per request (source "capture")
#   views.upload_team_match_result        keeps the result file it just scored (source "result_upload"), then
#                                         link_waiting() files the replays and logs that arrived before it
#   views_game_files.game_files_read      the Game files box keeps each file it reads (source "upload_box")
#   management command archive_result_logs  copies the MatchResultLog files kept since July (source "backfill")
#   tasks.build_archive_zip               reads the stored files back into a zip
#
# Nothing here ever touches scoring, standings or the live board: callers on the result path wrap every call in
# their own try/except, after the result is saved.

import gzip
import hashlib
import logging
import os
import re
import shutil
import time
import zipfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q

from . import game_files as gf
from .models import GameFile, MatchResultLog

MB = 1024 * 1024
GB = 1024 ** 3

# Uncompressed size caps per kind (design 8.3). Everything not named here: 1 MB.
KIND_CAPS = {"debugger": 250 * MB, "replay_bin": 64 * MB, "replay_info": 5 * MB}
DEFAULT_CAP = 1 * MB
KINDS = tuple(k for k, _ in GameFile.KIND_CHOICES)

# Space guard (design 8.7): refuse new archive files under this much free disk; warn under the second figure.
REFUSE_UNDER_GB = getattr(settings, "GAME_ARCHIVE_REFUSE_UNDER_GB", 15)
WARN_UNDER_GB = getattr(settings, "GAME_ARCHIVE_WARN_UNDER_GB", 40)

# (kind, folders the file may sit in, file name rule). The first group of the rule, when there is one, is the
# game's own match id. "" as a folder = the file named on its own (the Game files box sends bare names).
_RULES = (
    ("result", ("",), re.compile(r"^MatchResult_(\d+)(?:_[^/]*)?\.log$", re.I)),
    ("debugger", ("", "debugger"), re.compile(r"^debugger[^/]*\.log$", re.I)),
    ("replay_info", ("", "replays"), re.compile(r"^ReplayInfo_(\d+)_[^/]+\.json$", re.I)),
    ("replay_bin", ("", "replays"), re.compile(r"^ReplayInfo_(\d+)_[^/]+\.bin$", re.I)),
    ("safezone", ("",), re.compile(r"^SafeZone_(\d+)_[^/]+\.log$", re.I)),
    ("heatmap", ("", "heatmap/*"), re.compile(r"^hud_click_[^/]+_p\d+\.bin$", re.I)),
    ("marker", ("",), re.compile(r"^MatchId_(\d+)_[^/]+\.log$", re.I)),
    ("marker", ("",), re.compile(r"^RoomID_\d+_[^/]+\.log$", re.I)),
    # Written in custom rooms (UIModelCustomRoom.LogBatchMatchId; tasks/client-folder-names-2026-11-18.md).
    ("marker", ("",), re.compile(r"^BatchMatchID_(\d+)_[^/]+\.log$", re.I)),
)
_HEATMAP_FOLDER = re.compile(r"^heatmap/\d+$", re.I)


class ArchiveRefused(Exception):
    """A file the archive will not keep: `code` for the screen (R35), `status` for the HTTP answer."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _split(rel_path):
    """(folder, base name) of a relative path, either slash, or None when the path tries to leave its folder."""
    parts = [p for p in str(rel_path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts[:-1]), parts[-1]


def classify(rel_path):
    """(kind, game_match_id) for a file the archive keeps, else None. game_match_id is "" when the name has none."""
    split = _split(rel_path)
    if split is None:
        return None
    folder, base = split
    low = folder.lower()
    for kind, folders, rule in _RULES:
        m = rule.match(base)
        if not m:
            continue
        if low in folders or ("heatmap/*" in folders and _HEATMAP_FOLDER.match(folder)):
            return kind, (m.group(1) if m.groups() else "")
        return None
    return None


def space_state():
    """How much room the media disk has left, and whether the archive is refusing or warning."""
    free_gb = shutil.disk_usage(settings.MEDIA_ROOT).free / GB
    return {"free_gb": round(free_gb, 1), "refusing": free_gb < REFUSE_UNDER_GB, "warning": free_gb < WARN_UNDER_GB}


def _debugger_ids(data):
    """Every match id the log saw end, in order, once each (game_files.read_debugger does the reading)."""
    ids = []
    for piece in gf.read_debugger(data.decode("utf-8", "replace")):
        mid = piece.get("match_id")
        if mid and str(mid) not in ids:
            ids.append(str(mid))
    return ids


def _starts_with(gfile, data):
    """True when the stored file's bytes begin with `data` (an earlier, shorter copy of the same log)."""
    with gfile.file.open("rb") as fh, gzip.GzipFile(fileobj=fh) as gz:
        return gz.read(len(data)) == data


def _maps_for(event, game_ids):
    """The site maps of this event whose kept result file names one of these game ids."""
    q = Q()
    for gid in game_ids:
        q |= Q(file_name__iexact=f"MatchResult_{gid}.log") | Q(file_name__istartswith=f"MatchResult_{gid}_")
    if not q:
        return []
    return list(MatchResultLog.objects.filter(match__group__stage__event=event).filter(q)
                .values_list("match_id", flat=True).distinct())


def link_file(gfile, match=None):
    """File `gfile` under its map(s): the one given, plus every map of its event whose result file names its id
    (for a debugger log, every id it holds). A file nothing claims stays "not matched"."""
    if match is not None:
        gfile.matches.add(match)
    ids = gfile.game_match_ids if gfile.kind == "debugger" else ([gfile.game_match_id] if gfile.game_match_id else [])
    found = _maps_for(gfile.event, ids)
    if found:
        gfile.matches.add(*found)


def link_waiting(match, game_match_id):
    """A result for game `game_match_id` was just scored on `match`: file every row of that event which waited for
    it (a replay, safe zone or log that arrived first). Returns how many rows were newly linked."""
    gid = str(game_match_id or "")
    if not gid:
        return 0
    event_id = match.group.stage.event_id
    rows = (GameFile.objects.filter(event_id=event_id)
            .filter(Q(game_match_id=gid) | Q(kind="debugger", game_match_ids__contains=[gid]))
            .exclude(matches=match))
    count = 0
    for row in rows:
        row.matches.add(match)
        count += 1
    return count


def store_bytes(event, rel_path, data, *, source, user=None, stage=None, group=None, match=None, kind=None):
    """Keep `data` (the file's ORIGINAL bytes) for `event`. Returns (GameFile or None, result), result one of:
      stored       a new row
      duplicate    the same bytes were already kept for this event (the existing row comes back)
      replaced     a longer copy of a debugger log replaced the shorter one it starts with
      kept_longer  a shorter copy of a debugger log already kept in full (the longer row comes back)
    `kind` lets a caller that already proved the kind from the content (the Game files box reads a debugger log
    whatever its name) skip the name rule. Raises ArchiveRefused for a name off the allowlist, a file over its
    kind's cap, or a disk under the space guard."""
    named = classify(rel_path)
    if kind is None:
        if named is None:
            raise ArchiveRefused("archive_name_not_allowed", "That file is not one the archive keeps.", 400)
        kind, gid = named
    else:
        gid = named[1] if named else ""
    if len(data) > KIND_CAPS.get(kind, DEFAULT_CAP):
        raise ArchiveRefused("archive_file_too_large", "That file is larger than the archive keeps for its kind.", 413)
    if space_state()["refusing"]:
        raise ArchiveRefused("archive_space_low", "The archive is out of space for now. Results still work.", 503)

    base = _split(rel_path)[1] if _split(rel_path) else "file"
    sha = hashlib.sha256(data).hexdigest()
    same = GameFile.objects.filter(event=event, sha256=sha).first()
    if same is not None:
        link_file(same, match)
        return same, "duplicate"

    replaced = []
    if kind == "debugger":
        for old in GameFile.objects.filter(event=event, kind="debugger", original_name=base):
            if old.size_bytes < len(data) and hashlib.sha256(data[:old.size_bytes]).hexdigest() == old.sha256:
                replaced.append(old)
            elif old.size_bytes > len(data) and _starts_with(old, data):
                link_file(old, match)
                return old, "kept_longer"

    row = GameFile(
        event=event, stage=stage, group=group, kind=kind, game_match_id=gid,
        game_match_ids=_debugger_ids(data) if kind == "debugger" else [],
        original_name=base[:255], size_bytes=len(data), sha256=sha, source=source,
        uploaded_by=user if getattr(user, "pk", None) else None,
    )
    try:
        with transaction.atomic():
            row.file.save(f"{sha}.gz", ContentFile(gzip.compress(data, compresslevel=6)), save=False)
            row.save()
    except IntegrityError:
        # Two senders raced with the same bytes: the other one won, which is the same outcome.
        if row.file and row.file.name:
            row.file.delete(save=False)
        same = GameFile.objects.filter(event=event, sha256=sha).first()
        if same is None:
            raise
        link_file(same, match)
        return same, "duplicate"

    for old in replaced:
        keep = list(old.matches.values_list("pk", flat=True))
        if keep:
            row.matches.add(*keep)
        if old.file and old.file.name and os.path.exists(old.file.path):
            old.file.delete(save=False)
        old.delete()
    link_file(row, match)
    return row, ("replaced" if replaced else "stored")


# ── the two existing doors (never raise: the archive must not fail what they already do) ──────────────────
_log = logging.getLogger("afc_tournament_and_scrims")


def archive_scored_result(match, name, data, user=None):
    """Keep the result file views.upload_team_match_result / upload_solo_match_result just scored on `match`,
    then file every replay, safe zone or log of that game that arrived before it. Runs in its own savepoint after
    the result is saved; any failure is logged and swallowed, so a result never fails because of the archive."""
    try:
        with transaction.atomic():
            named = classify(name)
            gid = named[1] if named and named[0] == "result" else ""
            store_bytes(match.group.stage.event, name or f"MatchResult_{match.match_id}.log", data,
                        source="result_upload", user=user, match=match, kind="result")
            if gid:
                link_waiting(match, gid)
    except ArchiveRefused as refused:
        _log.warning("Archive kept no copy of the result for match %s: %s", match.match_id, refused.code)
    except Exception:
        _log.exception("Archive store failed for the result of match %s", match.match_id)


# The Game files box's own kinds -> the archive's.
_BOX_KINDS = {"debugger": "debugger", "result": "result", "replay": "replay_bin"}


def archive_box_file(event, name, data, box_kind, user=None):
    """Keep a file the Game files box just read (views_game_files.game_files_read). The box has already proved
    the kind from the content, so an odd file name is still kept. Never raises."""
    try:
        with transaction.atomic():
            store_bytes(event, name, data, source="upload_box", user=user, kind=_BOX_KINDS.get(box_kind, box_kind))
    except ArchiveRefused as refused:
        _log.warning("Archive kept no copy of box file %s for event %s: %s", name, event.pk, refused.code)
    except Exception:
        _log.exception("Archive store failed for box file %s of event %s", name, event.pk)


# ── zips (design 8.6): any level, built in the background, kept 24 hours ─────────────────────────────────────
# State lives in the cache under zip_key(zip_id) for ZIP_TTL: {event_id, scope, id, file_ids (frozen when asked),
# status: queued|building|ready|failed, size_bytes, created_at, user_id}. The file is
# MEDIA_ROOT/game_archive/_zips/<zip_id>.zip; tasks.purge_archive_zips removes it after a day.
ZIP_TTL = 24 * 60 * 60
ZIP_SCOPES = ("event", "stage", "group", "match", "unmatched")
_UNSAFE = re.compile(r'[\/:*?"<>|\x00-\x1f]+')


def zip_key(zip_id):
    return f"gamearchive:zip:{zip_id}"


def zip_folder():
    return os.path.join(settings.MEDIA_ROOT, "game_archive", "_zips")


def zip_path(zip_id):
    return os.path.join(zip_folder(), f"{zip_id}.zip")


def _segment(text, fallback):
    """A folder or file name that is safe inside a zip on every system."""
    clean = _UNSAFE.sub("-", str(text or "")).strip(" .")
    return clean[:120] or fallback


def scope_maps(event, scope, scope_id):
    """The site maps a scope covers (None for "event" = every map; [] for "unmatched")."""
    from .models import Match
    if scope == "event":
        return None
    if scope == "unmatched":
        return []
    field = {"stage": "group__stage_id", "group": "group_id", "match": "match_id"}[scope]
    return list(Match.objects.filter(group__stage__event=event, **{field: scope_id}).values_list("match_id", flat=True))


def scope_file_ids(event, scope, scope_id):
    """The ids of the files a zip of this scope holds, as they are right now."""
    qs = GameFile.objects.filter(event=event)
    if scope == "unmatched":
        qs = qs.filter(matches__isnull=True)
    elif scope != "event":
        qs = qs.filter(matches__in=scope_maps(event, scope, scope_id))
    return list(qs.distinct().order_by("pk").values_list("pk", flat=True))


def build_zip(zip_id):
    """Write the zip a request froze (see the state above). Each file is unpacked to its original name under
    Stage/Group/Map N/ for every map of the scope it belongs to, or not-matched/ when it has none."""
    from django.core.cache import cache
    state = cache.get(zip_key(zip_id))
    if not state:
        return None
    state["status"] = "building"
    cache.set(zip_key(zip_id), state, ZIP_TTL)
    try:
        allowed = scope_maps(state["event_id"], state["scope"], state.get("id")) if state["scope"] not in (
            "event", "unmatched") else None
        os.makedirs(zip_folder(), exist_ok=True)
        part = zip_path(zip_id) + ".part"
        used = set()
        rows = (GameFile.objects.filter(pk__in=state["file_ids"]).order_by("pk")
                .prefetch_related("matches__group__stage"))
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for row in rows:
                maps = [m for m in row.matches.all() if allowed is None or m.match_id in allowed]
                folders = [f"{_segment(m.group.stage.stage_name, 'Stage')}/{_segment(m.group.group_name, 'Group')}"
                           f"/Map {m.match_number}" for m in maps] or ["not-matched"]
                for folder in folders:
                    name = f"{folder}/{_segment(row.original_name, 'file')}"
                    stem, n = name, 2
                    while name in used:
                        root, ext = os.path.splitext(stem)
                        name, n = f"{root} ({n}){ext}", n + 1
                    used.add(name)
                    with row.file.open("rb") as fh, gzip.GzipFile(fileobj=fh) as gz, z.open(name, "w") as out:
                        shutil.copyfileobj(gz, out, 1024 * 1024)
        os.replace(part, zip_path(zip_id))
        state.update(status="ready", size_bytes=os.path.getsize(zip_path(zip_id)))
    except Exception:
        _log.exception("Archive zip %s failed", zip_id)
        state["status"] = "failed"
    cache.set(zip_key(zip_id), state, ZIP_TTL)
    return state["status"]


def purge_zips(max_age=ZIP_TTL):
    """Delete zips (and half-written parts) older than a day. Returns how many files went."""
    folder, gone, cutoff = zip_folder(), 0, time.time() - max_age
    if not os.path.isdir(folder):
        return 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            gone += 1
    return gone
