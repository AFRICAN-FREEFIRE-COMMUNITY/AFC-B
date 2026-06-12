"""
afc_sponsors.urls — route table for the sponsor-system P1 endpoints.

Mounted at `sponsors/` in afc/urls.py. Literal routes (mine/, create/) are declared before the
<int:sponsor_id> patterns so they are never swallowed. See views.py's header for request/response
shapes and which FE surface consumes each endpoint.
"""
from django.urls import path

from . import views

urlpatterns = [
    # ── portal (member-scoped) ──
    path("mine/", views.my_sponsors, name="sponsors_mine"),  # GET

    # ── admin collection ──
    path("create/", views.create_sponsor, name="sponsors_create"),  # POST
    path("", views.list_sponsors, name="sponsors_list"),            # GET (paginated)

    # ── admin item ──
    path("<int:sponsor_id>/", views.sponsor_detail, name="sponsors_detail"),      # GET
    path("<int:sponsor_id>/edit/", views.edit_sponsor, name="sponsors_edit"),     # PATCH

    # ── members ──
    path("<int:sponsor_id>/members/add/", views.add_member, name="sponsors_add_member"),  # POST
    path("<int:sponsor_id>/members/<int:member_id>/", views.remove_member, name="sponsors_remove_member"),  # DELETE

    # ── event attachment + portal reads ──
    path("<int:sponsor_id>/events/attach/", views.attach_event, name="sponsors_attach_event"),  # POST
    path("<int:sponsor_id>/events/", views.sponsor_events, name="sponsors_events"),             # GET
    path("<int:sponsor_id>/events/<int:event_id>/submissions/", views.event_submissions, name="sponsors_event_submissions"),  # GET (+?csv=1)
    path("<int:sponsor_id>/events/<int:event_id>/", views.detach_event, name="sponsors_detach_event"),  # DELETE
]
