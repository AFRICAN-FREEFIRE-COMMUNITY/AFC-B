import json
from collections import defaultdict

from django.db import transaction, models as dm

# Single source of truth for the per-match point formula + placement-points normalizer.
# Previously OCR carried its own DEFAULT_PLACEMENT + _normalize_pp copies, which is exactly
# how OCR scoring drifted from manual entry: both now go through scoring.* (see scoring.py).
from afc_tournament_and_scrims import scoring


def _get_lb_for_match(match):
    from afc_tournament_and_scrims.models import Leaderboard
    if match.leaderboard_id:
        return match.leaderboard
    if match.group_id:
        return Leaderboard.objects.filter(
            event=match.group.stage.event,
            stage=match.group.stage,
            group=match.group,
        ).first()
    return None


def _get_event_for_match(match):
    if match.leaderboard_id:
        return match.leaderboard.event
    if match.group_id:
        return match.group.stage.event
    return None


def commit_team_result(match, final_rows: list):
    """
    Write team match stats from OCR final_rows.
    Groups rows by placement: each placement group = one team.

    RINGER FLAGGING (owner 2026-07-13): OCR screenshots credit a player to a team by NAME only
    (no UID). When an OCR'd player's OWN registered team differs from the team their placement block
    is credited to, they are a "ringer" (played for a team they are not rostered on), the SAME
    concern the .log FILE upload records as MatchKillFlag (upload_team_match_result). To make the
    flagging feature "work with the OCR upload also", a ringer row now becomes a MatchKillFlag
    (reason name_matched_other_team, uid = synthetic "ocr:<user_id>" since OCR has no real UID,
    count_kills=None so it FOLLOWS the event count_flagged_kills toggle exactly like the .log flags)
    INSTEAD of a TournamentPlayerMatchStats, and their kills join the team total only while that flag
    currently counts. Writing a flag instead of a player-stat is also what keeps
    _recompute_team_kills_for_event (rostered player-stat kills + counted flag kills) from double
    counting them. The flag then surfaces in the SAME FlaggedKillsPanel + event toggle the .log path
    uses: one review surface for ringers from either upload method. Idempotent: a re-commit clears
    this match's flags first and RESTORES any prior per-flag count decision (mirrors the .log
    snapshot/restore), so an admin's approval survives a re-upload.
    """
    from afc_tournament_and_scrims.models import (
        TournamentTeamMatchStats, TournamentPlayerMatchStats, UnmatchedTeamBlock, MatchKillFlag,
    )

    lb = _get_lb_for_match(match)
    event = _get_event_for_match(match)
    # Event-wide "count flagged kills" default (True unless an admin turned it off). New ringer flags
    # are created count_kills=None, so they follow this until overridden in the FlaggedKillsPanel; the
    # stored team total below is computed to match what _recompute_team_kills_for_event would produce.
    count_flagged_default = bool(event.count_flagged_kills) if event else True
    # #14 (owner 2026-07-06 "never silently drop, always flag/notify"): OCR placement groups whose team
    # didn't match a registered team are recorded here as UnmatchedTeamBlock (the SAME flagged-teams
    # resolver the .log upload uses) and returned to the caller, instead of being silently skipped. This
    # is what previously made an OCR-uploaded map quietly lose its winner (booyah undercount).
    unmatched_blocks = []

    # `scoring` is now the imported module; use a distinct local name for the match's
    # per-match scoring config so it doesn't shadow scoring.compute_* / normalize_*.
    scoring_cfg = match.scoring_settings or {}
    if isinstance(scoring_cfg, str):
        scoring_cfg = json.loads(scoring_cfg)

    lb_pp = lb.placement_points if lb else {}
    placement_points = scoring.normalize_placement_points(scoring_cfg.get("placement_points") or lb_pp)
    kill_point         = float(scoring_cfg.get("kill_point", lb.kill_point if lb else 1.0))
    points_per_assist  = float(scoring_cfg.get("points_per_assist", 0))
    points_per_damage  = float(scoring_cfg.get("points_per_1000_damage", 0))

    # Group rows by placement
    groups: dict = defaultdict(list)
    for row in final_rows:
        groups[int(row.get("placement", 0))].append(row)

    with transaction.atomic():
        TournamentTeamMatchStats.objects.filter(match=match).delete()
        # Rebuild this match's unmatched-block records from scratch too, so a re-commit doesn't stack
        # duplicates (mirrors deleting the team stats above).
        UnmatchedTeamBlock.objects.filter(match=match).delete()

        # Ringer flags are re-derived every commit. Snapshot the admin's prior per-flag decision
        # (keyed by team + synthetic uid) BEFORE clearing, then restore it on the rebuilt rows below,
        # so a re-commit never silently wipes an approval (mirrors the .log path's approval restore).
        prior_flag_decision = {
            (f.tournament_team_id, f.uid): f.count_kills
            for f in MatchKillFlag.objects.filter(match=match)
        }
        MatchKillFlag.objects.filter(match=match).delete()
        flag_rows = []

        for placement, rows in sorted(groups.items()):
            t_team_id = rows[0].get("matched_team_id")
            if not t_team_id:
                # #14: record + surface instead of silently dropping this team block.
                _blk_name = (rows[0].get("team_name") or rows[0].get("ocr_team_name")
                             or rows[0].get("name") or f"Unmatched (placement {placement})")
                _blk_kills = sum(int(r.get("kills", 0)) for r in rows)
                UnmatchedTeamBlock.objects.create(
                    match=match, team_name=_blk_name, placement=placement,
                    kills=_blk_kills, attributed_team_id=None,
                )
                unmatched_blocks.append({"team_name": _blk_name, "placement": placement, "kills": _blk_kills})
                continue

            # Split the block into ROSTERED players and RINGERS. A ringer = a resolved player whose
            # OWN registered team (matched_team_id) differs from the team this block is credited to
            # (t_team_id): credited under a team they are not rostered on. Rows with no matched_team_id
            # (player has no registered team this event) count as rostered here, only a clear
            # cross-team mismatch is flagged, mirroring detect_team_mismatches / the .log rule.
            rostered_rows, ringer_rows = [], []
            for r in rows:
                mtid = r.get("matched_team_id")
                if r.get("matched_user_id") and mtid and mtid != t_team_id:
                    ringer_rows.append(r)
                else:
                    rostered_rows.append(r)

            # Resolve each ringer's effective count decision up front: its RESTORED per-flag override
            # (from a prior commit) if the admin set one, else None = follow the event default. We use
            # this both to build the flag rows AND to compute the stored team total, so the stored value
            # stays consistent with _recompute_team_kills_for_event even right after a re-commit that
            # restored a "do not count" decision (otherwise the total would re-inflate until the next
            # recompute). ring = (row, synthetic_uid, user_id, count_kills, effective_bool).
            ringers = []
            for r in ringer_rows:
                uid_ = r.get("matched_user_id")
                syn_uid = f"ocr:{uid_}"
                decision = prior_flag_decision.get((t_team_id, syn_uid))     # True / False / None
                effective = decision if decision is not None else count_flagged_default
                ringers.append((r, syn_uid, uid_, decision, effective))

            # Team totals mirror the .log path: rostered players contribute kills + damage + assists;
            # a ringer contributes ONLY (flagged) kills, and only while its flag currently counts.
            counted_ringer_kills = sum(int(r.get("kills", 0)) for (r, _u, _i, _d, eff) in ringers if eff)
            team_kills   = sum(int(r.get("kills", 0))   for r in rostered_rows) + counted_ringer_kills
            team_damage  = sum(int(r.get("damage", 0))  for r in rostered_rows)
            team_assists = sum(int(r.get("assists", 0)) for r in rostered_rows)
            bonus_pts    = int(rows[0].get("bonus_points", 0))
            penalty_pts  = int(rows[0].get("penalty_points", 0))

            # Shared team formula (same one manual entry uses). NOTE: the old code stored
            # int(round(total)); scoring uses int(total). Identical for integer point settings;
            # this keeps OCR in lockstep with the canonical formula (see report).
            pts = scoring.compute_team_points(
                placement_points=placement_points, kill_point=kill_point,
                points_per_assist=points_per_assist, points_per_1000_damage=points_per_damage,
                placement=placement, kills=team_kills, damage=team_damage, assists=team_assists,
                bonus=bonus_pts, penalty=penalty_pts, played=True,
            )

            team_stat = TournamentTeamMatchStats.objects.create(
                match=match,
                tournament_team_id=t_team_id,
                placement=placement,
                kills=team_kills,
                damage=team_damage,
                assists=team_assists,
                placement_points=pts["placement_points"],
                kill_points=pts["kill_points"],
                total_points=pts["total_points"],
                played=True,
                bonus_points=bonus_pts,
                penalty_points=penalty_pts,
            )

            # Rostered players get a per-player stat row (as before).
            for row in rostered_rows:
                user_id = row.get("matched_user_id")
                if not user_id:
                    continue
                TournamentPlayerMatchStats.objects.create(
                    team_stats=team_stat,
                    player_id=user_id,
                    kills=int(row.get("kills", 0)),
                    damage=int(row.get("damage", 0)),
                    assists=int(row.get("assists", 0)),
                    played=True,
                )

            # Ringers become MatchKillFlag rows (NOT player-stats) so they show in the flagged-players
            # panel + feed the recompute. uid = "ocr:<user_id>" is a stable synthetic id (OCR has no
            # real UID) that keeps (match, team, uid) unique + idempotent across re-commits. count_kills
            # is the restored prior decision (else None = follow event default), resolved above.
            for row, syn_uid, user_id, decision, _eff in ringers:
                flag_rows.append(MatchKillFlag(
                    match=match,
                    tournament_team_id=t_team_id,
                    uid=syn_uid,
                    name=(row.get("raw_name") or row.get("name") or "")[:120],
                    kills=int(row.get("kills", 0)),
                    reason="name_matched_other_team",
                    registered_user_id=user_id,
                    count_kills=decision,
                ))

        if flag_rows:
            # ignore_conflicts: a player flagged as a ringer twice in the same block (duplicate OCR
            # row) collapses to one flag by the (match, team, uid) unique key, exactly like the .log path.
            MatchKillFlag.objects.bulk_create(flag_rows, ignore_conflicts=True)

        match.result_inputted = True
        match.upload_method = "image_upload"
        match.save(update_fields=["result_inputted", "upload_method"])

        if lb and not match.leaderboard_id:
            match.leaderboard = lb
            match.save(update_fields=["leaderboard"])

    # (lb, unmatched_blocks): the caller surfaces unmatched_blocks to the reviewer (#14) so an
    # OCR-uploaded map never silently loses a team; [] when every block matched.
    return lb, unmatched_blocks


def commit_solo_result(match, final_rows: list):
    """
    Write solo match stats from OCR final_rows.

    SKIPPED-ROW SURFACING (owner 2026-07-14, A3): a solo row whose matched player has no
    RegisteredCompetitors entry for this event (or no matched user at all) used to be silently
    `continue`d past, so a commit could quietly drop players with no record and no signal to the
    reviewer. This is the solo-path parallel of the team path's silent-drop bug that #14 fixed
    (commit_team_result now returns unmatched_blocks). We now COLLECT those dropped rows and
    RETURN them as `skipped`, mirroring that handling.

    HOW IT CONNECTS: the caller is afc_ocr/views.py::commit_ocr_session (which imports this function
    lazily inside its body). It unpacks (lb, skipped) and surfaces `skipped` in the 200 response under a DISTINCT
    "skipped_rows" key, kept separate from the team path's "missing_teams" so the frontend
    OCRReviewTable.handleCommit can label them correctly ("N players were not on the event roster
    and were skipped") instead of one generic message that would read wrong for players vs teams.

    Returns (lb, skipped):
      lb      - the Leaderboard the stats were written to (unchanged from before).
      skipped - list of {"raw_name": str, "matched_user_id": int|None, "placement": int,
                "kills": int, "reason": "not_registered"|"no_user"}; [] when every row committed.
                reason "no_user" is defensive: commit_ocr_session validates every row has a
                matched_user_id before committing, so it is only reachable via a direct / future
                service caller (kept for completeness).
    """
    from afc_tournament_and_scrims.models import SoloPlayerMatchStats, RegisteredCompetitors
    # A7 _safe_int: the shared "coerce a Gemini-ish cell to int, never crash on null/blank/garbage"
    # helper lives in afc_ocr/views.py. Imported lazily here (this module already defers all its
    # imports) so the service layer never forms a top-level import cycle with views.py, which
    # itself imports commit_solo_result lazily inside commit_ocr_session.
    from afc_ocr.views import _safe_int

    lb = _get_lb_for_match(match)
    if not lb:
        raise ValueError("No leaderboard found for this match.")

    event = _get_event_for_match(match)
    placement_points = scoring.normalize_placement_points(lb.placement_points)
    kill_point = lb.kill_point

    # Collected as we walk final_rows and returned to the caller so dropped players are surfaced,
    # never silently lost (parallel to commit_team_result's unmatched_blocks).
    skipped = []

    with transaction.atomic():
        SoloPlayerMatchStats.objects.filter(match=match).delete()

        for row in final_rows:
            user_id   = row.get("matched_user_id")
            placement = _safe_int(row.get("placement"))
            kills     = _safe_int(row.get("kills"))

            # No resolved player: record + skip. Defensive branch (see docstring "no_user"),
            # only hit by direct callers since the view validates matched_user_id upstream.
            if not user_id:
                skipped.append({
                    "raw_name": row.get("raw_name", ""),
                    "matched_user_id": None,
                    "placement": placement,
                    "kills": kills,
                    "reason": "no_user",
                })
                continue

            # Matched a real user who is NOT on this event's roster: record + skip so the reviewer
            # sees "not registered" instead of the row vanishing (this was the silent `continue`).
            competitor = RegisteredCompetitors.objects.filter(
                user_id=user_id, event=event
            ).first()
            if not competitor:
                skipped.append({
                    "raw_name": row.get("raw_name", ""),
                    "matched_user_id": user_id,
                    "placement": placement,
                    "kills": kills,
                    "reason": "not_registered",
                })
                continue

            # Shared solo formula. NOTE: the old code stored kill_points/total as floats
            # (kills * kill_point); scoring returns int kill points. Identical at kill_point=1.0.
            pts = scoring.compute_solo_points(
                placement_points=placement_points, kill_point=kill_point,
                placement=placement, kills=kills, played=True,
            )

            SoloPlayerMatchStats.objects.create(
                match=match,
                competitor=competitor,
                placement=placement,
                kills=kills,
                placement_points=pts["placement_points"],
                kill_points=pts["kill_points"],
                total_points=pts["total_points"],
            )

        match.result_inputted = True
        match.upload_method = "image_upload"
        match.save(update_fields=["result_inputted", "upload_method"])

        if lb and not match.leaderboard_id:
            match.leaderboard = lb
            match.save(update_fields=["leaderboard"])

    # (lb, skipped): caller surfaces `skipped` under the response "skipped_rows" key (distinct from
    # the team path "missing_teams") so the FE can warn the admin without blocking the commit.
    return lb, skipped


def save_name_corrections(final_rows: list, original_rows: list):
    """
    For every row where the admin changed the matched player, save to OCRNameAlias.
    """
    from afc_ocr.models import OCRNameAlias

    original_map = {r["row_id"]: r for r in original_rows}

    for row in final_rows:
        orig = original_map.get(row.get("row_id"))
        if not orig:
            continue

        if row.get("matched_user_id") and row["matched_user_id"] != orig.get("matched_user_id"):
            raw_name = row.get("raw_name", "")
            if not raw_name:
                continue

            alias, created = OCRNameAlias.objects.get_or_create(
                raw_name=raw_name,
                defaults={"user_id": row["matched_user_id"]},
            )
            if not created:
                alias.user_id = row["matched_user_id"]
                alias.match_count = dm.F("match_count") + 1
                alias.save()


def save_team_notes(final_rows: list, match, confirmed_by_user):
    """
    For every row where the admin confirmed a sub, save an OCRTeamNote.
    """
    from afc_ocr.models import OCRTeamNote
    from afc_tournament_and_scrims.models import TournamentTeam

    for row in final_rows:
        if not (row.get("admin_confirmed_sub") and row.get("team_mismatch")):
            continue

        user_id         = row.get("matched_user_id")
        played_for_id   = row.get("matched_team_id")
        expected_t_id   = row.get("expected_team_id")

        if not user_id or not played_for_id:
            continue

        # Resolve actual Team FK (TournamentTeam → Team)
        try:
            played_for_t_team = TournamentTeam.objects.select_related("team").get(
                tournament_team_id=played_for_id
            )
            played_for_team = played_for_t_team.team
        except TournamentTeam.DoesNotExist:
            continue

        registered_team = None
        if expected_t_id:
            try:
                expected_t_team = TournamentTeam.objects.select_related("team").get(
                    tournament_team_id=expected_t_id
                )
                registered_team = expected_t_team.team
            except TournamentTeam.DoesNotExist:
                pass

        OCRTeamNote.objects.create(
            user_id=user_id,
            registered_team=registered_team,
            played_for_team=played_for_team,
            match=match,
            confirmed_by=confirmed_by_user,
        )
