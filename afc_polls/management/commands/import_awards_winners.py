"""
PHASE 0 of the polls build: import the published NFCA 2025 award winners into afc_polls.

WHY THIS COMMAND EXISTS, AND WHY IT RUNS BEFORE ANYTHING ELSE
    The published 2025 winners do NOT live in the database. They live in a hardcoded
    `MANUAL_WINNERS` array inside `frontend/app/(user)/awards/page.tsx`, rendered straight into
    the page. Replacing or deleting that page before these rows exist deletes the published
    results from the live site, and they cannot be rebuilt from the `afc_awards.Vote` table,
    because the two may legitimately disagree: the vote-count validation that would have kept
    them in step (`afc_awards/views.py:352-356`) has been COMMENTED OUT since before those votes
    were cast. See WEBSITE/tasks/polls-spec.md section 7.2 trap 2.

    So: THE PAGE FILE IS THE SOURCE OF TRUTH for the published winners. Nothing else is.

WHY IT PARSES THE .tsx INSTEAD OF CARRYING A PYTHON COPY OF THE DATA
    A transcribed copy is a second source of truth, and the whole point of this command is that
    there is exactly one. Parsing the file means the import cannot silently disagree with what the
    site shows, and `--verify` can re-read the file afterwards and prove row by row that it does
    not. The parse is deliberately strict: it reads only the ACTIVE `const MANUAL_WINNERS` block,
    drops commented-out lines, and refuses to guess at anything it cannot match.

WHAT IT WRITES (and nothing else, per spec section 8 "Phase 0")
    One Poll per section of the file, kind='award', awards_edition='NFCA 2025':
        content-creators -> slug nfca-2025-content-creators
        esports-awards   -> slug nfca-2025-esports
    (the two slugs the old `#content-creators` / `#esports-awards` anchors redirect onto, spec 7.3)
    One PollQuestion per award category, one PollOption for the winner, and the published claim
    itself on the question: published_winner_option + published_winner_votes.

WHAT IT DOES NOT WRITE
    No losing nominees (the file does not record them), no PollResponse, no PollAnswer, no vote
    migration. Those are a later phase reading afc_awards.Vote. This command imports the published
    RESULT, not the ballot, because the result is the thing that is currently at risk.

IDEMPOTENT: keyed on (poll slug, question order), so running it twice updates in place and never
    duplicates. Safe to re-run after editing the page file.

HOW IT CONNECTS
    Writes afc_polls.models Poll / PollQuestion / PollOption. Read back by the public
    /polls/<slug> endpoint (afc_polls/views.py) and rendered by
    frontend/app/(user)/polls/[slug]/page.tsx, which is what replaces the hardcoded array.

USAGE
    python manage.py import_awards_winners --dry-run     # parse and print, write nothing
    python manage.py import_awards_winners               # import
    python manage.py import_awards_winners --verify      # re-read the file, compare against the DB
"""
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from afc_polls.models import Poll, PollOption, PollQuestion

# The edition label that groups these two polls together in the Awards section of /polls.
AWARDS_EDITION = "NFCA 2025"

# Stamped on every question this command writes, so a reader a year from now can tell that the
# number beside the winner was TRANSCRIBED from a page file rather than counted from votes.
PUBLISHED_RESULT_SOURCE = "awards_page_manual_winners"

# The old tab anchors map onto these slugs (spec 7.3). Changing one breaks a bookmark.
SECTION_SLUGS = {
    "content-creators": "nfca-2025-content-creators",
    "esports-awards": "nfca-2025-esports",
}

# Default location of the source file, relative to the backend directory (BASE_DIR/..).
DEFAULT_PAGE_PATH = Path("frontend") / "app" / "(user)" / "awards" / "page.tsx"

# One category entry inside the array. Matching the whole entry in one pattern (rather than three
# separate line greps) is what makes a malformed entry FAIL to match instead of half-matching.
_ENTRY_RE = re.compile(
    r'id:\s*"(?P<key>\d+)",\s*\n'
    r'\s*name:\s*"(?P<name>[^"]+)",\s*\n'
    r'\s*winner:\s*\{\s*id:\s*"(?P<winner_key>w\d+)",\s*'
    r'name:\s*"(?P<winner>[^"]+)",\s*votes:\s*(?P<votes>\d+)\s*\},'
)

# The section headers inside the same array.
_SECTION_RE = re.compile(
    r'id:\s*"(?P<id>[a-z-]+)",\s*\n\s*name:\s*"(?P<name>[^"]+)",\s*\n\s*categories:\s*\['
)


def parse_manual_winners(page_path):
    """Read the ACTIVE MANUAL_WINNERS array out of the awards page and return
    ([{id, name, categories: [...]}, ...], skipped_comment_line_count).

    Only the active block is read. The file also contains an OLDER, fully commented-out
    MANUAL_WINNERS array further down with different category names and different winners
    (page.tsx lines 255 to 408); importing that one would publish results the site has never
    shown. The block is located by the first line that STARTS with `const MANUAL_WINNERS`, which
    a commented-out line cannot do because it starts with `//`.
    """
    text = Path(page_path).read_text(encoding="utf-8")
    lines = text.splitlines()

    start = next(
        (i for i, line in enumerate(lines) if line.startswith("const MANUAL_WINNERS")), None
    )
    if start is None:
        raise CommandError(
            f"No active `const MANUAL_WINNERS` declaration found in {page_path}. "
            f"If the array has been moved or renamed, this command must be updated before the "
            f"awards page is touched."
        )
    end = next((j for j in range(start, len(lines)) if lines[j].rstrip() == "];"), None)
    if end is None:
        raise CommandError(f"Unterminated MANUAL_WINNERS array in {page_path} (no closing `];`).")

    block_lines = lines[start:end + 1]
    # Drop commented-out entries. They are NOT published: the awards page renders the array, so a
    # commented line has never appeared on the live site, and importing it would publish a result
    # that was deliberately withheld.
    kept = [line for line in block_lines if not line.strip().startswith("//")]
    skipped = len(block_lines) - len(kept)
    block = "\n".join(kept)

    # Split the block at each section header, so every category entry is attributed to the section
    # it physically sits inside rather than to whichever header happens to be nearest.
    headers = list(_SECTION_RE.finditer(block))
    if not headers:
        raise CommandError(f"No sections found inside MANUAL_WINNERS in {page_path}.")

    sections = []
    for index, header in enumerate(headers):
        body_start = header.end()
        body_end = headers[index + 1].start() if index + 1 < len(headers) else len(block)
        body = block[body_start:body_end]
        categories = [
            {
                "key": m.group("key"),
                "name": m.group("name"),
                "winner": m.group("winner"),
                "votes": int(m.group("votes")),
            }
            for m in _ENTRY_RE.finditer(body)
        ]
        sections.append(
            {"id": header.group("id"), "name": header.group("name"), "categories": categories}
        )
    return sections, skipped


class Command(BaseCommand):
    help = "Phase 0: import the published NFCA 2025 award winners from the awards page into afc_polls."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            dest="file",
            default=None,
            help="Path to awards page.tsx. Defaults to ../frontend/app/(user)/awards/page.tsx.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Parse and print. Write nothing.",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Write nothing. Re-read the file and compare it row by row against the database.",
        )
        parser.add_argument(
            "--compare-votes",
            action="store_true",
            help=(
                "Write nothing. Recompute the tally from afc_awards.Vote and log where it "
                "disagrees with the published file. The file always wins; this only reports."
            ),
        )

    def handle(self, *args, **options):
        from django.conf import settings

        page_path = Path(options["file"]) if options["file"] else (
            Path(settings.BASE_DIR).parent / DEFAULT_PAGE_PATH
        )
        if not page_path.exists():
            raise CommandError(f"Awards page not found at {page_path}")

        sections, skipped_comment_lines = parse_manual_winners(page_path)
        total = sum(len(s["categories"]) for s in sections)

        self.stdout.write(f"Source file : {page_path}")
        self.stdout.write(f"Sections    : {len(sections)}")
        for section in sections:
            self.stdout.write(f"  {section['id']:<18} {section['name']:<20} "
                              f"{len(section['categories'])} categories")
        self.stdout.write(f"Categories  : {total} active")
        self.stdout.write(
            f"Skipped     : {skipped_comment_lines} commented-out line(s) inside the array "
            f"(never rendered on the live site, so never published)"
        )

        if options["compare_votes"]:
            return self._compare_votes(sections)
        if options["verify"]:
            return self._verify(sections)
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing written."))
            for section in sections:
                for category in section["categories"]:
                    self.stdout.write(
                        f"  [{section['id']}] #{category['key']:>2} {category['name']} "
                        f"-> {category['winner']} ({category['votes']} votes)"
                    )
            return

        self._import(sections)

    # ── import ────────────────────────────────────────────────────────────────────────────────
    def _import(self, sections):
        created_polls, created_questions, updated_questions = 0, 0, 0

        # One transaction for the whole import: a half-imported set of winners is worse than none,
        # because it looks complete on the page that renders it.
        with transaction.atomic():
            for order, section in enumerate(sections):
                slug = SECTION_SLUGS.get(section["id"])
                if not slug:
                    raise CommandError(
                        f"Unknown awards section id '{section['id']}'. Add it to SECTION_SLUGS "
                        f"with the slug its old #anchor should redirect to before importing."
                    )
                poll, poll_created = Poll.objects.get_or_create(
                    slug=slug,
                    defaults={
                        "title": section["name"],
                        "description": (
                            f"The {AWARDS_EDITION} {section['name']} winners, "
                            f"as voted by the AFC community."
                        ),
                        "kind": Poll.AWARD,
                        "awards_edition": AWARDS_EDITION,
                        "subject": Poll.INDIVIDUAL,
                        # Public and readable. NOT answerable: opens_at stays NULL because we do
                        # not know when this ballot opened and inventing a date would be a claim
                        # the site cannot support. Poll.is_open() requires opens_at, so a null
                        # opens_at is exactly "this ballot is not accepting answers".
                        "visibility": Poll.PUBLIC,
                        "results_visibility": Poll.ALWAYS,
                        "allow_edit_until_close": False,
                    },
                )
                created_polls += 1 if poll_created else 0

                for category in section["categories"]:
                    # Idempotency key: (poll, order), where order is the file's own `id`. Re-running
                    # updates the row in place instead of adding a second copy of the same award.
                    question, q_created = PollQuestion.objects.get_or_create(
                        poll=poll,
                        order=int(category["key"]),
                        defaults={
                            "prompt": category["name"],
                            "answer_type": PollQuestion.SINGLE_CHOICE,
                            "required": True,
                        },
                    )
                    if not q_created and question.prompt != category["name"]:
                        question.prompt = category["name"]
                        question.save(update_fields=["prompt"])

                    # The winner is the ONLY option we have. The file does not record the losing
                    # nominees, and inventing them from the Vote table would mix two sources of
                    # truth on the one page where that must not happen.
                    option, _ = PollOption.objects.get_or_create(
                        question=question,
                        order=0,
                        defaults={"label": category["winner"]},
                    )
                    if option.label != category["winner"]:
                        option.label = category["winner"]
                        option.save(update_fields=["label"])

                    question.published_winner_option = option
                    question.published_winner_votes = category["votes"]
                    question.published_result_source = PUBLISHED_RESULT_SOURCE
                    question.save(
                        update_fields=[
                            "published_winner_option",
                            "published_winner_votes",
                            "published_result_source",
                        ]
                    )
                    created_questions += 1 if q_created else 0
                    updated_questions += 0 if q_created else 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nImported. polls created={created_polls}, questions created={created_questions}, "
                f"questions updated={updated_questions}."
            )
        )
        self.stdout.write("Now run with --verify to compare the database against the file.")

    # ── compare against the recomputed tally (report only, never reconcile) ───────────────────
    def _compare_votes(self, sections):
        """Recompute the winner of each old afc_awards.Category from the Vote table and report
        where it disagrees with the published file.

        THIS CHANGES NOTHING. Spec 7.2 trap 2: where a recomputed tally disagrees with the file,
        the FILE wins and the discrepancy is logged for a human. This is that log.

        HOW THE TWO SIDES ARE MATCHED, and why the matching is advisory rather than authoritative:
        the file's category ids were RENUMBERED when its entries were rewritten, so they no longer
        line up with afc_awards.Category.category_id (the database has ids 1-7 and 9-29, the file
        has 1-17 and 19-29, and neither is a subset of the other). The only field the two sides
        still share is the published VOTE COUNT, so rows are matched on that. A count that occurs
        once on each side is an unambiguous match; anything else is reported as unmatched rather
        than guessed at.
        """
        from collections import defaultdict

        from afc_awards.models import Vote

        # Recomputed top nominee per old category, straight out of the Vote table.
        tallies = defaultdict(lambda: defaultdict(int))
        for row in Vote.objects.values("category_id", "category__name", "nominee__name"):
            tallies[(row["category_id"], row["category__name"])][row["nominee__name"]] += 1

        db_by_votes = defaultdict(list)
        for (category_id, category_name), counts in tallies.items():
            nominee, votes = max(counts.items(), key=lambda kv: kv[1])
            db_by_votes[votes].append((category_id, category_name, nominee))

        file_rows = [
            (section["id"], category) for section in sections for category in section["categories"]
        ]
        file_vote_counts = defaultdict(int)
        for _, category in file_rows:
            file_vote_counts[category["votes"]] += 1

        agree, differ, unmatched = [], [], []
        for section_id, category in file_rows:
            candidates = db_by_votes.get(category["votes"], [])
            if len(candidates) != 1 or file_vote_counts[category["votes"]] != 1:
                unmatched.append((section_id, category, candidates))
                continue
            category_id, category_name, nominee = candidates[0]
            # Compare loosely: the file shortened several names ("LORD_JAY_FF" became "LORD JAY",
            # "10N8E" became "10N8E ESPORTS"), which is a rewording of the same winner rather than
            # a different one. Only report a difference when neither name contains the other.
            a = category["winner"].upper().replace("_", " ").strip()
            b = (nominee or "").upper().replace("_", " ").strip()
            same = a in b or b in a
            (agree if same else differ).append(
                (section_id, category, category_id, category_name, nominee)
            )

        self.stdout.write("\nRecomputed tally vs the published file (the file is what is imported):\n")
        self.stdout.write(self.style.SUCCESS(f"  agree      : {len(agree)}"))
        self.stdout.write(self.style.ERROR(f"  DIFFER     : {len(differ)}"))
        self.stdout.write(self.style.WARNING(f"  unmatched  : {len(unmatched)}"))

        if differ:
            self.stdout.write("\n  Published winner differs from the recomputed winner:")
            for section_id, category, category_id, category_name, nominee in differ:
                self.stdout.write(self.style.ERROR(
                    f"    {category['votes']:>4} votes | published: {category['name']} "
                    f"-> {category['winner']}"
                ))
                self.stdout.write(
                    f"                | vote table (category {category_id}, "
                    f"{category_name.strip()}) -> {nominee}"
                )
        if unmatched:
            self.stdout.write("\n  No unambiguous match on vote count (reported, not guessed):")
            for section_id, category, candidates in unmatched:
                self.stdout.write(self.style.WARNING(
                    f"    {category['votes']:>4} votes | {category['name']} -> "
                    f"{category['winner']} | {len(candidates)} candidate(s) in the vote table"
                ))

        self.stdout.write(
            "\nNothing was written. The published file remains the source of truth for the "
            "winners, per spec 7.2 trap 2."
        )

    # ── verify ────────────────────────────────────────────────────────────────────────────────
    def _verify(self, sections):
        """Re-read the file and prove, row by row, that the database says the same thing.

        This is the check the spec demands before the awards page may be touched, so it compares
        the two things that were published together: the WINNER and the VOTE COUNT beside them.
        """
        failures = []
        checked = 0

        self.stdout.write("\nVerifying database against the file:\n")
        for section in sections:
            slug = SECTION_SLUGS[section["id"]]
            poll = Poll.objects.filter(slug=slug).first()
            if not poll:
                failures.append(f"MISSING POLL {slug}")
                continue

            for category in section["categories"]:
                checked += 1
                question = PollQuestion.objects.filter(
                    poll=poll, order=int(category["key"])
                ).select_related("published_winner_option").first()
                if not question:
                    failures.append(f"{slug} #{category['key']} MISSING QUESTION")
                    continue

                problems = []
                if question.prompt != category["name"]:
                    problems.append(f"prompt {question.prompt!r} != {category['name']!r}")
                winner = question.published_winner_option
                if not winner:
                    problems.append("no published_winner_option")
                elif winner.label != category["winner"]:
                    problems.append(f"winner {winner.label!r} != {category['winner']!r}")
                if question.published_winner_votes != category["votes"]:
                    problems.append(
                        f"votes {question.published_winner_votes} != {category['votes']}"
                    )

                if problems:
                    failures.append(f"{slug} #{category['key']}: " + "; ".join(problems))
                    mark = self.style.ERROR("FAIL")
                else:
                    mark = self.style.SUCCESS("OK  ")
                self.stdout.write(
                    f"  {mark} {slug:<28} #{category['key']:>2} {category['name']:<32} "
                    f"{category['winner']:<18} {category['votes']:>4} votes"
                )

        self.stdout.write("")
        if failures:
            for failure in failures:
                self.stdout.write(self.style.ERROR(f"  {failure}"))
            raise CommandError(f"{len(failures)} mismatch(es) out of {checked} checked.")
        self.stdout.write(
            self.style.SUCCESS(f"All {checked} published winners match the file exactly.")
        )
