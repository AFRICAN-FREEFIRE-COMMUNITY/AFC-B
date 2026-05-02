import json
from collections import defaultdict

from django.db import transaction, models as dm

DEFAULT_PLACEMENT = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}


def _normalize_pp(pp):
    if not pp:
        return DEFAULT_PLACEMENT
    try:
        normalized = {int(k): int(v) for k, v in pp.items()}
        return normalized if normalized else DEFAULT_PLACEMENT
    except Exception:
        return DEFAULT_PLACEMENT


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
        TournamentTeamMatchStats, TournamentPlayerMatchStats,
    )

    lb = _get_lb_for_match(match)

    scoring = match.scoring_settings or {}
    if isinstance(scoring, str):
        scoring = json.loads(scoring)

    lb_pp = lb.placement_points if lb else {}
    placement_points = _normalize_pp(scoring.get("placement_points") or lb_pp)
    kill_point         = float(scoring.get("kill_point", lb.kill_point if lb else 1.0))
    points_per_assist  = float(scoring.get("points_per_assist", 0))
    points_per_damage  = float(scoring.get("points_per_1000_damage", 0))

    # Group rows by placement
    groups: dict = defaultdict(list)
    for row in final_rows:
        groups[int(row.get("placement", 0))].append(row)

    with transaction.atomic():
        TournamentTeamMatchStats.objects.filter(match=match).delete()

        for placement, rows in sorted(groups.items()):
            t_team_id = rows[0].get("matched_team_id")
            if not t_team_id:
                continue

            team_kills   = sum(int(r.get("kills", 0))   for r in rows)
            team_damage  = sum(int(r.get("damage", 0))  for r in rows)
            team_assists = sum(int(r.get("assists", 0)) for r in rows)
            bonus_pts    = int(rows[0].get("bonus_points", 0))
            penalty_pts  = int(rows[0].get("penalty_points", 0))

            pp    = placement_points.get(placement, 0)
            kp    = team_kills * kill_point
            ap    = team_assists * points_per_assist
            dp    = (team_damage / 1000) * points_per_damage
            total = pp + kp + ap + dp + bonus_pts - penalty_pts

            team_stat = TournamentTeamMatchStats.objects.create(
                match=match,
                tournament_team_id=t_team_id,
                placement=placement,
                kills=team_kills,
                damage=team_damage,
                assists=team_assists,
                placement_points=int(pp),
                kill_points=int(kp),
                total_points=int(round(total)),
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

    return lb


def commit_solo_result(match, final_rows: list):
    """
    Write solo match stats from OCR final_rows.
    """
    from afc_tournament_and_scrims.models import SoloPlayerMatchStats, RegisteredCompetitors

    lb = _get_lb_for_match(match)
    if not lb:
        raise ValueError("No leaderboard found for this match.")

    event = _get_event_for_match(match)
    placement_points = _normalize_pp(lb.placement_points)
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

            pp    = placement_points.get(placement, 0)
            kp    = kills * kill_point
            total = pp + kp

            SoloPlayerMatchStats.objects.create(
                match=match,
                competitor=competitor,
                placement=placement,
                kills=kills,
                placement_points=int(pp),
                kill_points=kp,
                total_points=total,
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
