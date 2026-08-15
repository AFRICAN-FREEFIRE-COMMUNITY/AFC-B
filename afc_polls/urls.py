"""
afc_polls.urls - route table for the poll engine.

Mounted at `polls/` in afc/urls.py. The literal `admin/` routes are declared BEFORE the
`<slug:slug>/` patterns so an admin route can never be swallowed by a poll whose slug happens to
be "admin". See views.py's header for request/response shapes and which frontend surface consumes
each endpoint.
"""
from django.urls import path

from . import views

urlpatterns = [
    # ── admin (declared first: literal prefix before the <slug:slug> patterns) ──
    path("admin/polls/", views.admin_polls, name="polls_admin_list_create"),           # GET, POST
    path("admin/polls/<slug:slug>/", views.admin_poll_detail,
         name="polls_admin_detail"),                                          # GET, PATCH, DELETE
    path("admin/polls/<slug:slug>/questions/", views.admin_save_questions,
         name="polls_admin_questions"),                                                      # PUT
    path("admin/polls/<slug:slug>/results/", views.admin_results,
         name="polls_admin_results"),                                                        # GET
    path("admin/polls/<slug:slug>/publish-winner/", views.admin_publish_winner,
         name="polls_admin_publish_winner"),                                                # POST
    path("admin/polls/<slug:slug>/announce/", views.admin_announce,
         name="polls_admin_announce"),                                                      # POST
    path("admin/editions/", views.admin_editions, name="polls_admin_editions"),        # GET, POST
    path("admin/editions/<slug:slug>/", views.admin_edition_detail,
         name="polls_admin_edition_detail"),                                          # PATCH, DELETE

    # ── awards editions, public. Declared before <slug:slug>/ for the same reason `admin/` is:
    #    a poll whose slug happened to be "editions" would otherwise swallow the route ──
    path("editions/", views.list_editions, name="polls_editions"),                           # GET
    path("editions/<slug:slug>/", views.edition_detail, name="polls_edition_detail"),        # GET
    path("watch/", views.watch, name="polls_watch"),                                # POST, DELETE

    # ── public ──
    path("", views.list_polls, name="polls_list"),                                           # GET
    path("<slug:slug>/", views.poll_detail, name="polls_detail"),                            # GET
    path("<slug:slug>/responses/", views.submit_response, name="polls_submit"),             # POST
    path("<slug:slug>/team-answer/", views.captain_override, name="polls_captain_override"),
]
