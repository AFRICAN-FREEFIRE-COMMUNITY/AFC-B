"""
afc_results_import.urls - mounted at "results-import/" by afc/urls.py.

Four endpoints, in the order an admin uses them:

    GET  results-import/template/   download a workbook pre-filled with this event's structure
    POST results-import/preview/    upload it back, see what WOULD happen, nothing written
    POST results-import/commit/     write it
    POST results-import/pair/       fix what a name means, then re-run the import

Consumed by the admin Results Import screen.
"""
from django.urls import path

from .views import (
    commit_results_import,
    pair_result_team,
    results_import_settings,
    preview_results_import,
    results_import_template,
)

urlpatterns = [
    path("template/", results_import_template, name="results_import_template"),
    path("preview/", preview_results_import, name="results_import_preview"),
    path("commit/", commit_results_import, name="results_import_commit"),
    path("pair/", pair_result_team, name="results_import_pair"),
    # GET = read the four switches, POST = update any subset. Separate from commit/ on purpose:
    # an admin revisits these decisions long after the file was imported.
    path("settings/", results_import_settings, name="results_import_settings"),
]
