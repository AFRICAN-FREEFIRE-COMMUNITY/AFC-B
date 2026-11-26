"""
afc_polls.kol_nominations - the KOL creators' nomination round (owner, 25 to 26 Nov 2026, AFC inbox #633 / #634).

THE ASK
    Garena's KOL Awards need five nominees per award. The creators on Garena's KOL List nominate each other: up to
    6 picks per award, only creators may vote (owner's pick: "a personal code"). Their picks are half of each
    award's shortlist; the other half is the creators' own numbers, worked out offline.
    Owner, 26 Nov 2026 (#637), on top: "they should be able to nominate themselves now", "creators can add and
    nominate anybody", "If a new person is aded by one creator, everyone else should be able to see that person",
    "shouldnt be able to nominate a person more than once per category". So a creator is on their own list, may add
    anybody by TikTok handle (a WRITE-IN, see write_in below), and one person counts at most once per award.
    Design: WEBSITE/tasks/kol-nominations-design-2026-11-25.md (sections 10 and 11 for the write-ins).
    Plan: WEBSITE/tasks/plan-kol-nominations-2026-11-26.md.

THE SHAPE (tables in afc_polls.models, bottom of the file)
    KolNominationRound: one per KOL edition, owns the dates  ->  Poll (DRAFT, no edition, six questions)
    KolCreator: one per candidate  ->  KolCreatorOption  ->  PollOption (one per award)
        origin LIST: on the KOL List, a voter too, with KolVoterCode (hashed; a new one revokes the old)
        origin WRITE_IN: added by a creator, a candidate for everyone, never a voter (no code)
    A ballot is ONE PollResponse with respondent = NULL and respondent_key = respondent_key_for(round, creator),
    so the engine's unique (poll, respondent_key) index is what makes it one ballot per creator.
    ONE PERSON ONCE PER AWARD: a pick is a creator id, so an award's list is de-duplicated on save; a typed handle
    that is already a candidate IS that candidate (never a second row); and a write-in that turns out to be somebody
    already in the round under another handle is merged into them by an admin (merge_write_in).

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
from rest_framework.decorators import api_view, authentication_classes
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
    ordered = [question for _, question in sorted(questions.items())]
    for creator in KolCreator.objects.filter(edition=edition).order_by("creator_id"):
        _add_options(creator, ordered, have)
    return round_


def _add_options(creator, questions, have=frozenset()):
    """One PollOption per award for `creator`, skipping the (creator_id, question_id) pairs in `have`. Shared by
    ensure_round (everybody) and write_in (the one person just added)."""
    for question in questions:
        if (creator.pk, question.pk) in have:
            continue
        option = PollOption.objects.create(question=question, order=creator.pk, label=creator.name)
        KolCreatorOption.objects.create(creator=creator, option=option)


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
    """The live KolVoterCode this text opens in `round_`, or None: wrong, revoked, another edition's, a
    switched-off creator's, or a write-in's (a write-in is a candidate, never a voter, whatever code exists)."""
    normalized = normalize_code(raw)
    if len(normalized) != CODE_LENGTH:
        return None
    return (
        KolVoterCode.objects.select_related("creator")
        .filter(code_hash=_code_hash(normalized), revoked_at__isnull=True,
                creator__edition_id=round_.edition_id, creator__active=True, creator__origin=KolCreator.LIST)
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
    if (code is None or code.creator.edition_id != round_.edition_id or not code.creator.active
            or code.creator.origin != KolCreator.LIST):
        return None, "kol_code_needed"
    if code.revoked_at is not None:
        return None, "kol_code_replaced"
    return code, ""


def _refuse(message, code, http_status=status.HTTP_400_BAD_REQUEST):
    return Response({"message": message, "code": code}, status=http_status)


def _no_round():
    return _refuse("Nominations not found", "kol_round_not_found", status.HTTP_404_NOT_FOUND)


# THE BALLOT, as the page reads it. Speaks in creator ids only: option ids never leave the server.
def candidate_row(candidate):
    """One candidate as every creator's page sees it. `write_in` says somebody added them (the page labels it);
    WHO added them is never sent, to anybody (it would say who nominated whom)."""
    return {
        "creator_id": candidate.pk, "name": candidate.name, "handle": candidate.tiktok_handle,
        "country": candidate.country, "write_in": candidate.origin == KolCreator.WRITE_IN,
    }


def ballot_payload(round_, creator):
    """{round, me, awards, candidates, picks, saved_at} for `creator`. Candidates are ALL the round's active
    candidates by name, the creator included (#637: self-nomination is allowed) and every write-in any creator
    added; picks hold only active candidates (a switched-off one simply disappears)."""
    questions = list(round_.poll.questions.order_by("order"))
    candidates = list(KolCreator.objects.filter(edition_id=round_.edition_id, active=True).order_by("name"))
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
        "candidates": [candidate_row(c) for c in candidates],
        "picks": picks,
        "saved_at": response.submitted_at if response is not None else None,
    }


# ENDPOINTS (afc_polls/urls.py, prefix polls/). No Bearer token: the creator is known by the code, then the cookie.
# @authentication_classes([]): DRF's default SessionAuthentication would demand a CSRF token from a browser that
# also holds a Django admin session on the API host, and these views never use DRF's user anyway. The cookie is
# SameSite=Lax and the bodies are JSON (a cross-site form cannot send them), which is the CSRF guard here.
# Consumed by frontend app/(user)/awards/_kol/KolNominate.tsx through lib/kolNominations.ts.


@api_view(["POST"])
@authentication_classes([])
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
@authentication_classes([])
def ballot(request, edition_slug):
    """GET / PUT polls/kol-nominations/<edition_slug>/ballot/  (kol_ballot cookie)

    GET 200 the ballot payload. PUT {picks: {"<question_id>": [creator_id]}, device_key?} saves the whole sheet
    and answers the payload plus `dropped` (switched-off creators left out); 400 kol_bad_picks /
    kol_too_many_picks, 403 kol_round_closed / kol_round_not_open, 429 kol_save_too_many. Both: 401
    kol_code_needed (no or bad cookie) or kol_code_replaced (a newer code revoked this one); 404
    kol_round_not_found."""
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
@authentication_classes([])
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


def _minute_limited(prefix, code, limit):
    """True once `code` has gone past `limit` calls of `prefix` in this clock minute. Fails OPEN like the door limit:
    a cache outage must not stop 110 creators saving."""
    try:
        key = f"{prefix}:{code.code_id}:{int(time.time() // 60)}"
        if cache.add(key, 1, timeout=60):
            return False
        return cache.incr(key) > limit
    except Exception:
        return False


def _closed_refusal(round_):
    """The refusal for a change while the round is not open, or None. Read on every change (save, write-in)."""
    state = round_.state()
    if state == KolNominationRound.CLOSED:
        return _refuse("Nominations are closed.", "kol_round_closed", status.HTTP_403_FORBIDDEN)
    if state == KolNominationRound.NOT_OPEN:
        return _refuse("Nominations have not opened yet.", "kol_round_not_open", status.HTTP_403_FORBIDDEN)
    return None


class _BadPicks(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _clean_picks(round_, creator, raw):
    """({question_id: [creator_id]}, dropped creator ids) from the posted picks, or raise _BadPicks.

    The posted shape is {"<question_id>": [creator_id, ...]}. A question left out is saved empty (the page always
    sends all six). Unknown awards or creators and non-numbers are kol_bad_picks; the creator's own id is a pick
    like any other (#637); the same person twice in one award is kept once (#637: never more than once per
    category); a switched-off creator is DROPPED (a page opened before the switch-off still saves the rest) and
    reported back; more than MAX_PICKS active picks in one award is kol_too_many_picks."""
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
    refusal = _closed_refusal(round_)
    if refusal:
        return refusal
    if _minute_limited("kol_save", code, SAVES_PER_MINUTE):
        return _refuse("Saving too fast. Wait a moment.", "kol_save_too_many", status.HTTP_429_TOO_MANY_REQUESTS)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        picks, dropped = _clean_picks(round_, code.creator, data.get("picks", {}))
        _write_ballot(request, round_, code.creator, picks)
    except _BadPicks as refusal:
        return _refuse(str(refusal), refusal.code)
    KolVoterCode.objects.filter(pk=code.pk).update(last_used_at=timezone.now())
    return Response({**ballot_payload(round_, code.creator), "dropped": dropped}, status=status.HTTP_200_OK)


# WRITE-INS (owner, 26 Nov 2026, #636 / #637: "creators can add and nominate anybody ... If a new person is aded by
# one creator, everyone else should be able to see that person"). A creator types a TikTok @handle (or pastes a
# profile link) and, if they like, a name. Nobody in the round has that handle yet: the person becomes a KolCreator
# of origin WRITE_IN with an option in every award, so from then on they are a candidate on EVERY creator's ballot,
# picked, saved, capped, tallied and switched off exactly like a creator from the list. The page then picks them
# through the ordinary save (PUT ballot), so "at most 6 per award" and "one person once per award" stay in ONE place
# (_clean_picks). A handle that IS somebody in the round answers that candidate: never a second row for one handle.
#
# ANTI-SPAM (R59). An addition is seen by every creator, so one creator may ADD at most WRITE_INS_PER_CREATOR new
# people in the whole round (36 = six awards x six picks, every pick a newcomer: more is never needed) and may call
# the door at most WRITE_INS_PER_MINUTE times a minute. Typing somebody already in the round is not an addition and
# never counts toward the 36. An admin switches junk off or merges a duplicate (admin_creator, merge_write_in).
WRITE_INS_PER_CREATOR = MAX_PICKS * len(AWARDS)
WRITE_INS_PER_MINUTE = 10
WRITE_IN_NAME_MAX = 60
# TikTok usernames are letters, numbers, underscores and periods, at most 24 characters (TikTok's own rule; the floor
# of 2 keeps a stray letter from becoming a person). TikTok ignores case, so the key is lower case like the import's.
HANDLE_RE = re.compile(r"^[a-z0-9._]{2,24}$")
_PROFILE_LINK = re.compile(r"tiktok\.com/@([^/?#\s]+)", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def handle_from_text(raw):
    """What a creator typed, as a handle key: a pasted profile link (tiktok.com/@name) gives its name; then spaces
    and a leading "@" go and the rest is lower case, the same as kol_import_creators.clean_handle. "" for anything
    that is not text. Not validated here: a handle already in the round matches however the sheet spells it."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    link = _PROFILE_LINK.search(text)
    if link:
        text = link.group(1)
    return text.strip().lstrip("@").strip().lower()


def _clean_name(raw, handle):
    """The display name of a write-in: control characters out, runs of spaces folded, at most WRITE_IN_NAME_MAX
    characters; the handle when nothing is left. Shown to every creator, as text only (React escapes it)."""
    text = _CONTROL_CHARS.sub("", raw) if isinstance(raw, str) else ""
    return " ".join(text.split())[:WRITE_IN_NAME_MAX].strip() or handle


def _candidate_for_handle(round_, handle):
    """(candidate or None, refusal code or "") for a handle already in the round. A write-in merged into somebody
    leads to that somebody (so the duplicate handle typed again finds the right person); a switched-off candidate is
    kol_handle_unavailable (an admin took them out, and typing them must not bring them back). (None, "") means
    nobody in the round has this handle."""
    candidate = KolCreator.objects.filter(edition_id=round_.edition_id, tiktok_handle=handle).first()
    if candidate is None:
        return None, ""
    for _ in range(5):                               # a merge chain is one hop in practice; never loop forever
        if candidate.active or not candidate.merged_into_id:
            break
        target = KolCreator.objects.filter(pk=candidate.merged_into_id, edition_id=round_.edition_id).first()
        if target is None:
            break
        candidate = target
    if not candidate.active:
        return None, "kol_handle_unavailable"
    return candidate, ""


@transaction.atomic
def _create_write_in(round_, adder, handle, name):
    candidate = KolCreator.objects.create(
        edition_id=round_.edition_id, name=name, tiktok_handle=handle, origin=KolCreator.WRITE_IN, added_by=adder,
    )
    _add_options(candidate, list(round_.poll.questions.order_by("order")))
    return candidate


@api_view(["POST"])
@authentication_classes([])
def write_in(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/write-in/  {handle, name?}  (kol_ballot cookie)

    200 {candidate, created}: the candidate this handle is in the round, made now when nobody had it (created =
    true). The page adds them to the award's picks and saves the sheet as usual (PUT ballot). 400 kol_bad_handle
    (not a TikTok handle) / kol_handle_unavailable (an admin took that person out); 403 kol_round_closed /
    kol_round_not_open; 429 kol_write_in_too_fast / kol_write_ins_too_many; 401 kol_code_needed /
    kol_code_replaced; 404 kol_round_not_found. Consumed by KolNominate.tsx (lib/kolNominations addWriteIn)."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    refusal = _closed_refusal(round_)
    if refusal:
        return refusal
    if _minute_limited("kol_write_in", code, WRITE_INS_PER_MINUTE):
        return _refuse("Adding too fast. Wait a moment.", "kol_write_in_too_fast", status.HTTP_429_TOO_MANY_REQUESTS)

    data = request.data if isinstance(request.data, dict) else {}
    handle = handle_from_text(data.get("handle"))
    bad_handle = _refuse("Type their TikTok @handle: letters, numbers, _ and . only.", "kol_bad_handle")
    if not handle:
        return bad_handle
    candidate, unavailable = _candidate_for_handle(round_, handle)
    if unavailable:
        return _refuse("That person cannot be nominated in this round.", unavailable)
    created = False
    if candidate is None:
        if not HANDLE_RE.match(handle):
            return bad_handle
        if KolCreator.objects.filter(added_by=code.creator).count() >= WRITE_INS_PER_CREATOR:
            return _refuse(f"You have added {WRITE_INS_PER_CREATOR} people already, the most one creator can add.",
                           "kol_write_ins_too_many", status.HTTP_429_TOO_MANY_REQUESTS)
        try:
            candidate, created = _create_write_in(round_, code.creator, handle, _clean_name(data.get("name"), handle)), True
        except IntegrityError:                       # two creators adding one person in the same instant: one row
            candidate, unavailable = _candidate_for_handle(round_, handle)
            if candidate is None:
                return _refuse("That person cannot be nominated in this round.", "kol_handle_unavailable")
    KolVoterCode.objects.filter(pk=code.pk).update(last_used_at=timezone.now())
    return Response({"candidate": candidate_row(candidate), "created": created}, status=status.HTTP_200_OK)


@transaction.atomic
def merge_write_in(source, target):
    """The write-in `source` is `target` under another handle: every pick of `source` moves to `target`, award by
    award; a ballot that already held `target` in that award keeps it ONCE (the source pick goes), so one person is
    never counted twice in one category (#637). Then `source` is switched off and remembers `target`. Picks are
    PollAnswer rows whose option belongs to a creator (KolCreatorOption), and a multiple-choice answer's `value` is
    empty, so repointing `option` is the whole move. Called by admin_creator; ensure_round has run first."""
    options = {}
    for option_id, question_id, creator_id in KolCreatorOption.objects.filter(
            creator_id__in=[source.pk, target.pk]).values_list("option_id", "option__question_id", "creator_id"):
        options.setdefault(question_id, {})[creator_id] = option_id
    for by_creator in options.values():
        source_option, target_option = by_creator.get(source.pk), by_creator.get(target.pk)
        if not source_option or not target_option:
            continue
        holding_target = list(PollAnswer.objects.filter(option_id=target_option).values_list("response_id", flat=True))
        PollAnswer.objects.filter(option_id=source_option, response_id__in=holding_target).delete()
        PollAnswer.objects.filter(option_id=source_option).update(option_id=target_option)
    KolCreator.objects.filter(pk=source.pk).update(active=False, merged_into=target)


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


# ADMIN (AFC poll admins only: afc_polls.permissions.is_polls_admin). Consumed by frontend
# app/(a)/a/polls/_components/KolNominationsAdmin.tsx through lib/kolNominations.ts.
#   GET   polls/admin/kol-nominations/<edition_slug>/                          the round, the creators, the tally
#   POST  polls/admin/kol-nominations/<edition_slug>/                          {opens_at, closes_at}: set the round up
#   POST  polls/admin/kol-nominations/<edition_slug>/codes/                    {creator_ids?}: new codes as a CSV
#   PATCH polls/admin/kol-nominations/<edition_slug>/creators/<creator_id>/    {active} switch a candidate on or off,
#                                                                              {merge_into} merge a duplicate write-in
# An admin sees WHO has voted and how many picks each creator got, never which creator picked whom. For the same
# reason a write-in row never says who added it: only a count of additions per list creator, to spot a spammer.


def _require_polls_admin(request):
    """(user, refusal). The same Bearer session and the same admin gate as every other afc_polls admin page."""
    from .permissions import is_polls_admin
    from .views import _user_from_request

    user = _user_from_request(request)
    if not user:
        return None, _refuse("You need to be signed in to do this", "need_signed", status.HTTP_401_UNAUTHORIZED)
    if not is_polls_admin(user):
        return None, _refuse("Only AFC poll admins can run the nominations.", "kol_admin_only",
                             status.HTTP_403_FORBIDDEN)
    return user, None


def _admin_edition(request, edition_slug):
    """(edition, refusal) for an admin address."""
    _, refusal = _require_polls_admin(request)
    if refusal:
        return None, refusal
    edition = AwardsEdition.objects.filter(slug=edition_slug).first()
    if edition is None:
        return None, _refuse("Awards edition not found", "awards_edition_not_found", status.HTTP_404_NOT_FOUND)
    return edition, None


def _creator_row(creator, live_codes, voted_keys, round_, added_counts):
    last_used = (
        KolVoterCode.objects.filter(creator=creator).exclude(last_used_at=None)
        .order_by("-last_used_at").values_list("last_used_at", flat=True).first()
    )
    return {
        "creator_id": creator.pk,
        "name": creator.name,
        "handle": creator.tiktok_handle,
        "country": creator.country,
        "sheet_status": creator.sheet_status,
        "active": creator.active,
        "origin": creator.origin,                                    # "list" or "write-in"
        "merged_into": creator.merged_into_id,
        "write_ins_added": added_counts.get(creator.pk, 0),          # how many people this creator added
        "has_code": creator.pk in live_codes,
        "voted": bool(round_) and respondent_key_for(round_, creator) in voted_keys,
        "last_used_at": last_used,
    }


def _admin_payload(edition):
    round_ = KolNominationRound.objects.select_related("poll", "edition").filter(edition=edition).first()
    creators = list(KolCreator.objects.filter(edition=edition).order_by("name"))
    live_codes = set(
        KolVoterCode.objects.filter(creator__edition=edition, revoked_at__isnull=True).values_list("creator_id", flat=True)
    )
    voted_keys = set()
    if round_ is not None:
        voted_keys = set(PollResponse.objects.filter(poll=round_.poll, status=PollResponse.SUBMITTED)
                         .values_list("respondent_key", flat=True))
    added_counts = dict(
        KolCreator.objects.filter(edition=edition, added_by__isnull=False)
        .values("added_by").annotate(n=Count("creator_id")).values_list("added_by", "n")
    )
    return {
        "edition": {"slug": edition.slug, "title": edition.title, "hidden": edition.hidden},
        "round": None if round_ is None else {
            "state": round_.state(), "opens_at": round_.opens_at, "closes_at": round_.closes_at,
            "awards": list(round_.poll.questions.order_by("order").values("question_id", "prompt")),
        },
        "creators": [_creator_row(creator, live_codes, voted_keys, round_, added_counts) for creator in creators],
        "tally": tally(round_) if round_ is not None else {},
        "failed_attempts": round_.failed_attempts if round_ is not None else 0,
    }


@api_view(["GET", "POST"])
def admin_round(request, edition_slug):
    """GET the round, its creators (has a code, has voted, last used) and the tally. POST {opens_at, closes_at}
    sets the round up (idempotent, ensure_round) and writes the dates; 400 kol_bad_dates for an unreadable date or
    a close before the open."""
    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    if request.method == "POST":
        from .views import PollFieldError, _parse_moment

        data = request.data if isinstance(request.data, dict) else {}
        try:
            opens_at = _parse_moment(data.get("opens_at"), "opens_at")
            closes_at = _parse_moment(data.get("closes_at"), "closes_at")
        except PollFieldError as error:
            return _refuse(str(error), "kol_bad_dates")
        if opens_at and closes_at and closes_at <= opens_at:
            return _refuse("Nominations must close after they open.", "kol_bad_dates")
        ensure_round(edition, opens_at=opens_at, closes_at=closes_at)
    return Response(_admin_payload(edition), status=status.HTTP_200_OK)


def _frontend_origin():
    from django.conf import settings as dj_settings

    return dj_settings.FRONTEND_URL_LOCAL if dj_settings.DEBUG else dj_settings.FRONTEND_URL


@api_view(["POST"])
def admin_codes(request, edition_slug):
    """POST {creator_ids?: [int]} -> text/csv attachment, one row per creator: name, tiktok_handle, country, code,
    link, message. With no ids: every switched-on creator who has no live code (so a second click after a full run
    is empty). With ids: those creators get NEW codes, which revokes their old ones. Only creators on the KOL List
    ever get a code: a write-in is a candidate, never a voter (#637). The codes exist in plain text only in this
    answer: it is never stored, logged or cached (Cache-Control: no-store)."""
    import csv
    import io

    from django.http import HttpResponse

    from .kol_messages import message_for

    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    round_ = KolNominationRound.objects.filter(edition=edition).first()
    if round_ is None:
        return _refuse("Set the round up first.", "kol_round_missing")
    data = request.data if isinstance(request.data, dict) else {}
    creators = KolCreator.objects.filter(edition=edition, active=True, origin=KolCreator.LIST).order_by("name")
    if data.get("creator_ids") is not None:
        try:
            ids = [int(value) for value in data.get("creator_ids")]
        except (TypeError, ValueError):
            return _refuse("creator_ids must be numbers", "kol_bad_creator_ids")
        creators = creators.filter(pk__in=ids)
    else:
        with_live_code = KolVoterCode.objects.filter(
            creator__edition=edition, revoked_at__isnull=True).values_list("creator_id", flat=True)
        creators = creators.exclude(pk__in=list(with_live_code))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "tiktok_handle", "country", "code", "link", "message"])
    base = f"{_frontend_origin()}/awards/{edition.slug}/nominate"
    for creator in creators:
        code = new_code(creator)
        link = f"{base}#{code}"
        writer.writerow([creator.name, creator.tiktok_handle, creator.country, code, link,
                         message_for(creator, code, link, round_.closes_at, edition.title)])
    # A byte-order mark so Excel opens the names (emoji, accents) as UTF-8 rather than as mojibake.
    answer = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    answer["Content-Disposition"] = f'attachment; filename="kol-codes-{edition.slug}.csv"'
    answer["Cache-Control"] = "no-store"
    return answer


@api_view(["PATCH"])
def admin_creator(request, edition_slug, creator_id):
    """PATCH {active: bool} or {merge_into: creator_id}.

    active: switching a candidate off revokes their live codes at once (their cookie stops working), takes them off
    every ballot and out of the tally; switching one back on makes them a plain candidate again (a merge undone by
    hand starts clean: merged_into is forgotten, the moved picks stay where they went).
    merge_into: the write-in at this address is the same person as that candidate (merge_write_in): 400
    kol_merge_not_write_in for a creator on the KOL List (never merged away), kol_bad_merge for a target that is not
    another switched-on candidate of this edition. 404 for a creator of another edition."""
    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    creator = KolCreator.objects.filter(pk=creator_id, edition=edition).first()
    if creator is None:
        return _refuse("Creator not found", "kol_creator_not_found", status.HTTP_404_NOT_FOUND)
    data = request.data if isinstance(request.data, dict) else {}
    if "merge_into" in data:
        return _merge(edition, creator, data.get("merge_into"))
    active = data.get("active")
    if not isinstance(active, bool):
        return _refuse("active must be true or false", "kol_bad_active")
    creator.active = active
    if active:
        creator.merged_into = None
    creator.save(update_fields=["active", "merged_into"])
    if not active:
        KolVoterCode.objects.filter(creator=creator, revoked_at__isnull=True).update(revoked_at=timezone.now())
    if active:
        round_ = KolNominationRound.objects.filter(edition=edition).first()
        if round_ is not None:
            ensure_round(edition)          # a creator switched on later still needs an option in every award
    return Response({"message": "Saved", "active": creator.active}, status=status.HTTP_200_OK)


def _merge(edition, source, raw_target):
    """admin_creator's merge branch. See merge_write_in for what moves."""
    if source.origin != KolCreator.WRITE_IN:
        return _refuse("Only a person added by a creator can be merged into another.", "kol_merge_not_write_in")
    bad = _refuse("Choose another person in this round to merge into.", "kol_bad_merge")
    if isinstance(raw_target, bool):
        return bad
    try:
        target_id = int(raw_target)
    except (TypeError, ValueError):
        return bad
    target = KolCreator.objects.filter(pk=target_id, edition=edition, active=True).exclude(pk=source.pk).first()
    if target is None:
        return bad
    if KolNominationRound.objects.filter(edition=edition).exists():
        ensure_round(edition)              # both have an option in every award before picks move
    merge_write_in(source, target)
    return Response({"message": "Merged", "active": False, "merged_into": target.pk}, status=status.HTTP_200_OK)
