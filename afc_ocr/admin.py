from django.contrib import admin
from .models import OCRSession, OCRNameAlias, OCRTeamNote

@admin.register(OCRSession)
class OCRSessionAdmin(admin.ModelAdmin):
    list_display = ("session_id", "match", "map_index", "event_type", "status", "created_by", "created_at")
    list_filter = ("status", "event_type")
    search_fields = ("session_id",)
    readonly_fields = ("session_id", "created_at", "updated_at")

@admin.register(OCRNameAlias)
class OCRNameAliasAdmin(admin.ModelAdmin):
    list_display = ("raw_name", "user", "match_count", "updated_at")
    search_fields = ("raw_name", "user__username")

@admin.register(OCRTeamNote)
class OCRTeamNoteAdmin(admin.ModelAdmin):
    list_display = ("user", "registered_team", "played_for_team", "match", "confirmed_by", "created_at")
    search_fields = ("user__username",)
