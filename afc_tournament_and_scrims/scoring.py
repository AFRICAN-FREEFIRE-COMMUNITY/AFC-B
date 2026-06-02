"""
Single source of truth for AFC tournament scoring.

Historically the per-match point formula was copy-pasted across ~9 call sites in
views.py (manual team/solo entry, edit, upload variants) and afc_ocr/services/commit.py.
That duplication is why scoring drifts between manual entry, edit, and OCR. Everything
now computes points through compute_team_points / compute_solo_points here.

It also hosts the two pure helpers the on-read standings builder uses for the new
per-stage scoring features (see WEBSITE/tasks/scoring-modes-design.md, sub-project A):
  - champion_for_group : Champion-Point win rule (match-point threshold + ordered replay)
  - rewards_from_standings : Point-Rush carry-over reward per lobby
"""

# Canonical Free Fire battle-royale placement table. Was duplicated at
# views.py:12140 and afc_ocr/services/commit.py:6 — this is now the only copy.
DEFAULT_PLACEMENT = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}


def normalize_placement_points(pp):
    """Accept a stored placement_points dict (string or int keys) and return int->int.
    Falls back to DEFAULT_PLACEMENT when empty/missing; replaces the old _normalize_*
    with fail-loud validation (a non-dict payload raises ValueError instead of silently
    defaulting, so a malformed scoring config surfaces instead of scoring everyone as 0)."""
    if not pp:
        return DEFAULT_PLACEMENT
    if not isinstance(pp, dict):
        raise ValueError("placement_points must be a JSON object/dict")
    return {int(k): int(v) for k, v in pp.items()}


def compute_team_points(*, placement_points, kill_point, points_per_assist,
                        points_per_1000_damage, placement, kills, damage, assists,
                        bonus, penalty, played):
    """Per-match TEAM points. `placement_points` is an int->int dict (already normalized);
    kills/damage/assists are already summed across played players. Returns the three
    integer columns stored on TournamentTeamMatchStats. Verbatim port of views.py:12596-12611.

    Not-played teams: `played=False` zeroes only the placement_points (matching the live
    callers, which set `placement_pts = ... if team_played else 0`). The live callers also
    pre-zero kills/damage/assists for not-played teams (played_players is filtered to
    played==True), so those terms fall away too. Bonus/penalty are NOT zeroed: the original
    manual/edit paths stored `total_points = bonus - penalty` for a not-played team that still
    carried a penalty/bonus, so the total reconciles with the bonus_points/penalty_points
    columns. We must preserve that here (no early-return short-circuit) — this is a
    behavior-preserving refactor."""
    placement_pts = placement_points.get(placement, 0) if played else 0
    kill_pts = kills * kill_point
    assist_pts = assists * points_per_assist
    damage_pts = (damage / 1000) * points_per_1000_damage
    total = placement_pts + kill_pts + assist_pts + damage_pts + bonus - penalty
    return {
        "placement_points": int(placement_pts),
        "kill_points": int(kill_pts),
        "total_points": int(total),
    }


def compute_solo_points(*, placement_points, kill_point, placement, kills, played):
    """Per-match SOLO points. Verbatim port of views.py:13118-13120 (placement + kills only;
    bonus/penalty are stored on the row but not folded into total_points here)."""
    placement_pts = placement_points.get(placement, 0) if played else 0
    kill_pts = int(kills * kill_point) if played else 0
    total = placement_pts + kill_pts
    return {
        "placement_points": int(placement_pts),
        "kill_points": kill_pts,
        "total_points": total,
    }


# ── Phase 2 (sub-project A): the two PURE on-read helpers ──
# Both are deliberately DB-free and Django-free so the standings builder
# (get_all_leaderboard_details_for_event) can call them against any pre-aggregated
# rows, and so they can be unit-tested with SimpleTestCase (no MySQL).


def champion_for_group(matches, threshold, carry_over=None):
    """
    Champion-Point win rule. `matches` is an ordered list (play order); each item is
    {"rows": [{"id": competitor_id, "placement": int, "points": int}, ...]} where `points`
    is that competitor's effective points for that match (placement + kills + bonus - penalty).
    `carry_over` is an optional {id: points} head start (Point-Rush bonus) that counts toward
    the threshold. Returns the champion's id, or None if no team triggers the rule.

    Champion = the first competitor to place 1st (Booyah) in a match where its running total
    BEFORE that match was already >= threshold. The booyah that first crosses the threshold
    does not win — we snapshot `pre` (totals entering the match) before adding the match's
    points, so a team only wins on a booyah it earns while ALREADY at/over the threshold.
    """
    running = dict(carry_over or {})
    for m in matches:
        pre = dict(running)                       # totals BEFORE this match (the threshold check)
        booyah = None
        for r in m["rows"]:
            cid = r["id"]
            running[cid] = running.get(cid, 0) + int(r["points"])
            if int(r["placement"]) == 1:
                booyah = cid
        if booyah is not None and pre.get(booyah, 0) >= threshold:
            return booyah                          # stage decided; later matches ignored
    return None


def rewards_from_standings(ranked_ids, reward_table):
    """
    Point-Rush per-lobby reward. `ranked_ids` is one lobby's competitor ids in finishing
    order (index 0 = 1st). `reward_table` is {"1": pts, ...} placement->bonus. Returns
    {id: bonus}. Placements with no team (fewer teams than reward rows) are skipped — the
    (placement-1) index simply falls outside `ranked_ids`, so that reward row is dropped.
    """
    out = {}
    for placement_str, bonus in reward_table.items():
        idx = int(placement_str) - 1
        if 0 <= idx < len(ranked_ids):
            out[ranked_ids[idx]] = out.get(ranked_ids[idx], 0) + int(bonus)
    return out
