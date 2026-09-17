"""
afc_support/urls.py - the support desk's addresses, mounted at "support/" in afc/urls.py.

Public (no token):        contact/, t/<token>/, t/<token>/reply/
Staff (support role):     tickets/, tickets/<number>/, tickets/<number>/reply/, tickets/<number>/status/
Head admin only:          audit/
Either:                   attachments/<id>/   (staff session, or ?t=<ticket token>)

Every view is documented in afc_support/views.py, including which frontend surface calls it.
"""
from django.urls import path

from afc_support.views import (
    support_access,
    support_attachment,
    support_audit,
    support_contact,
    support_thread,
    support_thread_reply,
    support_ticket_detail,
    support_ticket_reply,
    support_ticket_status,
    support_tickets,
)

urlpatterns = [
    # ── public ──
    path("contact/", support_contact, name="support_contact"),
    path("t/<str:token>/", support_thread, name="support_thread"),
    path("t/<str:token>/reply/", support_thread_reply, name="support_thread_reply"),
    # ── staff ──
    path("access/", support_access, name="support_access"),
    path("tickets/", support_tickets, name="support_tickets"),
    path("tickets/<str:number>/", support_ticket_detail, name="support_ticket_detail"),
    path("tickets/<str:number>/reply/", support_ticket_reply, name="support_ticket_reply"),
    path("tickets/<str:number>/status/", support_ticket_status, name="support_ticket_status"),
    # ── head admin ──
    path("audit/", support_audit, name="support_audit"),
    # ── either ──
    path("attachments/<int:attachment_id>/", support_attachment, name="support_attachment"),
]
