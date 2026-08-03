"""
afc_feedback.models - ALWAYS-ON, REUSABLE site feedback (owner backlog item 29, 2026-08-03).

WHY THIS APP EXISTS
    The owner asked for a feedback mechanism that is ALWAYS available and REUSABLE "for different
    purposes" (reference: a Google Form). The reusable half is the load-bearing requirement, so this
    is deliberately NOT a single hardcoded "rating + comment" table. A form is DATA:

        FeedbackForm  ── has ordered ──▶  FeedbackField  (text | textarea | choice | rating)
              │
              └────── receives ────────▶  FeedbackSubmission (answers stored as {field_key: value})

    Adding a second purpose later (a post-tournament survey, a shop NPS prompt, a beta-feature poll)
    is then a row insert plus a `key` handed to the frontend widget, not a migration and a redeploy.

WHY answers ARE A JSON BLOB AND NOT A ROW PER ANSWER
    A per-answer table would be the textbook-normalized shape, but every read here is "show me this
    whole submission" and every write is "save this whole submission" - there is no query that
    filters or aggregates across a single answer value. A JSONField keeps a submission to ONE row and
    ONE query, and `fields_snapshot` (below) preserves the questions as they were worded at the time,
    which a normalized table would NOT give us for free once someone edits a label.

ANONYMOUS BY DESIGN
    `user` is nullable. The owner wants feedback from people who are not logged in (that is often the
    feedback that matters most: someone who could not sign up). The submit endpoint therefore accepts
    unauthenticated calls, which is exactly why it is rate limited - see views._rate_limit_ok.

HOW IT CONNECTS
    - Users: afc_auth.User via FeedbackSubmission.user (SET_NULL, so deleting an account keeps the
      feedback but drops the attribution) and .handled_by (the admin who triaged it).
    - Read/written by afc_feedback.views; routes mounted at `feedback/` in afc/urls.py.
    - Frontend: the always-on Footer entry point (components/feedback/FeedbackLauncher.tsx ->
      FeedbackDialog.tsx) posts submissions; app/(a)/a/feedback/page.tsx reads and triages them.
    - Seeded by `python manage.py seed_feedback_forms`, which creates the default "site_feedback"
      form (rating + comment + optional contact) so the widget works the moment the app is deployed.
"""
from django.conf import settings
from django.db import models


class FeedbackForm(models.Model):
    """ONE feedback form. The reusable unit: the widget asks for a form by `key`, renders whatever
    fields that form declares, and posts the answers back against the same key.

    `key` is the stable public handle used in URLs (feedback/forms/<key>/) and hardcoded in whichever
    frontend surface opens the form, so it must not change once a form is live. The title and
    description are what the user reads at the top of the dialog."""

    id = models.AutoField(primary_key=True)
    # Public, URL-safe handle: "site_feedback", "post_event_survey", ... SlugField so a key can never
    # contain a character that would need escaping in the route.
    key = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    # The always-on flag. An inactive form is invisible to the public schema endpoint AND refuses
    # submissions (checked server-side, not just hidden in the UI, so a stale open tab cannot post to
    # a form the owner has retired). Existing submissions are untouched and stay readable in admin.
    is_active = models.BooleanField(default=True)
    # Optional closing line shown after a successful submit ("Thanks, we read every message").
    # Blank falls back to a generic translated string on the frontend.
    thank_you_message = models.CharField(max_length=240, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="feedback_forms_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} ({'active' if self.is_active else 'inactive'})"


class FeedbackField(models.Model):
    """One question on a form. `order` gives the ordered set the brief asked for.

    The four types cover what a feedback form needs without turning into a form builder:
      text     - single-line answer (a name, an email, a URL)
      textarea - the free-text comment, the field that actually carries the signal
      choice   - pick one of `options` (renders as a select or a radio group)
      rating   - an integer 1..max_rating (renders as stars)
    Anything more exotic is a new type here plus a branch in the frontend renderer, deliberately."""

    TEXT = "text"
    TEXTAREA = "textarea"
    CHOICE = "choice"
    RATING = "rating"
    FIELD_TYPES = [
        (TEXT, "Short text"),
        (TEXTAREA, "Long text"),
        (CHOICE, "Choice"),
        (RATING, "Rating"),
    ]

    id = models.AutoField(primary_key=True)
    form = models.ForeignKey(FeedbackForm, on_delete=models.CASCADE, related_name="fields")
    # Stable machine name. This is the key inside FeedbackSubmission.answers, so renaming it orphans
    # the answers already stored under the old name - change the LABEL instead, never the key.
    key = models.SlugField(max_length=64)
    label = models.CharField(max_length=200)
    field_type = models.CharField(max_length=16, choices=FIELD_TYPES, default=TEXTAREA)
    # Enforced server-side in views._validate_answers, not merely marked in the UI.
    required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    # Greyed-out hint inside the input. Optional.
    placeholder = models.CharField(max_length=200, blank=True, default="")
    # Small explanatory line under the label. Optional.
    help_text = models.CharField(max_length=300, blank=True, default="")
    # CHOICE only: ["Bug", "Idea", "Something else"]. Ignored by the other three types.
    options = models.JSONField(default=list, blank=True)
    # RATING only: the top of the scale. 5 gives the familiar five stars.
    max_rating = models.PositiveSmallIntegerField(default=5)
    # TEXT/TEXTAREA only: hard cap, also enforced server-side so a scripted client cannot post a
    # megabyte of text into the answers blob.
    max_length = models.PositiveIntegerField(default=2000)

    class Meta:
        ordering = ["order", "id"]
        # A field key must be unique WITHIN its form (two different forms may both have "comment").
        unique_together = ("form", "key")

    def __str__(self):
        return f"{self.form.key}.{self.key}"


class FeedbackSubmission(models.Model):
    """One filled-in form.

    CONTEXT IS THE POINT: `page_path` records where the user was standing when they opened the
    widget. The brief calls it the single most useful piece of context for acting on feedback, and it
    is right - "confusing" is noise, "confusing, sent from /tournaments/dynasty-cup/register" is a
    bug report."""

    OPEN = "open"
    HANDLED = "handled"
    STATUS_CHOICES = [(OPEN, "Open"), (HANDLED, "Handled")]

    id = models.AutoField(primary_key=True)
    form = models.ForeignKey(FeedbackForm, on_delete=models.CASCADE, related_name="submissions")
    # NULL for an anonymous visitor, and NULL again if the account is later deleted. Never required.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="feedback_submissions",
    )
    # {field_key: value}. Values are str for text/textarea/choice and int for rating; shape is
    # enforced by views._validate_answers before anything is written.
    answers = models.JSONField(default=dict)
    # The page the feedback was sent from, e.g. "/tournaments/dynasty-cup". Path only, no origin and
    # no query string (stripped in the view: a query string can carry tokens we must not store).
    page_path = models.CharField(max_length=300, blank=True, default="")
    # The questions EXACTLY as worded when this was submitted: [{key, label, field_type}]. Without
    # this, editing a label later silently rewrites the history of every past answer.
    fields_snapshot = models.JSONField(default=list, blank=True)
    # Which language the user was reading the site in when they wrote this. Tells the admin whether a
    # reply should go out in French or Portuguese.
    locale = models.CharField(max_length=8, blank=True, default="")
    # Truncated. Useful for reproducing a layout complaint ("the button is off screen").
    user_agent = models.CharField(max_length=300, blank=True, default="")
    # Rate limiting needs to recognise a repeat sender, but a raw IP on a feedback row is PII we have
    # no use for. We store a salted HASH: enough to spot one abusive source, useless for tracking.
    # See views._client_ip_hash.
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # ── triage ──
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="feedback_submissions_handled",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    # Internal note from the admin who triaged it. Never shown to the submitter.
    admin_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # Newest first: the admin queue is read top-down.
        ordering = ["-created_at"]
        indexes = [
            # The admin list filters by form and by status and always sorts by recency.
            models.Index(fields=["form", "status", "-created_at"]),
        ]

    def __str__(self):
        who = self.user.username if self.user else "anonymous"
        return f"{self.form.key} from {who} ({self.status})"
