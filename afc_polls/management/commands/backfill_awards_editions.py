"""
manage.py backfill_awards_editions - give every award poll a real AwardsEdition row.

WHY THIS IS A COMMAND AND NOT A DATA MIGRATION
    Migrations are gitignored in this repo and generated on the server, so a data migration cannot
    be reviewed before it runs against production. A command can be run, read, and run again: it is
    idempotent, it prints what it is about to do, and `--dry-run` shows the plan without writing.

WHAT IT DOES
    `Poll.awards_edition` is a free-text label ("NFCA 2025") that groups award ballots. The grand
    awards surface needs an edition ROW, because a marquee, a countdown, a phase timeline and a
    "you have voted in 12 of 28" progress bar are all properties of the season rather than of any
    one ballot. This creates one AwardsEdition per distinct label and links the polls to it.

    The label column is NOT cleared. It stays as the fallback for a poll whose edition row does not
    exist yet, and dropping a populated column in a repo that generates its migrations on the
    server is a needless way to lose data.

DATES ARE NOT INVENTED
    An imported 2025 ballot has no opens_at, because nobody recorded when its voting ran (see
    afc_polls/management/commands/import_awards_winners.py). So this command leaves every date on
    a created edition NULL and sets `status` explicitly instead: an edition whose polls all carry a
    published winner is WINNERS, and anything else is left on AUTO for an admin to fill in.
    Guessing "voting closed on the day the row was created" would put a wrong date under a
    countdown, which is worse than no countdown.

CONNECTS TO
    afc_polls.models.AwardsEdition / Poll.edition, read by afc_polls.views.edition_detail and
    rendered by frontend/app/(user)/awards/.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from afc_polls.models import AwardsEdition, Poll, PollQuestion


class Command(BaseCommand):
    help = "Create an AwardsEdition per distinct Poll.awards_edition label and link the polls."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would change and write nothing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        labels = sorted({
            label for label in Poll.objects.filter(kind=Poll.AWARD)
            .exclude(awards_edition="")
            .values_list("awards_edition", flat=True)
        })
        if not labels:
            self.stdout.write("No award polls carry an edition label. Nothing to do.")
            return

        created, linked = 0, 0
        for label in labels:
            polls = list(Poll.objects.filter(kind=Poll.AWARD, awards_edition=label))
            edition = AwardsEdition.objects.filter(title=label).first()
            if not edition:
                edition = AwardsEdition.objects.filter(slug=slugify(label)[:120]).first()

            if not edition:
                # Every question published means the season is over and its winners are up, which
                # is exactly the NFCA 2025 case. Anything else stays on AUTO so the dates an admin
                # fills in later are what drives the page.
                question_count = PollQuestion.objects.filter(poll__in=polls).count()
                published = PollQuestion.objects.filter(
                    poll__in=polls, published_winner_option__isnull=False
                ).count()
                edition_status = (
                    AwardsEdition.WINNERS
                    if question_count and published == question_count
                    else AwardsEdition.AUTO
                )
                year = _year_from(label)
                self.stdout.write(
                    f"CREATE edition {slugify(label)!r} for {len(polls)} poll(s), "
                    f"status={edition_status}, year={year}"
                )
                if not dry_run:
                    with transaction.atomic():
                        edition = AwardsEdition.objects.create(
                            slug=_unique_slug(slugify(label)[:120] or "awards"),
                            title=label,
                            year=year,
                            status=edition_status,
                        )
                created += 1

            to_link = [poll for poll in polls if poll.edition_id != getattr(edition, "pk", None)]
            if to_link:
                self.stdout.write(f"  LINK {len(to_link)} poll(s) to {label!r}")
                if not dry_run and edition:
                    Poll.objects.filter(poll_id__in=[poll.pk for poll in to_link]).update(
                        edition=edition
                    )
                linked += len(to_link)

        verb = "would create" if dry_run else "created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {created} edition(s), linked {linked} poll(s)."
        ))


def _year_from(label):
    """The four-digit year inside a label like "NFCA 2025", or None.

    None rather than a guess: an edition with no year in its name is a real possibility, and a
    wrong year on an archive switcher is worse than a missing one.
    """
    for token in label.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None


def _unique_slug(base):
    slug, counter = base, 2
    while AwardsEdition.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug
