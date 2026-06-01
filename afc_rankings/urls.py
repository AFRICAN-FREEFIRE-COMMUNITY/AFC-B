"""
URL map for rankings & tiering (prefix ``rankings/``).

Phase 1 = the public read API (``views.py``). Phase 2 adds the admin WRITE endpoints,
which live in one ``admin_<surface>.py`` module each (built disjoint, so the auth gate
and audit trail stay in ``admin_views.py`` only). This file is the single place that
mounts every route.

Method dispatch
---------------
The Phase-2 endpoints are function-based ``@api_view`` views, so several of them share a
URL path but differ by HTTP method (e.g. ``seasons/`` is GET=list + POST=create, and
``ghost-teams/<uuid>/`` is GET/PATCH/DELETE). Django allows only one ``path()`` per URL,
so ``_route(GET=..., POST=...)`` returns a thin, csrf-exempt Django view that forwards the
raw request to the right ``@api_view`` handler. Each handler still does its own DRF
request/response handling — the router only picks which one runs.
"""
from django.http import HttpResponseNotAllowed
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import views as v
from . import (
    admin_seasons,
    admin_audit,
    admin_ghost,
    admin_overrides,
    admin_scoring_config,
    admin_tournament_tiers,
    admin_prize,
    # deferred wave (Phase 2b)
    admin_results,
    admin_social,
    admin_evaluation,
    # publish controls (Phase 2c)
    admin_publish,
)


def _route(**method_map):
    """Mount multiple ``@api_view`` handlers on ONE URL path, dispatched by HTTP method.

    ``method_map`` maps an HTTP method name to its handler view, e.g.
    ``_route(GET=admin_ghost.ghost_list, POST=admin_ghost.ghost_create)``. The returned
    view is a plain (csrf-exempt) Django view; it forwards the *raw* request to the chosen
    ``@api_view`` handler, which then does its own DRF wrapping — so there's no double
    wrapping. Unmatched methods get a proper 405.
    """
    @csrf_exempt
    def dispatch(request, *args, **kwargs):
        handler = method_map.get(request.method)
        if handler is None:
            return HttpResponseNotAllowed(list(method_map))
        return handler(request, *args, **kwargs)

    return dispatch


urlpatterns = [
    # ───────────────────────── Phase 1 — public reads ─────────────────────────
    # team rankings
    path("teams/monthly/", v.teams_monthly, name="rankings_teams_monthly"),
    path("teams/quarterly/", v.teams_quarterly, name="rankings_teams_quarterly"),
    path("teams/annual/", v.teams_annual, name="rankings_teams_annual"),
    path("teams/<int:team_id>/score/", v.team_score_detail, name="rankings_team_score"),
    # player rankings
    path("players/monthly/", v.players_monthly, name="rankings_players_monthly"),
    path("players/quarterly/", v.players_quarterly, name="rankings_players_quarterly"),
    path("players/annual/", v.players_annual, name="rankings_players_annual"),
    path("players/<int:player_id>/score/", v.player_score_detail, name="rankings_player_score"),

    # ───────────────────────── Phase 2 — Seasons (admin) ─────────────────────────
    # seasons/ is GET=public list (Phase 1) + POST=admin create (Phase 2, head_admin only)
    path("seasons/", _route(GET=v.seasons_list, POST=admin_seasons.season_create), name="rankings_seasons"),
    path("seasons/current/", v.season_current, name="rankings_season_current"),
    path("seasons/<int:season_id>/", admin_seasons.season_update, name="rankings_season_update"),
    path("seasons/<int:season_id>/transfer-window/", admin_seasons.transfer_window_action,
         name="rankings_season_transfer_window"),
    path("seasons/<int:season_id>/transfer-log/", admin_seasons.transfer_log_list,
         name="rankings_season_transfer_log"),

    # ───────────────────────── Phase 2 — Overrides / bans / deductions (season-scoped) ───────
    path("seasons/<int:season_id>/team-tier/<int:team_id>/", admin_overrides.team_tier_override,
         name="rankings_team_tier_override"),
    path("seasons/<int:season_id>/zero-team/<int:team_id>/", admin_overrides.zero_team,
         name="rankings_zero_team"),
    path("seasons/<int:season_id>/unzero-team/<int:team_id>/", admin_overrides.unzero_team,
         name="rankings_unzero_team"),
    path("seasons/<int:season_id>/zero-player/<int:player_id>/", admin_overrides.zero_player,
         name="rankings_zero_player"),
    path("seasons/<int:season_id>/deduct-points/<int:team_id>/", admin_overrides.deduct_points,
         name="rankings_deduct_points"),
    path("seasons/<int:season_id>/clear-deduction/<int:team_id>/", admin_overrides.clear_deduction,
         name="rankings_clear_deduction"),

    # ───────────────────────── Phase 2 — Audit log + raw viewer (read-only) ─────────────────
    path("admin/audit-log/", admin_audit.audit_log, name="rankings_admin_audit_log"),
    path("admin/teams/<int:team_id>/raw/", admin_audit.team_raw, name="rankings_admin_team_raw"),
    path("admin/players/<int:player_id>/raw/", admin_audit.player_raw, name="rankings_admin_player_raw"),

    # ───────────────────────── Phase 2 — Ghost teams + players + claims ─────────────────────
    path("ghost-teams/", _route(GET=admin_ghost.ghost_list, POST=admin_ghost.ghost_create),
         name="rankings_ghost_teams"),
    path("ghost-teams/<uuid:ghost_team_id>/",
         _route(GET=admin_ghost.ghost_detail, PATCH=admin_ghost.ghost_update, DELETE=admin_ghost.ghost_delete),
         name="rankings_ghost_team_detail"),
    path("ghost-teams/<uuid:ghost_team_id>/approve-claim/", admin_ghost.ghost_approve_claim,
         name="rankings_ghost_approve_claim"),
    path("ghost-teams/<uuid:ghost_team_id>/revoke-claim/", admin_ghost.ghost_revoke_claim,
         name="rankings_ghost_revoke_claim"),

    # ───────────────────────── Phase 2 — Scoring Config (versioned) ─────────────────────────
    # scoring-config/defaults/ is listed before the collection so it never shadows; literal
    # paths don't collide with the collection anyway, but order keeps intent clear.
    path("scoring-config/defaults/", admin_scoring_config.scoring_config_defaults,
         name="rankings_scoring_config_defaults"),
    path("scoring-config/",
         _route(GET=admin_scoring_config.scoring_config, POST=admin_scoring_config.scoring_config_save),
         name="rankings_scoring_config"),

    # ───────────────────────── Phase 2 — Tournament Tiers (classification rules) ────────────
    # Literal sub-paths first; <int:rule_id> only matches digits so it can't shadow them,
    # but keep reorder/classify above the detail route for readability.
    path("event-tier-rules/reorder/", admin_tournament_tiers.tier_rules_reorder,
         name="rankings_event_tier_rules_reorder"),
    path("event-tier-rules/classify/", admin_tournament_tiers.tier_rules_classify,
         name="rankings_event_tier_rules_classify"),
    path("event-tier-rules/",
         _route(GET=admin_tournament_tiers.tier_rules_list, POST=admin_tournament_tiers.tier_rule_create),
         name="rankings_event_tier_rules"),
    path("event-tier-rules/<int:rule_id>/",
         _route(PATCH=admin_tournament_tiers.tier_rule_update, DELETE=admin_tournament_tiers.tier_rule_delete),
         name="rankings_event_tier_rule_detail"),
    path("event-tier-config/", admin_tournament_tiers.tier_config_update,
         name="rankings_event_tier_config"),

    # ───────────────────────── Phase 2 — Prize entry ─────────────────────────
    path("admin/tournament-prizes/", admin_prize.tournament_prizes_list, name="rankings_tournament_prizes"),
    path("prize/", admin_prize.prize_create, name="rankings_prize_create"),
    path("prize/<int:payout_id>/",
         _route(PATCH=admin_prize.prize_update, DELETE=admin_prize.prize_delete),
         name="rankings_prize_detail"),

    # ───────────────────────── Phase 2b — Result Markers counting controls ─────────────────────────
    path("admin/results/markers/", admin_results.results_markers_list, name="rankings_results_markers"),
    path("event-counting/<int:event_id>/",
         _route(GET=admin_results.event_counting_detail, PATCH=admin_results.event_counting_update),
         name="rankings_event_counting"),
    path("result-exclusions/",
         _route(GET=admin_results.result_exclusions_list, POST=admin_results.result_exclusion_create),
         name="rankings_result_exclusions"),
    path("result-exclusions/<int:exclusion_id>/", admin_results.result_exclusion_delete,
         name="rankings_result_exclusion_delete"),

    # ───────────────────────── Phase 2b — Social (self-connect + verify) ─────────────────────────
    path("admin/seasons/<int:season_id>/social/", admin_social.social_list, name="rankings_social_list"),
    path("admin/seasons/<int:season_id>/social/<int:team_id>/", admin_social.social_edit, name="rankings_social_edit"),
    path("admin/seasons/<int:season_id>/social/<int:team_id>/verify/", admin_social.social_verify, name="rankings_social_verify"),
    path("admin/seasons/<int:season_id>/social/<int:team_id>/unverify/", admin_social.social_unverify, name="rankings_social_unverify"),
    path("admin/seasons/<int:season_id>/social/<int:team_id>/connect/", admin_social.social_connect, name="rankings_social_connect"),

    # ───────────────────────── Phase 2b — Run evaluation + recalc ─────────────────────────
    path("seasons/<int:season_id>/run-evaluation/", admin_evaluation.run_evaluation, name="rankings_run_evaluation"),
    path("admin/recalc-status/", admin_evaluation.recalc_status, name="rankings_recalc_status"),
    path("admin/recalc/", admin_evaluation.recalc_entity, name="rankings_recalc"),

    # ───────────────────────── Phase 2c — Publish controls + admin draft preview ─────────────────────────
    path("seasons/<int:season_id>/publish/", admin_publish.publish_state, name="rankings_publish_state"),
    path("admin/teams/quarterly/", admin_publish.admin_teams_quarterly, name="rankings_admin_teams_quarterly"),
    path("admin/players/quarterly/", admin_publish.admin_players_quarterly, name="rankings_admin_players_quarterly"),
]
