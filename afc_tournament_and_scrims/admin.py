from django.contrib import admin

# Register your models here.

# Kill feed weapon table (inbox #350): which Free Fire weapon ids are bolt-action snipers. Filled by staff in
# /admin/ as ids are matched to the gun seen on stream (only 601 = grenade is known today); read by
# views_kill_feed through kill_feed.sniper_counts.
from .models import GameWeapon  # noqa: E402


@admin.register(GameWeapon)
class GameWeaponAdmin(admin.ModelAdmin):
    list_display = ("weapon_id", "name", "is_sniper", "notes")
    list_filter = ("is_sniper",)
    search_fields = ("name", "weapon_id")
