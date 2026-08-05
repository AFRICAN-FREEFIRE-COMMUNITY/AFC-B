# afc_auth/views_broadcast_audience.py
# ──────────────────────────────────────────────────────────────────────────────
# ADMIN BROADCAST AUDIENCE endpoints (owner backlog item 15, 2026-08-03)
#
# "Notifications settings: admins select specific teams and players, or filter by category
#  (tier, country, others), for notification or bulk mail, and can send to the entire site."
#
# THREE endpoints, one job between them - let an admin build an audience, SEE HOW BIG IT IS, and
# then send to it:
#   GET  auth/admin/broadcast-audience/options/  - the filter values that exist (countries with
#        counts, tiers, roles, languages) so the composer's dropdowns are populated from real
#        data instead of a hardcoded list.
#   POST auth/admin/broadcast-audience/preview/  - resolve a spec to a COUNT plus a paged sample
#        of who is in it, and the email-volume verdict for that size.
#   POST auth/admin/broadcast-audience/send/     - send to the resolved audience through the
#        existing afc_auth.views.deliver_broadcast chokepoint.
#
# THE TWO RULES THAT MATTER MORE THAN THE FEATURE:
#
# 1. COUNT BEFORE SEND, ALWAYS. There is no undo on a broadcast. The send endpoint REQUIRES a
#    `confirmed_count` in the body: the number the admin was shown by the preview and clicked
#    through. If the audience has since changed size (a player joined a picked team, a new signup
#    fell into a country filter), the send is REJECTED with 409 and the new number, so the admin
#    re-reads and re-confirms. It is structurally impossible to send from this endpoint without
#    having seen the number first.
#
# 2. EMAIL VOLUME IS REAL. AFC's transactional mail goes through Microsoft 365 (~30 messages a
#    minute, ~1,000 a day to people who have never received AFC mail). A "send to everyone" over
#    ~6,800 users would take hours and be throttled. So the preview always returns an
#    email_volume verdict, the send endpoint requires confirm_large_email for a merely-slow blast,
#    and it REFUSES the email channel outright above the daily cap (400, not a silent queue). The
#    recommended default channel for a big audience is in-app push, which delivers instantly.
#
# 3. WHATSAPP VOLUME IS REAL IN A DIFFERENT WAY (owner 2026-08-05). WhatsApp is the third channel
#    and the only one that costs money per message. Above the cap it is REFUSED outright, never
#    truncated, because that is the same phone number AFC sends room IDs from and a marketing
#    blast that people mute or report drags its quality rating down. Same mechanism as the email
#    cap, same shape of response; the reasoning is in afc_auth/broadcast_whatsapp.py.
#
# Convention note: mirrors the sibling afc_auth view modules (views_watchlist.py,
# views_player_reports.py) - function-based @api_view views, the shared Bearer handshake via
# afc.api_utils.authenticate, inline dict serialization (no serializers.py), Response({...},
# status=...) on every return, and ?limit/?offset paging on anything that lists.
#
# HOW THIS CONNECTS TO THE REST OF THE SYSTEM:
#   - Audience resolution: afc_auth/audience.py (resolve_audience / audience_counts /
#     email_volume_assessment). That module owns the filter semantics; this one owns HTTP.
#   - Delivery: afc_auth.views.deliver_broadcast - the same chokepoint every other broadcast on
#     the site uses, so these sends produce the same Notifications rows, the same branded
#     per-recipient-localized email, and the same SentBroadcast history entry (scope="general",
#     which is exactly what the existing admin Settings > Notifications "Sent broadcasts" list
#     reads through get_general_broadcast_history).
#   - Deep links: the shared _parse_notification_targets from afc_auth.views, so an audience
#     broadcast can carry a "Take me there" target just like the existing composer.
#   - Rate limit: afc_auth.broadcast_ratelimit.check_broadcast_rate. AFC admins are exempt by
#     design there, so this is a no-op for them, but it is called anyway so the gate stays in one
#     place if the endpoint is ever opened to organizers.
#   - Routes: afc_auth/urls.py, prefix auth/admin/broadcast-audience/.
#   - Frontend consumer: the admin Settings > Notifications tab audience builder
#     (frontend/app/(a)/a/settings/_components/AudienceBuilder.tsx), which previews on every
#     filter change and disables Send until a preview for the CURRENT spec exists.
# ──────────────────────────────────────────────────────────────────────────────
from django.db.models import Count, Q

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc.api_utils import authenticate as _authenticate

from afc_team.models import Team, TeamMembers

from .audience import (
    EMAIL,
    WHATSAPP,
    audience_counts,
    delivery_token,
    eligible_users,
    email_volume_assessment,
    parse_audience_spec,
    parse_delivery,
    recommended_delivery,
    resolve_audience,
    spec_is_empty,
)
from .broadcast_whatsapp import whatsapp_max_recipients, whatsapp_volume_assessment
from .audit import set_audit
# One country can sit in the table under several spellings, so the chip list is built
# from folded groups rather than raw strings. See country_grouping.py.
from .country_grouping import group_country_counts
from .models import AdminHistory, User


# Granular admin roles allowed to build and send site-wide audiences, in addition to the coarse
# User.role == "admin". Site-wide messaging is a head-admin-grade action: an audience of 6,790
# people is the loudest thing anyone can do on this platform, so it is deliberately NOT handed to
# every granular admin role (a shop_admin has no business emailing the entire site).
BROADCAST_AUDIENCE_ROLES = ("head_admin", "super_admin")


def _is_broadcast_audience_admin(user):
    """True for the AFC staff allowed to use this surface: the coarse admin role, or a granular
    head_admin / super_admin row. `role__role_name__in` (NOT role_name__in) - UserRoles.role is
    an FK to Roles, so the lookup has to walk it."""
    if not user:
        return False
    if getattr(user, "role", None) == "admin":
        return True
    return user.userroles.filter(role__role_name__in=BROADCAST_AUDIENCE_ROLES).exists()


def _is_head_admin(user):
    """True only for head_admin / super_admin / a Django superuser.

    DELIBERATELY STRICTER than _is_broadcast_audience_admin above, which also lets a plain
    role=="admin" through. It gates the WhatsApp channel, where every message is billed - see the
    reasoning at the check itself in broadcast_audience_send. Mirrors afc_auth.views
    .require_head_admin, which cannot be reused here because that one re-reads the Authorization
    header and this endpoint has already resolved the user.
    """
    if not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin")).exists()


def _forbidden():
    """The one 403 body this module returns, so every endpoint refuses a non-admin identically."""
    return Response(
        {"message": "You do not have permission to send broadcasts."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _paginate(request, queryset):
    """List endpoints accept ?limit (default 25, max 100) and ?offset, returning
    (page, total_count, has_more). Junk values fall back to the default rather than 500-ing.
    Same helper shape as the sibling organizer/auth view modules. Body-carried paging is also
    accepted on the POST preview (the spec travels in the body, so the page does too)."""
    def _read(key, fallback):
        raw = request.GET.get(key)
        if raw is None and hasattr(request, "data") and isinstance(request.data, dict):
            raw = request.data.get(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    limit = max(1, min(_read("limit", 25), 100))
    offset = max(0, _read("offset", 0))
    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


def _serialize_recipient(u):
    """One row of the preview SAMPLE. Just enough for the admin to recognise who is in the
    audience (and to spot a filter that caught the wrong people); deliberately not a full user
    payload - this is a sanity check, not a user directory."""
    return {
        "user_id": u.user_id,
        "username": u.username,
        # has_email, not the address: the admin needs to know the email channel can reach this
        # person, and printing thousands of addresses into a preview response is needless PII
        # spread through logs and browser caches.
        "has_email": bool(u.email),
        "country": u.country or u.ip_country or "",
        "role": u.role,
        "language": u.language or "en",
    }


# ──────────────────────────────────────────────────────────────────────────────
# §1  GET options/  - what can be filtered on, with real counts
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def broadcast_audience_options(request):
    """The filter values that actually exist on this site, so the composer's dropdowns are built
    from data instead of a hardcoded list that drifts.

    Auth:  Bearer; admins only (_is_broadcast_audience_admin). 403 otherwise.
    Query: ?limit=&offset= (paging over the COUNTRIES list, the only unbounded one; tiers, roles
           and languages are closed sets of 3-4 values each and are returned whole).
    Response: 200 {
        total_users,                         # the eligible population - what "everyone" means
        countries: [{value, count}],         # ordered by count desc, paginated
        countries_total_count, countries_has_more,
        tiers:     [{value, count}],         # afc_team.Team.team_tier, players on such a team
        roles:     [{value, count}],         # afc_auth.User.role
        languages: [{value, count}],         # afc_auth.User.language ("" reported as "en")
        email_limits: {per_minute, daily_cap, comfortable_max} }
    FE consumer: AudienceBuilder.tsx populates its Country / Tier / Role / Language selects from
    this on mount, and shows total_users next to the "Everyone on AFC" option.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_broadcast_audience_admin(user):
        return _forbidden()

    base = eligible_users()

    # ── countries: profile country, falling back to the IP-derived one ──
    # A user with a blank profile country still has an ip_country from login, and an audience
    # built only on the typed field would miss them - so the OPTION list must offer both sources
    # too, or the admin would never see the country they can actually target. We count the two
    # columns separately and merge in Python (a few dozen rows, not a scan of the user table).
    country_counts = {}
    for row in base.exclude(country="").values("country").annotate(count=Count("user_id")):
        country_counts[row["country"]] = country_counts.get(row["country"], 0) + row["count"]
    for row in (
        base.filter(country="").exclude(ip_country="")
        .values("ip_country").annotate(count=Count("user_id"))
    ):
        country_counts[row["ip_country"]] = country_counts.get(row["ip_country"], 0) + row["count"]
    # ── one country, one chip ──
    # The raw column holds the same country under several spellings ('Nigeria' and
    # 'NG', 'South Africa' and 'ZA', and so on), so grouping by the raw string offered
    # the admin two chips per country and made each look like the whole country.
    # group_country_counts folds them together; see afc_auth/country_grouping.py for
    # the live numbers that exposed this. `value` is the canonical key the filter
    # sends back, `label` is the spelling the admin reads.
    countries = group_country_counts(country_counts)
    # Page the merged list with the same limit/offset contract the queryset lists use.
    try:
        limit = max(1, min(int(request.GET.get("limit", 100)), 100))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    countries_total = len(countries)
    countries_page = countries[offset:offset + limit]

    # ── tiers: a TEAM attribute, so the count is "players on a team of this tier" ──
    # Counted through TeamMembers with distinct members so a player on two Tier 1 teams counts
    # once. This is the same relation _category_q filters on, so the number the admin sees here
    # is the number the preview will produce (owners aside, who are folded in at resolve time).
    tiers = [
        {"value": row["team__team_tier"], "count": row["count"]}
        for row in TeamMembers.objects.exclude(team__team_tier="")
        .values("team__team_tier")
        .annotate(count=Count("member_id", distinct=True))
        .order_by("team__team_tier")
    ]

    roles = [
        {"value": row["role"], "count": row["count"]}
        for row in base.values("role").annotate(count=Count("user_id")).order_by("-count")
    ]

    # Blank language means "never chose one", which the whole site reads as English - so report
    # it as "en" rather than showing the admin an empty option they cannot interpret.
    language_counts = {}
    for row in base.values("language").annotate(count=Count("user_id")):
        key = row["language"] or "en"
        language_counts[key] = language_counts.get(key, 0) + row["count"]
    languages = sorted(
        ({"value": code, "count": count} for code, count in language_counts.items()),
        key=lambda entry: (-entry["count"], entry["value"]),
    )

    volume = email_volume_assessment(0)   # constants only; the count here is irrelevant
    return Response(
        {
            "total_users": base.count(),
            "countries": countries_page,
            "countries_total_count": countries_total,
            "countries_has_more": (offset + limit) < countries_total,
            "tiers": tiers,
            "roles": roles,
            "languages": languages,
            "email_limits": {
                "per_minute": volume["per_minute"],
                "daily_cap": volume["daily_cap"],
                "comfortable_max": volume["comfortable_max"],
            },
            # The WhatsApp ceiling, so the composer can state the limit next to the channel
            # instead of only discovering it when a send is refused.
            "whatsapp_limits": {"max_recipients": whatsapp_max_recipients()},
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  POST preview/  - how many people is this, and can email actually deliver it?
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def broadcast_audience_preview(request):
    """Resolve an audience spec to a COUNT (plus a small sample of who is in it) and the
    email-volume verdict for that size. Sends nothing.

    This is the endpoint that makes "count before send" possible: the composer calls it on every
    filter change, and the number it returns is what the admin must confirm at send time.

    Request (JSON) - the audience spec, at the top level or nested under "audience":
      {everyone?, user_ids?[], team_ids?[], tiers?[], countries?[], roles?[], languages?[],
       include_suspended?, limit?, offset?}
      See afc_auth/audience.py for the exact union/intersection semantics (picked players OR
      picked teams OR the categories; within the categories, tier AND country AND role AND
      language).
    Validation: 400 when the spec selects nothing at all, so an empty form can never be mistaken
      for "send to everyone".
    Auth: Bearer; admins only. 403 otherwise.
    Response: 200 {
        recipient_count,            # in-app reach - THE number the admin confirms
        email_recipient_count,      # of those, how many have an email address
        push_recipient_count,
        whatsapp_recipient_count,   # of those, how many have a WhatsApp number and consented
        email_volume: {level: ok|slow|blocked, estimated_minutes, per_minute, daily_cap,
                       requires_confirmation, blocked, message},
        whatsapp_volume: {level: ok|blocked, max_recipients, blocked, message},
        recommended_delivery: "push"|"both",
        sample: [{user_id, username, has_email, country, role, language}],   # paged
        sample_total_count, has_more }
    FE consumer: AudienceBuilder.tsx - shows the count headline, the volume warning banner, and a
    "who is in this" sample table; Send stays disabled until a preview for the CURRENT spec exists.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_broadcast_audience_admin(user):
        return _forbidden()

    spec = parse_audience_spec(request.data)
    if spec_is_empty(spec):
        return Response(
            {"message": "Select at least one recipient, team, or filter."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    counts = audience_counts(spec)
    volume = email_volume_assessment(counts["email_recipient_count"])
    wa_volume = whatsapp_volume_assessment(counts["whatsapp_recipient_count"])

    # The sample is ordered so repeat previews of the same spec show the same faces (an unordered
    # slice is not stable across queries and would look like the audience is churning).
    sample_qs = resolve_audience(spec).order_by("user_id")
    page, sample_total, has_more = _paginate(request, sample_qs)

    return Response(
        {
            **counts,
            "email_volume": volume,
            "whatsapp_volume": wa_volume,
            # Whether THIS admin may use the WhatsApp channel at all (owner 2026-08-05:
            # head-admin only, because those messages are billed per message). Reported by the
            # PREVIEW so the composer can grey the option out with a reason, rather than letting
            # somebody write a broadcast, pick WhatsApp and only find out at the send.
            "whatsapp_allowed": _is_head_admin(user),
            "recommended_delivery": recommended_delivery(counts["recipient_count"]),
            "sample": [_serialize_recipient(u) for u in page],
            "sample_total_count": sample_total,
            "has_more": has_more,
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  POST send/  - deliver to the resolved audience (count-confirmed, volume-guarded)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def broadcast_audience_send(request):
    """Send a notification and/or email to the audience a spec resolves to.

    THE THREE GUARDS (see the module header for why they exist):
      • confirmed_count is REQUIRED and must equal the audience size RIGHT NOW. A mismatch is a
        409 carrying the new number: the admin re-reads it and re-confirms. This is what makes it
        impossible to send without having seen the count.
      • The email channel is volume-checked. Above the comfortable size the request must carry
        confirm_large_email=true (400 with the warning otherwise); above the provider's daily cap
        the email channel is REFUSED entirely (400) and the admin is told to use in-app instead -
        we do not queue mail that cannot deliver.
      • The WhatsApp channel is capped. Above WHATSAPP_BROADCAST_MAX_RECIPIENTS reachable people
        the send is REFUSED (400) with the limit named. There is no "confirm anyway": this is the
        number that carries room IDs.

    Request (JSON):
      {audience: {...spec...} | ...spec at top level,
       title?, message,                       # message is required
       delivery?: "push"|"email"|"both"|"whatsapp", singly or comma-joined ("both,whatsapp").
                                              # default "push" - the channel that always delivers
       confirmed_count,                       # REQUIRED: the number the preview showed
       confirm_large_email?: bool,            # required for a "slow" email blast
       target_type?/target_id?/targets?}      # optional "Take me there" deep link(s)
    Auth: Bearer; admins only. 403 otherwise.
    Responses:
      200 {message, recipient_count, pushed, emailed, whatsapp_queued, whatsapp_skipped,
           delivery, email_volume, whatsapp_volume}
      400 empty audience / missing message / missing confirmed_count / unconfirmed or
          over-cap email blast / over-cap WhatsApp blast (each with a plain-English message and,
          for the volume cases, the full email_volume / whatsapp_volume payload so the FE can
          render the warning verbatim)
      409 {message, recipient_count, confirmed_count, email_volume} - the audience changed size
      429 broadcast rate limit (admins are exempt, so this is effectively unreachable today)
    FE consumer: AudienceBuilder.tsx "Send" button, behind a confirm dialog that repeats the
    count and, for email, the volume warning.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_broadcast_audience_admin(user):
        return _forbidden()

    # ── message body ──
    message = (request.data.get("message") or "").strip()
    if not message:
        return Response({"message": "A message is required."}, status=status.HTTP_400_BAD_REQUEST)
    title = (request.data.get("title") or "").strip() or None

    # Channels, not a single channel: "both,whatsapp" is three of them. parse_delivery owns the
    # vocabulary (afc_auth/audience.py) and drops anything it does not recognise, so an empty set
    # means the composer sent junk. delivery_token puts it back into the one canonical spelling
    # that is echoed to the composer, stored on SentBroadcast and written to the audit line.
    channels = parse_delivery(request.data.get("delivery") or "push")
    if not channels:
        return Response(
            {"message": "delivery must be 'push', 'email', 'whatsapp', or a comma separated "
                        "combination such as 'both,whatsapp'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    delivery = delivery_token(channels)

    # ── WhatsApp on THIS surface is head-admin only (owner 2026-08-05) ────────────────────────
    # This is a SPENDING control, not an ordinary permission, which is why it is stricter than the
    # gate on the endpoint itself.
    #
    # A general broadcast goes out on the `broadcast` template, which Meta categorises as
    # MARKETING. On Meta's rate card effective 2026-04-01, Nigeria - about 69% of AFC - is
    # $0.0516 per marketing message against $0.0067 for utility. So one broadcast to 500 people
    # costs roughly $26, and a daily habit of 1,000 messages is about $1,548 a month. Every other
    # admin role can still send in-app and email, which cost nothing.
    #
    # Room details are deliberately NOT affected. They go out on `room_details`, a UTILITY
    # template at an eighth of the price, from the event surfaces, and organizers need to send
    # them without asking anybody. This gate is only on the general-broadcast composer.
    if WHATSAPP in channels and not _is_head_admin(user):
        return Response(
            {"message": "Only a head admin can send a broadcast on WhatsApp. WhatsApp messages "
                        "are charged per message, so this channel is restricted. You can still "
                        "send this as an in-app notification or email.",
             "code": "whatsapp_requires_head_admin"},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── audience ──
    spec = parse_audience_spec(request.data)
    if spec_is_empty(spec):
        return Response(
            {"message": "Select at least one recipient, team, or filter."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    counts = audience_counts(spec)
    recipient_count = counts["recipient_count"]
    volume = email_volume_assessment(counts["email_recipient_count"])
    wa_volume = whatsapp_volume_assessment(counts["whatsapp_recipient_count"])

    if recipient_count == 0:
        return Response(
            {"message": "This audience has no recipients."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── GUARD 1: count before send ──
    # The admin must send back the number the preview showed them. Absent = they never previewed;
    # different = the audience moved under them and they must look again.
    raw_confirmed = request.data.get("confirmed_count")
    try:
        confirmed_count = int(raw_confirmed)
    except (TypeError, ValueError):
        return Response(
            {
                "message": "Preview the audience and confirm the recipient count before sending.",
                "recipient_count": recipient_count,
                "email_volume": volume,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if confirmed_count != recipient_count:
        return Response(
            {
                "message": (
                    f"This audience now has {recipient_count} recipients, not {confirmed_count}. "
                    f"Check the new number and send again."
                ),
                "recipient_count": recipient_count,
                "confirmed_count": confirmed_count,
                "email_volume": volume,
            },
            status=status.HTTP_409_CONFLICT,
        )

    # ── GUARD 2: email volume ──
    # Only applies when email is actually one of the chosen channels; a push-only send to the
    # whole site is fine and is exactly what we steer large audiences towards.
    if EMAIL in channels:
        if volume["blocked"]:
            return Response(
                {
                    "message": volume["message"],
                    "email_volume": volume,
                    "recipient_count": recipient_count,
                    "recommended_delivery": "push",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if volume["requires_confirmation"] and not request.data.get("confirm_large_email"):
            return Response(
                {
                    "message": volume["message"],
                    "email_volume": volume,
                    "recipient_count": recipient_count,
                    "recommended_delivery": "push",
                    "code": "email_volume_confirmation_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    # ── GUARD 3: WhatsApp volume (owner 2026-08-05) ──
    # Same mechanism as the email cap above, for a different reason: email above the cap CANNOT
    # deliver, WhatsApp above the cap can deliver and that is the problem. Every message is paid
    # for, Meta throttles a business that sends too much too fast, and a marketing blast people
    # mute or report damages the quality rating of the ONE number AFC also sends room IDs from.
    # Refused, never truncated: a broadcast that reached the first 500 of 3,000 people cannot be
    # reasoned about afterwards. See afc_auth/broadcast_whatsapp.py.
    if WHATSAPP in channels and wa_volume["blocked"]:
        return Response(
            {
                "message": wa_volume["message"],
                "whatsapp_volume": wa_volume,
                "recipient_count": recipient_count,
                "recommended_delivery": "push",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── rate limit (admins are exempt in broadcast_ratelimit, so this is a no-op for them; kept
    # so the gate lives in ONE place if this surface is ever opened up to organizers) ──
    from .broadcast_ratelimit import check_broadcast_rate, record_broadcast_send

    allowed, info = check_broadcast_rate(user)
    if not allowed:
        return Response(
            {
                "message": info.get("message", "You're sending broadcasts too quickly."),
                "resets_at": info.get("resets_at"),
                "remaining": info.get("remaining"),
                "reason": info.get("reason"),
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # ── deliver through the shared chokepoint ──
    # Imported here rather than at module scope: afc_auth.views imports a great deal of the app,
    # and a top-level import from this module (which afc_auth/urls.py loads) would tighten an
    # already-heavy import graph. _parse_notification_targets is the SAME deep-link parser the
    # existing composer uses, so a "Take me there" link behaves identically.
    from .views import deliver_broadcast, _parse_notification_targets

    recipients = resolve_audience(spec)
    result = deliver_broadcast(
        recipients,
        title,
        message,
        delivery=delivery,
        notification_type="admin_message",
        targets=_parse_notification_targets(request),
        sender=user,
        scope="general",            # lands in the existing admin Settings "Sent broadcasts" list
    )
    # The result IS the (pushed, emailed) pair it has always been; the WhatsApp numbers ride on it
    # as attributes (afc_auth.views.BroadcastResult), read with a default so anything handing back
    # a plain pair still works.
    pushed, emailed = result
    whatsapp_queued = getattr(result, "whatsapp_queued", 0)
    whatsapp_skipped = getattr(result, "whatsapp_skipped", 0)
    record_broadcast_send(user)

    # ── audit trail: a site-wide message is exactly the kind of action that must be traceable ──
    audience_summary = _describe_spec(spec)
    set_audit(request, f"Sent a broadcast to {recipient_count} users ({audience_summary})")
    AdminHistory.objects.create(
        admin_user=user,
        action="sent_audience_broadcast",
        description=(
            f"Broadcast to {recipient_count} users via {delivery}. Audience: {audience_summary}."
        ),
    )

    return Response(
        {
            "message": f"Sent to {recipient_count} recipient(s).",
            "recipient_count": recipient_count,
            "pushed": pushed,
            "emailed": emailed,
            # Both numbers, because WhatsApp reaches a fraction of the audience the other two do
            # and "we messaged 1,200 of your 3,000 players" is the sentence an admin needs. skipped
            # counts everyone with no number on file or an opt-out.
            "whatsapp_queued": whatsapp_queued,
            "whatsapp_skipped": whatsapp_skipped,
            "delivery": delivery,
            "email_volume": volume,
            "whatsapp_volume": wa_volume,
        },
        status=status.HTTP_200_OK,
    )


def _describe_spec(spec):
    """A short human sentence describing an audience, for the audit log and AdminHistory. Reading
    "Audience: everyone" or "Audience: 2 team(s), tier 1, country Nigeria" months later is far
    more useful than a raw JSON blob."""
    if spec["everyone"]:
        return "everyone"
    parts = []
    if spec["user_ids"]:
        parts.append(f"{len(spec['user_ids'])} player(s)")
    if spec["team_ids"]:
        parts.append(f"{len(spec['team_ids'])} team(s)")
    if spec["tiers"]:
        parts.append("tier " + ", ".join(spec["tiers"]))
    if spec["countries"]:
        # Countries arrive as canonical lowercase keys ("nigeria"), so title-case them:
        # the audit line is read by a person months later, not parsed by anything.
        parts.append("country " + ", ".join(c.title() for c in spec["countries"]))
    if spec["roles"]:
        parts.append("role " + ", ".join(spec["roles"]))
    if spec["languages"]:
        parts.append("language " + ", ".join(spec["languages"]))
    return "; ".join(parts) or "none"
