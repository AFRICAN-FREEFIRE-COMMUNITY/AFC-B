# URL map for the organizer feature (prefix ``organizers/``).
#
# Three view modules, split by audience so the auth gate for each stays obvious:
#   - views_admin     → AFC staff (head_admin / organizer_admin): provisioning + oversight
#   - views_organizer → org members (owner / sub_organizer), scoped to their own org
#   - views_public    → unauthenticated public org page
#
# Each view is a function-based @api_view with its own method list, so one ``path()`` per
# view is enough (DRF returns 405 for the wrong method). Slugs use the <slug:slug> converter
# to match the Organization.slug field. Phase 1 mounts member + management + public routes;
# event / results / design / review routes arrive in later phases (see
# WEBSITE/tasks/organizers-design.md).
from django.urls import path

from . import (
    views_admin, views_organizer, views_public,
    views_reviews, views_reports, views_blacklist, views_blacklist_lookup,
    views_leaderboard_design, co_organizers, payouts,
)

urlpatterns = [
    # ───────────────────────── AFC staff: provisioning + oversight ─────────────────────────
    path("admin/create-organization/", views_admin.admin_create_organization,
         name="organizers_admin_create"),
    path("admin/get-all-organizations/", views_admin.admin_list_organizations,
         name="organizers_admin_list"),
    path("admin/get-organization/<slug:slug>/", views_admin.admin_get_organization,
         name="organizers_admin_detail"),
    path("admin/edit-organization/<slug:slug>/", views_admin.admin_edit_organization,
         name="organizers_admin_edit"),
    path("admin/suspend-organization/<slug:slug>/", views_admin.admin_suspend_organization,
         name="organizers_admin_suspend"),
    path("admin/delete-organization/<slug:slug>/", views_admin.admin_delete_organization,
         name="organizers_admin_delete"),
    # F5 (owner 2026-06-19): admin restores a soft-deleted org (clean delete kept everything intact).
    path("admin/restore-organization/<slug:slug>/", views_admin.admin_restore_organization,
         name="organizers_admin_restore"),
    path("admin/manage-organization-member/<slug:slug>/", views_admin.admin_manage_organization_member,
         name="organizers_admin_manage_member"),

    # ───────────────────────── Org members: scoped to their own org ─────────────────────────
    path("get-my-organizations/", views_organizer.get_my_organizations,
         name="organizers_my"),
    path("get-organization/<slug:slug>/", views_organizer.get_organization,
         name="organizers_get"),
    path("edit-organization-profile/<slug:slug>/", views_organizer.edit_organization_profile,
         name="organizers_edit_profile"),
    path("get-organization-members/<slug:slug>/", views_organizer.get_organization_members,
         name="organizers_members"),
    path("add-organization-member/<slug:slug>/", views_organizer.add_organization_member,
         name="organizers_add_member"),
    path("edit-organization-member/<slug:slug>/<int:user_id>/", views_organizer.edit_organization_member,
         name="organizers_edit_member"),
    path("remove-organization-member/<slug:slug>/<int:user_id>/", views_organizer.remove_organization_member,
         name="organizers_remove_member"),
    # F6-P4 (owner 2026-06-19): organizer payouts + co-owner auto-split.
    path("admin/payouts/", payouts.admin_list_org_payouts, name="organizers_admin_payouts"),
    path("admin/payouts/release/", payouts.admin_release_org_payout, name="organizers_admin_payout_release"),
    path("admin/payouts/mark-paid/", payouts.admin_mark_org_payout_paid, name="organizers_admin_payout_paid"),
    path("<slug:slug>/payout-account/", payouts.save_payout_account, name="organizers_payout_account"),
    path("<slug:slug>/earnings/", payouts.my_org_earnings, name="organizers_earnings"),

    # F6 (owner 2026-06-19): multi-org event co-ownership. Creator-owner invites; invited-owner responds.
    path("co-organizers/invite/", co_organizers.invite_co_organizer, name="organizers_co_invite"),
    path("co-organizers/respond/", co_organizers.respond_co_organizer, name="organizers_co_respond"),
    path("co-organizers/revoke/", co_organizers.revoke_co_organizer, name="organizers_co_revoke"),
    path("co-organizers/", co_organizers.list_event_co_organizers, name="organizers_co_list"),

    # F5 (owner 2026-06-19): sub-organizer leaves; owner suspends/soft-deletes their own org.
    path("leave-organization/<slug:slug>/", views_organizer.leave_organization,
         name="organizers_leave"),
    path("organization/<slug:slug>/suspend/", views_organizer.suspend_my_organization,
         name="organizers_owner_suspend"),
    path("organization/<slug:slug>/delete/", views_organizer.delete_my_organization,
         name="organizers_owner_delete"),

    # (The "request a design" feature was removed 2026-06-13 in favour of the self-serve design
    # library below; its routes + views_design.py module were deleted.)

    # ───────────────────────── Event ratings + comments (Phase 4) ─────────────────────────
    # Ratings are anonymous to organizers (only the aggregate is exposed); comments are
    # readable only by the event's organizer. event-rating GET allows anonymous viewers.
    path("events/<int:event_id>/rate/", views_reviews.rate_event, name="organizers_rate_event"),
    path("events/<int:event_id>/rating/", views_reviews.event_rating, name="organizers_event_rating"),
    path("events/<int:event_id>/comment/", views_reviews.comment_event, name="organizers_comment_event"),
    path("event-comments/<int:event_id>/", views_reviews.event_comments, name="organizers_event_comments"),

    # ───────────────────────── Organizer metrics (Phase 4) ─────────────────────────
    path("metrics/<slug:slug>/", views_reviews.org_metrics, name="organizers_metrics"),

    # ───────────────────────── Organization reports (Phase 4) ─────────────────────────
    # Any user reports an org; AFC triages + resolves (resolution can exclude the reported
    # event from rankings — the integrity action).
    path("report-organization/<slug:slug>/", views_reports.report_organization, name="organizers_report"),
    path("admin/reports/", views_reports.admin_list_reports, name="organizers_admin_reports"),
    path("admin/reports/<int:report_id>/", views_reports.admin_update_report, name="organizers_admin_report_detail"),

    # ───────────────────────── Leaderboard design library (feature 2026-06-13) ─────────────────────────
    # A per-org (or AFC-native) library of branded leaderboard backgrounds. Organizers/admins
    # upload designs; the leaderboard export picker renders standings onto the chosen one. See
    # views_leaderboard_design + afc_leaderboard.graphic. by-id route before the collection.
    # Positioned-logo sub-routes (declared BEFORE the by-id design route's bare form is fine —
    # they are more specific paths). A design carries 0..N logos at x_pct/y_pct + size.
    path("leaderboard-designs/by-id/<int:design_id>/logos/<int:logo_id>/",
         views_leaderboard_design.design_logo_item,
         name="organizers_leaderboard_design_logo_item"),  # PATCH (move/resize) / DELETE
    # Duplicate a design wholesale (owner 2026-07-02): scalars+logos+fields+texts+pages.
    path("leaderboard-designs/by-id/<int:design_id>/duplicate/",
         views_leaderboard_design.design_duplicate, name="organizers_leaderboard_design_duplicate"),
    path("leaderboard-designs/by-id/<int:design_id>/logos/",
         views_leaderboard_design.design_logos,
         name="organizers_leaderboard_design_logos"),       # POST (add, multipart)
    # Connected-column FIELDS (placed data columns) — owner 2026-06-14. by-id form, specific first.
    path("leaderboard-designs/by-id/<int:design_id>/fields/<int:field_id>/",
         views_leaderboard_design.design_field_item,
         name="organizers_leaderboard_design_field_item"),  # PATCH (move/restyle) / DELETE
    path("leaderboard-designs/by-id/<int:design_id>/fields/",
         views_leaderboard_design.design_fields,
         name="organizers_leaderboard_design_fields"),      # POST (add a connected column)
    # Freeform TEXT elements.
    path("leaderboard-designs/by-id/<int:design_id>/texts/<int:text_id>/",
         views_leaderboard_design.design_text_item,
         name="organizers_leaderboard_design_text_item"),   # PATCH / DELETE
    path("leaderboard-designs/by-id/<int:design_id>/texts/",
         views_leaderboard_design.design_texts,
         name="organizers_leaderboard_design_texts"),       # POST (add freeform text)
    # Multi-page sub-endpoints (owner 2026-06-14). Specific (with page_id) before the collection,
    # and both before the bare by-id design route so the int converter never swallows them.
    path("leaderboard-designs/by-id/<int:design_id>/pages/<int:page_id>/",
         views_leaderboard_design.design_page_item,
         name="organizers_leaderboard_design_page_item"),   # PATCH (update bg/groups) / DELETE
    path("leaderboard-designs/by-id/<int:design_id>/pages/",
         views_leaderboard_design.design_pages,
         name="organizers_leaderboard_design_pages"),       # POST (create next page)
    # Apply ONE background image to EVERY page at once (or design-level when single-page). owner 2026-06-27.
    path("leaderboard-designs/by-id/<int:design_id>/apply-background-to-all/",
         views_leaderboard_design.apply_background_to_all,
         name="organizers_leaderboard_design_apply_bg_all"),  # POST (multipart bg -> all pages)
    # Apply a placed column's per-size enablement to BOTH sizes (+ all pages for one field). owner 2026-07-05.
    path("leaderboard-designs/by-id/<int:design_id>/apply-field-enablement-to-all/",
         views_leaderboard_design.apply_field_enablement_to_all,
         name="organizers_leaderboard_design_apply_field_enablement_all"),  # POST ({field_id?} -> both sizes)
    path("leaderboard-designs/by-id/<int:design_id>/", views_leaderboard_design.design_item,
         name="organizers_leaderboard_design_item"),       # PATCH / DELETE
    # One-click "Create default AFC design" generator (owner 2026-07-04): builds a ready-to-use
    # design (AFC theme + standard columns) for a 12 / 15 / 24 team size preset. Listed BEFORE the
    # bare collection path so "create-default/" is not swallowed by it. See create_default_design.
    path("leaderboard-designs/create-default/", views_leaderboard_design.create_default_design,
         name="organizers_leaderboard_design_create_default"),  # POST (preset=12|15|24)
    path("leaderboard-designs/", views_leaderboard_design.designs_collection,
         name="organizers_leaderboard_designs"),           # GET (?organization_id=) / POST
    # Uploaded FONT library (TTF/OTF), org-scoped or AFC-native.
    # The /file/ sub-route (CORS-enabled font bytes for the browser FontFace preview) is listed
    # BEFORE the bare by-id route so it is matched first. See font_file() for the CORS rationale.
    path("leaderboard-fonts/by-id/<int:font_id>/file/", views_leaderboard_design.font_file,
         name="organizers_leaderboard_font_file"),         # GET (public, CORS) — font bytes for preview
    path("leaderboard-fonts/by-id/<int:font_id>/", views_leaderboard_design.font_item,
         name="organizers_leaderboard_font_item"),         # DELETE
    path("leaderboard-fonts/", views_leaderboard_design.fonts_collection,
         name="organizers_leaderboard_fonts"),             # GET (?organization_id=) / POST (upload)

    # ───────────────────────── Organizer blacklist (feature "organizer-blacklist") ─────────────────────────
    # An organizer blacklists a team for a duration; the team AND its snapshotted players (even
    # after they leave) cannot register for THAT organizer's events. The affected party (team
    # manager or player) can request a lift; the organizer approves/denies. Enforcement lives in
    # afc_organizers/blacklist.py, called from register_for_event. Spec:
    # WEBSITE/tasks/organizer-blacklist-design.md.
    # NOTE: the more-specific string routes ("mine", "lift-requests") are listed BEFORE the
    # generic <int:blacklist_id> routes so they are never swallowed by the int converter.
    # The AFFECTED-PARTY discovery view (NO org gate): a team/player lists the active blacklists
    # that affect THEM so they can request a lift. Backs the team page RequestBlacklistLift UI.
    path("blacklists/mine/", views_blacklist.my_blacklists,
         name="organizers_blacklists_mine"),
    path("blacklists/lift-requests/", views_blacklist.list_lift_requests,
         name="organizers_blacklist_lift_requests"),
    path("blacklists/lift-requests/<int:request_id>/decide/", views_blacklist.decide_lift_request,
         name="organizers_blacklist_decide_lift"),
    # ONE path, two verbs: POST creates a blacklist, GET lists the org's blacklists
    # (the view branches on request.method).
    path("blacklists/", views_blacklist.blacklists,
         name="organizers_blacklists"),
    path("blacklists/<int:blacklist_id>/lift/", views_blacklist.lift_blacklist,
         name="organizers_blacklist_lift"),
    path("blacklists/<int:blacklist_id>/request-lift/", views_blacklist.request_lift,
         name="organizers_blacklist_request_lift"),

    # ───────────────────────── Blacklist VISIBILITY (owner ask 2026-06-12) ─────────────────────────
    # Cross-org transparency on top of the blacklist feature (views_blacklist_lookup.py):
    #   - blacklist-lookup/   : ANY active org member (or platform admin) looks up one team or
    #     one player: how many times blacklisted, by which orgs, when - over an optional date
    #     window. Organizers never see reasons (owner privacy rule); platform admins do.
    #   - admin/blacklists/   : platform-admin-only dashboard feed of EVERY blacklist row
    #     (reasons included) with search/status/date filters + stat-card aggregates.
    path("blacklist-lookup/", views_blacklist_lookup.blacklist_lookup,
         name="organizers_blacklist_lookup"),
    path("admin/blacklists/", views_blacklist_lookup.admin_list_blacklists,
         name="organizers_admin_blacklists"),
    #   - admin/blacklist-counts/ : platform-admin-only BULK counts ({"<id>": {total, active}})
    #     for a page of team_ids OR user_ids - one call decorates the "Blacklists" column on
    #     the admin Teams & Players tables (owner ask 2026-06-13) instead of one lookup per row.
    path("admin/blacklist-counts/", views_blacklist_lookup.admin_blacklist_counts,
         name="organizers_admin_blacklist_counts"),

    # ───────────────────────── Public org page (unauthenticated) ─────────────────────────
    path("get-organization-public/<slug:slug>/", views_public.get_organization_public,
         name="organizers_public"),
    # Public organizer DIRECTORY — backs the new "Organizers" tab on the frontend
    # /tournaments page (app/(user)/tournaments/page.tsx). Lists active orgs that
    # have published events, with logo + derived event_count / verified / tier.
    path("get-organizations-public/", views_public.get_organizations_directory,
         name="organizers_public_directory"),
]
