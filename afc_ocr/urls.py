from django.urls import path
from . import views

urlpatterns = [
    path("ocr-match-result/",                    views.upload_ocr_session,    name="ocr_upload"),
    path("ocr-from-image/",                      views.ocr_from_stored_image, name="ocr_from_image"),
    path("ocr-sessions/",                        views.list_ocr_sessions,     name="ocr_sessions_list"),
    path("ocr-session/<uuid:session_id>/",       views.ocr_session_detail,    name="ocr_session_detail"),
    path("ocr-session/<uuid:session_id>/commit/",views.commit_ocr_session,    name="ocr_session_commit"),

    # ── OCR MLOps control surface (all under /events/ocr/, admin/staff-gated) ──────
    # These power the "OCR Model" admin dashboard (model-stats render + Promote/Rollback
    # buttons + Download-dataset button) AND the off-box train_cycle.py trainer
    # (retrain-status poll, dataset-export download, upload-model push). See the
    # "OCR MLOps ENDPOINTS" section header in views.py for the full consumer map.
    path("ocr/model-stats/",     views.ocr_model_stats,     name="ocr_model_stats"),      # dashboard render
    path("ocr/dataset-export/",  views.ocr_dataset_export,  name="ocr_dataset_export"),   # train_cycle.py + dashboard download
    path("ocr/retrain-status/",  views.ocr_retrain_status,  name="ocr_retrain_status"),   # train_cycle.py poll
    path("ocr/upload-model/",    views.ocr_upload_model,    name="ocr_upload_model"),      # train_cycle.py push trained bundle
    path("ocr/promote/",         views.ocr_promote_model,   name="ocr_promote_model"),     # dashboard Promote button
    path("ocr/rollback/",        views.ocr_rollback_model,  name="ocr_rollback_model"),    # dashboard Rollback button
]
