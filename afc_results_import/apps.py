from django.apps import AppConfig


class AfcResultsImportConfig(AppConfig):
    """External tournament results import.

    Reads an organizer's published standings workbook into an AFC event: competitors (real teams or
    ghosts), stage and group membership, and results in either summed or per-match form.
    """
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_results_import"
