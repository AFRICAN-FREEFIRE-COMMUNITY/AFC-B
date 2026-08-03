"""
seed_feedback_forms - create (or repair) the default "site_feedback" form.

WHY  The widget in the site footer asks the backend for the form with key "site_feedback". Without
     that row the footer link opens onto a 404, so the feature would need a manual data step before
     it worked anywhere. This command is that step, and it is IDEMPOTENT, so it is safe to run on
     every deploy: existing forms are left alone, only missing pieces are created.

USE  python manage.py seed_feedback_forms
     python manage.py seed_feedback_forms --reset-fields   # restore the default questions

THE DEFAULT FORM  Deliberately three questions, per the brief ("keep the first form seeded and
     simple, so it works out of the box"): a rating, a free-text comment, and an optional contact
     field. Only the comment is required, because a rating with no words is nearly useless and a
     contact address must always stay optional for an anonymous sender.

ADDING A SECOND FORM  Do NOT extend this command. Create the FeedbackForm + FeedbackField rows (via
     the Django admin or a shell), then point a frontend surface at the new key. That is the whole
     reason the form is data rather than code.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from afc_feedback.models import FeedbackForm, FeedbackField

# The key the footer widget asks for. Changing this orphans the widget, so it is a constant.
DEFAULT_FORM_KEY = "site_feedback"

DEFAULT_FIELDS = [
    {
        "key": "rating",
        "label": "How is your experience on AFC so far?",
        "field_type": FeedbackField.RATING,
        "required": False,
        "order": 1,
        "max_rating": 5,
        "help_text": "",
    },
    {
        "key": "comment",
        "label": "What would you like to tell us?",
        "field_type": FeedbackField.TEXTAREA,
        "required": True,
        "order": 2,
        "placeholder": "Tell us what worked, what did not, or what you wish existed.",
        "help_text": "",
        "max_length": 2000,
    },
    {
        "key": "contact",
        "label": "Email or username, if you would like a reply",
        "field_type": FeedbackField.TEXT,
        "required": False,
        "order": 3,
        "placeholder": "you@example.com",
        "help_text": "Optional. Leave this blank to stay anonymous.",
        "max_length": 200,
    },
]


class Command(BaseCommand):
    help = "Create the default site_feedback form and its fields (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-fields",
            action="store_true",
            help="Delete and recreate the default form's fields. Submissions are NOT touched: their "
                 "answers and fields_snapshot survive, since the snapshot is what past submissions "
                 "are rendered from.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        form, created = FeedbackForm.objects.get_or_create(
            key=DEFAULT_FORM_KEY,
            defaults={
                "title": "Send us feedback",
                "description": "Found a bug, or have an idea? Tell us. We read everything.",
                "thank_you_message": "Thanks. Your feedback goes straight to the AFC team.",
                "is_active": True,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(f"Created form '{DEFAULT_FORM_KEY}'.")
            if created
            else f"Form '{DEFAULT_FORM_KEY}' already exists, left as is."
        )

        if options["reset_fields"]:
            form.fields.all().delete()
            self.stdout.write("Cleared existing fields (--reset-fields).")

        added = 0
        for spec in DEFAULT_FIELDS:
            # get_or_create on (form, key) so re-running never duplicates a question and never
            # overwrites a label an admin has since reworded.
            _, field_created = FeedbackField.objects.get_or_create(
                form=form, key=spec["key"], defaults={k: v for k, v in spec.items() if k != "key"}
            )
            added += 1 if field_created else 0

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {added} field(s) created, {form.fields.count()} total on '{DEFAULT_FORM_KEY}'."
            )
        )
