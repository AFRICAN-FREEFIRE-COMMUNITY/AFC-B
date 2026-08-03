from django.apps import AppConfig


class AfcFeedbackConfig(AppConfig):
    """Always-on, REUSABLE site feedback (owner backlog item 29, 2026-08-03).

    Not a single hardcoded form: a FeedbackForm carries its own ordered FeedbackFields, so a second
    purpose (a post-tournament survey, a shop NPS prompt) is a data row rather than a code change.
    See models.py for the shape and views.py for the endpoints."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_feedback"
