# afc_player_market/views_moderation.py
# ──────────────────────────────────────────────────────────────────────────────
# Player-market MODERATION endpoints — reporting + bans (feature "J-market-reporting").
#
# Built from the approved mockup WEBSITE/tasks/market-reporting-mockup.html. Two
# audiences live here, exactly like the sibling afc_organizers/views_reports.py this
# file is modelled on:
#
#   • Any logged-in USER can file a report against a market post (a team's recruitment
#     post or a player's availability post): a category + free-text details + optional
#     evidence. The row is created "open".
#   • MARKET MODERATORS (admin / moderator role, or head_admin / teams_admin granular
#     role) list + triage those reports, and BAN a player or a whole team from the
#     market for a duration (or permanently).
#
# Convention note (why the code looks like this): this module deliberately mirrors the
# original hand in afc_player_market/views.py and afc_organizers/views_reports.py — NOT
# the newer rankings app. So:
#   * function-based @api_view views, one job each;
#   * auth done inline by reading the Authorization header and calling validate_token
#     (imported the SAME way the rest of this app imports it — from afc_auth.views);
#     missing/garbled header → 400, bad/expired token → 401;
#   * dict serialization written out inline (no serializers.py);
#   * Response({...}, status=...) for every return.
#
# ENFORCEMENT (the load-bearing rule): `_active_market_ban(user)` is the single guard
# that the post/apply/invite entry points in views.py call BEFORE creating a row, so a
# banned poster (or a member of a banned team) is blocked from acting on the market.
# Reporting itself is NEVER gated — see file_market_report.
#
# Route mounting lives in afc_player_market/urls.py — this file ONLY defines view
# functions; it does not touch urls.py.
# ──────────────────────────────────────────────────────────────────────────────
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views — import it the SAME way the rest of this app
# does. Returns the User for a valid, non-expired session token, or None otherwise.
from afc_auth.views import validate_token

from afc_team.models import Team, TeamMembers
from .models import MarketBan, MarketReport, RecruitmentPost


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (auth, moderator gate, pagination, ban lookup, serializers)
# ──────────────────────────────────────────────────────────────────────────────

# Granular role names (in the UserRoles table) that also count as market moderators,
# in addition to the coarse user.role in {admin, moderator}. teams_admin owns the
# teams/market surface; head_admin is the platform super-role. Mirrors the role check
# afc_auth uses elsewhere (head_admin / teams_admin) and the coarse gate the existing
# player-market admin view uses (user.role in ["admin", "moderator"]).
MARKET_MODERATOR_ROLES = ("head_admin", "teams_admin")


def _authenticate(request):
    """Resolve the caller from the Bearer Authorization header.

    Returns a tuple (user, error_response):
      * (user, None)  → authenticated, proceed;
      * (None, resp)  → stop and return `resp` (400 missing/garbled header,
                        401 invalid-or-expired token).
    Same shape + wording as the rest of afc_player_market/views.py.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid session."}, status=401)
    return user, None


def _is_market_moderator(user) -> bool:
    """True for users allowed to triage market reports + issue market bans.

    Coarse gate (matches the existing player-market admin view): user.role in
    {admin, moderator}. PLUS the granular roles in MARKET_MODERATOR_ROLES, read the
    same way afc_auth reads them (user.userroles.filter(role__role_name=...)).
    """
    if not user:
        return False
    if user.role in ("admin", "moderator"):
        return True
    return user.userroles.filter(role__role_name__in=MARKET_MODERATOR_ROLES).exists()


def _paginate(request, queryset):
    """Tiny shared paginator. ?limit (default 25, max 100) + ?offset.

    Returns (page, total_count, has_more) so the admin list never loads the whole
    table into memory. Junk values fall back to defaults rather than 500-ing.
    Identical to afc_organizers/views_reports.py::_paginate.
    """
    try:
        limit = int(request.GET.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


def _active_market_ban(user):
    """ENFORCEMENT: return the active MarketBan blocking `user` from the market, or None.

    This is the single guard the post/apply/invite entry points call before creating a
    row. A user is blocked when EITHER:
      • a player-scoped ban targets them directly, OR
      • a team-scoped ban targets a team they belong to (owner or member).

    Only bans that are CURRENTLY active count (is_active AND not expired) — see
    MarketBan.is_currently_active. Returns the first matching active ban (so the caller
    can surface its reason + end date), or None when the user is clear.

    Called by afc_player_market/views.py wherever a market action is created. It is NOT
    called by the reporting endpoint — reporting is always available, banned or not.
    """
    # ── player-scoped bans against this user ──
    for ban in MarketBan.objects.filter(
        scope="player", banned_player=user, is_active=True
    ):
        if ban.is_currently_active():
            return ban

    # ── team-scoped bans against any team this user belongs to ──
    # Collect the user's team ids: teams they own + teams they're a member of.
    team_ids = set(
        Team.objects.filter(team_owner=user).values_list("team_id", flat=True)
    )
    team_ids.update(
        TeamMembers.objects.filter(member=user).values_list("team_id", flat=True)
    )
    if team_ids:
        for ban in MarketBan.objects.filter(
            scope="team", banned_team_id__in=team_ids, is_active=True
        ):
            if ban.is_currently_active():
                return ban

    return None


def _serialize_report(report):
    """Canonical inline dict for one MarketReport.

    Reused by the admin list view and returned (singly) from admin_update so both
    surfaces hand back an identical record shape. Denormalises the reported subject's
    name + the reporter username so the admin table never re-fetches the FKs. Image
    field follows the codebase contract: (img.url if img else None).
    """
    # Resolve a human label for the reported subject from whichever FK is set.
    if report.subject_type == "team":
        subject_name = report.reported_team.team_name if report.reported_team else None
    else:
        subject_name = report.reported_player.username if report.reported_player else None

    return {
        "id": report.id,
        "subject_type": report.subject_type,
        "subject_name": subject_name,
        # raw ids so the ban dialog can target the exact team/player.
        "reported_team_id": report.reported_team_id,
        "reported_player_id": report.reported_player_id,
        "post_id": report.post_id,
        "category": report.category,
        "details": report.details,
        "evidence": report.evidence.url if report.evidence else None,
        "status": report.status,
        "resolution_notes": report.resolution_notes,
        # reporter / reviewed_by are SET_NULL FKs — surface the username or None.
        # reporter_id is the reporter's User id, exposed so the admin "Ban reporter
        # (false report)" action (feature "J-market-rules", J5) can call admin_market_ban
        # with scope="player", target_id=reporter_id when a report is judged false or
        # abusive. None when the reporter row was deleted (SET_NULL) — the FE hides the
        # ban-reporter action in that case.
        "reporter_id": report.reporter_id,
        "reporter_username": report.reporter.username if report.reporter else None,
        "reviewed_by_username": report.reviewed_by.username if report.reviewed_by else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


def _serialize_ban(ban):
    """Canonical inline dict for one MarketBan. Returned from admin_market_ban so the
    frontend can confirm exactly what was applied (subject, duration, end date)."""
    if ban.scope == "team":
        target_name = ban.banned_team.team_name if ban.banned_team else None
    else:
        target_name = ban.banned_player.username if ban.banned_player else None
    return {
        "id": ban.id,
        "scope": ban.scope,
        "target_name": target_name,
        "ban_duration": ban.ban_duration,                       # null = permanent
        "is_permanent": ban.is_permanent,
        "ban_start_date": ban.ban_start_date.isoformat() if ban.ban_start_date else None,
        "ban_end_date": ban.ban_end_date.isoformat() if ban.ban_end_date else None,
        "reason": ban.reason,
        "is_active": ban.is_active,
    }


# ──────────────────────────────────────────────────────────────────────────────
# §1  POST /report-post/  — file a report against a market post  (ANY logged-in user)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def file_market_report(request):
    """File an abuse report against a player-market post. Open to ANY logged-in user.

    Request (JSON or multipart):
      • post_id   (required) — the RecruitmentPost being reported. Its post_type tells
                               us whether the subject is a team or a player; we resolve
                               and store the concrete team/player from the post itself
                               (the client cannot spoof who it is reporting).
      • category  (optional) — one of MarketReport.CATEGORY_CHOICES; defaults "other".
      • details   (required) — free text describing what happened.
      • evidence  (required) — image upload (screenshot / screen recording frame). As of
                               feature "J-market-rules" (J4) this is COMPULSORY; a report
                               with no evidence is rejected with 400.

    Response: 201 {"message"} on success; 400 on bad input; 404 if the post is gone.
    Auth: Bearer token (any valid session). 400/401 on bad auth.

    🛑 Always available regardless of the transfer-season window — reporting is never
    gated even when posting is. (Posting is not window-gated server-side today either;
    see note in views.py. This endpoint deliberately performs NO ban / window check.)

    Frontend consumer: the Report (red flag) dialog on each post card in
    app/(user)/player-markets/page.tsx.
    """
    # ── auth handshake — any logged-in user passes (no moderator gate, no ban gate) ──
    user, err = _authenticate(request)
    if err:
        return err

    # ── resolve the reported post (404 if it no longer exists) ──
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)
    try:
        post = RecruitmentPost.objects.select_related("team", "player").get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    # ── derive the subject (team vs player) FROM THE POST, never from the client ──
    # A team-recruitment post reports the team; a player-availability post reports the
    # player. This stops a caller naming an arbitrary victim.
    if post.post_type == "TEAM_RECRUITMENT":
        subject_type = "team"
        reported_team = post.team
        reported_player = None
        if reported_team is None:
            return Response({"message": "This post has no team to report."}, status=400)
    elif post.post_type == "PLAYER_AVAILABLE":
        subject_type = "player"
        reported_team = None
        # player posts store the author on both created_by and player — prefer player.
        reported_player = post.player or post.created_by
        if reported_player is None:
            return Response({"message": "This post has no player to report."}, status=400)
    else:
        return Response({"message": "Unsupported post type for reporting."}, status=400)

    # ── category: validate against the model choices, default to "other" ──
    valid_categories = {choice[0] for choice in MarketReport.CATEGORY_CHOICES}
    category = request.data.get("category") or "other"
    if category not in valid_categories:
        return Response({"message": "Invalid report category."}, status=400)

    # ── details: required free text (400 if empty / whitespace-only) ──
    details = (request.data.get("details") or "").strip()
    if not details:
        return Response({"message": "Please describe what happened."}, status=400)

    # ── REQUIRED evidence image (feature "J-market-rules", J4) ──
    # Evidence is now COMPULSORY: a report cannot be filed without an uploaded image
    # (a screenshot / screen-recording frame). This raises the bar for filing a report
    # and discourages baseless / joke reports — which, if judged false, can get the
    # REPORTER banned (J5). The model field stays null=True/blank=True so OLD rows that
    # predate this rule remain valid; we enforce the requirement HERE at the view only.
    # The FE report dialog (MarketReportDialog.tsx) mirrors this by disabling the submit
    # button until an image is attached, so users see the rule before they hit this 400.
    evidence = request.FILES.get("evidence")
    if not evidence:
        return Response({"message": "Evidence is required to file a report."}, status=400)

    # ── create the report (always starts "open"; reporter is the caller) ──
    MarketReport.objects.create(
        subject_type=subject_type,
        reported_team=reported_team,
        reported_player=reported_player,
        post=post,
        reporter=user,
        category=category,
        details=details,
        evidence=evidence,
        status="open",
    )

    return Response(
        {"message": "Report submitted. Thank you, AFC moderators will review it."},
        status=201,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET /admin/reports/  — moderator triage list of every market report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_market_reports(request):
    """Triage list of every market report for moderators. Gated by _is_market_moderator
    (403 for anyone else). Supports optional ?status= and ?category= filters and a
    ?search= over the reported subject name + reporter username, and is paginated
    (?limit default 25 / max 100, ?offset). Newest first so fresh reports surface at the
    top of the queue.

    Response: {"results": [...], "total_count", "has_more"}. Each row is _serialize_report.
    Frontend consumer: the "Reports & Flags" tab on app/(a)/a/player-markets/page.tsx.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_market_moderator(user):
        return Response({"message": "You do not have permission to view market reports."}, status=403)

    # Base queryset, newest first. select_related pulls the FK rows the serializer
    # touches (team / player / reporter / reviewed_by) in one query — no N+1 per page.
    qs = (
        MarketReport.objects.select_related(
            "reported_team", "reported_player", "reporter", "reviewed_by"
        )
        .all()
        .order_by("-created_at")
    )

    # ── optional exact status filter ──
    status_filter = request.GET.get("status")
    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    # ── optional exact category filter ──
    category_filter = request.GET.get("category")
    if category_filter and category_filter != "all":
        qs = qs.filter(category=category_filter)

    # ── optional search over subject name + reporter username ──
    search = (request.GET.get("search") or "").strip()
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(reported_team__team_name__icontains=search)
            | Q(reported_player__username__icontains=search)
            | Q(reporter__username__icontains=search)
        )

    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_report(report) for report in page]

    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  PATCH /admin/reports/<report_id>/  — moderator triage one report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def admin_update_market_report(request, report_id):
    """Triage one report: update its status and/or resolution_notes, recording the
    acting moderator as reviewed_by. Gated by _is_market_moderator.

    Body (true PATCH semantics — only applies keys that are present):
      • status            — one of MarketReport.STATUS_CHOICES (400 if invalid).
      • resolution_notes  — replaces the note (send "" to clear it).

    Response: 200 {"message", "report": <serialized>}; 404 if the report is gone.
    Frontend consumer: the Resolve / Dismiss / Mark-reviewing buttons in the admin
    "Reports & Flags" tab. (Banning a subject is a SEPARATE call — admin_market_ban.)
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_market_moderator(user):
        return Response({"message": "You do not have permission to manage market reports."}, status=403)

    report = (
        MarketReport.objects.select_related(
            "reported_team", "reported_player", "reporter", "reviewed_by"
        )
        .filter(pk=report_id)
        .first()
    )
    if not report:
        return Response({"message": "Report not found."}, status=404)

    # ── status: only apply when present AND valid ──
    if "status" in request.data:
        new_status = request.data.get("status")
        valid_statuses = {choice[0] for choice in MarketReport.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response({"message": "Invalid report status."}, status=400)
        report.status = new_status

    # ── resolution_notes: apply when the key was sent (allows clearing to "") ──
    if "resolution_notes" in request.data:
        report.resolution_notes = request.data.get("resolution_notes") or ""

    # ── reviewed_by: always stamp the acting moderator — they handled this report ──
    report.reviewed_by = user
    report.save()

    return Response(
        {"message": "Report updated.", "report": _serialize_report(report)},
        status=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  POST /admin/ban/  — moderator bans a player or a team from the market
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_market_ban(request):
    """Ban a PLAYER or a TEAM from the player market for a duration (or permanently).
    Gated by _is_market_moderator.

    Request (JSON):
      • scope        (required) — "player" or "team".
      • target_id    (required) — the player's user id (scope=player) or the team_id
                                   (scope=team).
      • duration_days(optional) — positive integer number of days. Omit / null / 0 →
                                   a PERMANENT ban (mirrors the mockup's "Permanent"
                                   preset, which sends days:null).
      • reason       (required) — shown to the banned user (max 255).
      • report_id    (optional) — the originating report; when given, that report is
                                   stamped status="banned" so the queue reflects it.

    Response: 201 {"message", "ban": <serialized>}; 400 on bad input; 404 if the
    target does not exist. Idempotency note: a fresh ban row is created each time (bans
    accumulate as a history; enforcement only reads the active one), matching how
    BannedPlayer rows are created in this codebase.

    Enforcement: once created, _active_market_ban(target) returns this ban, so the
    target's next post/apply/invite attempt is blocked in views.py.
    Frontend consumer: the Ban dialog on the admin "Reports & Flags" tab.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_market_moderator(user):
        return Response({"message": "You do not have permission to ban from the market."}, status=403)

    data = request.data

    # ── scope ──
    scope = data.get("scope")
    if scope not in ("player", "team"):
        return Response({"message": "scope must be 'player' or 'team'."}, status=400)

    # ── target_id ──
    target_id = data.get("target_id")
    if not target_id:
        return Response({"message": "target_id is required."}, status=400)

    # ── reason (required, shown to the banned user) ──
    reason = (data.get("reason") or "").strip()
    if not reason:
        return Response({"message": "A ban reason is required."}, status=400)

    # ── duration: omit / null / 0 → permanent; otherwise a positive integer of days ──
    raw_duration = data.get("duration_days")
    if raw_duration in (None, "", 0, "0"):
        duration_days = None                 # permanent
    else:
        try:
            duration_days = int(raw_duration)
        except (TypeError, ValueError):
            return Response({"message": "duration_days must be a whole number of days."}, status=400)
        if duration_days <= 0:
            return Response({"message": "duration_days must be a positive number of days."}, status=400)

    # ── resolve the concrete target (404 if it does not exist) ──
    banned_team = None
    banned_player = None
    if scope == "team":
        banned_team = Team.objects.filter(team_id=target_id).first()
        if not banned_team:
            return Response({"message": "Team not found."}, status=404)
    else:
        # the user model id field is user_id (afc_auth.User extends AbstractUser); pk works too.
        banned_player = validate_target_user(target_id)
        if not banned_player:
            return Response({"message": "Player not found."}, status=404)

    # ── optional originating report (stamp it "banned" so the queue reflects it) ──
    source_report = None
    report_id = data.get("report_id")
    if report_id:
        source_report = MarketReport.objects.filter(pk=report_id).first()

    # ── create the ban (save() computes ban_end_date for a finite duration) ──
    ban = MarketBan.objects.create(
        scope=scope,
        banned_team=banned_team,
        banned_player=banned_player,
        ban_duration=duration_days,
        reason=reason,
        banned_by=user,
        source_report=source_report,
        is_active=True,
    )

    # ── reflect the ban on the originating report ──
    if source_report:
        source_report.status = "banned"
        source_report.reviewed_by = user
        if not source_report.resolution_notes:
            dur_txt = "permanently" if duration_days is None else f"for {duration_days} days"
            scope_txt = "Team" if scope == "team" else "Player"
            source_report.resolution_notes = f"{scope_txt} banned {dur_txt}. Reason: {reason}"
        source_report.save()

    return Response(
        {"message": "Ban applied.", "ban": _serialize_ban(ban)},
        status=201,
    )


def validate_target_user(target_id):
    """Resolve a User by id for the ban target. Imported lazily to avoid a circular
    import at module load (afc_auth.models ← afc_player_market). Returns the User or None.
    """
    from afc_auth.models import User
    return User.objects.filter(pk=target_id).first()
