from django.urls import path
from . import views

urlpatterns = [
    path("ocr-match-result/",                    views.upload_ocr_session,  name="ocr_upload"),
    path("ocr-sessions/",                        views.list_ocr_sessions,   name="ocr_sessions_list"),
    path("ocr-session/<uuid:session_id>/",       views.ocr_session_detail,  name="ocr_session_detail"),
    path("ocr-session/<uuid:session_id>/commit/",views.commit_ocr_session,  name="ocr_session_commit"),
]
