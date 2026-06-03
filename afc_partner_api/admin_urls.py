# afc_partner_api/admin_urls.py
# ──────────────────────────────────────────────────────────────────────────────
# URL map for the AFC-staff partner-admin surface, mounted in afc/urls.py at
#   path("partners/", include("afc_partner_api.admin_urls"))
# so every route below lives under /partners/admin/… — the human provisioning
# surface, kept OFF the versioned /api/v1/partner/ tree (which is the partner-facing
# read API). These views are USER-SESSION (Bearer) authenticated + partner-admin
# gated, unlike the X-API-Key read endpoints.
#
# Each view is a function-based @api_view with its own method list, so one path() per
# view is enough (DRF returns 405 for the wrong verb). Partners are addressed by slug
# (matching Partner.slug); keys by their integer key_id (a key is the addressable
# thing for revoke); the publish route is keyed by the event's slug.
# Full spec: WEBSITE/tasks/partner-api-design.md (§9 admin surface).
# ──────────────────────────────────────────────────────────────────────────────
from django.urls import path

from . import views_admin

urlpatterns = [
    # ── provisioning + oversight ──
    path("admin/create/", views_admin.create_partner, name="partner_admin_create"),
    path("admin/list/", views_admin.list_partners, name="partner_admin_list"),

    # ── key management (key addressed by id; revoke is a POST so it carries no body
    #    requirement and stays consistent with the other action endpoints) ──
    # NOTE: declared BEFORE the <slug> detail routes so "keys" can never be swallowed
    # by the <slug:slug> converter (it would otherwise match "keys" as a partner slug).
    path("admin/keys/<int:key_id>/revoke/", views_admin.revoke_key, name="partner_admin_revoke_key"),

    # ── per-event publish gate (event addressed by slug) ──
    # Also declared before the <slug> partner detail routes for the same reason
    # ("events" must not be read as a partner slug).
    path("admin/events/<slug:event_slug>/publish/", views_admin.publish_event,
         name="partner_admin_publish_event"),

    # ── per-partner detail / edit / suspend / issue-key (slug-addressed) ──
    path("admin/<slug:slug>/keys/", views_admin.issue_key, name="partner_admin_issue_key"),
    path("admin/<slug:slug>/suspend/", views_admin.suspend_partner, name="partner_admin_suspend"),
    # GET detail + PATCH edit share one path (the @api_view method list routes by verb).
    path("admin/<slug:slug>/", views_admin.partner_detail, name="partner_admin_detail"),
]
