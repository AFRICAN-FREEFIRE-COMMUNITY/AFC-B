"""
afc_partner_apply.urls - route table for the public partner application queue.

Mounted at `partner-apply/` in afc/urls.py. The literal `admin/` routes are declared BEFORE the
`applications/<reference>/` patterns, the same ordering rule as afc_feedback/urls.py and
afc_sso/urls.py, so an admin route can never be swallowed by something shaped like a reference.

See views_public.py and views_admin.py for each endpoint's request and response shapes and which
frontend surface consumes it.
"""
from django.urls import path

from . import views_admin, views_public

urlpatterns = [
    # ── AFC staff: the review queue (head_admin / partner_admin) ──
    path("admin/applications/", views_admin.list_applications,
         name="partner_apply_admin_list"),                                            # GET
    path("admin/applications/<int:application_id>/", views_admin.application_detail,
         name="partner_apply_admin_detail"),                                          # GET
    path("admin/applications/<int:application_id>/decide/", views_admin.decide_application,
         name="partner_apply_admin_decide"),                                          # POST
    path("admin/applications/<int:application_id>/resend-credentials/",
         views_admin.resend_credentials,
         name="partner_apply_admin_resend"),                                          # POST

    # ── Public: the organisation applying, tracking, fixing and collecting ──
    path("applications/", views_public.submit_application,
         name="partner_apply_submit"),                                                # POST
    path("applications/<str:reference>/", views_public.application_status,
         name="partner_apply_status"),                                                # GET, PATCH
    path("applications/<str:reference>/claim/", views_public.claim_credentials,
         name="partner_apply_claim"),                                                 # POST

    # The integration guide, ungated (owner 2026-08-05). Declared AFTER the literal routes above
    # for the same ordering reason as the admin block: "integration-guide" is not shaped like a
    # reference, but keeping every literal path together is what stops the next one being
    # swallowed by applications/<reference>/.
    path("integration-guide/", views_public.integration_guide,
         name="partner_apply_guide"),                                                 # GET
]
