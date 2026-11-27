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
    Owner, later on 26 Nov (#638 to #640): every award explained on the page (AWARD_KEYS: the page writes the
    explanation in the reader's language), SPARE codes with nobody behind them for creators found later (claim),
    and codes readable by poll admins so they can be copied from the site (code_sealed, admin_code).
    Owner, 27 Nov 2026 (#643 to #648): "Don't allow awards stay empty" (a ballot COUNTS only once every award has a
    pick: counted_response_ids), "a field for email and Whatsapp number for us to reach them" (KolCreator contact,
    asked once on the ballot before any pick is saved: contact), the codes by email from the admin tab and a
    creator's own picks by email (afc_polls.kol_emails: admin_emails, email_me).
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
import base64
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
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.db.models import Count, F, Q
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
# A stable key per award, in the same order (#638). The pages explain each award by its key in the reader's
# language (awardsKol.nominate.awardHelp.<key>, adminPolls.kolNom.awardHelp.<key>), so a renamed award keeps its text.
AWARD_KEYS = ("creator", "breakthrough", "educational", "streamer", "voice", "funniest")
MAX_PICKS = 6


def award_key(question):
    """The AWARD_KEYS entry of a round question, by its position ("" for anything past the six)."""
    return AWARD_KEYS[question.order] if 0 <= question.order < len(AWARD_KEYS) else ""


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


# THE SEALED COPY (#640, owner 26 Nov 2026: "Should be able to copy codes from the site"). The hash above is what the
# door looks a code up by; this Fernet copy is what a poll admin reads back (admin_code) to copy a code or its ready
# message. Its key is derived from SECRET_KEY by HKDF with a label of its own, the same way afc_auth.two_factor seals
# TOTP secrets, so it cannot collide with any other key made from SECRET_KEY (R63: encrypted at rest).
_CODE_BOX = None


def _code_box():
    global _CODE_BOX
    if _CODE_BOX is None:
        base = getattr(settings, "KOL_CODE_ENCRYPTION_KEY", None) or settings.SECRET_KEY
        if not base:
            raise RuntimeError("KOL codes cannot be sealed: SECRET_KEY is not set.")
        material = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"afc-kol-voter-code-v1").derive(
            str(base).encode("utf-8"))
        _CODE_BOX = Fernet(base64.urlsafe_b64encode(material))
    return _CODE_BOX


def _seal(body):
    return _code_box().encrypt(body.encode("utf-8")).decode("ascii")


def unseal_code(sealed):
    """The code a sealed copy holds, as KOL-XXXX-XXXX, or "" when there is none or it cannot be opened (a code
    made before copying existed, or a rotated SECRET_KEY): the admin is then told to make a new one."""
    if not sealed:
        return ""
    try:
        body = _code_box().decrypt(sealed.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
    return format_code(body)


def format_code(body):
    return f"KOL-{body[:4]}-{body[4:]}"


@transaction.atomic
def new_code(creator, edition=None):
    """A fresh code for `creator`, returned in plain text. Revokes every live code the creator had, so a forwarded old
    code and its cookie stop working at once. With creator None it is a SPARE code of `edition` (#639): nobody
    behind it until the first person to open it says who they are (claim)."""
    now = timezone.now()
    if creator is not None:
        KolVoterCode.objects.filter(creator=creator, revoked_at__isnull=True).update(revoked_at=now)
        edition_id = creator.edition_id
    else:
        edition_id = edition.pk
    while True:
        body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        digest = _code_hash(body)
        if not KolVoterCode.objects.filter(code_hash=digest).exists():
            break
    KolVoterCode.objects.create(creator=creator, edition_id=edition_id, spare=creator is None, code_hash=digest,
                                code_sealed=_seal(body))
    return format_code(body)


def live_code(round_, raw):
    """The live KolVoterCode this text opens in `round_`, or None: wrong, revoked, another edition's, a
    switched-off creator's, or a write-in's (a write-in is a candidate, never a voter, whatever code exists). An
    unclaimed SPARE code of this round opens too: the door then asks who they are."""
    normalized = normalize_code(raw)
    if len(normalized) != CODE_LENGTH:
        return None
    return (
        KolVoterCode.objects.select_related("creator")
        .filter(code_hash=_code_hash(normalized), revoked_at__isnull=True)
        .filter(Q(creator__isnull=True, spare=True, edition_id=round_.edition_id)
                | Q(creator__edition_id=round_.edition_id, creator__active=True, creator__origin=KolCreator.LIST))
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
    if code is None:
        return None, "kol_code_needed"
    if code.creator_id is None:
        # An unclaimed spare code (#639): it is the caller's job to ask who they are (claim) before a ballot.
        if not code.spare or code.edition_id != round_.edition_id:
            return None, "kol_code_needed"
    elif (code.creator.edition_id != round_.edition_id or not code.creator.active
            or code.creator.origin != KolCreator.LIST):
        return None, "kol_code_needed"
    if code.revoked_at is not None:
        return None, "kol_code_replaced"
    return code, ""


def _refuse(message, code, http_status=status.HTTP_400_BAD_REQUEST):
    return Response({"message": message, "code": code}, status=http_status)


def _no_round():
    return _refuse("Nominations not found", "kol_round_not_found", status.HTTP_404_NOT_FOUND)


def _claim_needed():
    return _refuse("Tell us who you are first.", "kol_claim_needed", status.HTTP_409_CONFLICT)


# THE BALLOT, as the page reads it. Speaks in creator ids only: option ids never leave the server.
def round_info(round_):
    """The round as every creator page reads it: title, state, dates."""
    return {
        "edition_title": round_.edition.title,
        "state": round_.state(),
        "opens_at": round_.opens_at,
        "closes_at": round_.closes_at,
    }


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
        "round": round_info(round_),
        "me": {"creator_id": creator.pk, "name": creator.name, "handle": creator.tiktok_handle},
        "awards": [
            {"question_id": question.question_id, "name": question.prompt, "key": award_key(question),
             "max": MAX_PICKS}
            for question in questions
        ],
        "candidates": [candidate_row(c) for c in candidates],
        "picks": picks,
        "saved_at": response.submitted_at if response is not None else None,
        # #643: the ballot counts only once every award has a pick (the same rule as counted_response_ids, read off
        # the picks this payload already holds: active candidates only).
        # #652: an award holding only the creator themselves does not count.
        "complete": bool(picks) and all(any(cid != creator.pk for cid in ids) for ids in picks.values()),
        # #644: the page asks for an email and a WhatsApp number until the creator has given them (contact); the
        # stored values are never sent back, only a masked hint of the email "Email me my picks" writes to (#648).
        "contact_needed": creator.contact_given_at is None,
        "email_hint": mask_email(creator.email) if creator.contact_given_at else "",
    }


def mask_email(address):
    """"l***@gmail.com" for "layo@gmail.com": enough for a creator to recognise their address, too little for a
    stranger holding a leaked code to learn it."""
    local, _, domain = (address or "").partition("@")
    if not local or not domain:
        return ""
    return f"{local[0]}***@{domain}"


# CONTACT (owner, 27 Nov 2026, #644: "a field for email and Whatsapp number for us to reach them"; "Both, required on
# ballot"). One validation for the ballot (contact) and the admin tab (admin_creator), so the two never accept
# different things.
WHATSAPP_RE = re.compile(r"^\d{8,15}$")      # E.164: a country code and the number, 8 to 15 digits in all
_PHONE_PUNCTUATION = re.compile(r"[\s().\-]")
CONTACT_PER_MINUTE = 10


def clean_email(raw):
    """(address, refusal code): the address trimmed with its domain in lower case, or "" and kol_bad_email. An empty
    value is ("", "") so the admin tab can clear a field."""
    from afc_auth.views import is_deliverable_address

    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return "", ""
    local, at, domain = text.rpartition("@")
    address = f"{local}@{domain.lower()}" if at else text
    if len(address) > 254 or not is_deliverable_address(address):
        return "", "kol_bad_email"
    return address, ""


def clean_whatsapp(raw):
    """(number, refusal code): "+<digits>" from what was typed ("+234 801 234 5678", "00234...", "234-801..."), or ""
    and kol_bad_whatsapp, or kol_whatsapp_country_code for a local number that starts with a single 0 (WhatsApp needs
    the country code). An empty value is ("", "")."""
    text = _PHONE_PUNCTUATION.sub("", raw) if isinstance(raw, str) else ""
    if not text:
        return "", ""
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("00"):
        text = text[2:]
    elif text.startswith("0"):
        return "", "kol_whatsapp_country_code"
    if not WHATSAPP_RE.match(text):
        return "", "kol_bad_whatsapp"
    return f"+{text}", ""


CONTACT_REFUSALS = {
    "kol_bad_email": "Type a valid email address.",
    "kol_bad_whatsapp": "Type your WhatsApp number with its country code, like +234 801 234 5678.",
    "kol_whatsapp_country_code": "Start your WhatsApp number with your country code, like +234.",
}


def _contact_needed():
    return _refuse("Give your email and WhatsApp number first.", "kol_contact_needed", status.HTTP_403_FORBIDDEN)


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
    if code.creator_id is None:
        # A spare code nobody has claimed yet (#639): the page asks who they are, then POSTs claim/.
        return _set_cookie(Response({"needs_claim": True, "round": round_info(round_)}, status=status.HTTP_200_OK),
                           round_, code)
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
    if code.creator_id is None:
        return _claim_needed()
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


@api_view(["PUT"])
@authentication_classes([])
def contact(request, edition_slug):
    """PUT polls/kol-nominations/<edition_slug>/contact/  {email, whatsapp}  (kol_ballot cookie)

    #644 (owner, 27 Nov 2026): the creator's email and WhatsApp number, both required, asked once before their first
    pick; again whenever they choose "Change my email or WhatsApp". 200 the ballot payload (contact_needed false).
    400 kol_bad_email / kol_bad_whatsapp / kol_whatsapp_country_code, 429 kol_contact_too_fast, 401 kol_code_needed /
    kol_code_replaced, 404 kol_round_not_found. Any state of the round: a closed round still wants to reach its
    creators. Consumed by KolNominate.tsx (lib/kolNominations saveContact)."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    if code.creator_id is None:
        return _claim_needed()
    if _minute_limited("kol_contact", code, CONTACT_PER_MINUTE):
        return _refuse("Too many tries. Wait a moment.", "kol_contact_too_fast", status.HTTP_429_TOO_MANY_REQUESTS)
    data = request.data if isinstance(request.data, dict) else {}
    email, bad_email = clean_email(data.get("email"))
    whatsapp, bad_whatsapp = clean_whatsapp(data.get("whatsapp"))
    if not email and not bad_email:
        bad_email = "kol_bad_email"                 # both are required here (the admin tab may clear one)
    if not whatsapp and not bad_whatsapp:
        bad_whatsapp = "kol_bad_whatsapp"
    for problem in (bad_email, bad_whatsapp):
        if problem:
            return _refuse(CONTACT_REFUSALS[problem], problem)
    creator = code.creator
    creator.email, creator.whatsapp, creator.contact_given_at = email, whatsapp, timezone.now()
    creator.save(update_fields=["email", "whatsapp", "contact_given_at"])
    KolVoterCode.objects.filter(pk=code.pk).update(last_used_at=timezone.now())
    return Response(ballot_payload(round_, creator), status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([])
def email_me(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/email-me/  (kol_ballot cookie)

    #648 (owner, 27 Nov 2026: "They should also be able to request to send to their own votes to their mails"): the
    creator's picks as saved now, to the email they gave, at once (kol_emails.email_picks). No code in the email.
    200 {message, email_hint}; 403 kol_contact_needed (no email given yet); 429 kol_email_too_many (3 in 10
    minutes); 502 kol_email_send_failed (the mail server refused); 401 kol_code_needed / kol_code_replaced; 404
    kol_round_not_found. Consumed by KolNominate.tsx (lib/kolNominations emailMe)."""
    from .kol_emails import email_picks, picks_limited

    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    if code.creator_id is None:
        return _claim_needed()
    creator = code.creator
    if creator.contact_given_at is None or not creator.email:
        return _contact_needed()
    if picks_limited(creator):
        return _refuse("You have asked for this email a few times already. Try again in a few minutes.",
                       "kol_email_too_many", status.HTTP_429_TOO_MANY_REQUESTS)
    row = email_picks(round_, creator, ballot_payload(round_, creator)["picks"])
    if row.status != row.SENT:
        return _refuse("The email could not be sent just now. Try again later.", "kol_email_send_failed",
                       status.HTTP_502_BAD_GATEWAY)
    return Response({"message": "Sent", "email_hint": mask_email(creator.email)}, status=status.HTTP_200_OK)


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
    """PUT body {picks: {"<question_id>": [creator_id]}, device_key?} for the creator behind `code`. 403
    kol_contact_needed until the creator has given their email and WhatsApp (#644)."""
    refusal = _closed_refusal(round_)
    if refusal:
        return refusal
    if code.creator.contact_given_at is None:
        return _contact_needed()
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
    true). The page adds them to the award's picks and saves the sheet as usual (PUT ballot). 403 kol_contact_needed
    before the creator gave their email and WhatsApp (#644). 400 kol_bad_handle
    (not a TikTok handle) / kol_handle_unavailable (an admin took that person out); 403 kol_round_closed /
    kol_round_not_open; 429 kol_write_in_too_fast / kol_write_ins_too_many; 401 kol_code_needed /
    kol_code_replaced; 404 kol_round_not_found. Consumed by KolNominate.tsx (lib/kolNominations addWriteIn)."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    if code.creator_id is None:
        return _claim_needed()
    refusal = _closed_refusal(round_)
    if refusal:
        return refusal
    if code.creator.contact_given_at is None:
        return _contact_needed()
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


# CLAIMING A SPARE CODE (#639, owner 26 Nov 2026: "in case of we find creators who should be able to vote, but are
# not on the list"; answer: "They enter name + handle"). The first person to open a spare code tells us their name,
# TikTok @handle and country. A handle nobody has yet becomes a new creator of the list (a voter AND a candidate,
# with an option in every award); a handle somebody added as a write-in becomes that same person, now a voter; a
# handle already on the KOL List is refused (that creator has, or will get, a personal code of their own: a spare
# code must not take over somebody else's ballot). From then on the code is that creator's, on any phone.
COUNTRY_MAX = 80


def _clean_country(raw):
    text = _CONTROL_CHARS.sub("", raw) if isinstance(raw, str) else ""
    return " ".join(text.split())[:COUNTRY_MAX]


@api_view(["POST"])
@authentication_classes([])
def claim(request, edition_slug):
    """POST polls/kol-nominations/<edition_slug>/claim/  {name?, handle, country?}  (kol_ballot cookie of a SPARE code)

    200 the ballot payload of the creator the code now belongs to. 400 kol_bad_handle / kol_claim_listed (that handle
    is on the KOL List) / kol_handle_unavailable (an admin took that person out) / kol_already_claimed (this code
    already belongs to somebody); 403 kol_round_closed; 429 kol_claim_too_fast; 401 kol_code_needed /
    kol_code_replaced; 404 kol_round_not_found. Consumed by KolNominate.tsx (lib/kolNominations claim)."""
    round_ = round_for_edition_slug(edition_slug)
    if round_ is None:
        return _no_round()
    code, reason = code_from_cookie(request, round_)
    if code is None:
        return _refuse("Enter your code to open your ballot.", reason, status.HTTP_401_UNAUTHORIZED)
    already = _refuse("This code already belongs to a creator.", "kol_already_claimed")
    if code.creator_id is not None:
        return already
    if round_.state() == KolNominationRound.CLOSED:
        return _refuse("Nominations are closed.", "kol_round_closed", status.HTTP_403_FORBIDDEN)
    if _minute_limited("kol_claim", code, WRITE_INS_PER_MINUTE):
        return _refuse("Too many tries. Wait a moment.", "kol_claim_too_fast", status.HTTP_429_TOO_MANY_REQUESTS)

    data = request.data if isinstance(request.data, dict) else {}
    handle = handle_from_text(data.get("handle"))
    bad_handle = _refuse("Type your TikTok @handle: letters, numbers, _ and . only.", "kol_bad_handle")
    if not handle:
        return bad_handle
    existing = KolCreator.objects.filter(edition_id=round_.edition_id, tiktok_handle=handle).first()
    if existing is not None and existing.origin == KolCreator.LIST:
        return _refuse("That creator is already on the list. Ask for your own personal code.", "kol_claim_listed")
    if existing is not None and not existing.active:
        return _refuse("That person cannot be nominated in this round.", "kol_handle_unavailable")
    if existing is None and not HANDLE_RE.match(handle):
        return bad_handle
    typed_name = _clean_name(data.get("name"), "")
    country = _clean_country(data.get("country"))

    with transaction.atomic():
        locked = KolVoterCode.objects.select_for_update().get(pk=code.pk)
        if locked.creator_id is not None:            # the same code claimed from two phones at once
            return already
        if existing is not None:
            existing.origin = KolCreator.LIST
            existing.name = typed_name or existing.name
            existing.country = country or existing.country
            existing.save(update_fields=["origin", "name", "country"])
            creator = existing
        else:
            creator = KolCreator.objects.create(
                edition_id=round_.edition_id, name=typed_name or handle, tiktok_handle=handle, country=country,
                sheet_status="Spare code", origin=KolCreator.LIST,
            )
            _add_options(creator, list(round_.poll.questions.order_by("order")))
        locked.creator = creator
        locked.last_used_at = timezone.now()
        locked.save(update_fields=["creator", "last_used_at"])
    return Response(ballot_payload(round_, creator), status=status.HTTP_200_OK)


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


# A BALLOT COUNTS ONLY WHEN EVERY AWARD HAS A PICK (owner, 27 Nov 2026, #643: "Don't allow awards stay empty";
# "Counts only when full is good"). Read at the moment of asking, never stored: a ballot saved before this rule, or one
# whose only pick in an award was switched off since, simply stops counting until the creator fills the award, and no
# flag can drift from the picks. The tally, "Have nominated" (admin tab) and Garena's live page all read it here.
def _ballot_rows(round_):
    """{response_id: {"key": respondent_key, "awards": number of awards that hold an active pick of somebody OTHER
    than the voter}} for every saved ballot an admin has not removed.

    #652 (owner, 27 Nov 2026: "if they nominate only themselves, their nomination will be discarded", "Per award"): an
    award where the creator picked only themselves counts as EMPTY, so a ballot with one counts only once they add
    somebody else there. A self-pick beside others still counts in the tally like any pick."""
    from .vote_tracking import COUNTED_ANSWER

    voters = list(KolCreator.objects.filter(edition_id=round_.edition_id, origin=KolCreator.LIST))
    voter_of = {respondent_key_for(round_, creator): creator.pk for creator in voters}
    # #657: a creator whose "Ballot counts" switch is off has their own ballot left out entirely.
    not_counted = {respondent_key_for(round_, creator) for creator in voters if not creator.ballot_counts}
    rows = (
        PollAnswer.objects.filter(response__poll=round_.poll, response__status=PollResponse.SUBMITTED,
                                  option__kol_creator_link__creator__active=True)
        .filter(COUNTED_ANSWER)
        .values_list("response_id", "response__respondent_key", "question_id", "option__kol_creator_link__creator_id")
    )
    ballots = {}
    for response_id, key, question_id, creator_id in rows:
        if key in not_counted:
            continue
        entry = ballots.setdefault(response_id, {"key": key, "with_others": set()})
        if creator_id != voter_of.get(key):
            entry["with_others"].add(question_id)
    return {rid: {"key": e["key"], "awards": len(e["with_others"])} for rid, e in ballots.items()}


def counted_response_ids(round_):
    """The ids of the ballots that count: saved, not removed by an admin, and in every award an active pick of
    somebody other than the voter (#643 every award, #652 not only themselves)."""
    total = round_.poll.questions.count()
    if not total:
        return set()
    return {rid for rid, row in _ballot_rows(round_).items() if row["awards"] >= total}


def awards_done_by_key(round_):
    """{respondent_key: awards that count} for every saved ballot (the admin tab's "4 of 6 awards")."""
    return {row["key"]: row["awards"] for row in _ballot_rows(round_).values()}


def tally(round_):
    """{question_id: [{creator_id, name, picks}]}, most picks first then by name, counting only the ballots that
    count (counted_response_ids: every award has a pick, not removed by an admin) and only creators still switched
    on. Read by the admin tab and Garena's live page; never by a creator."""
    rows = (
        PollAnswer.objects.filter(response_id__in=list(counted_response_ids(round_)),
                                  option__kol_creator_link__creator__active=True)
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


def live_block(edition):
    """The nomination round on Garena's private live page (#641, owner 26 Nov 2026: "Both"), or None when the edition
    has no round. Read by afc_polls.views.live_view (GET polls/live/<token>/), drawn by frontend
    app/(user)/awards/live/[token]/LiveView.tsx.

    {state, opens_at, closes_at, ballots, voters, awards: [{key, name, total_picks,
     candidates: [{name, handle, country, write_in, picks}]}]}

    Counts only, the same ones the admin tab shows (tally): no creator id, no voter, no code, nothing that says who
    nominated whom. `ballots` = creators who have saved a ballot an admin has not removed; `voters` = creators on the
    KOL List who may vote (a write-in is a candidate, never a voter). A candidate with no pick is left out. Since
    #643 a ballot counts only once every award has a pick (counted_response_ids)."""
    round_ = KolNominationRound.objects.select_related("poll").filter(edition=edition).first()
    if round_ is None:
        return None
    counts = tally(round_)
    candidates = {creator.pk: creator for creator in KolCreator.objects.filter(edition=edition, active=True)}
    awards = []
    for question in round_.poll.questions.order_by("order"):
        rows = []
        for entry in counts.get(question.question_id, []):
            creator = candidates.get(entry["creator_id"])
            if creator is None:
                continue
            rows.append({"name": creator.name, "handle": creator.tiktok_handle, "country": creator.country,
                         "write_in": creator.origin == KolCreator.WRITE_IN, "picks": entry["picks"]})
        awards.append({"key": award_key(question), "name": question.prompt,
                       "total_picks": sum(row["picks"] for row in rows), "candidates": rows})
    return {
        "state": round_.state(),
        "opens_at": round_.opens_at,
        "closes_at": round_.closes_at,
        "ballots": len(counted_response_ids(round_)),
        "voters": KolCreator.objects.filter(edition=edition, active=True, origin=KolCreator.LIST).count(),
        "awards": awards,
    }


# ADMIN (AFC poll admins, and Garena's KOL staff on an edition that has a round: afc_polls.permissions
# .can_run_kol_round, #641). Consumed by frontend app/(a)/a/polls/_components/KolNominationsAdmin.tsx through
# lib/kolNominations.ts.
#   GET   polls/admin/kol-nominations/                                         the editions that have a round (#641)
#   GET   polls/admin/kol-nominations/<edition_slug>/                          the round, the creators, the tally
#   POST  polls/admin/kol-nominations/<edition_slug>/                          {opens_at, closes_at}: set the round up
#   POST  polls/admin/kol-nominations/<edition_slug>/codes/                    {creator_ids?}: new codes as a CSV
#   PATCH polls/admin/kol-nominations/<edition_slug>/creators/<creator_id>/    {active} switch a candidate on or off,
#                                                                              {merge_into} merge a duplicate write-in,
#                                                                              {email, whatsapp} their contact (#644)
#   POST  polls/admin/kol-nominations/<edition_slug>/emails/                   {creator_ids} or {all: true}: email
#                                                                              their codes (#646, kol_emails)
# An admin sees WHO has voted and how many picks each creator got, never which creator picked whom. For the same
# reason a write-in row never says who added it: only a count of additions per list creator, to spot a spammer.


def _require_kol_admin(request):
    """(user, refusal). The same Bearer session as every other afc_polls admin page; an AFC poll admin or a
    kol_admin (Garena's KOL staff, #641) passes. Which edition a kol_admin may reach is _admin_edition's call."""
    from .permissions import holds_kol_admin_role, is_polls_admin
    from .views import _user_from_request

    user = _user_from_request(request)
    if not user:
        return None, _refuse("You need to be signed in to do this", "need_signed", status.HTTP_401_UNAUTHORIZED)
    if not (is_polls_admin(user) or holds_kol_admin_role(user)):
        return None, _refuse("Only AFC poll admins and KOL admins can run the nominations.", "kol_admin_only",
                             status.HTTP_403_FORBIDDEN)
    return user, None


def _admin_edition(request, edition_slug):
    """(edition, refusal) for an admin address. A kol_admin reaches only an edition that already has a round
    (afc_polls.permissions.can_run_kol_round); any other edition answers the same 404 as a missing one, so the
    address cannot be used to learn which editions exist (R88)."""
    from .permissions import can_run_kol_round

    user, refusal = _require_kol_admin(request)
    if refusal:
        return None, refusal
    edition = AwardsEdition.objects.filter(slug=edition_slug).first()
    if edition is None or not can_run_kol_round(user, edition):
        return None, _refuse("Awards edition not found", "awards_edition_not_found", status.HTTP_404_NOT_FOUND)
    return edition, None


@api_view(["GET"])
def admin_editions(request):
    """GET polls/admin/kol-nominations/ -> {results: [{slug, title, theme, hidden}]}: the editions that have a
    nomination round, newest first. The edition picker of a kol_admin's Nominations tab (#641), who is refused the
    full editions list (polls/admin/editions/). Auth: an AFC poll admin or a kol_admin; 401 signed out, 403
    kol_admin_only otherwise. Consumed by frontend app/(a)/a/polls/page.tsx (kolNominationsAdminApi.editions)."""
    _, refusal = _require_kol_admin(request)
    if refusal:
        return refusal
    editions = AwardsEdition.objects.filter(kol_round__isnull=False).order_by("-year", "-pk")
    return Response({"results": [
        {"slug": edition.slug, "title": edition.title, "theme": edition.theme, "hidden": edition.hidden}
        for edition in editions
    ]}, status=status.HTTP_200_OK)


def _creator_row(creator, live_codes, voted_keys, round_, added_counts, awards_done, emails):
    """One creator on the admin tab. `live_codes` maps creator_id to their live code's id, which the tab passes to
    admin_code to copy the code (#640). `voted_keys` holds the ballots that COUNT (#643); `awards_done` how many awards
    each saved ballot fills, so a started ballot reads "4 of 6". Contact (#644) and the latest code email (#646)."""
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
        "code_id": live_codes.get(creator.pk),
        "voted": bool(round_) and respondent_key_for(round_, creator) in voted_keys,
        "awards_done": awards_done.get(respondent_key_for(round_, creator), 0) if round_ else 0,
        "last_used_at": last_used,
        "email": creator.email,
        "whatsapp": creator.whatsapp,
        "contact_given_at": creator.contact_given_at,
        "ballot_counts": creator.ballot_counts,                     # #657: their own ballot counts (separate switch)
        "last_email": emails.get(creator.pk),                        # {status, at, error, to} or None
    }


def _admin_payload(edition):
    round_ = KolNominationRound.objects.select_related("poll", "edition").filter(edition=edition).first()
    creators = list(KolCreator.objects.filter(edition=edition).order_by("name"))
    live_codes = dict(
        KolVoterCode.objects.filter(creator__edition=edition, revoked_at__isnull=True)
        .values_list("creator_id", "code_id")
    )
    from .kol_emails import last_code_emails

    voted_keys, awards_done = set(), {}
    if round_ is not None:
        voted_keys = set(PollResponse.objects.filter(pk__in=list(counted_response_ids(round_)))
                         .values_list("respondent_key", flat=True))
        awards_done = awards_done_by_key(round_)
    emails = last_code_emails([creator.pk for creator in creators])
    added_counts = dict(
        KolCreator.objects.filter(edition=edition, added_by__isnull=False)
        .values("added_by").annotate(n=Count("creator_id")).values_list("added_by", "n")
    )
    return {
        # live_view_token: Garena's private live link of this edition (#641), which the tab can make, copy,
        # replace or turn off (views.admin_edition_live_link); "" when it is off.
        "edition": {"slug": edition.slug, "title": edition.title, "hidden": edition.hidden,
                    "live_view_token": edition.live_view_token or ""},
        "round": None if round_ is None else {
            "state": round_.state(), "opens_at": round_.opens_at, "closes_at": round_.closes_at,
            "awards": [{"question_id": q.question_id, "prompt": q.prompt, "key": award_key(q)}
                       for q in round_.poll.questions.order_by("order")],
        },
        "creators": [_creator_row(creator, live_codes, voted_keys, round_, added_counts, awards_done, emails)
                     for creator in creators],
        "award_count": round_.poll.questions.count() if round_ is not None else 0,
        "tally": tally(round_) if round_ is not None else {},
        "failed_attempts": round_.failed_attempts if round_ is not None else 0,
        # Spare codes (#639), live ones only, oldest first; claimed_by once somebody has said who they are.
        "spare_codes": [
            {
                "code_id": code.code_id, "created_at": code.created_at, "last_used_at": code.last_used_at,
                "claimed_by": None if code.creator is None else {
                    "creator_id": code.creator.pk, "name": code.creator.name, "handle": code.creator.tiktok_handle},
            }
            for code in KolVoterCode.objects.filter(edition=edition, spare=True, revoked_at__isnull=True)
            .select_related("creator").order_by("created_at", "code_id")
        ],
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
    """POST {creator_ids?: [int]} or {spare: n} -> text/csv attachment, one row per code: name, tiktok_handle,
    country, code, link, message. {spare: n} (1 to MAX_SPARE) makes n SPARE codes with nobody behind them (#639): name,
    handle and country empty, the message the English spare one (the tab copies it in French or Portuguese). With no ids: every switched-on creator who has no live code (so a second click after a full run
    is empty). With ids: those creators get NEW codes, which revokes their old ones. Only creators on the KOL List
    ever get a code: a write-in is a candidate, never a voter (#637). The codes exist in plain text only in this
    answer and, sealed, in the database for admin_code (#640); never logged or cached (Cache-Control: no-store)."""
    from .kol_messages import message_for

    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    round_ = KolNominationRound.objects.filter(edition=edition).first()
    if round_ is None:
        return _refuse("Set the round up first.", "kol_round_missing")
    data = request.data if isinstance(request.data, dict) else {}
    if "spare" in data:
        return _spare_codes(edition, round_, data.get("spare"))
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

    rows = []
    for creator in creators:
        code = new_code(creator)
        link = ballot_link(edition, code)
        rows.append([creator.name, creator.tiktok_handle, creator.country, code, link,
                     message_for(creator, code, link, round_.closes_at, edition.title)])
    return _codes_csv(edition, rows)


MAX_SPARE = 50


def ballot_link(edition, code):
    """A creator's personal link: the code after "#" so it never reaches a server log (design 5.4)."""
    return f"{_frontend_origin()}/awards/{edition.slug}/nominate#{code}"


def _codes_csv(edition, rows):
    import csv
    import io

    from django.http import HttpResponse

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "tiktok_handle", "country", "code", "link", "message"])
    writer.writerows(rows)
    # A byte-order mark so Excel opens the names (emoji, accents) as UTF-8 rather than as mojibake.
    answer = HttpResponse("\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    answer["Content-Disposition"] = f'attachment; filename="kol-codes-{edition.slug}.csv"'
    answer["Cache-Control"] = "no-store"
    return answer


def _spare_codes(edition, round_, raw_count):
    from .kol_messages import spare_message_for

    if isinstance(raw_count, bool):
        raw_count = None
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= MAX_SPARE:
        return _refuse(f"Make between 1 and {MAX_SPARE} spare codes at a time.", "kol_bad_spare_count")
    rows = []
    for _ in range(count):
        code = new_code(None, edition=edition)
        link = ballot_link(edition, code)
        rows.append(["", "", "", code, link, spare_message_for(code, link, round_.closes_at, edition.title, "en")])
    return _codes_csv(edition, rows)


@api_view(["GET", "DELETE"])
def admin_code(request, edition_slug, code_id):
    """GET / DELETE polls/admin/kol-nominations/<edition_slug>/codes/<code_id>/  (AFC poll admins)

    GET {code, link, message, language}: one LIVE code read back from its sealed copy so the tab can copy it (#640);
    a creator's message is in their language, a spare code's in ?lang=en|fr|pt (default en). Cache-Control: no-store,
    and nothing here is logged. 404 kol_code_not_found (unknown, revoked or another edition's), kol_code_unreadable
    (made before copying existed: make a new one). DELETE revokes the code (a spare one leaves the spare list; a
    creator's leaves them without a code until a new one is made). Consumed by KolNominationsAdmin.tsx."""
    from .kol_messages import language_for, message_for, spare_message_for

    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    code = (
        KolVoterCode.objects.select_related("creator")
        .filter(pk=code_id, revoked_at__isnull=True)
        .filter(Q(edition=edition) | Q(creator__edition=edition))
        .first()
    )
    if code is None:
        return _refuse("Code not found", "kol_code_not_found", status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        KolVoterCode.objects.filter(pk=code.pk).update(revoked_at=timezone.now())
        return Response({"message": "Revoked"}, status=status.HTTP_200_OK)
    plain = unseal_code(code.code_sealed)
    if not plain:
        return _refuse("This code was made before codes could be copied. Make a new one to copy it.",
                       "kol_code_unreadable", status.HTTP_404_NOT_FOUND)
    round_ = KolNominationRound.objects.filter(edition=edition).first()
    closes_at = round_.closes_at if round_ is not None else None
    link = ballot_link(edition, plain)
    if code.creator is not None:
        language = language_for(code.creator.country)
        message = message_for(code.creator, plain, link, closes_at, edition.title)
    else:
        language = request.query_params.get("lang") if request.query_params.get("lang") in ("en", "fr", "pt") else "en"
        message = spare_message_for(plain, link, closes_at, edition.title, language)
    answer = Response({"code": plain, "link": link, "message": message, "language": language},
                      status=status.HTTP_200_OK)
    answer["Cache-Control"] = "no-store"
    return answer


@api_view(["PATCH"])
def admin_creator(request, edition_slug, creator_id):
    """PATCH {active: bool}, {merge_into: creator_id}, {email?, whatsapp?} or {ballot_counts: bool}.

    ballot_counts (#657, owner 27 Nov 2026: "do a separate switch for that"): whether this creator's own saved ballot
    counts. Off leaves their ballot saved but out of the tally, "Have nominated" and Garena's live page; it does not
    switch them off as a nominee or revoke their code. 400 kol_bad_ballot_counts for a non-boolean.

    email / whatsapp (#644): the creator's contact as an admin types it, validated like the ballot's (clean_email,
    clean_whatsapp); "" clears one. 400 kol_bad_email / kol_bad_whatsapp / kol_whatsapp_country_code. It does not
    count as the creator giving it: the ballot still asks them once.

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
    if "email" in data or "whatsapp" in data:
        return _admin_contact(creator, data)
    if "ballot_counts" in data:
        # #657: the separate "Ballot counts" switch; it never touches `active`, codes or the creator as a nominee.
        value = data.get("ballot_counts")
        if not isinstance(value, bool):
            return _refuse("ballot_counts must be true or false", "kol_bad_ballot_counts")
        KolCreator.objects.filter(pk=creator.pk).update(ballot_counts=value)
        return Response({"message": "Saved", "ballot_counts": value}, status=status.HTTP_200_OK)
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


def _admin_contact(creator, data):
    """admin_creator's contact branch (#644). Only the keys sent change."""
    fields = []
    if "email" in data:
        email, problem = clean_email(data.get("email"))
        if problem:
            return _refuse(CONTACT_REFUSALS[problem], problem)
        creator.email = email
        fields.append("email")
    if "whatsapp" in data:
        whatsapp, problem = clean_whatsapp(data.get("whatsapp"))
        if problem:
            return _refuse(CONTACT_REFUSALS[problem], problem)
        creator.whatsapp = whatsapp
        fields.append("whatsapp")
    creator.save(update_fields=fields)
    return Response({"message": "Saved", "email": creator.email, "whatsapp": creator.whatsapp},
                    status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_emails(request, edition_slug):
    """POST polls/admin/kol-nominations/<edition_slug>/emails/  {creator_ids: [int]} or {all: true}

    #646 (owner, 27 Nov 2026: "send individually or to add and send selected once or to send to all"): email each
    chosen creator their personal code, link and message (afc_polls.kol_emails). Only switched-on creators of the KOL
    List with an address are emailed; `all` means every one of them. A creator with no live readable code gets one
    first (which, like "New code", replaces an old unreadable one). A creator whose email is still on its way is not
    queued twice. Answers at once: {queued, no_email, not_voters, already_sending}; the sending happens on a paced
    thread and the tab reads each row's result (last_email). 400 kol_round_missing / kol_bad_creator_ids; the usual
    401 / 403 / 404 of the admin addresses. Auth: AFC poll admins and KOL admins (can_run_kol_round). Consumed by
    KolNominationsAdmin.tsx (lib/kolNominations sendCodeEmails)."""
    from .kol_emails import queue_code_emails, queued_now
    from .views import _user_from_request

    edition, refusal = _admin_edition(request, edition_slug)
    if refusal:
        return refusal
    if not KolNominationRound.objects.filter(edition=edition).exists():
        return _refuse("Set the round up first.", "kol_round_missing")
    data = request.data if isinstance(request.data, dict) else {}
    everyone = KolCreator.objects.filter(edition=edition)
    if data.get("all") is True:
        chosen = list(everyone.filter(active=True, origin=KolCreator.LIST))
    else:
        raw = data.get("creator_ids")
        if not isinstance(raw, list) or not raw or any(isinstance(value, bool) for value in raw):
            return _refuse("Choose at least one creator.", "kol_bad_creator_ids")
        try:
            ids = {int(value) for value in raw}
        except (TypeError, ValueError):
            return _refuse("creator_ids must be numbers", "kol_bad_creator_ids")
        chosen = list(everyone.filter(pk__in=ids))
    voters = [c for c in chosen if c.active and c.origin == KolCreator.LIST]
    with_address = [c for c in voters if c.email]
    sending = queued_now(c.pk for c in with_address)
    ready = [c for c in with_address if c.pk not in sending]
    for creator in ready:
        code = (KolVoterCode.objects.filter(creator=creator, revoked_at__isnull=True)
                .order_by("-created_at", "-code_id").first())
        if code is None or not unseal_code(code.code_sealed):
            new_code(creator)
    queue_code_emails(ready, _user_from_request(request))
    return Response({
        "queued": len(ready),
        "no_email": len(voters) - len(with_address),
        "not_voters": len(chosen) - len(voters),
        "already_sending": len(sending),
    }, status=status.HTTP_200_OK)
