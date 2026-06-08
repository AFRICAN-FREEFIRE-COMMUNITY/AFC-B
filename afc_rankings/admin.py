from django.contrib import admin
from .models import (
    Season, GhostTeam, TeamSeasonEnrollment, TeamSeasonRoster,
    TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
    AnnualLeaderboardEntry, TransferWindowLog, RankingAuditLog, TeamSocialSnapshot,
)


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ("season_id", "name", "year", "quarter", "is_active", "tier_eval_run")
    list_filter = ("year", "quarter", "is_active")


@admin.register(GhostTeam)
class GhostTeamAdmin(admin.ModelAdmin):
    list_display = ("team_name", "country", "is_active", "claim_status", "claimed_by")
    list_filter = ("is_active", "claim_status")
    search_fields = ("team_name", "external_id")


@admin.register(TeamMonthlyScore)
class TeamMonthlyScoreAdmin(admin.ModelAdmin):
    list_display = ("team", "ghost_team", "month", "total_score", "rank", "is_zeroed", "finalized")
    list_filter = ("month", "finalized", "is_zeroed")


@admin.register(TeamQuarterlyScore)
class TeamQuarterlyScoreAdmin(admin.ModelAdmin):
    list_display = ("team", "ghost_team", "season", "total_score", "tier_assigned", "rank")
    list_filter = ("season", "tier_assigned")


@admin.register(PlayerMonthlyScore)
class PlayerMonthlyScoreAdmin(admin.ModelAdmin):
    list_display = ("player", "month", "total_score", "rank", "is_zeroed", "finalized")
    list_filter = ("month", "finalized", "is_zeroed")


@admin.register(PlayerQuarterlyScore)
class PlayerQuarterlyScoreAdmin(admin.ModelAdmin):
    list_display = ("player", "season", "total_score", "tier_assigned", "tier_source", "rank")
    list_filter = ("season", "tier_assigned", "tier_source")


@admin.register(AnnualLeaderboardEntry)
class AnnualLeaderboardEntryAdmin(admin.ModelAdmin):
    list_display = ("year", "entity_type", "team", "player", "total_score", "rank")
    list_filter = ("year", "entity_type")


@admin.register(RankingAuditLog)
class RankingAuditLogAdmin(admin.ModelAdmin):
    list_display = ("audit_id", "object_type", "object_ref", "action", "changed_by", "changed_at")
    list_filter = ("object_type", "changed_at")
    readonly_fields = ("changed_at",)


admin.site.register(TeamSeasonEnrollment)
admin.site.register(TeamSeasonRoster)
admin.site.register(TransferWindowLog)
admin.site.register(TeamSocialSnapshot)
