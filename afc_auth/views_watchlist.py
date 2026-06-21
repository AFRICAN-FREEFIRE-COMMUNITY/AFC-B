# afc_auth/views_watchlist.py
# ──────────────────────────────────────────────────────────────────────────────
# WATCHLIST (owner 2026-06-21)
#
# A SHARED, AFC-WIDE ADVISORY watchlist of suspicious players + teams. Admins AND
# organizers both add/clear/view the SAME entries; every flagged name shows a "Watch"
# tag to admins/organizers, and there is a Watchlist tab in the admin + organizer
# dashboards. It NEVER blocks anything (unlike BannedPlayer/TeamBan/OrganizerBlacklist);
# it only warns, plus a soft heads-up notification when a watched entity registers (that
# notification is raised from afc_tournament_and_scrims, which reads WatchlistEntry).
#
# Mirrors afc_auth/views_player_reports.py: function-based @api_view, inline Bearer auth,
# inline dict serialization, _paginate. Route mounting in afc_auth/urls.py.
#
# Endpoints (prefix auth/)
#   • GET   auth/watchlist/        list_watchlist     (admin or organizer)
#   • POST  auth/watchlist/        add_watchlist      (admin or organizer)
#   • PATCH auth/watchlist/<id>/   update_watchlist   (clear / reactivate)
#   • GET   auth/watchlist/tags/   watchlist_tags     (bulk "which ids are watched", N+1-free)
#
# Frontend: lib/watchlist.ts -> /a/watchlist (admin, i18n-exempt) + /organizer/watchlist
# (i18n en/fr/pt) + the <WatchTag> badge + the "Add to watchlist" button on the leaderboard
# upload review. Spec: WEBSITE/tasks/watchlist-spec.md.
# ──────────────────────────────────────────────────────────────────────────────
from django.utils import timezone
from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .views import validate_token
from .models import User, WatchlistEntry
from afc_team.models import Team


# ──────────────────────────────────────────────────────────────────────────────
# §0  Auth + the watchlist permission gate + shared helpers
# ──────────────────────────────────────────────────────────────────────────────

# Granular admin roles (UserRoles) that count as AFC admins for the watchlist, on top of
# the coarse user.role == "admin". Mirrors the role reads elsewhere in afc_auth.
WATCHLIST_ADMIN_ROLES = ("head_admin", "super_admin", "event_admin", "organizer_admin")


def _authenticate(request):
    """Resolve the caller from the Bearer header. (user, None) or (None, error). Same
    shape/wording as views_player_reports._authenticate."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid session."}, status=401)
    return user, None


def can_use_watchlist(user) -> bool:
    """Who may view + add + clear watchlist entries: AFC admins (coarse role "admin", or a
    granular WATCHLIST_ADMIN_ROLES role) OR ANY organizer (an active member of any organization).
    View == add == clear (one gate). The watchlist is NEVER exposed to the public or to the
    flagged user, so every endpoint checks this. Imported by the soft-warning hook too."""
    if not user:
        return False
    if user.role in ("admin", "moderator", "support"):
        return True
    if user.userroles.filter(role__role_name__in=WATCHLIST_ADMIN_ROLES).exists():
        return True
    # Any active organizer (owner / sub-organizer of any org). Imported lazily to avoid a
    # load-time dependency cycle between afc_auth and afc_organizers.
    try:
        from afc_organizers.models import OrganizationMember
        return OrganizationMember.objects.filter(user=user, status="active").exists()
    except Exception:
        return False


def _paginate(request, queryset):
    """?limit (default 50, max 200) + ?offset. (page, total_count, has_more). Junk -> defaults."""
    try:
        limit = int(request.GET.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


def _serialize(entry):
    """Canonical inline dict for one WatchlistEntry (player OR team subject)."""
    return {
        "watch_id": entry.watch_id,
        "subject_type": entry.subject_type,
        # Subject ids (one is null per subject_type).
        "player_id": entry.player_id,
        "team_id": entry.team_id,
        # Display fields for whichever subject is set.
        "subject_name": entry.subject_name,
        "player_username": entry.player.username if entry.player else None,
        "player_uid": entry.player.uid if entry.player else None,  # FF UID, for ringer context
        "team_name": entry.team.team_name if entry.team else None,
        "reason": entry.reason,
        "source": entry.source,
        "context": entry.context,
        "status": entry.status,
        "added_by_username": entry.added_by.username if entry.added_by else None,
        "cleared_by_username": entry.cleared_by.username if entry.cleared_by else None,
        "cleared_at": entry.cleared_at.isoformat() if entry.cleared_at else None,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _find_subject_entry(subject_type, player=None, team=None):
    """The single logical entry for a subject (active or cleared), newest first, or None.
    Used to dedup: one logical watch per subject — re-adding reactivates instead of duplicating."""
    qs = WatchlistEntry.objects.filter(subject_type=subject_type)
    if subject_type == "team":
        qs = qs.filter(team=team)
    else:
        qs = qs.filter(player=player)
    return qs.order_by("-created_at").first()


# ──────────────────────────────────────────────────────────────────────────────
# §1  GET/POST auth/watchlist/  — list + add
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "POST"])
def watchlist_collection(request):
    """GET: list watchlist entries (admin/organizer). Filters: ?subject_type=player|team,
    ?status=active|cleared|all (default active), ?search= (subject name), paginated
    (?limit default 50 / max 200, ?offset). Newest first.

    POST: add a player or team to the watchlist. Body: {subject_type, player_id|team_id,
    reason (required), source? ("manual"|"upload"), context?}. Dedup: if the subject already
    has an entry it is REACTIVATED + reason refreshed (no duplicate row). Records added_by.

    Response: GET {results, total_count, has_more}; POST 201 {entry}. Gate: can_use_watchlist."""
    user, err = _authenticate(request)
    if err:
        return err
    if not can_use_watchlist(user):
        return Response({"message": "You do not have permission to use the watchlist."}, status=403)

    if request.method == "GET":
        qs = WatchlistEntry.objects.select_related("player", "team", "added_by", "cleared_by")
        status_filter = request.GET.get("status") or "active"
        if status_filter != "all":
            qs = qs.filter(status=status_filter)
        subject_filter = request.GET.get("subject_type")
        if subject_filter in ("player", "team"):
            qs = qs.filter(subject_type=subject_filter)
        search = (request.GET.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(player__username__icontains=search)
                | Q(player__uid__icontains=search)
                | Q(team__team_name__icontains=search)
            )
        qs = qs.order_by("-created_at")
        page, total_count, has_more = _paginate(request, qs)
        return Response(
            {"results": [_serialize(e) for e in page], "total_count": total_count, "has_more": has_more},
            status=200,
        )

    # POST = add (or reactivate)
    subject_type = request.data.get("subject_type")
    if subject_type not in ("player", "team"):
        return Response({"message": "subject_type must be 'player' or 'team'."}, status=400)
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"message": "A reason is required."}, status=400)
    source = request.data.get("source") if request.data.get("source") in ("manual", "upload") else "manual"
    context = (request.data.get("context") or "").strip()[:255]

    # Resolve the subject by id OR by name (the manual "Add to watchlist" dialog sends a typed
    # username/team name; the upload-review one-click + programmatic callers send the id). Mirrors
    # how file_player_report accepts reported_username as an alternative to reported_user_id.
    player = team = None
    if subject_type == "player":
        pid = request.data.get("player_id")
        uname = (request.data.get("player_username") or "").strip()
        if pid:
            player = User.objects.filter(pk=pid).first()
        elif uname:
            player = User.objects.filter(username__iexact=uname).first()
        if not player:
            return Response({"message": "Player not found."}, status=404)
    else:
        tid = request.data.get("team_id")
        tname = (request.data.get("team_name") or "").strip()
        if tid:
            team = Team.objects.filter(pk=tid).first()
        elif tname:
            team = Team.objects.filter(team_name__iexact=tname).first()
        if not team:
            return Response({"message": "Team not found."}, status=404)

    # Dedup: reactivate/refresh an existing logical entry rather than creating a duplicate.
    existing = _find_subject_entry(subject_type, player=player, team=team)
    if existing:
        existing.status = "active"
        existing.reason = reason
        existing.source = source
        existing.context = context or existing.context
        existing.added_by = user
        existing.cleared_by = None
        existing.cleared_at = None
        existing.save()
        return Response(
            {"message": f"{existing.subject_name} is now on the watchlist.", "entry": _serialize(existing)},
            status=200,
        )

    entry = WatchlistEntry.objects.create(
        subject_type=subject_type, player=player, team=team,
        reason=reason, source=source, context=context, added_by=user, status="active",
    )
    return Response(
        {"message": f"{entry.subject_name} added to the watchlist.", "entry": _serialize(entry)},
        status=201,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  PATCH auth/watchlist/<id>/  — clear or reactivate
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def watchlist_item(request, watch_id):
    """Clear (stop watching) or reactivate one entry. Body: {action: "clear"|"reactivate"}.
    Clearing is a soft-clear (status=cleared + cleared_by/at) so the audit trail survives.
    Gate: can_use_watchlist (any admin/organizer, since any can add). Response 200 {entry}."""
    user, err = _authenticate(request)
    if err:
        return err
    if not can_use_watchlist(user):
        return Response({"message": "You do not have permission to use the watchlist."}, status=403)

    entry = (
        WatchlistEntry.objects.select_related("player", "team", "added_by", "cleared_by")
        .filter(pk=watch_id).first()
    )
    if not entry:
        return Response({"message": "Watchlist entry not found."}, status=404)

    action = request.data.get("action", "clear")
    if action == "reactivate":
        entry.status = "active"
        entry.cleared_by = None
        entry.cleared_at = None
    else:  # clear
        entry.status = "cleared"
        entry.cleared_by = user
        entry.cleared_at = timezone.now()
    entry.save()
    return Response({"message": "Watchlist entry updated.", "entry": _serialize(entry)}, status=200)


# ──────────────────────────────────────────────────────────────────────────────
# §3  GET auth/watchlist/tags/  — bulk "which of these ids are currently watched"
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def watchlist_tags(request):
    """Bulk lookup so a list page renders <WatchTag> in ONE call (no N+1). Query:
    ?player_ids=1,2,3 &team_ids=4,5. Returns only ACTIVE entries.
    Response: {"watched_player_ids": [...], "watched_team_ids": [...]}. Gate: can_use_watchlist."""
    user, err = _authenticate(request)
    if err:
        return err
    if not can_use_watchlist(user):
        return Response({"message": "You do not have permission to use the watchlist."}, status=403)

    def _ids(param):
        raw = request.GET.get(param) or ""
        out = []
        for tok in raw.split(","):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        return out

    player_ids = _ids("player_ids")
    team_ids = _ids("team_ids")
    watched_players = []
    watched_teams = []
    if player_ids:
        watched_players = list(
            WatchlistEntry.objects.filter(status="active", subject_type="player", player_id__in=player_ids)
            .values_list("player_id", flat=True)
        )
    if team_ids:
        watched_teams = list(
            WatchlistEntry.objects.filter(status="active", subject_type="team", team_id__in=team_ids)
            .values_list("team_id", flat=True)
        )
    return Response(
        {"watched_player_ids": list(set(watched_players)), "watched_team_ids": list(set(watched_teams))},
        status=200,
    )
