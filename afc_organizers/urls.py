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

from . import views_admin, views_organizer, views_public

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

    # ───────────────────────── Public org page (unauthenticated) ─────────────────────────
    path("get-organization-public/<slug:slug>/", views_public.get_organization_public,
         name="organizers_public"),
]
