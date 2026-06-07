from django.contrib import admin

from .models import MarketBan, MarketReport

# Register your models here.

# Moderation models (feature "J-market-reporting") registered for AFC-staff visibility
# in the Django admin. The real triage happens in the app UI (Reports & Flags tab);
# these registrations just give staff a fallback view + raw search.


@admin.register(MarketReport)
class MarketReportAdmin(admin.ModelAdmin):
    list_display = ("id", "subject_type", "category", "status", "reporter", "created_at")
    list_filter = ("subject_type", "category", "status")
    search_fields = ("reported_team__team_name", "reported_player__username", "reporter__username")


@admin.register(MarketBan)
class MarketBanAdmin(admin.ModelAdmin):
    list_display = ("id", "scope", "banned_team", "banned_player", "ban_duration", "ban_end_date", "is_active")
    list_filter = ("scope", "is_active")
