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

    # ── OCR assist (Phase 2) ── extract a screenshot into a draft, then apply the reviewed rows ──
    path("standalone/<int:lb_id>/ocr/", views.ocr_extract, name="standalone_ocr_extract"),  # POST (multipart)
    path("standalone/<int:lb_id>/ocr/apply/", views.ocr_apply, name="standalone_ocr_apply"),  # POST
]
