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
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.client_ip import client_ip

from .models import (
    AwardsEdition, KolCreator, KolCreatorOption, KolNominationRound, KolVoterCode, Poll, PollAnswer, PollOption,
    PollPickSwitch, PollQuestion, PollResponse,
)

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


# THE CODE (design 5.3). "KOL-XXXX-XXXX", 8 characters from 31 that cannot be mistaken for each other on a phone
# screen (no 0 O 1 I L): about 8.5 x 10^11 codes for 110 creators. Stored only as HMAC(SECRET_KEY) (R63).
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize_code(raw):
    """What a creator typed, as the 8 characters the hash is taken over: upper case, spaces and dashes gone, a
    leading "KOL" dropped. " kol 7q4m x2pa " and "7Q4MX2PA" both become "7Q4MX2PA"."""
    text = _NOT_ALNUM.sub("", str(raw or "").upper())
    if len(text) == CODE_LENGTH + 3 and text.startswith("KOL"):
        text = text[3:]
    return text


def _code_hash(normalized):
    key = (settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, f"kolcode:{normalized}".encode("utf-8"), hashlib.sha256).hexdigest()


@transaction.atomic
def new_code(creator):
    """A fresh code for `creator`, returned in plain text ONCE (the admin export is the only place it is shown).
    Revokes every live code the creator had, so a forwarded old code and its cookie stop working at once."""
    now = timezone.now()
    KolVoterCode.objects.filter(creator=creator, revoked_at__isnull=True).update(revoked_at=now)
    while True:
        body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        digest = _code_hash(body)
        if not KolVoterCode.objects.filter(code_hash=digest).exists():
            break
    KolVoterCode.objects.create(creator=creator, code_hash=digest)
    return f"KOL-{body[:4]}-{body[4:]}"


def live_code(round_, raw):
    """The live KolVoterCode this text opens in `round_`, or None: wrong, revoked, another edition's, or a
    switched-off creator's."""
    normalized = normalize_code(raw)
    if len(normalized) != CODE_LENGTH:
        return None
    return (
        KolVoterCode.objects.select_related("creator")
        .filter(code_hash=_code_hash(normalized), revoked_at__isnull=True,
                creator__edition_id=round_.edition_id, creator__active=True)
        .first()
    )


# THE DOOR LIMIT (R59). Wrong codes per connection per 15-minute window; the window is a clock bucket so the
# key needs no bookkeeping. Fails OPEN like afc_auth.views_recovery._ip_throttled: a cache outage must not lock
# 110 creators out, and with 8.5 x 10^11 codes a guesser gets nowhere at 10 tries a window anyway. nginx adds
# the afc_codes zone on this path in production (deploy/vps/nginx-afc.conf).
DOOR_TRIES = 10
DOOR_WINDOW_SECONDS = 15 * 60


def _door_key(request):
    window = int(time.time() // DOOR_WINDOW_SECONDS)
    return f"kol_door:{client_ip(request) or 'unknown'}:{window}"


def _door_blocked(request):
    try:
        return int(cache.get(_door_key(request)) or 0) >= DOOR_TRIES
    except Exception:
        return False


def _door_failed(request):
    try:
        key = _door_key(request)
        if not cache.add(key, 1, timeout=DOOR_WINDOW_SECONDS):
            cache.incr(key)
    except Exception:
        pass


# THE COOKIE (R66). The code is exchanged once for a signed {"c": code_id}: http-only, Secure outside DEBUG,
# SameSite=Lax (the site and the API are the same site, so the page's fetch with credentials sends it), scoped to
# this API's nomination paths, living until the round closes (capped). Never the code itself.
COOKIE_NAME = "kol_ballot"
COOKIE_PATH = "/polls/kol-nominations/"
COOKIE_SALT = "afc_polls.kol_nominations"
COOKIE_MAX_SECONDS = 60 * 24 * 3600


def _cookie_seconds(round_, now):
    if round_.closes_at and round_.closes_at > now:
        return int(min((round_.closes_at - now + timedelta(days=1)).total_seconds(), COOKIE_MAX_SECONDS))
    return 24 * 3600


def _set_cookie(response, round_, code):
    response.set_cookie(
        COOKIE_NAME, signing.dumps({"c": code.code_id}, salt=COOKIE_SALT),
        max_age=_cookie_seconds(round_, timezone.now()), path=COOKIE_PATH,
        httponly=True, secure=not settings.DEBUG, samesite="Lax",
    )
    return response


def code_from_cookie(request, round_):
    """(KolVoterCode or None, refusal code). "" when the cookie opens a live code of an active creator of this
    round; "kol_code_replaced" when it names a code that a newer one revoked (the page says so); else
    "kol_code_needed"."""
    raw = request.COOKIES.get(COOKIE_NAME) or ""
    try:
        code_id = int(signing.loads(raw, salt=COOKIE_SALT, max_age=COOKIE_MAX_SECONDS)["c"])
    except Exception:
        return None, "kol_code_needed"
    code = KolVoterCode.objects.select_related("creator").filter(pk=code_id).first()
    if code is None or code.creator.edition_id != round_.edition_id or not code.creator.active:
        return None, "kol_code_needed"
    if code.revoked_at is not None:
        return None, "kol_code_replaced"
    return code, ""


def _refuse(message, code, http_status=status.HTTP_400_BAD_REQUEST):
    return Response({"message": message, "code": code}, status=http_status)


def _no_round():
    return _refuse("Nominations not found", "kol_round_not_found", status.HTTP_404_NOT_FOUND)


# THE BALLOT, as the page reads it. Speaks in creator ids only: option ids never leave the server.
def ballot_payload(round_, creator):
    """{round, me, awards, candidates, picks, saved_at} for `creator`. Candidates are the round's ACTIVE creators
    other than `creator`, by name; picks hold only active creators (a switched-off one simply disappears)."""
    questions = list(round_.poll.questions.order_by("order"))
    candidates = list(
        KolCreator.objects.filter(edition_id=round_.edition_id, active=True).exclude(pk=creator.pk).order_by("name")
    )
    response = PollResponse.objects.filter(
        poll=round_.poll, respondent_key=respondent_key_for(round_, creator)
    ).first()
    picks = {str(question.question_id): [] for question in questions}
    if response is not None:
        rows = (
            response.answers.filter(option__kol_creator_link__creator__active=True)
            .order_by("created_at", "answer_id")
            .values_list("question_id", "option__kol_creator_link__creator_id")
        )
        for question_id, creator_id in rows:
            picks.setdefault(str(question_id), []).append(creator_id)
    return {
        "round": {
            "edition_title": round_.edition.title,
            "state": round_.state(),
            "opens_at": round_.opens_at,
            "closes_at": round_.closes_at,
        },
        "me": {"creator_id": creator.pk, "name": creator.name, "handle": creator.tiktok_handle},
        "awards": [
            {"question_id": question.question_id, "name": question.prompt, "max": MAX_PICKS} for question in questions
        ],
        "candidates": [
            {"creator_id": c.pk, "name": c.name, "handle": c.tiktok_handle, "country": c.country} for c in candidates
        ],
        "picks": picks,
        "saved_at": response.submitted_at if response is not None else None,
    }


# ENDPOINTS (afc_polls/urls.py, prefix polls/). No Bearer token: the creator is known by the code, then the cookie.
# Consumed by frontend app/(user)/awards/_kol/KolNominate.tsx through lib/kolNominations.ts.


@api_view(["POST"])
def door(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/door/  {code}

    200 the ballot payload + the kol_ballot cookie; 400 kol_code_wrong (counted on the round); 429
    kol_code_too_many (10 wrong a connection a quarter hour); 404 kol_round_not_found. Answers in every state of
    the round (not open yet, open, closed) so a creator can always see where things stand."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    if _door_blocked(request):
        return _refuse("Too many wrong codes. Try again in 15 minutes.", "kol_code_too_many",
                       status.HTTP_429_TOO_MANY_REQUESTS)
    code = live_code(round_, request.data.get("code") if isinstance(request.data, dict) else None)
    if code is None:
        _door_failed(request)
        KolNominationRound.objects.filter(pk=round_.pk).update(failed_attempts=F("failed_attempts") + 1)
        return _refuse("That code does not open a ballot. Check it and try again.", "kol_code_wrong")
    KolVoterCode.objects.filter(pk=code.pk).update(last_used_at=timezone.now())
    return _set_cookie(Response(ballot_payload(round_, code.creator), status=status.HTTP_200_OK), round_, code)


@api_view(["GET", "PUT"])
def ballot(request, edition_slug):
    """GET / PUT polls/kol-nominations/<edition_slug>/ballot/  (kol_ballot cookie)

    GET 200 the ballot payload. PUT {picks: {"<question_id>": [creator_id]}, device_key?} saves the whole sheet
    and answers the payload plus `dropped` (switched-off creators left out); 400 kol_bad_picks / kol_self_pick /
    kol_too_many_picks, 403 kol_round_closed / kol_round_not_open, 429 kol_save_too_many. Both: 401 kol_code_needed (no or bad cookie) or kol_code_replaced (a newer code revoked
    this one); 404 kol_round_not_found."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    if request.method == "PUT":
        return _save(request, round_, code)
    return Response(ballot_payload(round_, code.creator), status=status.HTTP_200_OK)


@api_view(["POST"])
def signout(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/signout/: forgets the cookie ("Not you? Use another code")."""
    response = Response({"message": "Signed out of the ballot"}, status=status.HTTP_200_OK)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, samesite="Lax")
    return response


# SAVING A BALLOT. One PollResponse per creator (respondent_key_for), the whole sheet replaced on each save, in
# the same storage shape and with the same answer check (views._validate_answers) as every other poll.
SAVES_PER_MINUTE = 60          # per code (R59); the page autosaves, so a creator clicking fast is normal


@dataclass(frozen=True)
class CodeVoter:
    """Who a code ballot stands for, in the two attributes vote_tracking reads (`pk` for the voter key,
    `date_joined` for the account age). A code voter has no account, so no age: the tracking stays the same as
    the public vote's in every other column, and the engine is not edited."""

    pk: str
    date_joined: object = None


def _save_limited(code):
    try:
        key = f"kol_save:{code.code_id}:{int(time.time() // 60)}"
        if cache.add(key, 1, timeout=60):
            return False
        return cache.incr(key) > SAVES_PER_MINUTE
    except Exception:
        return False


class _BadPicks(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _clean_picks(round_, creator, raw):
    """({question_id: [creator_id]}, dropped creator ids) from the posted picks, or raise _BadPicks.

    The posted shape is {"<question_id>": [creator_id, ...]}. A question left out is saved empty (the page always
    sends all six). Unknown awards or creators and non-numbers are kol_bad_picks; the creator's own id anywhere is
    kol_self_pick; a switched-off creator is DROPPED (a page opened before the switch-off still saves the rest)
    and reported back; more than MAX_PICKS active picks in one award is kol_too_many_picks."""
    if not isinstance(raw, dict):
        raise _BadPicks("Picks must be grouped by award", "kol_bad_picks")
    question_ids = set(round_.poll.questions.values_list("question_id", flat=True))
    known = dict(KolCreator.objects.filter(edition_id=round_.edition_id).values_list("creator_id", "active"))
    cleaned, dropped = {}, []
    for key, values in raw.items():
        try:
            question_id = int(key)
            ids = [int(value) for value in (values or [])]
        except (TypeError, ValueError):
            raise _BadPicks("Picks must be numbers", "kol_bad_picks")
        if question_id not in question_ids or any(value not in known for value in ids):
            raise _BadPicks("That pick is not on this ballot", "kol_bad_picks")
        if creator.pk in ids:
            raise _BadPicks("You cannot nominate yourself", "kol_self_pick")
        ids = list(dict.fromkeys(ids))
        dropped.extend(value for value in ids if not known[value])
        ids = [value for value in ids if known[value]]
        if len(ids) > MAX_PICKS:
            raise _BadPicks(f"Pick at most {MAX_PICKS} creators for each award", "kol_too_many_picks")
        cleaned[question_id] = ids
    return cleaned, sorted(set(dropped))


@transaction.atomic
def _write_ballot(request, round_, creator, picks):
    """Write `picks` ({question_id: [creator_id]}) as this creator's one ballot. Returns nothing; refusals have all
    happened before this point."""
    from .views import _validate_answers       # the engine's own answer check; imported late, views imports a lot
    from . import vote_tracking

    poll = round_.poll
    questions = list(poll.questions.all())
    options = {
        (question_id, creator_id): option_id
        for option_id, question_id, creator_id in KolCreatorOption.objects.filter(
            option__question__poll=poll).values_list("option_id", "option__question_id", "creator_id")
    }
    raw = [
        {"question_id": question_id, "option_ids": [options[(question_id, cid)] for cid in ids]}
        for question_id, ids in picks.items() if ids
    ]
    cleaned = {}
    if raw:
        cleaned, error_message = _validate_answers(poll, raw, questions)
        if error_message:                         # unreachable after _clean_picks; kept as the engine's last word
            raise _BadPicks(error_message, "kol_bad_picks")

    key = respondent_key_for(round_, creator)
    now = timezone.now()
    response = PollResponse.objects.select_for_update().filter(poll=poll, respondent_key=key).first()
    if response is None:
        try:
            with transaction.atomic():
                response = PollResponse.objects.create(poll=poll, respondent=None, respondent_key=key)
        except IntegrityError:                    # the same creator saving from two phones at once: last one wins
            response = PollResponse.objects.select_for_update().get(poll=poll, respondent_key=key)
    previous = {}
    for question_id, option_id, at in response.answers.values_list("question_id", "option_id", "created_at"):
        previous.setdefault(question_id, {})[option_id] = at

    voter = CodeVoter(pk=f"kol:{creator.pk}")
    response.respondent = None
    response.status = PollResponse.SUBMITTED
    response.submitted_at = now
    response.path_snapshot = [question.question_id for question in questions]
    # The sheet's country stands in for a profile country, so country_mismatch works the same as on the public vote.
    response.eligibility_snapshot = {"country": creator.country}
    vote_tracking.capture(poll, request, voter, response, now)
    response.save()
    vote_tracking.record_sheet_open(poll, voter)

    # Pick switches, the same rule as views.submit_response: one row for a one-for-one change, else a row per
    # pick dropped and per pick added. Only questions that had picks before.
    switches = []
    for question in questions:
        before = {option_id for option_id in previous.get(question.question_id, {}) if option_id}
        after = {row["option_id"] for row in cleaned.get(question.question_id, [])}
        if not before or before == after:
            continue
        gone, came = sorted(before - after), sorted(after - before)
        pairs = [(gone[0], came[0])] if len(gone) == 1 and len(came) == 1 else (
            [(option_id, None) for option_id in gone] + [(None, option_id) for option_id in came])
        switches.extend(
            PollPickSwitch(poll=poll, question_id=question.question_id, from_option_id=old, to_option_id=new, at=now)
            for old, new in pairs
        )
    if switches:
        PollPickSwitch.objects.bulk_create(switches)

    response.answers.all().delete()
    PollAnswer.objects.bulk_create([
        PollAnswer(
            response=response, question_id=question_id, option_id=row["option_id"], value=row["value"],
            created_at=previous.get(question_id, {}).get(row["option_id"]) or now,
        )
        for question_id, rows in cleaned.items()
        for row in rows
    ])


def _save(request, round_, code):
    """PUT body {picks: {"<question_id>": [creator_id]}, device_key?} for the creator behind `code`."""
    state = round_.state()
    if state == KolNominationRound.CLOSED:
        return _refuse("Nominations are closed.", "kol_round_closed", status.HTTP_403_FORBIDDEN)
    if state == KolNominationRound.NOT_OPEN:
        return _refuse("Nominations have not opened yet.", "kol_round_not_open", status.HTTP_403_FORBIDDEN)
    if _save_limited(code):
        return _refuse("Saving too fast. Wait a moment.", "kol_save_too_many", status.HTTP_429_TOO_MANY_REQUESTS)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        picks, dropped = _clean_picks(round_, code.creator, data.get("picks", {}))
        _write_ballot(request, round_, code.creator, picks)
    except _BadPicks as refusal:
        return _refuse(str(refusal), refusal.code)
    KolVoterCode.objects.filter(pk=code.pk).update(last_used_at=timezone.now())
    return Response({**ballot_payload(round_, code.creator), "dropped": dropped}, status=status.HTTP_200_OK)


def tally(round_):
    """{question_id: [{creator_id, name, picks}]}, most picks first then by name, counting only submitted ballots
    an admin has not removed (vote_tracking.COUNTED_ANSWER) and only creators still switched on. Read by the admin
    tab and Garena's live page; never by a creator."""
    from .vote_tracking import COUNTED_ANSWER

    rows = (
        PollAnswer.objects.filter(response__poll=round_.poll, response__status=PollResponse.SUBMITTED,
                                  option__kol_creator_link__creator__active=True)
        .filter(COUNTED_ANSWER)
        .values("question_id", "option__kol_creator_link__creator_id", "option__kol_creator_link__creator__name")
        .annotate(picks=Count("answer_id"))
    )
    result = {question_id: [] for question_id in round_.poll.questions.values_list("question_id", flat=True)}
    for row in rows:
        result.setdefault(row["question_id"], []).append({
            "creator_id": row["option__kol_creator_link__creator_id"],
            "name": row["option__kol_creator_link__creator__name"],
            "picks": row["picks"],
        })
    for entries in result.values():
        entries.sort(key=lambda entry: (-entry["picks"], entry["name"].lower()))
    return result
