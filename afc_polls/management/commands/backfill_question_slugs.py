"""
manage.py backfill_question_slugs - give every existing PollQuestion its anchor slug.

WHY IT IS NEEDED
    PollQuestion.slug was added after the NFCA 2025 ballots were imported, so those 28 rows carry
    "". The slug is what makes /awards/2025#best-esports-player a link that survives a reorder, and
    what polls-spec 7.3 promises when it says the old #content-creators anchors keep working.

    Every question written from now on gets one in PollQuestion.save(). This is only for the rows
    that predate the column.

IDEMPOTENT
    A question that already has a slug is skipped, never regenerated. A published slug is a
    bookmark: renaming a category must not break a link that names it.

THE ONE INTERESTING CASE
    The 2025 content-creators ballot holds "Favorite DUO (Male)" and "Favorite DUO (MALE)", which
    slugify to the same string. `ensure_slug` suffixes the second one, so they become
    `favorite-duo-male` and `favorite-duo-male-2`. That is correct behaviour on data that is itself
    a known duplicate (awards-grand-design.md, finding 2: somebody split one category in two so
    both names would display). It is worth an owner deciding whether to merge them, and it is NOT
    this command's job to decide that.

CONNECTS TO
    afc_polls.models.PollQuestion.ensure_slug. Read by afc_polls.views (`slug` on every serialised
    question) and used as the anchor by frontend/app/(user)/awards/.
"""
from django.core.management.base import BaseCommand

from afc_polls.models import Poll, PollQuestion


class Command(BaseCommand):
    help = "Fill PollQuestion.slug for rows created before the column existed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would change and write nothing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        filled = 0

        for poll in Poll.objects.all().prefetch_related("questions"):
            # Slugs already handed out on THIS poll, so a freshly generated one cannot collide
            # with a sibling that has not been written yet.
            taken = {
                question.slug for question in poll.questions.all() if question.slug
            }
            for question in poll.questions.all():
                if question.slug:
                    continue
                # Work on a detached copy under dry-run so nothing is mutated in memory either.
                candidate = PollQuestion(
                    pk=question.pk, poll_id=poll.pk, prompt=question.prompt, order=question.order,
                )
                slug = candidate.ensure_slug(taken=taken)
                taken.add(slug)
                self.stdout.write(f"{poll.slug} / {question.prompt!r} -> {slug}")
                if not dry_run:
                    PollQuestion.objects.filter(pk=question.pk).update(slug=slug)
                filled += 1

        verb = "would fill" if dry_run else "filled"
        self.stdout.write(self.style.SUCCESS(f"{verb} {filled} question slug(s)."))
