"""
afc_leaderboard.urls — route table for the Standalone Leaderboards endpoints.

Mounted at `leaderboards/` in afc/urls.py, so every path below resolves under
`leaderboards/standalone/…`. Order matters: the literal `standalone/matches/<mid>/…` routes are
declared BEFORE the `standalone/<lb_id>/…` routes so a numeric match id is never swallowed by the
leaderboard-id pattern.

Maps each URL to the function view in afc_leaderboard.views. See that module's header for the full
request/response shapes and which FE surface consumes each endpoint.
"""
from django.urls import path

from . import views

urlpatterns = [
    # ── collection ── (verb-suffixed paths, matching the repo's create-team/ edit-team/ idiom) ──
    path("standalone/", views.list_leaderboards, name="standalone_list"),            # GET (paginated)
    path("standalone/create/", views.create_leaderboard, name="standalone_create"),  # POST

    # ── match-scoped routes (declared before <lb_id> to avoid pattern collision) ──
    path("standalone/matches/<int:mid>/", views.delete_match, name="standalone_delete_match"),            # DELETE
    path("standalone/matches/<int:mid>/results/", views.save_match_results, name="standalone_save_results"),  # POST

    # ── item ──
    path("standalone/<int:lb_id>/", views.leaderboard_detail, name="standalone_detail"),     # GET
    path("standalone/<int:lb_id>/edit/", views.edit_leaderboard, name="standalone_edit"),    # PATCH
    path("standalone/<int:lb_id>/delete/", views.delete_leaderboard, name="standalone_delete"),  # DELETE

    # ── participants ──
    path("standalone/<int:lb_id>/participants/", views.add_participant, name="standalone_add_participant"),  # POST
    path("standalone/<int:lb_id>/participants/<int:pid>/", views.remove_participant, name="standalone_remove_participant"),  # DELETE

    # ── matches ──
    path("standalone/<int:lb_id>/matches/", views.add_match, name="standalone_add_match"),  # POST

    # ── result FILE upload ── parse the game's match-log export into the same review rows; the FE
    # applies the reviewed rows through the ocr/apply/ endpoint below (one apply pipeline).
    path("standalone/<int:lb_id>/results-file/", views.results_file_extract, name="standalone_results_file"),  # POST (multipart)

    # ── OCR assist (Phase 2, legacy single-shot) ── extract a screenshot, then apply the reviewed rows ──
    # Kept for backward compatibility; the FE now uses the async batch endpoints below (the single-shot
    # extract is synchronous and could time out on prod). Declared BEFORE the batch jobs/ routes is fine —
    # "ocr/" and "ocr/apply/" are exact, they never collide with "ocr/jobs/…".
    path("standalone/<int:lb_id>/ocr/", views.ocr_extract, name="standalone_ocr_extract"),  # POST (multipart)
    path("standalone/<int:lb_id>/ocr/apply/", views.ocr_apply, name="standalone_ocr_apply"),  # POST

    # ── OCR BATCH (Phase 2.6, async multi-image) ── upload many maps × many images, read in the background ──
    # run-all/ is declared BEFORE jobs/<job_id>/ so "run-all" is never parsed as a job id.
    path("standalone/<int:lb_id>/ocr/jobs/", views.ocr_job_list, name="standalone_ocr_job_list"),       # GET (poll)
    path("standalone/<int:lb_id>/ocr/jobs/create/", views.ocr_job_create, name="standalone_ocr_job_create"),  # POST (multipart)
    path("standalone/<int:lb_id>/ocr/run-all/", views.ocr_run_all, name="standalone_ocr_run_all"),      # POST
    path("standalone/<int:lb_id>/ocr/jobs/<uuid:job_id>/run/", views.ocr_job_run, name="standalone_ocr_job_run"),    # POST
    path("standalone/<int:lb_id>/ocr/jobs/<uuid:job_id>/apply/", views.ocr_job_apply, name="standalone_ocr_job_apply"),  # POST
    path("standalone/<int:lb_id>/ocr/jobs/<uuid:job_id>/", views.ocr_job_delete, name="standalone_ocr_job_delete"),  # DELETE
]
