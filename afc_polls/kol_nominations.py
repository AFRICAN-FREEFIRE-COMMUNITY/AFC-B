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
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.client_ip import client_ip

from .models import (
    AwardsEdition, KolCreator, KolCreatorOption, KolNominationRound, KolVoterCode, Poll, PollOption, PollQuestion,
    PollResponse,
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


@api_view(["GET"])
def ballot(request, edition_slug):
    """GET polls/kol-nominations/<edition_slug>/ballot/  (kol_ballot cookie)

    200 the ballot payload; 401 kol_code_needed (no or bad cookie) or kol_code_replaced (a newer code revoked
    this one); 404 kol_round_not_found."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    return Response(ballot_payload(round_, code.creator), status=status.HTTP_200_OK)


@api_view(["POST"])
def signout(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/signout/: forgets the cookie ("Not you? Use another code")."""
    response = Response({"message": "Signed out of the ballot"}, status=status.HTTP_200_OK)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, samesite="Lax")
    return response
