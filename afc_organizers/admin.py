# Django-admin registration for the organizer models (so AFC staff can inspect/repair
# org + membership rows directly if needed). The product surfaces live in the API.
from django.contrib import admin

from .models import Organization, OrganizationMember


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("organization_id", "name", "slug", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "slug", "email")


@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "user", "role", "status")
    list_filter = ("role", "status")
    search_fields = ("organization__name", "user__username")
