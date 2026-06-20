# afc_auth/views_player_reports.py
# ──────────────────────────────────────────────────────────────────────────────
# PLAYER-TO-PLAYER REPORTS (owner 2026-06-20)
#
# Any logged-in player can report ANOTHER player with a category + notes + optional
# proof image. Admins review the whole queue and ANSWER each report (the answer is
# shown back to the reporter under "My reports"). When one player collects
# REPEAT_OFFENDER_THRESHOLD (3) reports inside REPEAT_OFFENDER_WINDOW_DAYS (14), a
# "repeat offender" flag is surfaced to admins on that player's rows.
#
# This module deliberately mirrors afc_player_market/views_moderation.py (the
# approved reporting pattern in this codebase): function-based @api_view views, one
# job each; inline Bearer-token auth via validate_token; inline dict serialization;
# Response({...}, status=...) everywhere. Route mounting lives in afc_auth/urls.py.
#
# Endpoints
#   • POST  /auth/report-player/            file_player_report          (any user)
#   • GET   /auth/my-player-reports/        my_player_reports           (reporter)
#   • GET   /auth/admin/player-reports/     admin_list_player_reports   (moderator)
#   • PATCH /auth/admin/player-reports/<id>/admin_respond_player_report (moderator)
#
# Frontend consumers
#   • file        -> components/player/ReportPlayerDialog.tsx on app/(user)/players/[username]
#   • my reports  -> the "My reports" view (settings) reading my_player_reports
#   • triage      -> the admin "Player Reports" tab reading admin_list_player_reports
# ──────────────────────────────────────────────────────────────────────────────
from datetime import timedelta

from django.utils import timezone
from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

# validate_token + the User/UserReport/Notifications models. validate_token lives in
# this same app's views.py; import it directly (afc_player_market imports it the same
# way). Importing from .views is safe here because urls.py imports this module, not
# the reverse at module-load time.
from .views import validate_token
from .models import User, UserReport, Notifications
from afc_team.models import Team


# ──────────────────────────────────────────────────────────────────────────────
# §0  Config + shared helpers (auth, moderator gate, pagination, repeat-offender)
# ──────────────────────────────────────────────────────────────────────────────

# Repeat-offender rule (owner): 3 reports against the SAME player inside a rolling
# 14-day window raises the admin-facing flag.
REPEAT_OFFENDER_WINDOW_DAYS = 14
REPEAT_OFFENDER_THRESHOLD = 3

# Granular roles (UserRoles table) that may triage player reports, on top of the
# coarse user.role in {admin, moderator}. head_admin is the platform super-role;
# support handles user-care queues. Mirrors the role reads elsewhere in afc_auth.
REPORT_MODERATOR_ROLES = ("head_admin", "support")


def _authenticate(request):
    """Resolve the caller from the Bearer Authorization header.

    Returns (user, None) when authenticated, or (None, error_response) with a
    400 (missing/garbled header) or 401 (invalid/expired token). Same shape +
    wording as afc_player_market/views_moderation.py::_authenticate.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid session."}, status=401)
    return user, None


def _is_report_moderator(user) -> bool:
    """True for users allowed to triage player reports.

    Coarse gate (matches the rest of the admin surface): user.role in
    {admin, moderator}. PLUS the granular roles in REPORT_MODERATOR_ROLES, read the
    same way afc_auth reads them (user.userroles.filter(role__role_name=...)).
    """
    if not user:
        return False
    if user.role in ("admin", "moderator"):
        return True
    return user.userroles.filter(role__role_name__in=REPORT_MODERATOR_ROLES).exists()


def _paginate(request, queryset):
    """?limit (default 25, max 100) + ?offset. Returns (page, total_count, has_more).
    Junk values fall back to defaults rather than 500-ing. Identical to the market /
    organizer report paginators."""
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


def _recent_report_count(subject_type, subject_id) -> int:
    """How many reports the given SUBJECT (player or team) has collected in the last
    REPEAT_OFFENDER_WINDOW_DAYS days. Drives the admin repeat-offender flag. Returns 0
    for a null subject (a deleted account/team)."""
    if not subject_id:
        return 0
    since = timezone.now() - timedelta(days=REPEAT_OFFENDER_WINDOW_DAYS)
    qs = UserReport.objects.filter(created_at__gte=since)
    if subject_type == "team":
        return qs.filter(reported_team_id=subject_id).count()
    return qs.filter(reported_user_id=subject_id).count()


def _subject_id(report):
    """The pk of whatever this report targets (team id for team reports, else user id)."""
    return report.reported_team_id if report.subject_type == "team" else report.reported_user_id


def _subject_name(report):
    """Human label for the reported subject (team name or username), or None if deleted."""
    if report.subject_type == "team":
        return report.reported_team.team_name if report.reported_team else None
    return report.reported_user.username if report.reported_user else None


def _serialize_report(report, *, for_admin=False, recent_count=None):
    """Canonical inline dict for one UserReport (player OR team subject).

    `for_admin` adds the reporter identity + the repeat-offender flag (admins only).
    `recent_count` lets the caller pass a precomputed count (admin list) to avoid a
    per-row query; when omitted and for_admin is True it is computed here.
    """
    data = {
        "id": report.id,
        "subject_type": report.subject_type,
        # Subject ids (one is null depending on subject_type).
        "reported_user_id": report.reported_user_id,
        "reported_team_id": report.reported_team_id,
        # Unified display label for whichever subject is set.
        "reported_name": _subject_name(report),
        # Back-compat: keep reported_username for the reporter "My reports" view.
        "reported_username": report.reported_user.username if report.reported_user else None,
        "category": report.category,
        "details": report.details,
        # The uploaded proof image (absolute-from-MEDIA url) so admins can OPEN it.
        "evidence": report.evidence.url if report.evidence else None,
        "status": report.status,
        # The admin's reporter-facing answer (empty until an admin responds).
        "admin_response": report.admin_response,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "updated_at": report.updated_at.isoformat() if report.updated_at else None,
    }
    if for_admin:
        count = recent_count if recent_count is not None else _recent_report_count(report.subject_type, _subject_id(report))
        data.update({
            "reporter_id": report.reporter_id,
            "reporter_username": report.reporter.username if report.reporter else None,
            "reviewed_by_username": report.reviewed_by.username if report.reviewed_by else None,
            # Repeat-offender flag: the reported subject has >= threshold reports in the window.
            "recent_report_count": count,
            "is_repeat_offender": count >= REPEAT_OFFENDER_THRESHOLD,
        })
    return data


def _create_report(reporter, *, subject_type, reported_user=None, reported_team=None,
                   category, details, evidence):
    """Shared creator for player + team reports (DRY for file_player_report /
    file_team_report). Returns the created UserReport row, always status 'open'."""
    return UserReport.objects.create(
        reporter=reporter,
        subject_type=subject_type,
        reported_user=reported_user,
        reported_team=reported_team,
        category=category,
        details=details,
        evidence=evidence,
        status="open",
    )


# ──────────────────────────────────────────────────────────────────────────────
# §1  POST /auth/report-player/  — file a report against another player (any user)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def file_player_report(request):
    """File a report against another player. Open to ANY logged-in user.

    Request (JSON or multipart):
      • reported_user_id  — the User being reported (by primary key), OR
      • reported_username — the User being reported (by username). One of the two is
                            required; the PUBLIC player profile only has the username
                            (PublicPlayer has no id), so it sends reported_username.
      • category          (optional) — one of UserReport.CATEGORY_CHOICES; default "other".
      • details           (required) — free-text notes describing what happened.
      • evidence          (optional) — proof image (screenshot). Encouraged, not required.

    Rules: you cannot report yourself; the reported user must exist.

    Response: 201 on success; 400 bad input / self-report; 404 reported user gone.
    Auth: Bearer token (any valid session).
    Frontend: ReportPlayerDialog on the public player profile (players/[username]).
    """
    user, err = _authenticate(request)
    if err:
        return err

    # Accept either the id (other callers) or the username (the public profile).
    reported_user_id = request.data.get("reported_user_id")
    reported_username = (request.data.get("reported_username") or "").strip()
    if not reported_user_id and not reported_username:
        return Response({"message": "reported_user_id or reported_username is required."}, status=400)

    if reported_user_id:
        reported_user = User.objects.filter(pk=reported_user_id).first()
    else:
        reported_user = User.objects.filter(username__iexact=reported_username).first()
    if not reported_user:
        return Response({"message": "The player you are reporting was not found."}, status=404)

    # Cannot report yourself (checked AFTER resolving so it works for both inputs).
    if reported_user.user_id == user.user_id:
        return Response({"message": "You cannot report yourself."}, status=400)

    # category: validate against the model choices, default "other".
    valid_categories = {c[0] for c in UserReport.CATEGORY_CHOICES}
    category = request.data.get("category") or "other"
    if category not in valid_categories:
        return Response({"message": "Invalid report category."}, status=400)

    # details: required free text.
    details = (request.data.get("details") or "").strip()
    if not details:
        return Response({"message": "Please describe what happened."}, status=400)

    evidence = request.FILES.get("evidence")  # optional proof image

    _create_report(
        user, subject_type="player", reported_user=reported_user,
        category=category, details=details, evidence=evidence,
    )

    return Response(
        {"message": "Report submitted. Thank you, AFC admins will review it."},
        status=201,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §1b  POST /auth/report-team/  — file a report against a whole TEAM (any user)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def file_team_report(request):
    """File a report against a whole TEAM. Open to ANY logged-in user (owner 2026-06-20,
    "player and team reports"). Mirrors file_player_report but the subject is a Team.

    Request (JSON or multipart):
      • reported_team_id   — the Team being reported (by primary key team_id), OR
      • reported_team_name — the Team being reported (by exact team name).
      • category           (optional) — one of UserReport.CATEGORY_CHOICES; default "other".
      • details            (required) — free-text notes describing what happened.
      • evidence           (optional) — proof image (screenshot).

    Rules: the team must exist. (No self-team guard - a player CAN report their own team,
    e.g. for internal misconduct; admins triage.)

    Response: 201 on success; 400 bad input; 404 team gone.
    Frontend: the Report button on the public team page (teams/[id]).
    """
    user, err = _authenticate(request)
    if err:
        return err

    reported_team_id = request.data.get("reported_team_id")
    reported_team_name = (request.data.get("reported_team_name") or "").strip()
    if not reported_team_id and not reported_team_name:
        return Response({"message": "reported_team_id or reported_team_name is required."}, status=400)

    if reported_team_id:
        reported_team = Team.objects.filter(pk=reported_team_id).first()
    else:
        reported_team = Team.objects.filter(team_name__iexact=reported_team_name).first()
    if not reported_team:
        return Response({"message": "The team you are reporting was not found."}, status=404)

    valid_categories = {c[0] for c in UserReport.CATEGORY_CHOICES}
    category = request.data.get("category") or "other"
    if category not in valid_categories:
        return Response({"message": "Invalid report category."}, status=400)

    details = (request.data.get("details") or "").strip()
    if not details:
        return Response({"message": "Please describe what happened."}, status=400)

    evidence = request.FILES.get("evidence")

    _create_report(
        user, subject_type="team", reported_team=reported_team,
        category=category, details=details, evidence=evidence,
    )

    return Response(
        {"message": "Report submitted. Thank you, AFC admins will review it."},
        status=201,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET /auth/my-player-reports/  — reports the CALLER filed (with admin answers)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def my_player_reports(request):
    """List the reports the calling user has filed, newest first, paginated.

    Includes the admin's `admin_response` + `status` so the reporter can read the
    answer to each report. Does NOT expose other reporters' reports.
    Response: {"results": [...], "total_count", "has_more"}.
    Frontend: the user's "My reports" view (settings).
    """
    user, err = _authenticate(request)
    if err:
        return err

    qs = (
        UserReport.objects.select_related("reported_user", "reported_team")
        .filter(reporter=user)
        .order_by("-created_at")
    )
    page, total_count, has_more = _paginate(request, qs)
    # Reporter view: no other-reporter identity, no repeat-offender flag.
    results = [_serialize_report(r, for_admin=False) for r in page]
    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  GET /auth/admin/player-reports/  — moderator triage queue of every report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_player_reports(request):
    """Triage list of every player report for moderators. Gated by _is_report_moderator.

    Filters: ?status=, ?category=, ?search= (over reported username + reporter
    username), and ?flagged=true to show ONLY repeat offenders (players at/above the
    3-in-2-weeks threshold). Paginated (?limit default 25 / max 100, ?offset).
    Newest first. Each row carries the repeat-offender flag + recent_report_count.

    Response: {"results": [...], "total_count", "has_more", "flagged_total"} where
    flagged_total is the number of DISTINCT players currently over the threshold.
    Frontend: the admin "Player Reports" tab.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_report_moderator(user):
        return Response({"message": "You do not have permission to view player reports."}, status=403)

    qs = (
        UserReport.objects.select_related(
            "reported_user", "reported_team", "reporter", "reviewed_by"
        )
        .all()
        .order_by("-created_at")
    )

    status_filter = request.GET.get("status")
    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    category_filter = request.GET.get("category")
    if category_filter and category_filter != "all":
        qs = qs.filter(category=category_filter)

    # ?subject_type=player|team filter (owner 2026-06-20: player + team reports share this queue).
    subject_filter = request.GET.get("subject_type")
    if subject_filter in ("player", "team"):
        qs = qs.filter(subject_type=subject_filter)

    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(reported_user__username__icontains=search)
            | Q(reported_team__team_name__icontains=search)
            | Q(reporter__username__icontains=search)
        )

    # Precompute the recent-report count per reported SUBJECT ONCE (covers the whole
    # filtered set, not just the page) so every row's flag is correct and we avoid an
    # N+1 of per-row COUNTs. Two maps: players (reported_user_id) and teams (reported_team_id),
    # each over the 14-day window.
    since = timezone.now() - timedelta(days=REPEAT_OFFENDER_WINDOW_DAYS)
    from django.db.models import Count
    player_rows = (
        UserReport.objects.filter(created_at__gte=since, reported_user__isnull=False)
        .values("reported_user_id").annotate(n=Count("id"))
    )
    team_rows = (
        UserReport.objects.filter(created_at__gte=since, reported_team__isnull=False)
        .values("reported_team_id").annotate(n=Count("id"))
    )
    player_map = {row["reported_user_id"]: row["n"] for row in player_rows}
    team_map = {row["reported_team_id"]: row["n"] for row in team_rows}
    flagged_total = (
        sum(1 for n in player_map.values() if n >= REPEAT_OFFENDER_THRESHOLD)
        + sum(1 for n in team_map.values() if n >= REPEAT_OFFENDER_THRESHOLD)
    )

    def _count_for(report):
        if report.subject_type == "team":
            return team_map.get(report.reported_team_id, 0)
        return player_map.get(report.reported_user_id, 0)

    # ?flagged=true -> restrict to reports whose reported SUBJECT (player OR team) is over threshold.
    if (request.GET.get("flagged") or "").lower() in ("1", "true", "yes"):
        flagged_uids = [uid for uid, n in player_map.items() if n >= REPEAT_OFFENDER_THRESHOLD]
        flagged_tids = [tid for tid, n in team_map.items() if n >= REPEAT_OFFENDER_THRESHOLD]
        qs = qs.filter(
            Q(reported_user_id__in=flagged_uids) | Q(reported_team_id__in=flagged_tids)
        )

    page, total_count, has_more = _paginate(request, qs)
    results = [
        _serialize_report(r, for_admin=True, recent_count=_count_for(r))
        for r in page
    ]
    return Response(
        {
            "results": results,
            "total_count": total_count,
            "has_more": has_more,
            "flagged_total": flagged_total,
        },
        status=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  PATCH /auth/admin/player-reports/<id>/  — moderator answers / triages a report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def admin_respond_player_report(request, report_id):
    """Answer / triage one player report. Gated by _is_report_moderator.

    Body (PATCH semantics — only applies keys that are present):
      • status         — one of UserReport.STATUS_CHOICES (400 if invalid).
      • admin_response — the reporter-facing answer (send "" to clear).

    Stamps reviewed_by = the acting moderator. When an answer is provided, notifies
    the reporter with a "Take me there" deep link to their My-reports view.

    Response: 200 {"message", "report": <serialized for_admin>}; 404 if gone.
    Frontend: the Answer / Resolve / Dismiss controls on the admin "Player Reports" tab.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_report_moderator(user):
        return Response({"message": "You do not have permission to manage player reports."}, status=403)

    report = (
        UserReport.objects.select_related(
            "reported_user", "reported_team", "reporter", "reviewed_by"
        )
        .filter(pk=report_id)
        .first()
    )
    if not report:
        return Response({"message": "Report not found."}, status=404)

    answered = False
    if "status" in request.data:
        new_status = request.data.get("status")
        valid_statuses = {c[0] for c in UserReport.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response({"message": "Invalid report status."}, status=400)
        report.status = new_status

    if "admin_response" in request.data:
        new_answer = request.data.get("admin_response") or ""
        # Only fire a notification when a non-empty answer is newly set/changed.
        answered = bool(new_answer.strip()) and new_answer != report.admin_response
        report.admin_response = new_answer

    report.reviewed_by = user
    report.save()

    # Notify the reporter that their report was answered, with a deep link to where
    # they can read it (target_type="custom" -> a relative path used as-is). Guarded
    # so a notification hiccup never fails the admin's save.
    if answered and report.reporter_id:
        try:
            subj = "team" if report.subject_type == "team" else "player"
            Notifications.objects.create(
                user=report.reporter,
                title="Your report was reviewed",
                message=f"An AFC admin has responded to a {subj} report you filed.",
                notification_type="player_report_response",
                target_type="custom",
                target_id="/profile?tab=reports",
            )
        except Exception:
            pass

    return Response(
        {"message": "Report updated.", "report": _serialize_report(report, for_admin=True)},
        status=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §5  POST /auth/complete-onboarding/  — mark the first-login onboarding done/skipped
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def complete_onboarding(request):
    """Flip the caller's User.has_completed_onboarding to True (owner 2026-06-20).

    Called by the frontend /onboarding flow on BOTH "Finish" and "Skip for now" - either
    way the user has seen it, so we never auto-send them back. Idempotent. Open to any
    logged-in user (it only affects their own row).

    AUTH     : Bearer SessionToken.
    REQUEST  : POST /auth/complete-onboarding/  (no body)
    RESPONSE : 200 {"status": "ok", "has_completed_onboarding": true}
    FRONTEND : app/(onboarding)/onboarding/page.tsx (Finish + Skip), and the first-login
               redirect guard in contexts/AuthContext.tsx reads has_completed_onboarding
               from get_user_profile.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not user.has_completed_onboarding:
        user.has_completed_onboarding = True
        user.save(update_fields=["has_completed_onboarding"])
    return Response({"status": "ok", "has_completed_onboarding": True}, status=200)
