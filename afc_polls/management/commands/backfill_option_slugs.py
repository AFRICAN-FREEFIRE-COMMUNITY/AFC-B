"""
manage.py backfill_option_slugs - give every existing PollOption its address slug.

WHY IT IS NEEDED
    PollOption.slug was added for the themed awards pages (inbox #419), which address a nominee as
    /awards/<edition>/<award>/<nominee>. Options written from now on get a slug in
    PollOption.save(); this fills the rows that predate the column. Until it has run, the
    serializers fall back to afc_polls.models.option_slug() (a plain slugify of the label), so no
    link is broken in between.

IDEMPOTENT
    An option that already has a slug is skipped, never regenerated: a published slug is a bookmark.

CONNECTS TO
    afc_polls.models.PollOption.ensure_slug. Read by afc_polls.views (`slug` on every option) and
    by frontend app/(user)/awards/[edition]/[award]/[nominee].
"""
from django.core.management.base import BaseCommand

from afc_polls.models import PollOption, PollQuestion


class Command(BaseCommand):
    help = "Fill PollOption.slug for rows created before the column existed."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would change and write nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        filled = 0
        for question in PollQuestion.objects.all().prefetch_related("options"):
            taken = {option.slug for option in question.options.all() if option.slug}
            for option in question.options.all():
                if option.slug:
                    continue
                candidate = PollOption(pk=option.pk, question_id=question.pk, label=option.label, order=option.order)
                slug = candidate.ensure_slug(taken=taken)
                taken.add(slug)
                self.stdout.write(f"{question.slug} / {option.label!r} -> {slug}")
                if not dry_run:
                    PollOption.objects.filter(pk=option.pk).update(slug=slug)
                filled += 1
        verb = "would fill" if dry_run else "filled"
        self.stdout.write(self.style.SUCCESS(f"{verb} {filled} option slug(s)."))
