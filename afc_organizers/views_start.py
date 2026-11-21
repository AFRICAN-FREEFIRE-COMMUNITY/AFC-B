# afc_organizers/views_start.py
# ──────────────────────────────────────────────────────────────────────────────
# Anyone starts an organization (open organizations, plan 1 Task 2; owner 16 Nov 2026, AFC inbox #582 #583:
# "anyone will be able to create an organization ... any organization can use the full features free").
#
# Until now only an AFC admin provisioned an organization (views_admin.admin_create_organization). These two
# addresses let a signed-in person start one themselves, as a COMMUNITY organization (badges.py: every tool works the
# same; its results do not count for the rankings until an AFC admin gives the Verified tick).
#
#   POST organizers/start/check-name/   JSON {name}          -> {ok: true, slug} | {ok: false, code}
#   POST organizers/start/              multipart, see start_organization below -> 201 {organization}
#
# Caller: the site's /organizations/start page (app/(user)/organizations/start, StartOrganization.tsx): step 1 checks
# the name as it is typed, step 2 posts the whole form with the Turnstile token.
#
# Guards: a session (validate_token), the bot check (afc_auth.bot_protection.require_human, R68) on the start, the
# reserved names (whole word, so nobody can pose as Garena, Free Fire, AFC or Booyah), a name folded for case and
# spacing must be free, at most MAX_OWNED self-started organizations per person and MAX_PER_DAY starts a day (R59),
# the logo through require_image_upload (R70). The name check and the create run under one cache lock per folded
# name, so two people pressing at the same second cannot both get it.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import re

from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.bot_protection import require_human
from afc_auth.image_utils import require_image_upload
from afc_auth.models import Roles, UserRoles
from afc_auth.views import validate_token

from .models import Organization, OrganizationBadgeHistory, OrganizationMember

# Whole words, after folding case and turning every run of non-letters into one space (the approved mockup's
# rule: "Garena-ish" is refused, "Garenaish" is not).
RESERVED = ("garena", "free fire", "freefire", "afc", "african free fire community", "booyah")
NAME_MIN, NAME_MAX = 3, 60
DESCRIPTION_MAX = 1000
MAX_OWNED = 3          # self-started organizations one person may own
MAX_PER_DAY = 3        # starts a day per person
CHECKS_PER_MINUTE = 60
SOCIAL_KEYS = ("instagram", "discord")

_MESSAGES = {
    "org_name_reserved": "That name belongs to someone else, so it cannot be used. Pick another name.",
    "org_name_taken": "There is already an organization with that name. Pick another one.",
    "org_name_invalid": "Use a name of 3 to 60 characters.",
    "org_country_invalid": "Pick the country your organization is based in.",
    "org_description_too_long": "Keep the about text under 1,000 characters.",
    "org_email_invalid": "That email address does not look right.",
    "org_social_invalid": "A social link must be a full https:// address.",
    "org_rules_not_accepted": "Read and accept the organizer rules to continue.",
    "org_start_limit": "You already run 3 organizations you started. Ask AFC if you need another one.",
    "org_start_rate_limited": "You started 3 organizations today. Try again tomorrow.",
    "org_name_check_rate_limited": "Too many name checks. Wait a minute and try again.",
}


def _refuse(code, http=status.HTTP_400_BAD_REQUEST):
    return Response({"message": _MESSAGES[code], "code": code}, status=http)


def _signed_in(request):
    """(user, None) or (None, the 401 Response), the same handshake as views_organizer.get_my_organizations."""
    header = request.headers.get("Authorization")
    if not header:
        return None, Response({"message": "Please sign in to continue.", "code": "authorization_header_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    if not header.startswith("Bearer ") or " " not in header:
        return None, Response({"message": "Please sign in to continue.", "code": "invalid_token_format"},
                              status=status.HTTP_401_UNAUTHORIZED)
    user = validate_token(header.split(" ", 1)[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token.", "code": "invalid_expired_session_token"},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def normalize_name(name):
    """Lower case, spaces collapsed: the form two names are compared in."""
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def _words(folded):
    return " " + re.sub(r"[^a-z0-9]+", " ", folded).strip() + " "


def name_refusal(name):
    """The refusal code for a proposed organization name, or None when it is free to use."""
    folded = normalize_name(name)
    if not (NAME_MIN <= len(folded) <= NAME_MAX):
        return "org_name_invalid"
    words = _words(folded)
    if any(" " + r + " " in words for r in RESERVED):
        return "org_name_reserved"
    # Folded comparison against every organization that shares the first word (cheap, and exact on spacing).
    first = folded.split(" ")[0]
    for existing in Organization.objects.filter(name__icontains=first).values_list("name", flat=True):
        if normalize_name(existing) == folded:
            return "org_name_taken"
    return None


def _unique_slug(name):
    base = slugify(name) or "organization"
    slug, n = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _day_key(user):
    return f"orgstart:day:{user.pk}:{datetime.date.today().isoformat()}"


@api_view(["POST"])
def check_organization_name(request):
    """Whether a name may be used, as the person types it on step 1. Always 200 with ok / code, except auth and the
    per-minute limit."""
    user, err = _signed_in(request)
    if err:
        return err
    key = f"orgstart:check:{user.pk}:{int(datetime.datetime.now().timestamp() // 60)}"
    cache.add(key, 0, 90)
    try:
        if cache.incr(key) > CHECKS_PER_MINUTE:
            return _refuse("org_name_check_rate_limited", status.HTTP_429_TOO_MANY_REQUESTS)
    except ValueError:
        pass
    name = request.data.get("name") if isinstance(request.data, dict) else None
    code = name_refusal(name)
    if code:
        return Response({"ok": False, "code": code, "message": _MESSAGES[code]})
    return Response({"ok": True, "slug": _unique_slug(normalize_name(name))})


@api_view(["POST"])
def start_organization(request):
    """Start a Community organization owned by the signed-in person.

    Body (multipart): name, country (ISO 3166 alpha-2), description (optional, up to 1,000), email (optional),
    instagram and discord (optional https:// links), logo (optional picture), rules_accepted ("true"), and the
    Turnstile token the bot check reads.
    201 {organization: {slug, name, badge}}; refusals carry a code (see _MESSAGES, plus bot_check_failed and the
    picture gate's not_an_image / image_too_large)."""
    user, err = _signed_in(request)
    if err:
        return err
    blocked = require_human(request, where="start organization")
    if blocked is not None:
        return blocked

    data = request.data
    if str(data.get("rules_accepted", "")).lower() not in ("true", "1", "yes"):
        return _refuse("org_rules_not_accepted")
    name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
    country = str(data.get("country") or "").strip().upper()
    description = str(data.get("description") or "").strip()
    email = str(data.get("email") or "").strip()
    # Optional links on step 2 (the mockup's Instagram and Discord); one of them completes the profile rule.
    socials = {}
    for key in SOCIAL_KEYS:
        link = str(data.get(key) or "").strip()
        if link:
            if len(link) > 200 or not link.lower().startswith("https://"):
                return _refuse("org_social_invalid")
            socials[key] = link
    try:
        import pycountry
        if len(country) != 2 or pycountry.countries.get(alpha_2=country) is None:
            return _refuse("org_country_invalid")
    except ImportError:  # pragma: no cover - pycountry is in requirements
        if not re.fullmatch(r"[A-Z]{2}", country):
            return _refuse("org_country_invalid")
    if len(description) > DESCRIPTION_MAX:
        return _refuse("org_description_too_long")
    if email:
        try:
            validate_email(email)
        except ValidationError:
            return _refuse("org_email_invalid")
    logo = None
    if request.FILES.get("logo") is not None:
        logo, code = require_image_upload(request.FILES["logo"])
        if code:
            return Response({"message": "That logo is not a picture AFC can show. Use a PNG, JPG or WEBP.",
                             "code": code}, status=status.HTTP_400_BAD_REQUEST)

    if (cache.get(_day_key(user)) or 0) >= MAX_PER_DAY:
        return _refuse("org_start_rate_limited", status.HTTP_429_TOO_MANY_REQUESTS)
    owned = OrganizationMember.objects.filter(user=user, role="owner", status="active",
                                              organization__self_started=True).exclude(
        organization__status="deleted").count()
    if owned >= MAX_OWNED:
        return _refuse("org_start_limit")

    folded = normalize_name(name)
    lock = "orgstart:name:" + slugify(folded)
    if not cache.add(lock, 1, 30):
        return _refuse("org_name_taken")
    try:
        code = name_refusal(name)
        if code:
            return _refuse(code)
        with transaction.atomic():
            org = Organization.objects.create(
                name=name, slug=_unique_slug(name), country=country, description=description,
                email=email or None, badge="community", self_started=True, created_by=user, logo=logo,
                socials=socials,
            )
            OrganizationMember.objects.create(organization=org, user=user, role="owner", status="active",
                                              invited_by=user)
            role, _ = Roles.objects.get_or_create(role_name="organizer")
            UserRoles.objects.get_or_create(user=user, role=role)
            OrganizationBadgeHistory.objects.create(organization=org, action="started", to_badge="community", by=user)
    finally:
        cache.delete(lock)

    day = _day_key(user)
    cache.add(day, 0, 26 * 3600)
    try:
        cache.incr(day)
    except ValueError:
        cache.set(day, 1, 26 * 3600)
    return Response({"organization": {"slug": org.slug, "name": org.name, "badge": org.badge}},
                    status=status.HTTP_201_CREATED)
