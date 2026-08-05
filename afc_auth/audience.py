# afc_auth/audience.py
# ──────────────────────────────────────────────────────────────────────────────
# BROADCAST AUDIENCE BUILDER (owner backlog item 15, 2026-08-03)
#
# "Notifications settings: admins select specific teams and players, or filter by category
#  (tier, country, others), for notification or bulk mail, and can send to the entire site."
#
# The DELIVERY half of broadcasting already exists and is untouched by this module:
# afc_auth.views.deliver_broadcast writes the in-app Notifications rows, sends the branded email
# on a daemon thread, and records a SentBroadcast history row. What was missing was RECIPIENT
# SELECTION. This module is that missing piece and nothing else: it turns an admin's filter
# choices into a User QUERYSET, and it answers "how big is this audience?" with a database
# count. It never sends anything and never loads users into memory.
#
# WHY A QUERYSET, NOT A LIST: AFC has thousands of users. Every count on this page is
# queryset.count() (one SQL COUNT), and every id set that feeds a filter is passed as a
# SUBQUERY (a .values() queryset), never as a Python list of ids. An admin can therefore preview
# "everyone" without the server materialising 6,790 User objects.
#
# THE FILTER SPEC (one dict, sent by the composer, parsed by parse_audience_spec):
#   {
#     "everyone":   bool,          # send to the whole site - overrides everything else
#     "user_ids":   [int],         # explicitly picked players
#     "team_ids":   [int],         # explicitly picked teams (their members AND their owner)
#     "tiers":      ["1","2","3"], # category: players on a team of this tier (afc_team.Team.team_tier)
#     "countries":  ["Nigeria"],   # category: the player's country
#     "roles":      ["player"],    # category: afc_auth.User.role
#     "languages":  ["fr"],        # category: afc_auth.User.language (who reads French, etc.)
#     "include_suspended": bool,   # default False - suspended accounts are excluded
#   }
#
# HOW THE PIECES COMBINE (this is the rule the UI states in words, so it must not drift):
#   - "everyone" wins outright: the audience is every eligible user.
#   - Otherwise the audience is the UNION of three independent selections:
#         explicitly picked players
#       + members/owners of explicitly picked teams
#       + everyone matching the CATEGORY filters
#     so "these two teams, plus this one player, plus every Tier 1 player in Ghana" is one send.
#   - WITHIN the category block the filters INTERSECT: tiers AND countries AND roles AND
#     languages. "Tier 1" + "Ghana" means Tier 1 players who are in Ghana, not the two added
#     together. A category with nothing selected simply does not narrow anything.
#   - Nothing selected at all = an EMPTY audience (never an accidental send-to-all).
#
# ELIGIBILITY FLOOR: an audience always excludes deactivated accounts (User.is_active=False) and,
# unless include_suspended is set, suspended ones (User.status="suspended"). Messaging an account
# that cannot log in is wasted email volume.
#
# EMAIL VOLUME (the constraint that shapes the whole feature): AFC's transactional mail goes out
# through Microsoft 365, which throttles at roughly 30 messages per MINUTE and 1,000 per day to
# recipients who have never received mail from AFC. A "send to everyone" over ~6,800 users cannot
# physically deliver as email. email_volume_assessment() below turns a recipient count into a
# plain warning the admin sees BEFORE sending, and the send endpoint refuses an email blast that
# exceeds the daily cap rather than queueing something that will silently fail.
#
# HOW THIS CONNECTS TO THE REST OF THE SYSTEM:
#   - Consumed by afc_auth/views_broadcast_audience.py (preview / options / send endpoints),
#     which are routed in afc_auth/urls.py under auth/admin/broadcast-audience/.
#   - The send endpoint hands the resolved recipients to afc_auth.views.deliver_broadcast, so
#     these audiences produce exactly the same Notifications rows, branded emails and
#     SentBroadcast history as every other broadcast on the site.
#   - Reads afc_auth.User (role/country/ip_country/language/status) and afc_team.Team /
#     TeamMembers (team_tier + membership). It writes nothing.
#   - Frontend consumer: the admin Settings > Notifications tab audience builder
#     (frontend/app/(a)/a/settings/_components/AudienceBuilder.tsx).
# ──────────────────────────────────────────────────────────────────────────────
import math

from django.db.models import Q

from afc_team.models import Team, TeamMembers

from .country_grouping import expand_country_keys
from .models import User


# ── Email volume limits (Microsoft 365 transactional mail) ────────────────────────────────────
# Named constants so the endpoint copy, the warning text and the frontend all move together with
# one edit. These are the provider's published shape, not AFC policy: if AFC moves off M365 (see
# the separate email-provider research), only these three numbers change.
EMAIL_PER_MINUTE = 30                  # roughly what M365 accepts per minute
EMAIL_DAILY_CAP = 1000                 # per-day ceiling to recipients who never got AFC mail
EMAIL_COMFORTABLE_MAX = 200            # above this we warn; below it a blast is unremarkable


def email_volume_assessment(email_recipient_count):
    """Judge whether emailing `email_recipient_count` people is safe, and say so in plain words.

    Returns a dict the API hands straight to the composer:
      {level, email_recipient_count, estimated_minutes, per_minute, daily_cap,
       requires_confirmation, blocked, message}

    Levels:
      "ok"      - comfortably within one send. No confirmation needed for the email channel.
      "slow"    - will take a noticeable time and eats into the daily cap. The send endpoint
                  REQUIRES confirm_large_email=true before it will accept the email channel.
      "blocked" - above the daily cap: this cannot deliver today, so the email channel is
                  REFUSED outright and the admin is pointed at in-app notification instead.
                  We would rather say no than accept a send that quietly dies in the queue.

    estimated_minutes is deliberately honest arithmetic (count / 30, rounded up) so an admin can
    see "this will take about 4 hours" instead of discovering it afterwards."""
    count = max(0, int(email_recipient_count or 0))
    estimated_minutes = math.ceil(count / EMAIL_PER_MINUTE) if count else 0

    if count > EMAIL_DAILY_CAP:
        level = "blocked"
        message = (
            f"{count} email recipients is above the {EMAIL_DAILY_CAP}-per-day limit AFC's mail "
            f"provider allows, and would take about {estimated_minutes} minutes to send. Email "
            f"cannot deliver to this many people. Send an in-app notification instead, or narrow "
            f"the audience."
        )
    elif count > EMAIL_COMFORTABLE_MAX:
        level = "slow"
        message = (
            f"{count} email recipients will take about {estimated_minutes} minutes to send "
            f"(roughly {EMAIL_PER_MINUTE} emails a minute) and uses most of today's "
            f"{EMAIL_DAILY_CAP}-email allowance. In-app notification reaches everyone instantly. "
            f"Confirm if you still want to email."
        )
    else:
        level = "ok"
        message = (
            f"{count} email recipients, about {estimated_minutes} minute(s) to send."
            if count else "No recipients have an email address."
        )

    return {
        "level": level,
        "email_recipient_count": count,
        "estimated_minutes": estimated_minutes,
        "per_minute": EMAIL_PER_MINUTE,
        "daily_cap": EMAIL_DAILY_CAP,
        "comfortable_max": EMAIL_COMFORTABLE_MAX,
        "requires_confirmation": level == "slow",
        "blocked": level == "blocked",
        "message": message,
    }


def recommended_delivery(recipient_count):
    """The channel the composer should DEFAULT to for an audience of this size.

    Large audiences default to "push" (in-app only) because that is the channel that can actually
    deliver to everyone at once; small ones default to "both". The admin can still override, but
    the default should never be the one that will throttle."""
    return "push" if recipient_count > EMAIL_COMFORTABLE_MAX else "both"


# ── Delivery channels ─────────────────────────────────────────────────────────────────────────
# A broadcast picks CHANNELS: the in-app notification, the email, and (owner 2026-08-05) WhatsApp.
# The wire value stayed a plain STRING and gained a comma, rather than becoming a list or growing
# a token per combination:
#   • the three values that existed before ("push", "email", "both") still mean exactly what they
#     always did, so every existing caller, every stored SentBroadcast row and every frontend
#     select is untouched;
#   • every endpoint already normalises the value with `(request.data.get("delivery") or
#     "both").strip().lower()`. A list would have made that line raise AttributeError in about
#     eight places across three apps; a comma costs those places nothing;
#   • one token per combination ("pushemail", "pushwhatsapp", ...) doubles with every future
#     channel, and "both" already shows how badly that ages as a name.
# So the accepted values are the three legacy ones plus "whatsapp", singly or comma-joined:
# "whatsapp", "push,whatsapp", "email,whatsapp", "both,whatsapp". A list is accepted too, purely
# so a future frontend that sends an array is not a backend change.
#
# CONSUMED BY: afc_auth.views.deliver_broadcast (which channels to actually send on) and
# afc_auth.views_broadcast_audience.broadcast_audience_send (validating what the composer sent).
PUSH = "push"
EMAIL = "email"
WHATSAPP = "whatsapp"

# Every accepted token, and the channels it stands for. "both" is a historical alias for the two
# channels that existed when it was named, NOT for "all of them" - widening it would have turned
# every broadcast already in flight into a WhatsApp blast.
_DELIVERY_ALIASES = {
    PUSH: (PUSH,),
    EMAIL: (EMAIL,),
    "both": (PUSH, EMAIL),
    WHATSAPP: (WHATSAPP,),
}


def parse_delivery(value):
    """Turn a delivery value into the SET of channels it selects.

    Accepts "push" / "email" / "both" / "whatsapp", any comma-joined combination of them, or a
    list of them. Unknown tokens are dropped, so an empty set means "nothing recognised" - the
    endpoints turn that into a 400, and deliver_broadcast sends nothing, which is what a junk
    value already did before this existed."""
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [str(item or "") for item in value]
    else:
        parts = str(value or "").split(",")

    channels = set()
    for part in parts:
        channels.update(_DELIVERY_ALIASES.get(part.strip().lower(), ()))
    return frozenset(channels)


def delivery_token(channels):
    """The canonical string for a channel set: the value stored on SentBroadcast.delivery and
    echoed back to the composer. Returns "" for an empty set.

    Round-trips (parse_delivery(delivery_token(x)) == x), and a set that does not include WhatsApp
    produces exactly the token it always did, so history rows written before this change and after
    it read identically."""
    channels = set(channels or ())
    parts = []
    if PUSH in channels and EMAIL in channels:
        parts.append("both")
    elif PUSH in channels:
        parts.append(PUSH)
    elif EMAIL in channels:
        parts.append(EMAIL)
    if WHATSAPP in channels:
        parts.append(WHATSAPP)
    return ",".join(parts)


# ── Spec parsing ──────────────────────────────────────────────────────────────────────────────


def _int_list(raw, cap=500):
    """Coerce a request value into a clean list of ints, dropping junk rather than 400-ing.

    The composer sends ids it read from our own endpoints, so a stray value means a client bug,
    not a user error - dropping it keeps a good selection usable. `cap` bounds how many explicit
    ids one send may carry (an admin picking 500 individual players is already using the wrong
    tool - that is what the category filters are for)."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
        if len(out) >= cap:
            break
    return out


def _str_list(raw, cap=100):
    """Coerce a request value into a clean list of non-blank trimmed strings (tiers, countries,
    roles, languages). Same lenient contract as _int_list."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for value in raw:
        text = str(value or "").strip()
        if text:
            out.append(text)
        if len(out) >= cap:
            break
    return out


def parse_audience_spec(data):
    """Pull the audience spec off a request body (`data` is request.data) into a normalised dict.

    Accepts the spec either at the top level or nested under an "audience" key, so the preview and
    send endpoints can share one parser while the send body also carries title/message/delivery."""
    spec = data.get("audience") if isinstance(data.get("audience"), dict) else data
    return {
        "everyone": bool(spec.get("everyone")),
        "user_ids": _int_list(spec.get("user_ids")),
        "team_ids": _int_list(spec.get("team_ids")),
        "tiers": _str_list(spec.get("tiers")),
        "countries": _str_list(spec.get("countries")),
        "roles": _str_list(spec.get("roles")),
        "languages": _str_list(spec.get("languages")),
        "include_suspended": bool(spec.get("include_suspended")),
    }


def spec_is_empty(spec):
    """True when the admin has selected nothing at all. The endpoints turn this into a 400 rather
    than resolving it, so an empty form can never be mistaken for "send to everyone"."""
    return not (
        spec["everyone"]
        or spec["user_ids"]
        or spec["team_ids"]
        or spec["tiers"]
        or spec["countries"]
        or spec["roles"]
        or spec["languages"]
    )


# ── Resolution ────────────────────────────────────────────────────────────────────────────────


def eligible_users(include_suspended=False):
    """The floor every audience sits on: accounts that can actually receive and act on a message.

    Excludes deactivated accounts always, and suspended ones unless the admin opted in (an admin
    may legitimately want to tell suspended users something about their suspension)."""
    qs = User.objects.filter(is_active=True)
    if not include_suspended:
        qs = qs.exclude(status="suspended")
    return qs


def _category_q(spec):
    """The Q for the CATEGORY block (tier / country / role / language), or None when no category
    filter is set. Filters INTERSECT here - see the module header.

    Tier lives on the TEAM (afc_team.Team.team_tier), not on the user, so "Tier 1" resolves to
    "users who are on a Tier 1 team", counting both roster members and the team owner (an owner is
    not always in TeamMembers). Both id sets are passed as SUBQUERIES: .values("member_id") stays
    a queryset, so this becomes a single SQL statement with an IN (SELECT ...) and nothing is
    pulled into Python.

    Country matches EITHER the profile country the user typed or the IP-derived one we record on
    login (afc_auth.User.ip_country), because the profile field is blank for a large share of
    accounts and an audience built on it alone would silently miss them."""
    clauses = []

    if spec["tiers"]:
        tier_member_ids = TeamMembers.objects.filter(
            team__team_tier__in=spec["tiers"]
        ).values("member_id")
        tier_owner_ids = Team.objects.filter(
            team_tier__in=spec["tiers"]
        ).values("team_owner_id")
        clauses.append(Q(user_id__in=tier_member_ids) | Q(user_id__in=tier_owner_ids))

    if spec["countries"]:
        # The picked values are CANONICAL country keys, not raw column values, because
        # the same country is stored under several spellings ('Nigeria' and 'NG', and
        # so on). Expanding the key back to every spelling present in the data is what
        # makes "Nigeria" mean all 4,709 Nigerians rather than only the 2,892 who
        # happen to be recorded with the full name. See afc_auth/country_grouping.py.
        #
        # The distinct list is two cheap index reads over a low-cardinality column (a
        # couple of hundred rows), not a scan, and it has to be read rather than
        # hardcoded because a new spelling can appear at any signup.
        raw_present = set(
            User.objects.exclude(country="").values_list("country", flat=True).distinct()
        ) | set(
            User.objects.exclude(ip_country="").values_list("ip_country", flat=True).distinct()
        )
        matching = expand_country_keys(spec["countries"], raw_present)
        # The IP-derived country counts ONLY for accounts with no profile country, which is
        # exactly the rule the options endpoint counts by. Matching ip_country unconditionally
        # made the send reach people the chip never counted: 27 live accounts have a profile
        # country that disagrees with their IP (Nigeria/GB, South Africa/FR, South Sudan/ZA),
        # so picking Nigeria showed 4,247 and delivered to 4,249. On the one screen whose job
        # is showing exactly who a broadcast reaches, the number has to be the promise, and a
        # person's own stated country has to outrank where they happened to log in from.
        clauses.append(
            Q(country__in=matching)
            | (Q(country="") & Q(ip_country__in=matching))
        )

    if spec["roles"]:
        clauses.append(Q(role__in=spec["roles"]))

    if spec["languages"]:
        clauses.append(Q(language__in=spec["languages"]))

    if not clauses:
        return None

    combined = clauses[0]
    for clause in clauses[1:]:
        combined &= clause          # intersect: tier AND country AND role AND language
    return combined


def resolve_audience(spec):
    """Turn a parsed spec into the User QUERYSET it selects (deduped, never materialised).

    Returns a queryset; callers .count() it for the preview and iterate it only at send time.
    See the module header for the union/intersection rule this implements."""
    base = eligible_users(include_suspended=spec["include_suspended"])

    # "Send to the entire site" - the whole eligible population, no further narrowing.
    if spec["everyone"]:
        return base

    # Otherwise: the UNION of the three selections. Each is a Q on the same base queryset, so the
    # whole thing stays one SQL statement.
    selections = []

    if spec["user_ids"]:
        selections.append(Q(user_id__in=spec["user_ids"]))

    if spec["team_ids"]:
        # A picked team means its whole roster AND its owner (the owner may not have a
        # TeamMembers row, and leaving them out of a message to their own team would be wrong).
        member_ids = TeamMembers.objects.filter(team_id__in=spec["team_ids"]).values("member_id")
        owner_ids = Team.objects.filter(team_id__in=spec["team_ids"]).values("team_owner_id")
        selections.append(Q(user_id__in=member_ids) | Q(user_id__in=owner_ids))

    category = _category_q(spec)
    if category is not None:
        selections.append(category)

    if not selections:
        # Nothing selected: an EMPTY audience, never everyone. spec_is_empty() normally catches
        # this at the endpoint, so this is the belt-and-braces half of the same rule.
        return base.none()

    combined = selections[0]
    for selection in selections[1:]:
        combined |= selection       # union: picked players OR picked teams OR the categories
    # distinct(): a user reachable two ways (picked individually and on a picked team) must count
    # once. The Q form avoids join multiplication, but distinct() makes the guarantee explicit.
    return base.filter(combined).distinct()


def audience_counts(spec):
    """The numbers the admin must see BEFORE sending, computed with SQL counts only.

    Returns {recipient_count, email_recipient_count, push_recipient_count,
             whatsapp_recipient_count}:
      - recipient_count          - everyone the send would reach in-app.
      - email_recipient_count    - of those, how many have an email address on file. This is the
                                   number the volume warning is judged on, because it is the number
                                   of messages the mail provider would actually be asked to send.
      - push_recipient_count     - same as recipient_count (every account can receive an in-app
                                   notification); returned explicitly so the composer can show the
                                   channels side by side without doing arithmetic.
      - whatsapp_recipient_count - of those, how many have a WhatsApp number AND have not opted
                                   out. Far smaller than the other two, and it is the number the
                                   WhatsApp cap is judged on, for the same reason the email cap is
                                   judged on addresses: it is how many messages get paid for."""
    qs = resolve_audience(spec)
    recipient_count = qs.count()
    email_recipient_count = qs.exclude(email="").exclude(email__isnull=True).count()
    # Imported here, not at module scope: afc_auth.broadcast_whatsapp reaches into afc_whatsapp
    # (and through it Celery), and this module is imported by the audience endpoints on every
    # keystroke of the composer.
    from .broadcast_whatsapp import whatsapp_recipient_count
    return {
        "recipient_count": recipient_count,
        "email_recipient_count": email_recipient_count,
        "push_recipient_count": recipient_count,
        "whatsapp_recipient_count": whatsapp_recipient_count(qs),
    }
