"""
afc_polls.views - the poll engine's HTTP surface.

HOUSE IDIOMS (mirrors afc_feedback.views / afc_sponsors.views)
    - Function-based @api_view, Bearer SessionToken resolved by afc_auth.views.validate_token.
    - Errors: Response({"message": ...}, status=4xx).
    - Pagination envelope {results, has_more, next_offset, total_count}, limit <= 100, default 25.

ENDPOINTS (mounted at polls/ via afc/urls.py)
    GET    polls/                                   list_polls           PUBLIC, auth optional
    GET    polls/<slug>/                            poll_detail          PUBLIC, auth optional
    POST   polls/<slug>/responses/                  submit_response      AUTH REQUIRED
    GET    polls/admin/polls/                       admin_list_polls     poll manager
    POST   polls/admin/polls/                       admin_create_poll    poll manager
    GET    polls/admin/polls/<slug>/                admin_poll_detail    poll manager
    PATCH  polls/admin/polls/<slug>/                admin_update_poll    poll manager
    DELETE polls/admin/polls/<slug>/                admin_delete_poll    poll manager
    PUT    polls/admin/polls/<slug>/questions/      admin_save_questions poll manager
    GET    polls/admin/polls/<slug>/results/        admin_results        poll manager

WHO IS A "POLL MANAGER": afc_polls.permissions.can_manage_poll, which is the existing event-admin
gate composed with the existing organizer gate. Not a new permission. See permissions.py.

THE TWO RULES THAT MATTER MOST HERE
  1. ELIGIBILITY IS RE-CHECKED AT SUBMIT. poll_detail returns the verdict so the page can render
     the checklist, and submit_response calls check_eligibility AGAIN before writing anything.
     The first call is a courtesy to the UI; the second is the only actual gate. A client that
     posts straight to the endpoint gets exactly the same 403, with the same per-requirement body.
  2. ANONYMITY IS A STORAGE SHAPE. On an anonymous poll `respondent` is never written; the server
     finds your sheet again through an HMAC of your user id under the poll's own key, and the
     submit time is rounded down to the hour so it cannot be joined against the participation roll.
     See afc_polls.models.PollResponse.

CONSUMED BY
    frontend app/(user)/polls/page.tsx            -> list_polls
    frontend app/(user)/polls/[slug]/page.tsx     -> poll_detail + submit_response
    frontend app/(a)/a/polls/...                  -> the admin endpoints
"""
import hashlib
import hmac

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.audience import freeze_ranking_filters, parse_audience_spec
from afc_auth.locale_middleware import get_locale
from afc_auth.translation import localize_field
from afc_auth.views import validate_token

from .branching import canonical_path, rating_map, serialize_rules
from .eligibility import check_eligibility
from .hydration import hydrate_options
from .models import (
    AwardsEdition,
    Poll,
    PollAnswer,
    PollBranchRule,
    PollEligibilityRule,
    PollOption,
    PollParticipation,
    PollQuestion,
    PollWatch,
)
from .models import PollResponse
from .permissions import can_manage_poll, is_polls_admin
from .team_voting import (
    recompute_team_result,
    rollup_for,
    set_captain_override,
    user_is_captain,
    user_team_for_poll,
)

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Never publish a bucket with fewer than this many respondents (polls spec 5.2). A public
# "Tier 1 voted X" over a population of 18 teams effectively names people.
SMALL_CELL_FLOOR = 5


# ── auth helpers ──────────────────────────────────────────────────────────────────────────────


def _user_from_request(request):
    """The signed-in user, or None. Used by the PUBLIC endpoints, where being signed out is a
    normal state rather than an error: the requirements panel still renders, it simply cannot say
    whether you pass."""
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    return validate_token(header.split(" ", 1)[1])


def _require_user(request):
    """(user, error_response). Decision 4: login is always required to ANSWER a poll."""
    user = _user_from_request(request)
    if not user:
        return None, Response(
            {"message": "You need to be signed in to do this"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _paginate(request, queryset):
    """The house pagination envelope. Never returns an unbounded list."""
    try:
        limit = min(int(request.GET.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    return rows, {
        "has_more": offset + len(rows) < total,
        "next_offset": offset + len(rows),
        "total_count": total,
    }


# ── anonymity ─────────────────────────────────────────────────────────────────────────────────


def _respondent_key(poll, user):
    """HMAC(poll.pseudonym_key, user_id): how the server finds YOUR sheet on an anonymous poll.

    Read afc_polls.models.PollResponse before relying on this for more than it claims. It defeats
    the admin UI, the CSV export and every accidental join. It does not defeat somebody holding
    both the database and this code, because the key is in the same database."""
    if not poll.pseudonym_key:
        # Only possible if a poll was flipped to anonymous by a direct database write. Refuse
        # rather than write a response with an empty key, which would collide with every other one.
        raise ValueError("anonymous poll has no pseudonym key")
    return hmac.new(
        poll.pseudonym_key.encode("utf-8"), str(user.pk).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _anonymous_submit_time(now):
    """Rounded down to the hour. A response timestamped to the second sitting beside a
    participation row timestamped to the second is a join with extra steps."""
    return now.replace(minute=0, second=0, microsecond=0)


# ── serialisation ─────────────────────────────────────────────────────────────────────────────
# Poll titles, question prompts and option labels are USER-GENERATED content, so they go through
# the backend translate-on-read layer (afc_auth.translation.localize_field) keyed off the request
# locale, not through the frontend message catalogues. Static chrome ("Vote", "Results", "Closed")
# is the frontend's job and lives in messages/<locale>/polls.json.


def _serialize_poll_card(poll, locale):
    """The listing shape. Deliberately smaller than the detail shape: /polls renders dozens of
    these and has no use for questions or options."""
    card = {
        "slug": poll.slug,
        "kind": poll.kind,
        "awards_edition": poll.awards_edition,
        "subject": poll.subject,
        "opens_at": poll.opens_at,
        "closes_at": poll.closes_at,
        "is_open": poll.is_open(),
        "is_closed": poll.is_closed(),
        "question_count": getattr(poll, "question_count", None),
        "response_count": getattr(poll, "response_count", None),
    }
    localize_field(card, "title", poll.title, locale)
    localize_field(card, "description", poll.description, locale)
    return card


def _serialize_question(question, locale, include_results=False, tally=None, hydrated=None):
    """One question and its options.

    `tally` is {option_id: count} when results are being shown. It is passed IN rather than
    computed here so the caller can do ONE grouped query for the whole poll instead of one per
    question, which is the difference between 1 query and 29 on the NFCA ballots. `hydrated` is
    the same arrangement for the linked player or team behind each option, batched by
    afc_polls.hydration.hydrate_options.

    WHAT IS TRANSLATED AND WHAT IS NOT, because getting this wrong is very visible:
      - `prompt` and `help_text` ARE. They are sentences an admin wrote, and a French reader
        should get "Meilleur joueur" rather than "Best Player".
      - `description` IS. It is the "why nominated" line, which is also a sentence.
      - `label` IS NOT. Option labels are NAMES. "SCARLETT", "V-ENT ESPORTS" and "3C SMITH" put
        through machine translation is a bug that ships quietly and reads as nonsense the moment a
        French speaker opens the awards page. See awards-grand-design.md item 9.
    """
    data = {
        "question_id": question.question_id,
        # The stable anchor, so /awards/2025#best-esports-player survives a reorder and a share
        # link keeps naming the same category.
        "slug": question.slug,
        "order": question.order,
        "section_id": question.section_id,
        "answer_type": question.answer_type,
        "required": question.required,
        "config": question.config or {},
        "options": [],
    }
    localize_field(data, "prompt", question.prompt, locale)
    localize_field(data, "help_text", question.help_text, locale)

    for option in question.options.all():
        item = {
            "option_id": option.option_id,
            "order": option.order,
            # NOT localize_field. A name is not a sentence. See the docstring.
            "label": option.label,
            "image_url": option.image_url,
            "video_url": option.video_url,
            "linked_type": option.linked_type,
            "linked_id": option.linked_id,
            # The player or team behind the link, already resolved: {type, id, display_name,
            # avatar_url, team_name, team_logo_url, profile_url} or null. Null `avatar_url` is
            # deliberate and means "draw the monogram", never "use a placeholder".
            "linked": (hydrated or {}).get(option.option_id),
        }
        localize_field(item, "description", option.description, locale)
        if include_results and tally is not None:
            item["votes"] = tally.get(option.option_id, 0)
        data["options"].append(item)

    if include_results:
        # A published award result is NOT a tally. It is the claim the site has been making, taken
        # from the page file, and it wins over anything recomputed (spec 7.2 trap 2). Carried
        # separately so a reader can always tell which of the two they are looking at.
        data["published_winner_option_id"] = question.published_winner_option_id
        data["published_winner_votes"] = question.published_winner_votes
        data["published_result_source"] = question.published_result_source
        data["published_at"] = question.published_at
        total = sum((tally or {}).values())
        data["response_count"] = total
    return data


def _results_visible(poll, user):
    """Whether THIS viewer may see the numbers.

    Two halves: a manager always may, and everybody else asks the poll itself. The second half
    lives on the model (Poll.results_are_public) because AFTER_ANNOUNCEMENT has to consult the
    edition, and a rule spread across a view and a model is a rule that gets applied in one place
    and forgotten in the other."""
    if can_manage_poll(user, poll):
        return True
    return poll.results_are_public()


def _tally_for_poll(poll):
    """{option_id: count} across the whole poll, in ONE grouped query.

    Only SUBMITTED responses count. A sheet somebody is still filling in is not a vote, and
    counting it would make the totals move as people browse."""
    rows = (
        PollAnswer.objects.filter(
            response__poll=poll, response__status=PollResponse.SUBMITTED, option__isnull=False
        )
        .values("option_id")
        .annotate(votes=Count("answer_id"))
    )
    return {row["option_id"]: row["votes"] for row in rows}


def _apply_small_cell_floor(tally, is_admin_view, poll):
    """Hide a per-question tally that is small enough to name people.

    THE SCOPE OF THIS RULE WAS NARROWED ON 2026-08-08, and the narrowing is the point.
    Spec 5.2 wrote the floor of five about DEMOGRAPHIC BREAKDOWN buckets ("Tier 1 voted X" over a
    population of 18 teams effectively names people). Applied literally to an option's own tally it
    also swallowed the result itself, so a nominee who finished with three votes read "fewer than
    5" on a public awards page. That is both odd and pointless: the vote is public, and the count
    is attributable to nobody. `awards-grand-design.md` item 8 called for the decision; this is it.

        The floor applies to breakdowns by country, tier, team or role, and to every number on an
        ANONYMOUS poll. It never applies to an option's own tally on a poll whose results are
        public.

    The anonymous half is untouched and is the stricter one (spec 5.3): on an anonymous poll the
    floor covers ADMINS too and the poll as a whole, because the first three answers on a scoped
    poll are trivially attributable by whoever is watching the count climb, and every protection
    above it would be decoration.

    Returns (tally, suppressed) so the UI can say "fewer than 5" instead of silently showing 0,
    which would read as "nobody voted for them" and be a different, false claim.
    """
    if not poll.anonymous:
        # An option tally on a normal poll is a published result, not a demographic bucket.
        return tally, False
    total = sum(tally.values())
    if total and total < SMALL_CELL_FLOOR:
        return {}, True
    return tally, False


# ── PUBLIC: the listing ───────────────────────────────────────────────────────────────────────


@api_view(["GET"])
def list_polls(request):
    """GET polls/ - the polls a visitor may see.

    Query: ?kind=award|standard  ?edition=NFCA%202025  ?state=open|closed  ?limit= ?offset=
    Response: {results: [poll card], has_more, next_offset, total_count}
    Auth: optional. Signing in adds nothing to this list; drafts are never listed to anyone here
    (a manager finds their drafts through the admin listing, which is a different surface with a
    different shape, rather than through a flag on the public one).
    Consumed by frontend app/(user)/polls/page.tsx.
    """
    locale = get_locale(request)
    queryset = (
        Poll.objects.filter(visibility=Poll.PUBLIC)
        .annotate(
            question_count=Count("questions", distinct=True),
            response_count=Count(
                "responses", filter=Q(responses__status=PollResponse.SUBMITTED), distinct=True
            ),
        )
        .order_by("-created_at", "-poll_id")
    )

    kind = (request.GET.get("kind") or "").strip()
    if kind in (Poll.AWARD, Poll.STANDARD):
        queryset = queryset.filter(kind=kind)

    edition = (request.GET.get("edition") or "").strip()
    if edition:
        queryset = queryset.filter(awards_edition=edition)

    state = (request.GET.get("state") or "").strip()
    now = timezone.now()
    if state == "open":
        queryset = queryset.filter(opens_at__isnull=False, opens_at__lte=now).filter(
            Q(closes_at__isnull=True) | Q(closes_at__gt=now)
        )
    elif state == "closed":
        queryset = queryset.filter(closes_at__isnull=False, closes_at__lte=now)

    rows, page = _paginate(request, queryset)
    return Response(
        {"results": [_serialize_poll_card(poll, locale) for poll in rows], **page},
        status=status.HTTP_200_OK,
    )


# ── PUBLIC: one poll ──────────────────────────────────────────────────────────────────────────


@api_view(["GET"])
def poll_detail(request, slug):
    """GET polls/<slug>/ - one poll, its questions, the viewer's eligibility verdict, and the
    results if this viewer may see them.

    Auth: OPTIONAL. A signed-out visitor gets the whole page, with every requirement marked
    "cannot tell yet" rather than failed, because refusing somebody we have not identified is a
    guess dressed as a decision.

    Response: {poll, questions, eligibility, your_response, results_visible}
      poll         - the card fields plus visibility/anonymity/editing switches the page must know
      eligibility  - the verdict from afc_polls.eligibility.check_eligibility, ALWAYS present and
                     ALWAYS rendered, pass or fail (spec 2.3)
      your_response- {answers: {question_id: [option_id]}, submitted_at} when you have answered
    Consumed by frontend app/(user)/polls/[slug]/page.tsx.
    """
    locale = get_locale(request)
    user = _user_from_request(request)
    poll = (
        Poll.objects.filter(slug=slug)
        .prefetch_related("questions__options", "branch_rules", "sections")
        .select_related("eligibility", "edition")
        .first()
    )
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)

    # A draft is visible only to somebody who could edit it. `preview_only` and `link_only` are
    # deliberately readable here: the first is "look but do not touch", the second is "not listed",
    # and neither is "hidden".
    if poll.visibility == Poll.DRAFT and not can_manage_poll(user, poll):
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)

    verdict = check_eligibility(poll, user)
    show_results = _results_visible(poll, user)
    tally, suppressed = ({}, False)
    if show_results:
        tally, suppressed = _apply_small_cell_floor(
            _tally_for_poll(poll), can_manage_poll(user, poll), poll
        )

    questions = list(poll.questions.all())
    # ONE batched pass for every option on the poll: the awards ballots run to 140 nominees, and a
    # per-option lookup would be 140 round trips inside one request.
    hydrated = hydrate_options(
        [option for question in questions for option in question.options.all()], request
    )

    payload = _serialize_poll_card(poll, locale)
    payload.update({
        "visibility": poll.visibility,
        "results_visibility": poll.results_visibility,
        "anonymous": poll.anonymous,
        "allow_edit_until_close": poll.allow_edit_until_close,
        "show_voter_list": poll.show_voter_list,
        "can_manage": can_manage_poll(user, poll),
        # preview_only means visible to everyone and answerable by nobody.
        "accepting_answers": poll.is_open() and poll.visibility != Poll.PREVIEW_ONLY,
        "edition_slug": poll.edition.slug if poll.edition_id and poll.edition else "",
    })

    serialized = [
        _serialize_question(question, locale, include_results=show_results, tally=tally,
                            hydrated=hydrated)
        for question in questions
    ]

    # ── team voting (Phase 4) ────────────────────────────────────────────────────────────────
    # The roll-up is sent only on a TEAM poll and only to somebody who is actually on a roster,
    # because it is that team's private standing until the poll closes. `show_rollup_while_open`
    # is the admin's switch to withhold it even from them, for a poll where seeing the running
    # count would change how the rest of the roster answers.
    team_block = None
    if poll.subject == Poll.TEAM and user:
        team = user_team_for_poll(poll, user)
        if team:
            team_block = {
                "team_id": team.team_id,
                "team_name": team.team_name,
                "is_captain": user_is_captain(team, user),
                "captain_override_allowed": poll.captain_override_allowed,
                "tie_policy": poll.team_tie_policy,
                "quorum": poll.team_quorum,
                "rollup": (
                    rollup_for(poll, team, questions)
                    if (poll.show_rollup_while_open or not poll.is_open()) else []
                ),
            }

    return Response(
        {
            "poll": payload,
            "questions": serialized,
            # The branch rules, sent to the CLIENT so the form reacts to a tap without a network
            # round trip. Never a security boundary: submit_response recomputes the path server
            # side and discards anything off it (afc_polls.branching).
            "branch_rules": serialize_rules(poll),
            "sections": [
                {"section_id": section.section_id, "title": section.title,
                 "order": section.order, "max_selections": section.max_selections}
                for section in poll.sections.all()
            ],
            "eligibility": verdict,
            "your_response": _your_response(poll, user),
            "team": team_block,
            "results_visible": show_results,
            "results_suppressed_small_cell": suppressed,
            "response_count": PollResponse.objects.filter(
                poll=poll, status=PollResponse.SUBMITTED
            ).count(),
        },
        status=status.HTTP_200_OK,
    )


def _your_response(poll, user):
    """The viewer's own answers, so the ballot can render them selected and, when the poll allows
    editing, let them be changed.

    Works on an anonymous poll through the HMAC, which is the entire reason that column exists:
    without it, anonymity and "change your answer" would be mutually exclusive."""
    if not user:
        return None
    response = _find_response(poll, user)
    if not response:
        return None
    answers, values = {}, {}
    for answer in response.answers.all():
        if answer.option_id:
            answers.setdefault(str(answer.question_id), []).append(answer.option_id)
        # A rating or a free-text answer has no option, so it travels in `values` instead. Kept as
        # a second map rather than crammed into `answers` so the ballot never has to guess whether
        # a number it received is an option id or a scale point.
        if isinstance(answer.value, dict) and answer.value:
            existing = values.setdefault(str(answer.question_id), {})
            existing.update(answer.value)
            if answer.option_id:
                # A ranking stores one row per option carrying its position, so the position map
                # has to be keyed by option to be reconstructable.
                existing.setdefault("positions", {})[str(answer.option_id)] = answer.value.get(
                    "position"
                )
    return {
        "answers": answers,
        "values": values,
        "status": response.status,
        "submitted_at": response.submitted_at,
        "can_edit": poll.allow_edit_until_close and poll.is_open(),
    }


def _find_response(poll, user):
    """This user's sheet on this poll, whichever storage shape the poll uses."""
    queryset = PollResponse.objects.filter(poll=poll).prefetch_related("answers")
    if poll.anonymous:
        try:
            return queryset.filter(respondent_key=_respondent_key(poll, user)).first()
        except ValueError:
            return None
    return queryset.filter(respondent=user).first()


# ── PUBLIC: submitting ────────────────────────────────────────────────────────────────────────


@api_view(["POST"])
def submit_response(request, slug):
    """POST polls/<slug>/responses/ - answer a poll.

    Body: {"answers": [{"question_id": 1, "option_ids": [4]}, ...]}
    Auth: REQUIRED (decision 4).

    Returns 201 on a first submission, 200 on an edit, and 403 with the FULL eligibility verdict
    when the server's own re-check refuses. That body is the same shape the page already renders,
    so a refusal arriving at submit time explains itself with no extra frontend code.

    THE ORDER OF THE CHECKS IS THE SECURITY MODEL: authenticate, then confirm the poll is open,
    then re-run eligibility, then validate the answers against the poll's own questions, and only
    then write. Nothing trusts a question id, an option id or an eligibility decision that came
    from the client.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).prefetch_related("questions__options").first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)

    if poll.visibility == Poll.PREVIEW_ONLY:
        return Response(
            {"message": "This poll is a preview and is not taking answers"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not poll.is_open():
        return Response(
            {"message": "This poll is not open for answers"}, status=status.HTTP_403_FORBIDDEN
        )

    # THE ONLY REAL GATE. Anything the client did is a courtesy.
    verdict = check_eligibility(poll, user)
    if not verdict["eligible"]:
        return Response(
            {"message": "You are not eligible to vote in this poll", "eligibility": verdict},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── team polls: you answer ON BEHALF OF a roster, so being on one is part of being able to
    #    answer at all. Checked here rather than folded into check_eligibility because it is a
    #    property of the poll's SUBJECT, not of its audience: an admin who scoped a team poll to
    #    "everyone" still means "everyone who is on a team". ──
    team = None
    if poll.subject == Poll.TEAM:
        team = user_team_for_poll(poll, user)
        if not team:
            return Response(
                {"message": "This poll is answered by teams, and you are not on a roster"},
                status=status.HTTP_403_FORBIDDEN,
            )

    existing = _find_response(poll, user)
    if existing and existing.status == PollResponse.SUBMITTED and not poll.allow_edit_until_close:
        return Response(
            {"message": "You have already voted in this poll, and answers cannot be changed"},
            status=status.HTTP_403_FORBIDDEN,
        )

    questions = list(poll.questions.all())
    cleaned, error_message = _validate_answers(poll, request.data.get("answers"), questions)
    if error_message:
        return Response({"message": error_message}, status=status.HTTP_400_BAD_REQUEST)

    # ── BRANCHING: the server decides the path, then throws away everything off it ────────────
    # The client evaluated the same rules live so the form could react, but a person who answers
    # Q3, changes their mind on Q1 and submits would otherwise contribute a Q3 answer they were
    # never supposed to be asked. Every individual response would look reasonable and the Q3
    # totals would be quietly wrong, which is the worst kind of wrong.
    rules = list(poll.branch_rules.all())
    if rules:
        picked = {qid: [row["option_id"] for row in rows if row["option_id"]]
                  for qid, rows in cleaned.items()}
        ratings = {qid: rows[0]["value"].get("rating")
                   for qid, rows in cleaned.items()
                   if rows and isinstance(rows[0]["value"], dict) and "rating" in rows[0]["value"]}
        path = canonical_path(poll, picked, ratings, questions=questions, rules=rules)
        cleaned = {qid: rows for qid, rows in cleaned.items() if qid in set(path)}
    else:
        path = [question.question_id for question in questions]

    # Required is checked AFTER the path is known. A required question the branching took off this
    # person's path is not a question they failed to answer.
    missing = [
        question.prompt for question in questions
        if question.required and question.question_id in path and not cleaned.get(
            question.question_id
        )
    ]
    if missing:
        return Response(
            {"message": f"These questions still need an answer: {', '.join(missing[:3])}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not cleaned:
        return Response({"message": "No answers were sent"}, status=status.HTTP_400_BAD_REQUEST)

    now = timezone.now()
    with transaction.atomic():
        response = existing or PollResponse(poll=poll)
        if poll.anonymous:
            response.respondent = None
            response.respondent_key = _respondent_key(poll, user)
            response.submitted_at = _anonymous_submit_time(now)
        else:
            response.respondent = user
            # NULL, never "": the (poll, respondent_key) unique index treats NULLs as distinct and
            # empty strings as equal, so a blank here would make the second voter on any normal
            # poll collide with the first.
            response.respondent_key = None
            response.submitted_at = now
        response.status = PollResponse.SUBMITTED
        response.eligibility_snapshot = verdict.get("snapshot") or {}
        response.path_snapshot = path
        response.team = team
        response.save()

        # An edit REPLACES the sheet. Merging would leave an answer to a question the person has
        # since cleared, which is the same class of bug as keeping an off-path branch answer.
        response.answers.all().delete()
        PollAnswer.objects.bulk_create([
            PollAnswer(
                response=response, question_id=question_id,
                option_id=row["option_id"], value=row["value"],
            )
            for question_id, rows in cleaned.items()
            for row in rows
        ])

        # The roll. Separate from the sheet on every poll, not only anonymous ones, so that the
        # two never accidentally share a code path (see models.py).
        PollParticipation.objects.get_or_create(poll=poll, user=user)

        # The team's standing, recomputed for THIS team and THIS poll only: one grouped count per
        # question, so it is cheap, and the roll-up panel has to be live or it is pointless.
        if team:
            for question in questions:
                recompute_team_result(poll, team, question)

    return Response(
        {"message": "Your answers were saved", "response_id": response.response_id},
        status=status.HTTP_200_OK if existing else status.HTTP_201_CREATED,
    )


DEFAULT_TEXT_LIMIT = 800
MAX_TEXT_LIMIT = 5000
MAX_RANKED_OPTIONS = 5


def _validate_answers(poll, raw, questions=None):
    """Turn the posted answers into {question_id: [{option_id, value}]} or return an error message.

    ONE ROW PER (question, option) is the storage shape, because that makes a tally a plain
    GROUP BY rather than JSON arithmetic. So:
      single / multiple choice - one row per pick, `value` empty.
      ranking                  - one row per ranked option, `value` = {"position": n}.
      rating                   - ONE row, option null, `value` = {"rating": n}.
      short / long text        - ONE row, option null, `value` = {"text": "..."}.

    Every id is checked against THIS poll: a question id from another poll, or an option id from
    another question, is rejected rather than stored. That is what stops somebody voting in a poll
    they can see for an option that is not on it.

    REQUIRED IS NOT CHECKED HERE. It moved to the caller, after branching has decided the path: a
    required question the branching took off this person's path is not one they failed to answer.
    """
    if not isinstance(raw, list) or not raw:
        return None, "No answers were sent"

    questions = {
        question.question_id: question
        for question in (questions if questions is not None else poll.questions.all())
    }
    valid_options = {
        question_id: {option.option_id for option in question.options.all()}
        for question_id, question in questions.items()
    }

    cleaned = {}
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each answer must be an object"
        try:
            question_id = int(entry.get("question_id"))
        except (TypeError, ValueError):
            return None, "Each answer needs a question_id"
        question = questions.get(question_id)
        if not question:
            return None, "That question is not part of this poll"

        rows, error_message = _rows_for_answer(question, entry, valid_options[question_id])
        if error_message:
            return None, error_message
        if rows:
            cleaned[question_id] = rows

    if not cleaned:
        return None, "No answers were sent"
    return cleaned, None


def _rows_for_answer(question, entry, valid_options):
    """([{option_id, value}], error_message) for ONE posted answer. One branch per answer type,
    each validating only what its own type can express."""
    config = question.config or {}

    if question.answer_type == PollQuestion.RATING:
        raw = entry.get("rating")
        if raw in (None, ""):
            return [], None
        try:
            rating = int(raw)
        except (TypeError, ValueError):
            return None, "A rating must be a number"
        # Default 5, matching the mockup's five-point scale. Clamped rather than silently stored
        # out of range, because a 9 on a five-point scale would break every average that reads it.
        points = int(config.get("scale_points") or 5)
        if rating < 1 or rating > points:
            return None, f"Pick a rating between 1 and {points}"
        return [{"option_id": None, "value": {"rating": rating}}], None

    if question.answer_type in (PollQuestion.SHORT_TEXT, PollQuestion.LONG_TEXT):
        text = (entry.get("text") or "").strip()
        if not text:
            return [], None
        limit = int(config.get("max_length") or DEFAULT_TEXT_LIMIT)
        limit = min(max(limit, 1), MAX_TEXT_LIMIT)
        if len(text) > limit:
            return None, f"Keep this answer under {limit} characters"
        return [{"option_id": None, "value": {"text": text}}], None

    # ── the choice family: single, multiple and ranking all post option ids ──
    try:
        option_ids = [int(value) for value in (entry.get("option_ids") or [])]
    except (TypeError, ValueError):
        return None, "option_ids must be numbers"
    option_ids = list(dict.fromkeys(option_ids))       # de-duplicate, keep order

    if set(option_ids) - valid_options:
        return None, "That option is not part of this question"

    if question.answer_type == PollQuestion.SINGLE_CHOICE and len(option_ids) > 1:
        return None, "Only one option can be picked for this question"

    if question.answer_type == PollQuestion.MULTIPLE_CHOICE:
        # Enforced on NEW polls only, never backfilled onto migrated award rows: the equivalent
        # check in afc_awards has been commented out since before the live votes were cast, so
        # historical data may violate it (spec 7.2 trap 1).
        max_choices = config.get("max_choices")
        if max_choices and len(option_ids) > int(max_choices):
            return None, f"Pick at most {max_choices} options for this question"

    if question.answer_type == PollQuestion.RANKING:
        if len(option_ids) > MAX_RANKED_OPTIONS:
            return None, f"Rank at most {MAX_RANKED_OPTIONS} options"
        # The POSITION is the answer, so it is stored on each row. Order in the posted list is the
        # ranking, which is what the up/down arrow control produces (drag-to-reorder inside a
        # scrolling page on a touchscreen is genuinely bad, and most AFC users are on phones).
        return [
            {"option_id": option_id, "value": {"position": index + 1}}
            for index, option_id in enumerate(option_ids)
        ], None

    return [{"option_id": option_id, "value": {}} for option_id in option_ids], None


# ── ADMIN ─────────────────────────────────────────────────────────────────────────────────────


def _manageable_polls(user):
    """Every poll `user` may manage. An AFC admin sees all of them; an organizer sees the polls on
    the events they can already edit, which is the same set can_manage_poll would admit one at a
    time, expressed as a queryset so the listing is one query."""
    if is_polls_admin(user):
        return Poll.objects.all()
    from afc_organizers.models import EventCoOrganizer, OrganizationMember

    org_ids = list(
        OrganizationMember.objects.filter(user=user, status="active")
        .values_list("organization_id", flat=True)
    )
    if not org_ids:
        return Poll.objects.none()
    co_event_ids = EventCoOrganizer.objects.filter(
        organization_id__in=org_ids, status="accepted", can_edit_events=True
    ).values("event_id")
    return Poll.objects.filter(
        Q(event__organization_id__in=org_ids) | Q(event_id__in=co_event_ids)
    )


@api_view(["GET", "POST"])
def admin_polls(request):
    """GET polls/admin/polls/  - every poll this manager may see, drafts included.
    POST polls/admin/polls/ - create one.

    POST body: {title, description, kind, awards_edition, subject, event_id, visibility,
                results_visibility, anonymous, show_voter_list, allow_edit_until_close,
                opens_at, closes_at, eligibility: {<audience spec>}}
    Auth: poll manager. An ORGANIZER must pass an event_id they can edit; a site-wide poll (no
    event) is AFC staff only. That is decided by can_manage_poll on the unsaved poll, so creating
    and editing can never disagree about who is allowed.
    Consumed by frontend app/(a)/a/polls/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    if request.method == "GET":
        locale = get_locale(request)
        queryset = _manageable_polls(user).annotate(
            question_count=Count("questions", distinct=True),
            response_count=Count(
                "responses", filter=Q(responses__status=PollResponse.SUBMITTED), distinct=True
            ),
        ).order_by("-created_at", "-poll_id")
        rows, page = _paginate(request, queryset)
        results = []
        for poll in rows:
            card = _serialize_poll_card(poll, locale)
            card["visibility"] = poll.visibility
            card["anonymous"] = poll.anonymous
            results.append(card)
        return Response({"results": results, **page}, status=status.HTTP_200_OK)

    data = request.data or {}
    title = (data.get("title") or "").strip()
    if not title:
        return Response({"message": "A title is required"}, status=status.HTTP_400_BAD_REQUEST)

    event = None
    if data.get("event_id"):
        from afc_tournament_and_scrims.models import Event

        event = Event.objects.filter(event_id=data.get("event_id")).first()
        if not event:
            return Response({"message": "Event not found"}, status=status.HTTP_404_NOT_FOUND)

    draft = Poll(event=event)
    if not can_manage_poll(user, draft):
        return Response(
            {"message": "You do not have permission to create this poll"},
            status=status.HTTP_403_FORBIDDEN,
        )

    poll = Poll(slug=_unique_slug(data.get("slug") or title), title=title, event=event,
                created_by=user)
    try:
        _apply_poll_fields(poll, data)
    except PollFieldError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    poll.save()
    _save_eligibility(poll, data.get("eligibility"))
    return Response({"slug": poll.slug}, status=status.HTTP_201_CREATED)


def _unique_slug(source):
    """A URL-safe slug that is not already taken. Suffixes rather than overwrites, because a slug
    collision is two different polls, not the same one twice."""
    base = slugify(source)[:120] or "poll"
    slug, counter = base, 2
    while Poll.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# Fields an admin may set through the API. Listed explicitly rather than looped over request.data
# so that a client cannot write `pseudonym_key`, `created_by` or `published_winner_votes` by
# putting them in the body.
_EDITABLE_FIELDS = (
    "title", "description", "kind", "awards_edition", "subject", "visibility",
    "results_visibility", "show_voter_list", "allow_edit_until_close", "opens_at", "closes_at",
    "team_quorum", "team_tie_policy", "captain_override_allowed", "show_rollup_while_open",
)


class PollFieldError(ValueError):
    """A request body field this endpoint cannot use. Turned into a 400 by both callers."""


def _parse_moment(value, field):
    """A request body's date string as an aware datetime, or None for a cleared input.

    THE STRING MUST BE PARSED HERE, not left for the ORM. Django does not coerce on assignment:
    `poll.opens_at = "2026-08-16T00:00"` leaves a str on the instance, the row saves fine because
    MySQL parses it, and the very next in-memory read blows up. `Poll.is_open` compares
    `self.opens_at > now`, which raised
        TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'
    and returned a 500 from every save that set an opening time (2026-08-16). Saving with the
    field EMPTY worked, which is why this survived: it only broke the one path every real poll
    needs, since a poll with no opening time never takes answers.

    Naive input is read in the server's own zone, matching what the datetime-local input means by
    the wall clock the admin typed.
    """
    if value in ("", None):
        return None
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise PollFieldError(f"{field} is not a date and time this can read.")
        value = parsed
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _apply_poll_fields(poll, data):
    """Copy the permitted fields off a request body onto a Poll, and enforce the switch rules that
    cannot be left to the UI.

    Raises PollFieldError when a value cannot be used; both callers answer 400 with its message
    rather than letting it reach the client as a 500."""
    for field in _EDITABLE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        # A cleared date input posts "", and MySQL will not take an empty string for a DATETIME.
        if field in ("opens_at", "closes_at"):
            value = _parse_moment(value, field)
        setattr(poll, field, value)

    # The edition FK is resolved by SLUG rather than id, because that is what the builder's picker
    # and the public /awards/<slug> route both use, and an id in a request body is one renumbered
    # database away from pointing at the wrong season.
    if "edition_slug" in data:
        edition_slug = (data.get("edition_slug") or "").strip()
        poll.edition = (
            AwardsEdition.objects.filter(slug=edition_slug).first() if edition_slug else None
        )
        # Keep the legacy label in step so a listing that reads the char column never disagrees
        # with the edition page that reads the row.
        if poll.edition:
            poll.awards_edition = poll.edition.title[:120]

    # An award ballot defaults `anonymous` OFF and the builder should not fight that. The design
    # shows a voter their own picks and lets them change them, and the archive publishes counts;
    # anonymity is a storage shape rather than a display toggle, so an anonymous award poll would
    # lose the per-question breakdowns and gain nothing, because an award vote is a preference
    # between public figures and not a private disclosure.
    if poll.kind == Poll.AWARD and "anonymous" not in data and not poll.pk:
        poll.anonymous = False

    # `anonymous` is a ONE-WAY switch (spec 1.7). It may be turned on while no response exists,
    # and turned off only while no response exists. Turning it off later would leave the responses
    # already collected with no respondent to restore, producing a half-anonymous data set, which
    # is worse than either honest answer.
    if "anonymous" in data:
        wanted = bool(data["anonymous"])
        has_responses = poll.pk and PollResponse.objects.filter(poll=poll).exists()
        if not has_responses:
            poll.anonymous = wanted

    # Mutually exclusive with anonymity, enforced here and not only in the builder: a client that
    # posts both must not end up publishing usernames on a poll that promised it would not.
    if poll.anonymous:
        poll.show_voter_list = False


def _save_eligibility(poll, raw):
    """Store the audience spec, and FREEZE the afc_rankings-derived parts once the poll opens.

    Freezing happens here rather than on a schedule because "the poll is open" is the moment the
    audience stops being a preview and starts being a promise. freeze_ranking_filters is
    idempotent, so re-saving an open poll never re-freezes it against a newer ranking."""
    if raw is None:
        return
    spec = parse_audience_spec(raw if isinstance(raw, dict) else {})
    rule, _ = PollEligibilityRule.objects.get_or_create(poll=poll)
    if poll.is_open() and (spec.get("rank_range") or spec.get("season_tiers")):
        spec = freeze_ranking_filters(spec)
        rule.snapshot_at = rule.snapshot_at or timezone.now()
    rule.spec = spec
    rule.save()


@api_view(["GET", "PATCH", "DELETE"])
def admin_poll_detail(request, slug):
    """GET / PATCH / DELETE polls/admin/polls/<slug>/ - read, edit or remove one poll.

    PATCH accepts the same body as create, applying only the keys present.
    DELETE refuses once responses exist: deleting a poll people have answered destroys their
    answers, and an admin who wants it off the site wants `visibility: draft`.
    Auth: poll manager (afc_polls.permissions.can_manage_poll).
    Consumed by frontend app/(a)/a/polls/[slug]/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).select_related("eligibility", "event").first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_poll(user, poll):
        return Response({"message": "You do not have permission to manage this poll"},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == "GET":
        # The builder edits the raw stored values, so this returns the ORIGINAL English, never a
        # translation: an admin who saved "Best Player" must not find "Meilleur joueur" in the
        # field the next time they open it in French.
        rule = getattr(poll, "eligibility", None)
        return Response({
            "slug": poll.slug,
            "title": poll.title,
            "description": poll.description,
            "kind": poll.kind,
            "awards_edition": poll.awards_edition,
            "subject": poll.subject,
            "visibility": poll.visibility,
            "results_visibility": poll.results_visibility,
            "anonymous": poll.anonymous,
            "show_voter_list": poll.show_voter_list,
            "allow_edit_until_close": poll.allow_edit_until_close,
            "opens_at": poll.opens_at,
            "closes_at": poll.closes_at,
            "event_id": poll.event_id,
            "edition_id": poll.edition_id,
            "subject": poll.subject,
            "team_quorum": poll.team_quorum,
            "team_tie_policy": poll.team_tie_policy,
            "captain_override_allowed": poll.captain_override_allowed,
            "show_rollup_while_open": poll.show_rollup_while_open,
            "is_open": poll.is_open(),
            "has_responses": PollResponse.objects.filter(poll=poll).exists(),
            "eligibility": (rule.spec if rule else {}),
            "eligibility_frozen_at": (rule.snapshot_at if rule else None),
            "questions": [
                {
                    "question_id": question.question_id,
                    "slug": question.slug,
                    "order": question.order,
                    "prompt": question.prompt,
                    "help_text": question.help_text,
                    "answer_type": question.answer_type,
                    "required": question.required,
                    "config": question.config or {},
                    "published_winner_option_id": question.published_winner_option_id,
                    "published_winner_votes": question.published_winner_votes,
                    "published_at": question.published_at,
                    "options": [
                        {
                            "option_id": option.option_id,
                            "order": option.order,
                            "label": option.label,
                            "description": option.description,
                            "image_url": option.image_url,
                            "video_url": option.video_url,
                            "linked_type": option.linked_type,
                            "linked_id": option.linked_id,
                        }
                        for option in question.options.all()
                    ],
                }
                for question in poll.questions.all().prefetch_related("options")
            ],
            "branch_rules": serialize_rules(poll),
            "sections": [
                {"section_id": section.section_id, "title": section.title,
                 "order": section.order, "max_selections": section.max_selections}
                for section in poll.sections.all()
            ],
        }, status=status.HTTP_200_OK)

    if request.method == "DELETE":
        if PollResponse.objects.filter(poll=poll).exists():
            return Response(
                {"message": "This poll has answers and cannot be deleted. Set it back to draft "
                            "to take it off the site."},
                status=status.HTTP_409_CONFLICT,
            )
        poll.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    try:
        _apply_poll_fields(poll, request.data or {})
    except PollFieldError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    poll.save()
    _save_eligibility(poll, (request.data or {}).get("eligibility"))
    return Response({"slug": poll.slug}, status=status.HTTP_200_OK)


@api_view(["PUT"])
def admin_save_questions(request, slug):
    """PUT polls/admin/polls/<slug>/questions/ - replace the poll's questions and options.

    Body: {"questions": [{question_id?, prompt, help_text, answer_type, required, config,
                          options: [{option_id?, label, description, image_url, video_url}]}]}

    A WHOLE-LIST REPLACE, not a per-row API, because that is how the builder works: an admin
    reorders, renames and removes in one edit and presses save once. Rows carrying an id are
    UPDATED IN PLACE so that answers already pointing at them survive; rows without one are
    created; rows that have disappeared from the list are deleted.

    REFUSED once the poll has answers. Editing the questions under an existing response set
    silently changes what those people were asked, and no amount of care in the client prevents
    that, so it is refused here.
    Auth: poll manager. Consumed by the questions tab of the admin builder.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_poll(user, poll):
        return Response({"message": "You do not have permission to manage this poll"},
                        status=status.HTTP_403_FORBIDDEN)
    if PollResponse.objects.filter(poll=poll).exists():
        return Response(
            {"message": "People have already answered this poll, so its questions cannot change"},
            status=status.HTTP_409_CONFLICT,
        )

    payload = (request.data or {}).get("questions")
    if not isinstance(payload, list):
        return Response({"message": "questions must be a list"}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        kept_questions = []
        slugs_used = set()
        for order, raw in enumerate(payload):
            prompt = (raw.get("prompt") or "").strip()
            if not prompt:
                return Response({"message": "Every question needs a prompt"},
                                status=status.HTTP_400_BAD_REQUEST)
            answer_type = raw.get("answer_type") or PollQuestion.SINGLE_CHOICE
            if answer_type not in dict(PollQuestion.ANSWER_TYPE_CHOICES):
                return Response({"message": f"Unknown answer type {answer_type}"},
                                status=status.HTTP_400_BAD_REQUEST)

            question = PollQuestion.objects.filter(
                poll=poll, question_id=raw.get("question_id")
            ).first() or PollQuestion(poll=poll)
            question.order = order
            question.prompt = prompt[:300]
            question.help_text = (raw.get("help_text") or "")[:300]
            question.answer_type = answer_type
            question.required = bool(raw.get("required"))
            question.config = raw.get("config") or {}
            # An EXISTING slug is left alone by ensure_slug, because a published slug is a
            # bookmark. `taken` carries the slugs handed out earlier in this same pass, so two new
            # questions with the same prompt cannot collide before either has been written.
            question.ensure_slug(taken=slugs_used)
            question.save()
            slugs_used.add(question.slug)
            kept_questions.append(question.question_id)

            kept_options = []
            for option_order, raw_option in enumerate(raw.get("options") or []):
                label = (raw_option.get("label") or "").strip()
                if not label:
                    continue
                option = PollOption.objects.filter(
                    question=question, option_id=raw_option.get("option_id")
                ).first() or PollOption(question=question)
                option.order = option_order
                option.label = label[:200]
                option.description = (raw_option.get("description") or "")[:300]
                option.image_url = raw_option.get("image_url") or ""
                option.video_url = raw_option.get("video_url") or ""
                # The soft link to a player or team. Validated against the model's own choices so
                # a typo cannot write a linked_type nothing can resolve; the id itself is NOT
                # checked against the table, on purpose, because the link has to survive the
                # account or team it names being deleted (see PollOption in models.py).
                linked_type = raw_option.get("linked_type") or PollOption.LINK_NONE
                if linked_type not in dict(PollOption.LINKED_TYPE_CHOICES):
                    linked_type = PollOption.LINK_NONE
                option.linked_type = linked_type
                try:
                    option.linked_id = (
                        int(raw_option["linked_id"]) if raw_option.get("linked_id") else None
                    )
                except (TypeError, ValueError):
                    option.linked_id = None
                option.save()
                kept_options.append(option.option_id)
            question.options.exclude(option_id__in=kept_options).delete()

        poll.questions.exclude(question_id__in=kept_questions).delete()
        # Branch rules point AT questions, so they are saved in the same transaction and only once
        # every question exists. Sending no `branch_rules` key leaves the existing rules alone;
        # sending an empty list clears them, which is how the builder removes the last one.
        if "branch_rules" in (request.data or {}):
            error = _save_branch_rules(poll, (request.data or {}).get("branch_rules"))
            if error:
                return Response({"message": error}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"message": "Questions saved"}, status=status.HTTP_200_OK)


def _save_branch_rules(poll, raw):
    """Replace the poll's branch rules. Returns an error message, or None on success.

    A whole-list replace for the same reason the questions are: the builder edits them together
    and presses save once. Every question id is re-checked against THIS poll, so a rule can never
    point at a question on somebody else's poll.
    """
    if not isinstance(raw, list):
        return "branch_rules must be a list"

    question_ids = set(poll.questions.values_list("question_id", flat=True))
    section_ids = set(poll.sections.values_list("section_id", flat=True))

    rules = []
    for order, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return "Each branch rule must be an object"
        try:
            when_id = int(entry.get("when_question_id"))
        except (TypeError, ValueError):
            return "Each branch rule needs a question to watch"
        if when_id not in question_ids:
            return "A branch rule is watching a question that is not on this poll"

        operator = entry.get("operator") or PollBranchRule.IS
        if operator not in dict(PollBranchRule.OPERATOR_CHOICES):
            return f"Unknown branch operator {operator}"
        action = entry.get("action") or PollBranchRule.SHOW
        if action not in dict(PollBranchRule.ACTION_CHOICES):
            return f"Unknown branch action {action}"

        target_question_id = entry.get("target_question_id") or None
        target_section_id = entry.get("target_section_id") or None
        if target_question_id and int(target_question_id) not in question_ids:
            return "A branch rule targets a question that is not on this poll"
        if target_section_id and int(target_section_id) not in section_ids:
            return "A branch rule targets a section that is not on this poll"
        if not target_question_id and not target_section_id:
            return "A branch rule must show or hide something"
        if target_question_id and int(target_question_id) == when_id:
            # A rule that hides the question it is watching can never be satisfied twice the same
            # way, and reads to an admin as a page that flickers. Refused rather than stored.
            return "A branch rule cannot target the question it is watching"

        rules.append(PollBranchRule(
            poll=poll, order=order, when_question_id=when_id, operator=operator,
            value=entry.get("value") or {}, action=action,
            target_question_id=int(target_question_id) if target_question_id else None,
            target_section_id=int(target_section_id) if target_section_id else None,
        ))

    poll.branch_rules.all().delete()
    PollBranchRule.objects.bulk_create(rules)
    return None


@api_view(["GET"])
def admin_results(request, slug):
    """GET polls/admin/polls/<slug>/results/ - the numbers, for somebody who may see them.

    Response: {headline: {responses, turnout, eligible_count}, questions: [...], anonymous}

    TURNOUT IS A FRACTION OF THE ELIGIBLE POPULATION, not of every account on the site, which
    would be meaningless for a scoped poll. On an ANONYMOUS poll the per-question numbers are
    still returned but the breakdown selector is not offered at all (spec 5.3), and the small-cell
    floor applies to this admin view too.
    Auth: poll manager. Consumed by frontend app/(a)/a/polls/[slug]/results/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).prefetch_related("questions__options").first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_poll(user, poll):
        return Response({"message": "You do not have permission to see these results"},
                        status=status.HTTP_403_FORBIDDEN)

    locale = get_locale(request)
    tally, suppressed = _apply_small_cell_floor(_tally_for_poll(poll), True, poll)
    responses = PollResponse.objects.filter(poll=poll, status=PollResponse.SUBMITTED).count()

    # THE DENOMINATOR. An empty spec means "anyone with an AFC account" for a poll, which is what
    # afc_polls.eligibility.check_eligibility tells the voter in so many words, and it is the
    # commonest poll there is. Counting it as None printed "-" for both "People who could vote"
    # and "Turnout" on exactly those polls, so the admin saw an answer count with nothing to read
    # it against. (The audience module treats an empty spec as an ERROR for broadcasts, where an
    # unfilled form must never mean "send to everyone". A poll is the opposite: the open audience
    # is the default, not an accident.)
    from afc_auth.audience import eligible_users, resolve_audience, spec_is_empty

    rule = getattr(poll, "eligibility", None)
    spec = parse_audience_spec(rule.spec) if (rule and rule.spec) else None
    if spec and not spec_is_empty(spec):
        eligible_count = resolve_audience(spec).count()
    else:
        eligible_count = eligible_users(
            include_suspended=bool(spec["include_suspended"]) if spec else False
        ).count()

    return Response({
        "anonymous": poll.anonymous,
        # The switch that removes the breakdown selector from the UI. Sent as a fact rather than
        # left for the client to infer from `anonymous`, so the reason travels with the data.
        "breakdowns_available": not poll.anonymous,
        "results_suppressed_small_cell": suppressed,
        "headline": {
            "responses": responses,
            "participants": PollParticipation.objects.filter(poll=poll).count(),
            "eligible_count": eligible_count,
            "turnout_percent": (
                round(responses * 100.0 / eligible_count, 1)
                if eligible_count else None
            ),
        },
        "questions": [
            _serialize_question(question, locale, include_results=True, tally=tally,
                                hydrated=hydrate_options(
                                    [option for q in poll.questions.all()
                                     for option in q.options.all()], request,
                                ))
            for question in poll.questions.all()
        ],
        # Phase 4: how each TEAM voted, only on a team poll. Sent as its own block rather than
        # folded into the per-question numbers, because a team result is a different fact from a
        # member tally and reading them as one number is how a "no consensus" team gets counted as
        # having agreed with whoever answered first.
        "team_results": _serialize_team_results(poll) if poll.subject == Poll.TEAM else [],
    }, status=status.HTTP_200_OK)


def _serialize_team_results(poll):
    """Every team's rolled-up answer on this poll, for the admin results view.

    `no_consensus` and `below_quorum` appear here as REAL ROWS with their own resolution, not as
    absences. An admin reading the results has to be able to tell a split team from a silent one,
    because the follow-up is different: one needs a decision, the other needs a nudge.
    """
    from .models import PollTeamResult

    rows = (
        PollTeamResult.objects.filter(poll=poll)
        .select_related("team", "winning_option", "set_by")
        .order_by("question_id", "team__team_name")
    )
    return [
        {
            "question_id": row.question_id,
            "team_id": row.team_id,
            "team_name": row.team.team_name,
            "winning_option_id": row.winning_option_id,
            "winning_option_label": row.winning_option.label if row.winning_option else "",
            "tally": row.tally or {},
            "answered_count": row.answered_count,
            "playing_roster_size": row.playing_roster_size,
            "full_roster_size": row.full_roster_size,
            "quorum_met": row.quorum_met,
            "resolution": row.resolution,
            "set_by_username": row.set_by.username if row.set_by_id and row.set_by else "",
            "frozen_at": row.frozen_at,
        }
        for row in rows
    ]


# ── PUBLIC: awards editions ───────────────────────────────────────────────────────────────────


@api_view(["GET"])
def list_editions(request):
    """GET polls/editions/ - every awards edition, newest first.

    Response: {results: [{slug, title, year, tagline, phase, poll_count, ...}], ...}
    Auth: optional. Consumed by frontend app/(user)/awards/page.tsx, which uses it to pick the
    edition to lead with and to build the archive's edition switcher.
    """
    queryset = AwardsEdition.objects.all().prefetch_related("polls")
    rows, page = _paginate(request, queryset)
    return Response(
        {"results": [_serialize_edition(edition) for edition in rows], **page},
        status=status.HTTP_200_OK,
    )


def _serialize_edition(edition):
    """The edition card. `phase` is DERIVED from the dates every time it is read, never stored, so
    a countdown and a ballot can never disagree about which moment the season is in."""
    return {
        "slug": edition.slug,
        "title": edition.title,
        "year": edition.year,
        "tagline": edition.tagline,
        "hero_image": edition.hero_image,
        "nominations_close": edition.nominations_close,
        "voting_opens_at": edition.voting_opens_at,
        "voting_closes_at": edition.voting_closes_at,
        "winners_announced_at": edition.winners_announced_at,
        "phase": edition.phase(),
        "winners_are_public": edition.winners_are_public(),
        "poll_count": edition.polls.count(),
    }


@api_view(["GET"])
def edition_detail(request, slug):
    """GET polls/editions/<slug>/ - one awards edition, its ballots, and where the CALLER stands.

    Response: {edition, polls: [{slug, title, question_count, eligibility, your_progress, ...}],
               totals: {questions, answered}}
    Auth: OPTIONAL. Signed out, every eligibility verdict comes back undecided rather than failed,
    and `your_progress` is null.

    WHY THIS ENDPOINT EXISTS AT ALL: "you have voted in 12 of 28 categories" crosses poll
    boundaries. An edition is several Poll rows (see afc_polls.models.Poll), so no single poll can
    answer it, and asking the client to fetch every ballot to add up its own progress would be one
    request per section on a page most people open on a phone.
    Consumed by frontend app/(user)/awards/[edition]/page.tsx and app/(user)/awards/page.tsx.
    """
    locale = get_locale(request)
    user = _user_from_request(request)
    edition = AwardsEdition.objects.filter(slug=slug).first()
    if not edition:
        return Response({"message": "Awards edition not found"}, status=status.HTTP_404_NOT_FOUND)

    polls = list(
        edition.polls.filter(visibility__in=[Poll.PUBLIC, Poll.LINK_ONLY, Poll.PREVIEW_ONLY])
        .prefetch_related("questions__options")
        .select_related("eligibility")
        .order_by("poll_id")
    )

    total_questions, total_answered = 0, 0
    payload = []
    for poll in polls:
        questions = list(poll.questions.all())
        total_questions += len(questions)

        answered_ids = []
        if user:
            response = _find_response(poll, user)
            if response:
                answered_ids = sorted({answer.question_id for answer in response.answers.all()})
                total_answered += len(answered_ids)

        show_results = _results_visible(poll, user)
        tally = _tally_for_poll(poll) if show_results else {}
        hydrated = hydrate_options(
            [option for question in questions for option in question.options.all()], request
        )

        card = _serialize_poll_card(poll, locale)
        card.update({
            "accepting_answers": poll.is_open() and poll.visibility != Poll.PREVIEW_ONLY,
            "results_visible": show_results,
            "eligibility": check_eligibility(poll, user),
            # Which question ids this caller has already answered, so the ballot rail can tick
            # them and the progress line can be stated in words.
            "answered_question_ids": answered_ids,
            "questions": [
                _serialize_question(question, locale, include_results=show_results, tally=tally,
                                    hydrated=hydrated)
                for question in questions
            ],
        })
        payload.append(card)

    return Response({
        "edition": _serialize_edition(edition),
        "polls": payload,
        "totals": {
            "questions": total_questions,
            "answered": total_answered if user else None,
        },
    }, status=status.HTTP_200_OK)


# ── PUBLIC: "tell me when this changes" ───────────────────────────────────────────────────────


@api_view(["POST", "DELETE"])
def watch(request):
    """POST / DELETE polls/watch/ - start or stop watching a poll or an edition.

    Body: {"poll_slug": "..."} or {"edition_slug": "..."}, plus {"reason": "opens"|"eligibility"|
    "results"}.
    Auth: REQUIRED. A watch is a promise to notify a specific person, so there is nobody to promise
    it to without an account.

    THIS EXISTS BECAUSE OF THE SCREENS IT SITS ON. A refused voter, somebody reading a countdown,
    and somebody waiting on a result are all being told to come back later, and "come back later"
    with no reminder is how a poll loses the responses it should have had. Idempotent on POST:
    asking twice is the same promise, not two notifications.
    Consumed by the requirements panel and the awards countdown.
    """
    user, error = _require_user(request)
    if error:
        return error

    data = request.data or {}
    reason = data.get("reason") or PollWatch.OPENS
    if reason not in dict(PollWatch.REASON_CHOICES):
        return Response({"message": "Unknown watch reason"}, status=status.HTTP_400_BAD_REQUEST)

    poll = Poll.objects.filter(slug=data.get("poll_slug")).first() if data.get("poll_slug") else None
    edition = (
        AwardsEdition.objects.filter(slug=data.get("edition_slug")).first()
        if data.get("edition_slug") else None
    )
    if not poll and not edition:
        return Response(
            {"message": "Say which poll or edition to watch"}, status=status.HTTP_400_BAD_REQUEST
        )

    if request.method == "DELETE":
        PollWatch.objects.filter(user=user, poll=poll, edition=edition, reason=reason).delete()
        return Response({"watching": False}, status=status.HTTP_200_OK)

    PollWatch.objects.get_or_create(user=user, poll=poll, edition=edition, reason=reason)
    return Response({"watching": True}, status=status.HTTP_201_CREATED)


# ── PUBLIC: the captain override ──────────────────────────────────────────────────────────────


@api_view(["POST"])
def captain_override(request, slug):
    """POST polls/<slug>/team-answer/ - the captain sets the team's answer directly.

    Body: {"question_id": 1, "option_id": 4}
    Auth: REQUIRED, and the caller must be the captain of the team they are answering for, on a
    poll whose `captain_override_allowed` is on.

    THREE GATES, and all three are the point rather than defensive coding: the switch is OFF by
    default, only a captain passes, and the members' tally is KEPT and shown beside the override.
    An override the roster cannot see is a trust problem, not a feature: a captain who can quietly
    overturn five people's votes gets fewer answers on the next poll and fewer still on the one
    after.
    Consumed by frontend app/(user)/polls/[slug]/_components/TeamRollup.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).prefetch_related("questions__options").first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if poll.subject != Poll.TEAM:
        return Response({"message": "This is not a team poll"}, status=status.HTTP_400_BAD_REQUEST)
    if not poll.captain_override_allowed:
        return Response(
            {"message": "The captain cannot set this poll's answer directly"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not poll.is_open():
        return Response(
            {"message": "This poll is not open for answers"}, status=status.HTTP_403_FORBIDDEN
        )

    team = user_team_for_poll(poll, user)
    if not team or not user_is_captain(team, user):
        return Response(
            {"message": "Only the team captain can set the team's answer"},
            status=status.HTTP_403_FORBIDDEN,
        )

    question = poll.questions.filter(question_id=request.data.get("question_id")).first()
    if not question:
        return Response({"message": "That question is not part of this poll"},
                        status=status.HTTP_400_BAD_REQUEST)
    option = question.options.filter(option_id=request.data.get("option_id")).first()
    if not option:
        return Response({"message": "That option is not part of this question"},
                        status=status.HTTP_400_BAD_REQUEST)

    result = set_captain_override(poll, team, question, option, user)
    return Response(
        {"message": "The team's answer was set", "resolution": result.resolution},
        status=status.HTTP_200_OK,
    )


# ── ADMIN: publishing a winner, and announcing a poll ─────────────────────────────────────────


@api_view(["POST"])
def admin_publish_winner(request, slug):
    """POST polls/admin/polls/<slug>/publish-winner/ - record the ANNOUNCED winner of a question.

    Body: {"question_id": 1, "option_id": 4, "votes": 310}  (votes optional)
          {"question_id": 1, "option_id": null}             clears it
    Auth: poll manager.

    A PUBLISHED WINNER IS NOT A TALLY, and that distinction is the whole reason this endpoint
    exists rather than the reveal reading `max(votes)`. Three reasons, all from the spec: a tie has
    no maximum, an announcement is an editorial act with a date on it, and for NFCA 2025 the stored
    votes may disagree with what the site published, because the vote-count validation in
    afc_awards has been commented out since before those votes were cast. Where they disagree the
    PUBLISHED value wins and the difference is a discrepancy for a human, never silently
    reconciled.
    Consumed by frontend app/(a)/a/polls/[slug]/results/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_poll(user, poll):
        return Response({"message": "You do not have permission to manage this poll"},
                        status=status.HTTP_403_FORBIDDEN)

    question = poll.questions.filter(question_id=request.data.get("question_id")).first()
    if not question:
        return Response({"message": "That question is not part of this poll"},
                        status=status.HTTP_400_BAD_REQUEST)

    option_id = request.data.get("option_id")
    if not option_id:
        question.published_winner_option = None
        question.published_winner_votes = None
        question.published_at = None
        question.published_result_source = ""
        question.save(update_fields=[
            "published_winner_option", "published_winner_votes", "published_at",
            "published_result_source",
        ])
        return Response({"message": "The published winner was cleared"}, status=status.HTTP_200_OK)

    option = question.options.filter(option_id=option_id).first()
    if not option:
        return Response({"message": "That option is not part of this question"},
                        status=status.HTTP_400_BAD_REQUEST)

    votes = request.data.get("votes")
    question.published_winner_option = option
    try:
        question.published_winner_votes = int(votes) if votes not in (None, "") else None
    except (TypeError, ValueError):
        return Response({"message": "The vote count must be a number"},
                        status=status.HTTP_400_BAD_REQUEST)
    question.published_at = timezone.now()
    # Provenance, so a reader a year from now can tell a transcribed 2025 number from one an admin
    # published out of the live tally.
    question.published_result_source = f"admin:{user.username}"
    question.save(update_fields=[
        "published_winner_option", "published_winner_votes", "published_at",
        "published_result_source",
    ])
    return Response({"message": "The winner was published"}, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
def admin_editions(request):
    """GET  polls/admin/editions/ - every awards edition, for the builder's edition picker.
    POST polls/admin/editions/ - create one.

    POST body: {title, slug?, year, tagline, hero_image, nominations_close, voting_opens_at,
                voting_closes_at, winners_announced_at, status, order}
    Auth: AFC poll admin only. An edition spans several polls and is a site-wide object, so it sits
    on the same side of the line as a site-wide poll: an organizer running a poll on their own
    event has no claim on an awards season.
    Consumed by frontend app/(a)/a/polls/editions/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error
    if not is_polls_admin(user):
        return Response({"message": "You do not have permission to manage awards editions"},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == "GET":
        rows, page = _paginate(request, AwardsEdition.objects.all().prefetch_related("polls"))
        return Response(
            {"results": [_serialize_edition(edition) for edition in rows], **page},
            status=status.HTTP_200_OK,
        )

    data = request.data or {}
    title = (data.get("title") or "").strip()
    if not title:
        return Response({"message": "A title is required"}, status=status.HTTP_400_BAD_REQUEST)

    edition = AwardsEdition(slug=_unique_edition_slug(data.get("slug") or title), title=title)
    _apply_edition_fields(edition, data)
    edition.save()
    return Response({"slug": edition.slug}, status=status.HTTP_201_CREATED)


def _unique_edition_slug(source):
    """Suffix rather than overwrite, for the same reason poll slugs do: a collision is two
    different seasons, not the same one twice."""
    base = slugify(source)[:120] or "awards"
    slug, counter = base, 2
    while AwardsEdition.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# Listed explicitly rather than looped over request.data, so a client cannot write `edition_id` or
# anything else the model happens to gain later by putting it in the body.
_EDITABLE_EDITION_FIELDS = (
    "title", "year", "tagline", "hero_image", "nominations_close", "voting_opens_at",
    "voting_closes_at", "winners_announced_at", "status", "order",
)


# The columns that accept NULL. A cleared date input posts "", and MySQL will not take an empty
# string for a DATETIME, so the empty case has to become None here rather than 500 at save time.
_NULLABLE_EDITION_FIELDS = {
    "year", "nominations_close", "voting_opens_at", "voting_closes_at", "winners_announced_at",
}


def _apply_edition_fields(edition, data):
    """Copy the permitted fields off a request body onto an AwardsEdition."""
    for field in _EDITABLE_EDITION_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field in _NULLABLE_EDITION_FIELDS and value in ("", None):
            value = None
        setattr(edition, field, value)


@api_view(["GET", "PATCH", "DELETE"])
def admin_edition_detail(request, slug):
    """GET / PATCH / DELETE polls/admin/editions/<slug>/ - read, edit or remove one edition.

    DELETE refuses while polls still point at it: removing the season out from under a published
    ballot would leave that ballot's AFTER_ANNOUNCEMENT results with nothing to ask, and an admin
    who wants a season off the site wants `status: archived`.
    Auth: AFC poll admin only. Consumed by frontend app/(a)/a/polls/editions/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error
    if not is_polls_admin(user):
        return Response({"message": "You do not have permission to manage awards editions"},
                        status=status.HTTP_403_FORBIDDEN)

    edition = AwardsEdition.objects.filter(slug=slug).first()
    if not edition:
        return Response({"message": "Awards edition not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        payload = _serialize_edition(edition)
        payload["polls"] = list(edition.polls.values("slug", "title", "kind", "visibility"))
        return Response(payload, status=status.HTTP_200_OK)

    if request.method == "DELETE":
        if edition.polls.exists():
            return Response(
                {"message": "Polls still belong to this edition. Move them first, or archive it."},
                status=status.HTTP_409_CONFLICT,
            )
        edition.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    _apply_edition_fields(edition, request.data or {})
    edition.save()
    return Response({"slug": edition.slug}, status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_announce(request, slug):
    """POST polls/admin/polls/<slug>/announce/ - tell the poll's own audience that it exists.

    Body: {"title": "...", "message": "...", "delivery": "push"|"email"|"both"}
    Auth: poll manager.

    THE AUDIENCE IS THE POLL'S OWN ELIGIBILITY SPEC, not a second list an admin has to rebuild.
    That is the payoff for extending afc_auth.audience instead of writing a second engine: the
    people notified are EXACTLY the people who may vote, by construction rather than by an admin
    remembering to match two forms. Delivery goes through afc_auth.views.deliver_broadcast, so it
    inherits the existing email-volume guard (roughly 30 messages a minute, which is why a
    site-wide poll defaults to a push notification rather than email) and the existing
    SentBroadcast history.

    Every notification carries target_type='poll' + the slug, so "Take me there" deep links to
    /polls/<slug> through afc_auth.notification_links.build_notification_link.
    Consumed by frontend app/(a)/a/polls/[slug]/page.tsx.
    """
    user, error = _require_user(request)
    if error:
        return error

    poll = Poll.objects.filter(slug=slug).select_related("eligibility").first()
    if not poll:
        return Response({"message": "Poll not found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_poll(user, poll):
        return Response({"message": "You do not have permission to manage this poll"},
                        status=status.HTTP_403_FORBIDDEN)
    if poll.visibility == Poll.DRAFT:
        # Announcing a draft sends people to a page they cannot see. Refused rather than sent,
        # because the notification cannot be recalled once it has gone.
        return Response(
            {"message": "Publish this poll before announcing it"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from afc_auth.audience import eligible_users, resolve_audience, spec_is_empty
    from afc_auth.views import deliver_broadcast

    rule = getattr(poll, "eligibility", None)
    spec = parse_audience_spec(rule.spec if rule and rule.spec else {})
    recipients = eligible_users() if spec_is_empty(spec) else resolve_audience(spec)

    title = (request.data.get("title") or "").strip() or poll.title
    message = (request.data.get("message") or "").strip() or (
        poll.description or "A new poll is open for you to vote in."
    )
    delivery = request.data.get("delivery") or "push"

    result = deliver_broadcast(
        recipients, title, message, delivery=delivery,
        notification_type="admin_message",
        target_type="poll", target_id=poll.slug,
        sender=user, scope="poll",
    )
    pushed, emailed = result[0], result[1]
    return Response(
        {"message": "The poll was announced", "pushed": pushed, "emailed": emailed,
         "audience_count": recipients.count()},
        status=status.HTTP_200_OK,
    )
