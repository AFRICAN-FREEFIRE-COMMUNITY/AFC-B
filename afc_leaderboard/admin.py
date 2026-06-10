"""
afc_leaderboard.admin — Django-admin registration for the standalone leaderboard models.

Light read-only-friendly registration so AFC staff can inspect standalone leaderboards, their
participants, maps, and per-map results directly in /admin/. The authoritative create/edit surface
is the FE wizard + the afc_leaderboard API; this is just an inspection aid.
"""
from django.contrib import admin

from .models import (
    StandaloneLeaderboard,
    LeaderboardParticipant,
    LeaderboardMatch,
    ParticipantMatchResult,
)


@admin.register(StandaloneLeaderboard)
class StandaloneLeaderboardAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "format", "organization", "status", "counts_toward_rankings", "creator", "created_at")
    list_filter = ("format", "status", "counts_toward_rankings")
    search_fields = ("name",)


@admin.register(LeaderboardParticipant)
class LeaderboardParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "leaderboard", "team", "ghost_team", "user", "ghost_player")
    list_filter = ("leaderboard",)


@admin.register(LeaderboardMatch)
class LeaderboardMatchAdmin(admin.ModelAdmin):
    list_display = ("id", "leaderboard", "match_number", "match_map", "created_at")
    list_filter = ("leaderboard",)


@admin.register(ParticipantMatchResult)
class ParticipantMatchResultAdmin(admin.ModelAdmin):
    list_display = ("id", "match", "participant", "placement", "kills", "total_points", "played")
    list_filter = ("played",)
