"""
afc_feedback.urls - route table for the always-on site feedback form (backlog item 29).

Mounted at `feedback/` in afc/urls.py. The literal `admin/` routes are declared BEFORE the
`forms/<slug:key>/` patterns so an admin route can never be swallowed by a form key. See views.py's
header for request/response shapes and which frontend surface consumes each endpoint.
"""
from django.urls import path

from . import views

urlpatterns = [
    # ── admin triage (declared first: literal prefix before the <slug:key> patterns) ──
    path("admin/forms/", views.admin_list_forms, name="feedback_admin_forms"),                # GET
    path("admin/submissions/", views.admin_list_submissions,
         name="feedback_admin_submissions"),                                                  # GET
    path("admin/submissions/<int:submission_id>/", views.admin_update_submission,
         name="feedback_admin_update_submission"),                                            # PATCH

    # ── public: the widget reads the schema, then posts the answers ──
    path("forms/<slug:key>/", views.form_schema, name="feedback_form_schema"),                # GET
    path("forms/<slug:key>/submit/", views.submit_feedback, name="feedback_submit"),          # POST
]
