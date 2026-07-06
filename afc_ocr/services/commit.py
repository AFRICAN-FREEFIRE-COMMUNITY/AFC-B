import json
from collections import defaultdict

from django.db import transaction, models as dm

# Single source of truth for the per-match point formula + placement-points normalizer.
# Previously OCR carried its own DEFAULT_PLACEMENT + _normalize_pp copies, which is exactly
# how OCR scoring drifted from manual entry — both now go through scoring.* (see scoring.py).
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
    Groups rows by placement — each placement group = one team.
    """
    from afc_tournament_and_scrims.models import (
        TournamentTeamMatchStats, TournamentPlayerMatchStats, UnmatchedTeamBlock,
    )

    lb = _get_lb_for_match(match)
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

            team_kills   = sum(int(r.get("kills", 0))   for r in rows)
            team_damage  = sum(int(r.get("damage", 0))  for r in rows)
            team_assists = sum(int(r.get("assists", 0)) for r in rows)
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

            for row in rows:
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
    """
    from afc_tournament_and_scrims.models import SoloPlayerMatchStats, RegisteredCompetitors

    lb = _get_lb_for_match(match)
    if not lb:
        raise ValueError("No leaderboard found for this match.")

    event = _get_event_for_match(match)
    placement_points = scoring.normalize_placement_points(lb.placement_points)
    kill_point = lb.kill_point

    with transaction.atomic():
        SoloPlayerMatchStats.objects.filter(match=match).delete()

        for row in final_rows:
            user_id   = row.get("matched_user_id")
            placement = int(row.get("placement", 0))
            kills     = int(row.get("kills", 0))

            if not user_id:
                continue

            competitor = RegisteredCompetitors.objects.filter(
                user_id=user_id, event=event
            ).first()
            if not competitor:
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

    return lb


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
