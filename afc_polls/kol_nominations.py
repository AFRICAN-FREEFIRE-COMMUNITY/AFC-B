"""
afc_polls.kol_nominations - the KOL creators' nomination round (owner, 25 to 26 Nov 2026, AFC inbox #633 / #634).

THE ASK
    Garena's KOL Awards need five nominees per award. The creators on Garena's KOL List nominate each other: up to
    6 picks per award, never themselves, only creators may vote (owner's pick: "a personal code"). Their picks are
    half of each award's shortlist; the other half is the creators' own numbers, worked out offline.
    Design: WEBSITE/tasks/kol-nominations-design-2026-11-25.md. Plan: WEBSITE/tasks/plan-kol-nominations-2026-11-26.md.

THE SHAPE (tables in afc_polls.models, bottom of the file)
    KolNominationRound: one per KOL edition, owns the dates  ->  Poll (DRAFT, no edition, six questions)
    KolCreator: one per creator on the KOL List  ->  KolCreatorOption  ->  PollOption (one per award)
        and KolVoterCode (hashed; a new one revokes the old)
    A ballot is ONE PollResponse with respondent = NULL and respondent_key = respondent_key_for(round, creator),
    so the engine's unique (poll, respondent_key) index is what makes it one ballot per creator.

WHY THE ENGINE'S OWN DOORS NEVER SEE THIS POLL
    It is a DRAFT with no edition: list_polls lists only PUBLIC, edition_detail reads edition.polls, poll_detail
    shows a draft only to a manager, and submit_response refuses anything not is_open() (a draft never is). So the
    code door below is the only way in, and the engine is not edited at all.

HOW THIS CONNECTS
    - models: AwardsEdition, Poll, PollQuestion, PollOption, PollResponse, PollAnswer, PollPickSwitch, and the
      four Kol* tables.
    - views._validate_answers: the same answer check every poll uses (max_choices = 6).
    - vote_tracking.capture / record_sheet_open: country, device and connection of each ballot, the same as the
      public vote, through CodeVoter (they read only `pk` and `date_joined`).
    - Frontend: app/(user)/awards/[edition]/nominate (the creators' page, lib/kolNominations.ts) and
      app/(a)/a/polls (the Nominations tab, KolNominationsAdmin.tsx).
"""
import hashlib
import hmac

from django.db import transaction

from .models import AwardsEdition, KolCreator, KolCreatorOption, KolNominationRound, Poll, PollOption, PollQuestion

# The six awards, in the order Layo named them on 19 Nov 2026 (projectsmanagement hub 373). The question prompts.
AWARDS = (
    "Creator of the Year",
    "Breakthrough Creator of the Year",
    "Educational Creator of the Year",
    "Streamer of the Year",
    "Best Voice Over Creator of the Year",
    "Funniest Creator of the Year",
)
MAX_PICKS = 6


def round_poll_slug(edition):
    """The round's poll slug. Never shown to anybody; it only has to be unique."""
    return f"kol-nominations-{edition.slug}"[:140]


@transaction.atomic
def ensure_round(edition, opens_at=None, closes_at=None):
    """The round of `edition`, created on first call. Idempotent and additive: every call also gives each
    creator who lacks one an option in each award, and never deletes anything (a switched-off creator keeps
    their options so picks already made stay readable). Dates are written only when given."""
    round_ = KolNominationRound.objects.select_related("poll").filter(edition=edition).first()
    if round_ is None:
        poll = Poll.objects.create(
            slug=round_poll_slug(edition),
            title=f"{edition.title}: creators' nominations",
            kind=Poll.STANDARD,
            visibility=Poll.DRAFT,
            edition=None,
            results_visibility=Poll.ADMINS_ONLY,
            allow_edit_until_close=True,
        )
        round_ = KolNominationRound.objects.create(edition=edition, poll=poll)
    if opens_at is not None or closes_at is not None:
        if opens_at is not None:
            round_.opens_at = opens_at
        if closes_at is not None:
            round_.closes_at = closes_at
        round_.save(update_fields=["opens_at", "closes_at"])

    poll = round_.poll
    questions = {question.order: question for question in poll.questions.all()}
    for order, name in enumerate(AWARDS):
        if order not in questions:
            questions[order] = PollQuestion.objects.create(
                poll=poll, order=order, prompt=name,
                answer_type=PollQuestion.MULTIPLE_CHOICE, config={"max_choices": MAX_PICKS},
            )

    have = set(
        KolCreatorOption.objects.filter(option__question__poll=poll).values_list("creator_id", "option__question_id")
    )
    for creator in KolCreator.objects.filter(edition=edition).order_by("creator_id"):
        for order, question in sorted(questions.items()):
            if (creator.pk, question.pk) in have:
                continue
            option = PollOption.objects.create(question=question, order=creator.pk, label=creator.name)
            KolCreatorOption.objects.create(creator=creator, option=option)
    return round_


def respondent_key_for(round_, creator):
    """HMAC(the poll's tracking key, "kolcreator:<id>"). Per CREATOR, never per code, so a reissued code opens
    the same ballot. Fills PollResponse.respondent_key (64 hex), unique per poll."""
    key = round_.poll.ensure_tracking_key().encode("utf-8")
    return hmac.new(key, f"kolcreator:{creator.pk}".encode("utf-8"), hashlib.sha256).hexdigest()


def round_for_edition_slug(slug):
    """The round of the edition with this slug, or None. Hidden editions included: that is the point."""
    edition = AwardsEdition.objects.filter(slug=slug).first()
    if edition is None:
        return None
    return KolNominationRound.objects.select_related("poll", "edition").filter(edition=edition).first()
