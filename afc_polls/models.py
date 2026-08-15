"""
afc_polls.models - ONE poll engine, with award ballots as a preset of it.

Spec: WEBSITE/tasks/polls-spec.md (all ten decisions settled 2026-08-07).
Mockup: WEBSITE/mockups/polls-system.html (owner approved).

THE ONE IDEA THIS RESTS ON
    An award ballot IS a questionnaire. "Best Esports Player" with eight nominees is a
    single-choice question whose options are the nominees. So there is ONE set of tables, and
    `Poll.kind = 'award'` is styling plus a place on the Polls page, not a second code path.
    The alternative (afc_awards kept alive beside a new poll app) means two vote-counting paths,
    two results pages, two eligibility models, and two places for the same bug to live.

THE SHAPE

    Poll  ── has ordered ──▶  PollSection (optional heading)
      │                          │
      │      ── has ordered ──▶  PollQuestion  ── has ordered ──▶  PollOption
      │
      ├── has one ──────────▶  PollEligibilityRule   (who may answer: an extended AudienceSpec)
      ├── collects ─────────▶  PollParticipation     (WHO took part. Never their answers)
      └── collects ─────────▶  PollResponse          (ONE answer sheet)
                                   └── has ──────▶  PollAnswer  (one row per question per option)

WHY PARTICIPATION AND RESPONSE ARE TWO TABLES (decision 8, spec 1.7)
    "Admins cannot see who gave which answer" cannot be a permission check on a table holding
    both facts, because a permission check is one `if` away from being wrong and a CSV export is
    one query away from bypassing it. On an anonymous poll the LINK has to be absent from the
    data, not merely hidden. So "did this person take part" lives in PollParticipation and "what
    was answered" lives in PollResponse, and on an anonymous poll the second one has no
    respondent at all. This is why the split ships in Phase 1 even though the builder switch does
    not: a poll engine that writes `respondent` on every response can never retroactively promise
    anonymity for what it has already collected.

HOW THIS CONNECTS TO THE REST OF THE SYSTEM
    - Users:      afc_auth.User via PollParticipation.user, PollResponse.respondent, Poll.created_by.
    - Teams:      afc_team.Team via PollResponse.team (Phase 4 team voting stamps it at submit).
    - Events:     afc_tournament_and_scrims.Event via Poll.event. That FK is also what lets an event
                  ORGANIZER own a poll (afc_polls.permissions.can_manage_poll composes
                  afc_tournament_and_scrims.views._is_event_admin with
                  afc_organizers.permissions.org_can_event).
    - Eligibility: PollEligibilityRule.spec is an extended afc_auth.audience AudienceSpec, resolved
                  by afc_auth.audience.resolve_audience and explained per requirement by
                  afc_polls.eligibility.check_eligibility.
    - Read/written by afc_polls.views; routes mounted at `polls/` in afc/urls.py.
    - Frontend:   app/(user)/polls/page.tsx (listing), app/(user)/polls/[slug]/page.tsx (ballot),
                  app/(a)/a/polls/... (builder + results). /awards and /a/votes redirect in.

THE LATER PHASES, NOW LANDED
    PollBranchRule (Phase 2, an answer decides the next question) and PollTeamResult (Phase 4, a
    team casts one vote through its members) are NEW TABLES rather than columns on the old ones.
    Adding a table later is safe; adding a column to a table that already holds responses is the
    thing this file is arranged to avoid, which is why every Poll-level switch from the spec was
    already here before the phase that reads it.

    AwardsEdition and PollWatch come from WEBSITE/tasks/awards-grand-design.md, which is the
    OWNER-APPROVED presentation layer for award ballots. An edition is what a marquee, a countdown,
    a phase timeline and a "you have voted in 12 of 28" progress bar are all properties OF: none of
    them belong to one Poll, because an edition is several polls (see Poll's own docstring).
"""
import secrets

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from afc_team.models import Team
from afc_tournament_and_scrims.models import Event


class AwardsEdition(models.Model):
    """One awards SEASON, e.g. "NFCA 2025". Several Poll rows hang off it.

    WHY THIS IS A TABLE AND NOT THE `Poll.awards_edition` STRING IT REPLACES
        The string can group ballots and nothing else. An awards season has a ceremony date, a
        tagline, a hero image, an order, and four moments that each want a different page
        (nominations, nominees revealed, voting, winners). Those are facts about the EDITION, so a
        char column would have meant storing them on whichever poll happened to be first, or
        typing them into the frontend, which is where the 2025 winners ended up and is exactly the
        mistake this whole lane exists to undo.

    THE FOUR MOMENTS, and why they are four datetimes rather than a status column
        `status` is derived from the clock by `phase()` below, the same way Poll.is_open() is. A
        stored status plus a set of dates drift apart the first time somebody edits one without the
        other, and then the countdown says "voting opens in 3 days" on a page with a live ballot.
        `status` exists ONLY as an admin override for the cases a clock cannot express (an edition
        that was abandoned, or one held back for a manual reveal).

    HOW THIS CONNECTS
        Poll.edition FK (below). Read by afc_polls.views.edition_detail, which is what
        frontend/app/(user)/awards/page.tsx renders: the marquee, the countdown, the phase
        timeline, and the per-poll progress for the signed-in viewer.
    """

    # Derived from the dates by phase(). `status` overrides it only when set to something other
    # than AUTO, which is the normal value.
    AUTO = "auto"
    ANNOUNCED = "announced"      # nominees are public, voting has not opened
    VOTING = "voting"
    COUNTING = "counting"        # voting closed, winners not yet published
    WINNERS = "winners"
    ARCHIVED = "archived"
    STATUS_CHOICES = [
        (AUTO, "Follow the dates"),
        (ANNOUNCED, "Nominees announced"),
        (VOTING, "Voting open"),
        (COUNTING, "Counting"),
        (WINNERS, "Winners announced"),
        (ARCHIVED, "Archived"),
    ]

    edition_id = models.AutoField(primary_key=True)
    # The public URL is /awards/<slug>. Stable once published, for the same bookmark reason
    # Poll.slug is.
    slug = models.SlugField(max_length=140, unique=True)
    title = models.CharField(max_length=200)
    year = models.PositiveIntegerField(null=True, blank=True)
    tagline = models.CharField(max_length=300, blank=True, default="")
    hero_image = models.URLField(blank=True, default="")

    # ── the four moments. All UTC; every one of them renders through the frontend LocalTime
    #    component, because a countdown in the wrong timezone is worse than no countdown ──
    nominations_close = models.DateTimeField(null=True, blank=True)
    voting_opens_at = models.DateTimeField(null=True, blank=True)
    voting_closes_at = models.DateTimeField(null=True, blank=True)
    # THE ONE THE SPEC WAS MISSING. `results_visibility = after_close` reveals winners the instant
    # voting closes, which is exactly wrong for an awards night: counting takes days and the
    # announcement is itself the event. 2025's own page carried a "we are now tallying" screen.
    # Poll.AFTER_ANNOUNCEMENT is gated on this.
    winners_announced_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=AUTO)
    # Display order on /awards. Newest edition first by default, but an owner running two at once
    # needs to say which leads.
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-year", "-edition_id"]

    def __str__(self):
        return self.slug

    def phase(self, now=None):
        """Which of the four moments this edition is in, right now.

        Returns one of ANNOUNCED / VOTING / COUNTING / WINNERS / ARCHIVED. Never AUTO: AUTO is the
        instruction "work it out", so it can never be an answer.

        The order of the tests runs BACKWARDS through the season on purpose. Each moment is
        recognised by the latest date that has already passed, so a missing date simply means the
        season has not reached that moment, and an edition with no dates at all reads as
        ANNOUNCED (nominees are up, nothing else has happened) rather than crashing or claiming a
        ballot is open.
        """
        from django.utils import timezone

        if self.status != self.AUTO:
            return self.status
        now = now or timezone.now()
        if self.winners_announced_at and self.winners_announced_at <= now:
            return self.WINNERS
        if self.voting_closes_at and self.voting_closes_at <= now:
            return self.COUNTING
        if self.voting_opens_at and self.voting_opens_at <= now:
            return self.VOTING
        return self.ANNOUNCED

    def winners_are_public(self, now=None):
        """True once the winners have been ANNOUNCED, which is not the same as voting having
        closed. This is the gate behind Poll.AFTER_ANNOUNCEMENT."""
        from django.utils import timezone

        if self.status in (self.WINNERS, self.ARCHIVED):
            return True
        if self.status != self.AUTO:
            return False
        return bool(
            self.winners_announced_at and self.winners_announced_at <= (now or timezone.now())
        )


class Poll(models.Model):
    """The container. One row per ballot or questionnaire.

    An award EDITION (for example "NFCA 2025") is several Poll rows sharing `awards_edition`, not
    one. That is deliberate and it preserves history: the old afc_awards.submit_votes enforced one
    submission PER SECTION per user, so collapsing "Content Creators" and "Esports Awards" into a
    single poll would silently change what those historical submissions meant. See spec 7.1.
    """

    # ── kind: which section of /polls this lands in, and whether it gets award styling ──
    AWARD = "award"
    STANDARD = "standard"
    KIND_CHOICES = [(AWARD, "Award ballot"), (STANDARD, "Standard poll")]

    # ── subject: who casts the vote. `team` is Phase 4; the builder only offers `individual` now ──
    INDIVIDUAL = "individual"
    TEAM = "team"
    SUBJECT_CHOICES = [(INDIVIDUAL, "Individual"), (TEAM, "Team")]

    # ── visibility: `link_only` and `preview_only` are Phase 3, the columns ship now ──
    DRAFT = "draft"
    PUBLIC = "public"
    LINK_ONLY = "link_only"
    PREVIEW_ONLY = "preview_only"
    VISIBILITY_CHOICES = [
        (DRAFT, "Draft"),                 # only people who can manage the poll can see it
        (PUBLIC, "Public"),               # listed on /polls
        (LINK_ONLY, "Link only"),         # reachable at its URL, never listed
        (PREVIEW_ONLY, "Preview only"),   # visible to everyone, answerable by nobody
    ]

    ADMINS_ONLY = "admins_only"
    AFTER_CLOSE = "after_close"
    # "Closed" and "announced" are DIFFERENT STATES (awards-grand-design.md item 3). after_close
    # publishes the winners the instant voting stops, which is exactly wrong for an awards night:
    # counting takes days, the announcement is itself the event, and 2025's own page carried a "we
    # are now tallying" screen for it. Gated on AwardsEdition.winners_announced_at, so the window
    # between closing and announcing is its own page state rather than an accident.
    AFTER_ANNOUNCEMENT = "after_announcement"
    ALWAYS = "always"
    RESULTS_VISIBILITY_CHOICES = [
        (ADMINS_ONLY, "Admins only"),
        (AFTER_CLOSE, "Everyone, after the poll closes"),
        (AFTER_ANNOUNCEMENT, "Everyone, once the winners are announced"),
        (ALWAYS, "Everyone, always"),
    ]

    # ── team voting (Phase 4). Defaults are decisions 5 and 6, recorded here so the column and
    #    the decision cannot drift apart ──
    QUORUM_ANY = "any"
    QUORUM_HALF = "half"
    QUORUM_ALL = "all"
    QUORUM_CHOICES = [
        (QUORUM_ANY, "Any one member"),
        (QUORUM_HALF, "More than half of the playing roles"),
        (QUORUM_ALL, "Every playing role"),
    ]

    TIE_CAPTAIN = "captain"
    TIE_NONE = "none"
    TIE_EARLIEST = "earliest"
    TIE_POLICY_CHOICES = [
        (TIE_CAPTAIN, "The captain's own answer wins"),
        (TIE_NONE, "No team vote, recorded as no consensus"),
        (TIE_EARLIEST, "The option that reached the winning count first"),
    ]

    poll_id = models.AutoField(primary_key=True)
    # The public URL is /polls/<slug>. Stable once published: the old awards anchors
    # (#content-creators, #esports-awards) are redirected onto specific slugs, so renaming one
    # breaks a bookmark that the SEO work on this site went out of its way to keep alive.
    slug = models.SlugField(max_length=140, unique=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=STANDARD, db_index=True)
    # THE LABEL, kept. "NFCA 2025" as free text, which is all the listing needs and all this
    # column has ever held. It is NOT deleted now that `edition` exists: it is the fallback for a
    # poll whose edition row has not been created yet, and dropping a populated column in a repo
    # that generates its migrations on the server is a needless way to lose data.
    awards_edition = models.CharField(max_length=120, blank=True, default="", db_index=True)
    # THE EDITION ITSELF, added for the grand awards surface. Null on a standard poll, and null on
    # an award poll until somebody creates the edition row. `manage.py backfill_awards_editions`
    # creates one per distinct `awards_edition` string and links them, so the two never have to be
    # kept in step by hand. Read afc_polls.views.edition_detail for what hangs off it.
    edition = models.ForeignKey(
        "AwardsEdition", on_delete=models.SET_NULL, null=True, blank=True, related_name="polls",
    )
    subject = models.CharField(max_length=12, choices=SUBJECT_CHOICES, default=INDIVIDUAL)

    visibility = models.CharField(
        max_length=14, choices=VISIBILITY_CHOICES, default=DRAFT, db_index=True,
    )
    results_visibility = models.CharField(
        # 24, not 14: "after_announcement" is 18 characters, and a max_length under the longest
        # choice is a system-check ERROR that stops the whole server from starting, not a warning.
        max_length=24, choices=RESULTS_VISIBILITY_CHOICES, default=ADMINS_ONLY,
    )

    # ── anonymity (decision 8) ──
    # When True the respondent is NEVER written onto PollResponse. See the module header and
    # spec 1.7. One-way switch: it may be turned on while the poll is a draft and turned off only
    # while no response exists, because turning it off later would leave the responses already
    # collected with no respondent to restore, which is a half-anonymous data set and worse than
    # either honest answer. Enforced in afc_polls.views, not by the database.
    anonymous = models.BooleanField(default=False)
    # Per-poll HMAC key behind PollResponse.respondent_key. Generated on first save of an
    # anonymous poll (see save() below) and NEVER serialised out of this app. Not a secret in the
    # cryptographic sense: it lives in the same database as the responses, which is exactly the
    # limit spec 1.7 states out loud rather than papering over.
    pseudonym_key = models.CharField(max_length=64, blank=True, default="")
    # Publish usernames under each option. MUTUALLY EXCLUSIVE with `anonymous` (decision 9 keeps
    # this per poll, not per question, so the voter can hold "my name is on this" as ONE fact).
    show_voter_list = models.BooleanField(default=False)
    # Decision 3: the admin decides per poll. Off means the first submission is final, which is
    # what today's awards do. On is the default because an accidental final answer is a worse
    # failure than a changed mind. The write path has to know this from the FIRST response, which
    # is why it is Phase 1 and not later.
    allow_edit_until_close = models.BooleanField(default=True)

    # UTC, like everything else in this backend. Rendered per viewer through the frontend
    # LocalTime component (lib/i18n/time.ts), never with a server-side format.
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)

    # Scopes an event eligibility rule AND is what lets an event organizer own this poll
    # (spec 1.11). Null means a site-wide poll, which only an AFC event admin may create.
    event = models.ForeignKey(
        Event, on_delete=models.SET_NULL, null=True, blank=True, related_name="polls",
    )

    # ── team voting settings (Phase 4 reads these; the columns ship now) ──
    team_quorum = models.CharField(max_length=8, choices=QUORUM_CHOICES, default=QUORUM_HALF)
    team_tie_policy = models.CharField(
        max_length=10, choices=TIE_POLICY_CHOICES, default=TIE_CAPTAIN,
    )
    # Decision 6: default OFF, and always visible to the roster when used. An override the roster
    # cannot see is a trust problem, not a feature.
    captain_override_allowed = models.BooleanField(default=False)
    show_rollup_while_open = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="polls_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest first: both the admin list and the public listing read top-down.
        ordering = ["-created_at", "-poll_id"]
        indexes = [
            # The public listing filters on visibility and kind and sorts by recency.
            models.Index(fields=["visibility", "kind", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.slug} ({self.get_kind_display()})"

    # ── open / closed, derived from the dates rather than stored ────────────────────────────────
    # There is no `status` column on purpose: a stored status and a pair of dates drift apart the
    # first time somebody edits one without the other, and then the ballot is answerable on a page
    # that says "closed". These two methods are the ONLY definition of open, and both the API and
    # the write path call them.
    #
    # `opens_at` is REQUIRED for a poll to be answerable. That is what lets the imported NFCA 2025
    # ballots be public and readable while being answerable by nobody: we do not know when they
    # opened, so we do not claim a date. The builder stamps opens_at = now when an admin publishes
    # a poll, so a normal poll is never accidentally un-answerable.
    def is_open(self, now=None):
        """True when this poll accepts answers right now."""
        from django.utils import timezone

        now = now or timezone.now()
        if self.visibility not in (self.PUBLIC, self.LINK_ONLY):
            return False
        if not self.opens_at or self.opens_at > now:
            return False
        return not self.closes_at or self.closes_at > now

    def is_closed(self, now=None):
        """True when this poll HAS run and is now finished. A draft that never opened is neither
        open nor closed, which is why this is not `not is_open()`."""
        from django.utils import timezone

        now = now or timezone.now()
        return bool(self.closes_at and self.closes_at <= now)

    def results_are_public(self, now=None):
        """Whether ANY visitor may see this poll's numbers, ignoring who is asking.

        The one non-obvious branch is AFTER_ANNOUNCEMENT, which asks the EDITION rather than the
        clock: voting closing and the winners being announced are different events, and the days
        between them are the "we are counting" state that an awards night needs and a plain poll
        does not. An after_announcement poll with no edition row falls back to after_close, because
        the alternative is results that can never be published by any route the admin can see."""
        if self.results_visibility == self.ALWAYS:
            return True
        if self.results_visibility == self.AFTER_CLOSE:
            return not self.is_open(now)
        if self.results_visibility == self.AFTER_ANNOUNCEMENT:
            if self.edition_id and self.edition:
                return self.edition.winners_are_public(now)
            return not self.is_open(now)
        return False

    def save(self, *args, **kwargs):
        # An anonymous poll needs its pseudonym key BEFORE the first response is written, and the
        # only moment guaranteed to be before that is here. Generated once and never rotated:
        # rotating it would orphan every respondent_key already stored, so nobody could edit their
        # own answer any more.
        if self.anonymous and not self.pseudonym_key:
            self.pseudonym_key = secrets.token_hex(32)
        super().save(*args, **kwargs)


class PollSection(models.Model):
    """Optional grouping inside a poll: a heading and a page break in a long questionnaire.

    Presentational, with one exception that makes it a real row rather than a label on a question:
    a Phase 2 branch rule can target a whole section.

    `max_selections` carries the old afc_awards.Section.max_votes. Read spec 7.2 trap 1 before
    writing any code that trusts it: the equivalent check in afc_awards has been COMMENTED OUT
    since before the live votes were cast, so migrated rows may violate it. It is enforced on NEW
    polls only and never backfill-validated.
    """

    section_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="sections")
    title = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)
    max_selections = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["order", "section_id"]

    def __str__(self):
        return f"{self.poll.slug} / {self.title}"


class PollQuestion(models.Model):
    """One question. Every award category is one of these with `answer_type = single_choice`.

    NOTE, and it is load-bearing: `prompt` is NOT unique, unlike the afc_awards.Category.name it
    replaces. The live 2025 data contains "Favorite DUO (Male)" and "Favorite DUO (MALE)" as two
    separate categories with different winners, so a unique constraint here would reject real,
    published history at import time.
    """

    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    RATING = "rating"                 # Phase 2
    RANKING = "ranking"               # Phase 2
    SHORT_TEXT = "short_text"         # Phase 2
    LONG_TEXT = "long_text"           # Phase 2
    ANSWER_TYPE_CHOICES = [
        (SINGLE_CHOICE, "Single choice"),
        (MULTIPLE_CHOICE, "Multiple choice"),
        (RATING, "Rating"),
        (RANKING, "Ranking"),
        (SHORT_TEXT, "Short text"),
        (LONG_TEXT, "Long text"),
    ]

    # Where the options come from. `manual` is the default; the others are what make an award
    # ballot pleasant to build (say "the teams in this event" instead of typing eight names).
    # Only `manual` and `nominees` are generated in Phase 1.
    SOURCE_MANUAL = "manual"
    SOURCE_NOMINEES = "nominees"
    SOURCE_EVENT_TEAMS = "event_teams"
    SOURCE_EVENT_PLAYERS = "event_players"
    OPTION_SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Typed by the admin"),
        (SOURCE_NOMINEES, "Nominees"),
        (SOURCE_EVENT_TEAMS, "Teams in the event"),
        (SOURCE_EVENT_PLAYERS, "Players in the event"),
    ]

    question_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="questions")
    # A STABLE ANCHOR, so /awards/2025#best-esports-player survives a reorder. Unique per poll,
    # not globally, because two editions legitimately both have a "best-esports-player".
    #
    # UNIQUENESS IS ENFORCED IN `ensure_slug`, NOT BY A DATABASE CONSTRAINT, and that is a
    # deliberate trade rather than an omission. A unique_together (poll, slug) is correct in
    # principle and undeployable in practice here: every question row that already exists carries
    # "", MySQL treats empty strings as equal, and this repo GENERATES ITS MIGRATIONS ON THE
    # SERVER. So `makemigrations && migrate` on production would fail on the second question of
    # every poll, which is a deploy-time landmine bought for a constraint the only writer already
    # respects. `manage.py backfill_question_slugs` fills the existing rows; save() fills every new
    # one. If the blanks are ever all gone, the constraint can be added then, safely.
    #
    # De-duplication needs to see its siblings: the live 2025 data holds "Favorite DUO (Male)" and
    # "Favorite DUO (MALE)", which slugify to the same string.
    slug = models.SlugField(max_length=160, blank=True, default="", db_index=True)
    section = models.ForeignKey(
        PollSection, on_delete=models.SET_NULL, null=True, blank=True, related_name="questions",
    )
    order = models.PositiveIntegerField(default=0)
    prompt = models.CharField(max_length=300)
    help_text = models.CharField(max_length=300, blank=True, default="")
    answer_type = models.CharField(
        max_length=20, choices=ANSWER_TYPE_CHOICES, default=SINGLE_CHOICE,
    )
    required = models.BooleanField(default=False)
    # {max_choices, scale_points, scale_labels, max_length, allow_other}. A JSON blob rather than
    # five nullable columns because each key belongs to exactly one answer_type, so five columns
    # would be four NULLs on every row. Shape is validated in the views before anything is written,
    # the same way afc_feedback validates its answers blob.
    config = models.JSONField(default=dict, blank=True)
    option_source = models.CharField(
        max_length=16, choices=OPTION_SOURCE_CHOICES, default=SOURCE_MANUAL,
    )

    # ── published result (spec 7.2 trap 2) ──
    # The winner AS PUBLISHED, which is not always the winner a tally would recompute. For the
    # NFCA 2025 import these two fields are the whole point of Phase 0: the vote-count validation
    # in afc_awards has been commented out since before those votes were cast, so a tally
    # recomputed from the Vote table may disagree with what the site has been showing for a year.
    # Where they disagree, THIS is the published claim and the recomputed one is a discrepancy for
    # a human to look at. Null on a normal poll, where the tally is the result.
    published_winner_option = models.ForeignKey(
        "PollOption", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="published_for_questions",
    )
    published_winner_votes = models.PositiveIntegerField(null=True, blank=True)
    # Provenance, so a reader a year from now knows the number above was transcribed from a page
    # file rather than counted. Set by `manage.py import_awards_winners`.
    published_result_source = models.CharField(max_length=64, blank=True, default="")
    # WHEN the winner was announced. Publishing is an editorial act with a date on it, not a
    # side effect of the clock passing closes_at, and the reveal page needs to be able to say
    # "announced on" without inferring it from an edition that may cover several polls.
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "question_id"]

    def __str__(self):
        return f"{self.poll.slug} Q{self.order}: {self.prompt}"

    def save(self, *args, **kwargs):
        # Fill the anchor before the row exists, so nothing can be written slugless. One extra
        # SELECT on an admin write, which is not a hot path: questions are saved when somebody
        # presses save in the builder, never on a read.
        if not self.slug:
            self.ensure_slug()
        super().save(*args, **kwargs)

    def ensure_slug(self, taken=None):
        """Fill `slug` from `prompt`, avoiding collisions inside this poll.

        `taken` is an optional set of slugs already handed out in this same save pass, so a caller
        writing a whole question list at once does not need a query per row. Existing slugs are
        left alone: a slug that has been published is a bookmark, and renaming a category should
        not break a link that names it.
        """
        if self.slug:
            return self.slug
        base = slugify(self.prompt)[:140] or f"question-{self.order + 1}"
        used = set(taken or ())
        if self.poll_id:
            used |= set(
                PollQuestion.objects.filter(poll_id=self.poll_id)
                .exclude(pk=self.pk)
                .values_list("slug", flat=True)
            )
        slug, counter = base, 2
        while slug in used:
            slug = f"{base}-{counter}"
            counter += 1
        self.slug = slug
        return slug


class PollOption(models.Model):
    """One answer option. Replaces afc_awards.Nominee plus the CategoryNominee join table.

    THE DELIBERATE CHANGE: an option belongs to exactly ONE question. In afc_awards a Nominee was
    a globally unique reusable row joined to many categories, which meant two awards could not
    both have a nominee called "JOKKIE" without fighting the unique-name constraint. Reuse is now
    expressed by pointing several options at the same `linked_id`, which is also what lets an
    option carry a real avatar and deep-link to /players/<uid> or /teams/<id>.
    """

    LINK_NONE = "none"
    LINK_USER = "user"
    LINK_TEAM = "team"
    LINKED_TYPE_CHOICES = [
        (LINK_NONE, "Not linked"),
        (LINK_USER, "A player"),
        (LINK_TEAM, "A team"),
    ]

    option_id = models.AutoField(primary_key=True)
    question = models.ForeignKey(PollQuestion, on_delete=models.CASCADE, related_name="options")
    order = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=200)
    description = models.CharField(max_length=300, blank=True, default="")
    image_url = models.URLField(blank=True, default="")
    video_url = models.URLField(blank=True, default="")
    # Soft link, not a FK, because an option must survive the player or team it names being
    # deleted: a published award winner cannot vanish from the record because somebody closed
    # their account. `label` is the durable copy of the name and is what renders.
    linked_type = models.CharField(max_length=8, choices=LINKED_TYPE_CHOICES, default=LINK_NONE)
    linked_id = models.PositiveIntegerField(null=True, blank=True)
    # Migration only: which afc_awards.Nominee this option came from, so a later phase can attach
    # the historical Vote rows to the right option. Null for anything created in the new engine.
    legacy_nominee_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["order", "option_id"]

    def __str__(self):
        return f"{self.question_id}: {self.label}"


class PollBranchRule(models.Model):
    """BRANCHING: one sentence that watches an answer and shows or hides a question or a section.

    RULES, NOT A NODE GRAPH (spec 1.10), and the reasoning is worth keeping next to the table:
      1. The whole logic of a poll is a list of readable sentences ("When Q1 is 'Double
         elimination', show Q3"). A graph is not comprehensible without a diagram editor, and the
         owner would need one before building their first branching poll.
      2. It is auditable. "Why was this player asked Q4" answers with a rule id and the value that
         satisfied it, both stored. A graph answers by replaying a traversal.
      3. Adding a question touches one row. Adding a node means re-wiring the edges either side.
      4. The storage is two foreign keys and a JSON value. A graph needs an edge table and cycle
         detection, and cycle detection is exactly the sort of thing that ships with a bug.

    The cost, stated plainly: no loop back to an earlier question, and no "two paths merge and then
    diverge again differently". Neither has come up. If one ever does, the fix is a `skip_to`
    action rather than a rewrite.

    A question no rule TARGETS is always shown, so a poll with zero rules is simply linear, which
    is the common case and costs nothing.

    EVALUATED TWICE, and that is deliberate. The client evaluates live so the form reacts as you
    answer; the server re-evaluates at submit, computes the canonical path and DISCARDS answers to
    questions that are not on it (afc_polls.branching.canonical_path). Without the second pass,
    somebody who answers Q3, changes their mind on Q1 and submits would contribute a Q3 answer they
    were never supposed to be asked, and the Q3 totals would be wrong in a way nobody would notice.
    """

    IS = "is"
    IS_NOT = "is_not"
    IS_ANY_OF = "is_any_of"
    GTE = "gte"
    LTE = "lte"
    OPERATOR_CHOICES = [
        (IS, "is"),
        (IS_NOT, "is not"),
        (IS_ANY_OF, "is any of"),
        (GTE, "is at least"),
        (LTE, "is at most"),
    ]

    SHOW = "show"
    HIDE = "hide"
    ACTION_CHOICES = [(SHOW, "Show"), (HIDE, "Hide")]

    rule_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="branch_rules")
    order = models.PositiveIntegerField(default=0)
    when_question = models.ForeignKey(
        PollQuestion, on_delete=models.CASCADE, related_name="branch_rules_watching",
    )
    operator = models.CharField(max_length=12, choices=OPERATOR_CHOICES, default=IS)
    # {"option_ids": [4, 7]} for a choice question, {"rating": 3} for a rating one. JSON because
    # the two shapes are genuinely different and a single nullable column for each would be four
    # NULLs per row. Branching deliberately reads single choice, multiple choice and RATING only:
    # an option id and a scale number are stable things to write a rule against, and a rule like
    # "if the answer contains 'double'" over free text is a bug waiting to happen.
    value = models.JSONField(default=dict, blank=True)
    action = models.CharField(max_length=6, choices=ACTION_CHOICES, default=SHOW)
    target_question = models.ForeignKey(
        PollQuestion, on_delete=models.CASCADE, null=True, blank=True,
        related_name="branch_rules_targeting",
    )
    target_section = models.ForeignKey(
        PollSection, on_delete=models.CASCADE, null=True, blank=True,
        related_name="branch_rules_targeting",
    )

    class Meta:
        ordering = ["order", "rule_id"]

    def __str__(self):
        target = self.target_question_id or f"section {self.target_section_id}"
        return f"rule {self.rule_id}: {self.action} {target} when Q{self.when_question_id}"


class PollTeamResult(models.Model):
    """A TEAM'S vote on one question, rolled up from its members' answers.

    Set Poll.subject = 'team' and only roster members may answer; their PollResponse.team is
    stamped at submit and this row is what the team as a whole is recorded as having said.

    TWO ROSTER COUNTS, NOT ONE, AND THE NAMES SAY WHICH IS WHICH (spec 1.9). Quorum is a fraction
    of the PLAYING roles (captain, vice captain, player), never of the whole roster, because
    counting coaches, managers and analysts gives the better-staffed team the harder quorum: a
    five-player roster needs three answers while the same five plus three staff would need five.
    A team should not fail quorum because its analyst is on holiday. `playing_roster_size` is the
    quorum denominator and `full_roster_size` is what the team actually had on its books. A single
    field called `roster_size` would be read as the second and used as the first by whoever touches
    this next. Storing both also means a result can be explained a year later without re-deriving
    who was a coach at the time, which is the same argument as PollResponse.eligibility_snapshot.

    `no_consensus` IS ITS OWN BUCKET, not a missing row. A team that was SPLIT is not the same
    event as a team that was SILENT and not the same as one led by somebody who never opened the
    poll, and each calls for a different follow-up. Collapsing all three into "no result" takes
    that distinction away from the admin reading the results.

    Computed by afc_polls.team_voting.recompute_team_result on every member submit (one grouped
    count over that team's responses, so it is cheap and the roll-up panel can be live), and frozen
    at close against the roster AS IT STANDS THEN.
    """

    PLURALITY = "plurality"
    TIE_BROKEN_BY_CAPTAIN = "tie_broken_by_captain"
    NO_CONSENSUS = "no_consensus"
    CAPTAIN_OVERRIDE = "captain_override"
    BELOW_QUORUM = "below_quorum"
    RESOLUTION_CHOICES = [
        (PLURALITY, "Most member answers"),
        (TIE_BROKEN_BY_CAPTAIN, "Tied, so the captain decided"),
        (NO_CONSENSUS, "Tied with no captain answer, so no team vote"),
        (CAPTAIN_OVERRIDE, "Set directly by the captain"),
        (BELOW_QUORUM, "Not enough of the team answered"),
    ]

    result_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="team_results")
    question = models.ForeignKey(
        PollQuestion, on_delete=models.CASCADE, related_name="team_results",
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="poll_team_results")
    winning_option = models.ForeignKey(
        PollOption, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="team_wins",
    )
    # {option_id: count} as it stood when this row was computed. Kept even on a captain override,
    # because decision 6 says the members' tally stays VISIBLE beside the override: an override the
    # roster cannot see is a trust problem, not a feature.
    tally = models.JSONField(default=dict, blank=True)
    playing_roster_size = models.PositiveIntegerField(default=0)
    full_roster_size = models.PositiveIntegerField(default=0)
    answered_count = models.PositiveIntegerField(default=0)
    quorum_met = models.BooleanField(default=False)
    resolution = models.CharField(max_length=24, choices=RESOLUTION_CHOICES, default=PLURALITY)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="poll_team_results_set",
    )
    computed_at = models.DateTimeField(auto_now=True)
    # Stamped by the close-time freeze. A frozen row is what results and exports read, so a roster
    # change after the poll closed cannot rewrite history.
    frozen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("poll", "question", "team")
        ordering = ["poll_id", "question_id", "team_id"]

    def __str__(self):
        return f"{self.team_id} on Q{self.question_id}: {self.resolution}"


class PollWatch(models.Model):
    """"Tell me when this changes." One row per person per poll or edition per reason.

    Three reasons, and each one exists because it sits on a screen where the person can do NOTHING
    ELSE. A refused voter, somebody looking at a countdown, and somebody waiting for a result are
    all being asked to come back later, and "come back later" without a reminder is how a poll
    loses the responses it should have had.

      opens        - tell me when voting opens (the nominees-announced screen).
      eligibility  - tell me if I become able to vote (the refusal screen, spec 2.3 item 5).
      results      - tell me when the winners land (the counting screen).

    Feeds the existing Notifications table with target_type='poll', so "Take me there" deep links
    through afc_auth.notification_links.build_notification_link with no new delivery code.
    """

    OPENS = "opens"
    ELIGIBILITY = "eligibility"
    RESULTS = "results"
    REASON_CHOICES = [
        (OPENS, "When voting opens"),
        (ELIGIBILITY, "If I become eligible"),
        (RESULTS, "When the winners are announced"),
    ]

    watch_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="poll_watches",
    )
    # Exactly one of these is set. A watch on an EDITION is what the awards countdown writes, and
    # it survives the individual ballots being added or renamed; a watch on a POLL is what the
    # refusal screen writes, because eligibility is a property of one ballot.
    poll = models.ForeignKey(
        Poll, on_delete=models.CASCADE, null=True, blank=True, related_name="watches",
    )
    edition = models.ForeignKey(
        AwardsEdition, on_delete=models.CASCADE, null=True, blank=True, related_name="watches",
    )
    reason = models.CharField(max_length=12, choices=REASON_CHOICES, default=OPENS)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set when the reminder actually went out, so the sweep never notifies the same person twice.
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("user", "poll", "reason"), ("user", "edition", "reason")]
        ordering = ["-watch_id"]

    def __str__(self):
        return f"{self.user_id} watches {self.poll_id or self.edition_id} for {self.reason}"


class PollEligibilityRule(models.Model):
    """WHO may answer. One row per poll, holding an extended AudienceSpec.

    `spec` is the same dict shape afc_auth.audience parses for broadcasts, plus the poll-only keys
    (event_ids, team_roles, rank_range, season_tiers, require_profile_fields). That reuse is the
    point: the audience an admin picks for a poll is literally the audience they would pick for a
    broadcast, so "announce this poll" in Phase 3 reaches exactly the people who may vote.

    `snapshot_at` is stamped when the poll opens and records WHEN the afc_rankings-derived filters
    (rank, season tier) were frozen. The frozen id sets themselves live inside `spec`, under the
    block they belong to (`rank_range.frozen_team_ids` and so on), written by
    afc_auth.audience.freeze_ranking_filters. They are kept there rather than in a column of their
    own so that `spec` stays a COMPLETE, self-contained description of an audience: one dict in,
    one queryset out, resolvable by afc_auth.audience.resolve_audience with no side table to
    remember to pass alongside it.

    The rule, from spec 2.4: anything afc_rankings COMPUTES is frozen, anything a human can FIX
    stays live. That line falls exactly where the fix does. A frozen country rule would make "add
    your country and try again" a lie; a live rank rule would show somebody a ballot on Monday and
    refuse their submission on Tuesday.
    """

    rule_id = models.AutoField(primary_key=True)
    poll = models.OneToOneField(Poll, on_delete=models.CASCADE, related_name="eligibility")
    spec = models.JSONField(default=dict, blank=True)
    snapshot_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"eligibility for {self.poll.slug}"


class PollParticipation(models.Model):
    """THE ROLL: this person took part. It carries nothing about WHAT they answered.

    It exists for every poll, anonymous or not, and it is what:
      - enforces one response per person,
      - the Phase 3 close-soon reminder reads to find eligible non-responders,
      - the turnout number counts.

    `user` is NOT nullable (decision 4: login is always required). That is the one real difference
    from the afc_awards.Vote.user it replaces, which was nullable although submit_votes always
    required an authenticated user anyway.
    """

    participation_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="participations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="poll_participations",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("poll", "user")
        ordering = ["poll_id", "participation_id"]

    def __str__(self):
        return f"{self.user_id} took part in {self.poll_id}"


class PollResponse(models.Model):
    """THE ANSWER SHEET: one per respondent per poll.

    Normal poll  (anonymous = False): `respondent` is set, `respondent_key` is blank.
    Anonymous poll (anonymous = True): `respondent` is NULL and never written; `respondent_key`
    holds HMAC(Poll.pseudonym_key, user_id), which lets the server find YOUR response so you can
    edit it without storing a column anybody can join on.

    What that actually protects against, stated as honestly as the spec does: it defeats the admin
    UI, the CSV export and every accidental join, which is where a leak realistically comes from.
    It does NOT defeat somebody holding both the database and the application code, because the
    HMAC key is in the same database. Only `anonymous` together with allow_edit_until_close=False
    is unlinkable in principle, and the builder says so in place.
    """

    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    STATUS_CHOICES = [(IN_PROGRESS, "In progress"), (SUBMITTED, "Submitted")]

    response_id = models.AutoField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="responses")
    respondent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="poll_responses",
    )
    # NULL on a normal poll, not blank. The unique index below pairs this with `poll`, and MySQL
    # treats NULLs as distinct but empty strings as equal, so a default of "" would make the
    # SECOND person to answer any non-anonymous poll collide with the first. Storing NULL is what
    # makes the constraint apply to anonymous polls and stand down on the others.
    respondent_key = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True,
    )
    # Phase 4: stamped at submit on a team poll so the roll-up can group by team.
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="poll_responses",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=IN_PROGRESS)
    started_at = models.DateTimeField(auto_now_add=True)
    # NULL until submitted. On an ANONYMOUS poll this is stored rounded down to the hour: a
    # response timestamped to the second sitting beside a participation row timestamped to the
    # second is a join with extra steps, and it would undo the whole arrangement above.
    submitted_at = models.DateTimeField(null=True, blank=True)
    # The eligibility verdict AS IT WAS at submit (country, tier, role, rank). Without it a result
    # set is indefensible six weeks later when half the voters have changed tier. On an anonymous
    # poll it is stored stripped to BUCKET VALUES ONLY, never ids, because a snapshot carrying a
    # team id and a role is a name.
    eligibility_snapshot = models.JSONField(default=dict, blank=True)
    # Which questions the SERVER decided were on this person's path (Phase 2 branching).
    path_snapshot = models.JSONField(default=list, blank=True)

    class Meta:
        # MySQL treats NULLs as distinct in a unique index, so this constrains named respondents
        # (one sheet each) and simply does not apply on an anonymous poll, where every respondent
        # is NULL. The anonymous side is constrained by (poll, respondent_key) below.
        unique_together = [("poll", "respondent"), ("poll", "respondent_key")]
        ordering = ["-response_id"]

    def __str__(self):
        who = self.respondent_id if self.respondent_id else "anonymous"
        return f"response {self.response_id} on {self.poll_id} by {who}"


class PollAnswer(models.Model):
    """One answer. A single-choice question produces one row; a multiple-choice question produces
    one row per pick, which is what makes the tally a plain GROUP BY rather than JSON arithmetic.

    `option` is null for the Phase 2 free-text and rating types, where the answer lives in `value`
    ({"text": ...} or {"rating": n}). unique_together (response, question, option) lets a
    multiple-choice question hold several rows while making a duplicate pick impossible.
    """

    answer_id = models.AutoField(primary_key=True)
    response = models.ForeignKey(PollResponse, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(PollQuestion, on_delete=models.CASCADE, related_name="answers")
    option = models.ForeignKey(
        PollOption, on_delete=models.CASCADE, null=True, blank=True, related_name="answers",
    )
    value = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("response", "question", "option")
        indexes = [
            # The tally: count answers per option for one question. This index is what keeps it a
            # single grouped read rather than a scan of every answer on the poll.
            models.Index(fields=["question", "option"]),
        ]

    def __str__(self):
        return f"answer {self.answer_id} (response {self.response_id}, question {self.question_id})"
