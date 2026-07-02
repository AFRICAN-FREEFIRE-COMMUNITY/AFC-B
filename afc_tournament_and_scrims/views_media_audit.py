# ── EVENT MEDIA AUDIT + FLAGS + OPT-OUTS (owner 2026-07-02) ─────────────────────
# Broadcast-media hygiene for one event, surfaced on the overlay STUDIO (admin + organizer):
#   • AUDIT   — which registered TEAMS have no team logo, which roster PLAYERS have no esport image
#               (both render on overlays/graphics at a fixed size, so gaps + bad art show on stream).
#   • FLAG    — tag a bad team logo / player esport image; the owner gets a Notification asking for
#               a replacement (deep-linkable). Flags stay listed until resolved.
#   • OPT-OUT — per-event suppression: remove a team's logo / a player's image from THIS event's
#               broadcast surfaces without deleting the upload (EventMediaOptOut; the overlay feed
#               skips suppressed logos).
#
# ENDPOINTS (gate = _broadcast_gate — AFC event admin OR org can_edit_events):
#   GET  events/<event_id>/media-audit/            -> teams+players with media status, flags, opt-outs
#   POST events/<event_id>/media-flags/            -> {kind, team_id?|user_id?, reason?} flag + notify
#   POST events/<event_id>/media-flags/<id>/resolve/
#   POST events/<event_id>/media-opt-outs/         -> {kind, team_id?|user_id?, remove?: bool}
#     (the studio calls this for admins/organizers; a self-serve user surface can reuse it later
#      with its own ownership gate)
#
# CONSUMED BY: components/overlay/MediaAuditCard.tsx inside EventOverlayStudio.

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Event, EventMediaOptOut, MediaFlag, TournamentTeam, TournamentTeamMember
from .views import _broadcast_gate


def _team_rows(event, request):
    """Registered teams with logo status + suppression/flag state."""
    opt_team_ids = set(
        EventMediaOptOut.objects.filter(event=event, kind="team_logo")
        .values_list("team_id", flat=True)
    )
    flags = {
        f.team_id: f for f in
        MediaFlag.objects.filter(event=event, kind="team_logo", resolved=False)
    }
    rows = []
    for tt in TournamentTeam.objects.filter(event=event).select_related("team"):
        team = tt.team
        if not team:
            continue
        has_logo = bool(getattr(team, "team_logo", None))
        rows.append({
            "team_id": team.team_id,
            "team_name": team.team_name,
            "has_logo": has_logo,
            "logo_url": request.build_absolute_uri(team.team_logo.url) if has_logo else None,
            "suppressed": team.team_id in opt_team_ids,
            "flagged": team.team_id in flags,
        })
    return rows


def _player_rows(event, request):
    """Roster players with esport-image status + suppression/flag state."""
    opt_user_ids = set(
        EventMediaOptOut.objects.filter(event=event, kind="esports_image")
        .values_list("user_id", flat=True)
    )
    flags = {
        f.user_id: f for f in
        MediaFlag.objects.filter(event=event, kind="esports_image", resolved=False)
    }
    rows, seen = [], set()
    for m in TournamentTeamMember.objects.filter(
        tournament_team__event=event
    ).select_related("user", "tournament_team__team"):
        u = m.user
        if u is None or u.user_id in seen:
            continue
        seen.add(u.user_id)
        # esports_pic lives on UserProfile, not User (bug fix 2026-07-02).
        from afc_auth.models import esports_pic_url
        img_url = esports_pic_url(u, request)
        rows.append({
            "user_id": u.user_id,
            "username": u.username,
            "in_game_name": getattr(u, "in_game_name", "") or u.username,
            "team_name": m.tournament_team.team.team_name if m.tournament_team.team else None,
            "has_image": bool(img_url),
            "image_url": img_url,
            "suppressed": u.user_id in opt_user_ids,
            "flagged": u.user_id in flags,
        })
    return rows


@api_view(["GET"])
def media_audit(request, event_id):
    """GET events/<event_id>/media-audit/ — the studio's media hygiene report."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    teams = _team_rows(event, request)
    players = _player_rows(event, request)
    return Response({
        "teams": teams,
        "players": players,
        "teams_missing_logo": sum(1 for t in teams if not t["has_logo"]),
        "players_missing_image": sum(1 for p in players if not p["has_image"]),
    }, status=200)


@api_view(["POST"])
def media_flag(request, event_id):
    """POST events/<event_id>/media-flags/ {kind, team_id?|user_id?, reason?} — flag bad media +
    notify the owner (team owner for a logo; the player for an esport image)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    from afc_auth.models import Notifications, User
    from afc_team.models import Team
    kind = (request.data.get("kind") or "").strip()
    if kind not in ("team_logo", "esports_image"):
        return Response({"message": "kind must be team_logo or esports_image."}, status=400)
    reason = (request.data.get("reason") or "").strip()[:200]

    # Resolve the acting user for the flag's audit trail (the gate validates but doesn't stash it).
    from afc_auth.views import validate_token
    auth = request.headers.get("Authorization") or ""
    flagger = validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") else None
    if kind == "team_logo":
        team = Team.objects.filter(team_id=request.data.get("team_id")).first()
        if not team:
            return Response({"message": "Team not found."}, status=404)
        flag, _ = MediaFlag.objects.get_or_create(
            event=event, kind=kind, team=team, resolved=False,
            defaults={"reason": reason, "flagged_by": flagger},
        )
        # Notify the team's OWNER (fall back to captain/creator) to replace the logo.
        owner = (getattr(team, "team_owner", None) or getattr(team, "team_captain", None)
                 or getattr(team, "team_creator", None))
        if owner:
            Notifications.objects.create(
                user=owner,
                notification_type="media_flag",
                title="Please update your team logo",
                message=(
                    f'Your team "{team.team_name}" logo was flagged for the event '
                    f'"{event.event_name}"{f": {reason}" if reason else ""}. '
                    "Please upload a replacement so it looks right on the broadcast."
                ),
                related_event=event,
            )
    else:
        user = User.objects.filter(user_id=request.data.get("user_id")).first()
        if not user:
            return Response({"message": "User not found."}, status=404)
        flag, _ = MediaFlag.objects.get_or_create(
            event=event, kind=kind, user=user, resolved=False,
            defaults={"reason": reason, "flagged_by": flagger},
        )
        Notifications.objects.create(
            user=user,
            notification_type="media_flag",
            title="Please update your esport image",
            message=(
                f'Your esport image was flagged for the event "{event.event_name}"'
                f'{f": {reason}" if reason else ""}. '
                "Please upload a replacement so it looks right on the broadcast."
            ),
            related_event=event,
        )
    return Response({"message": "Flagged and the owner was notified.", "flag_id": flag.id}, status=201)


@api_view(["POST"])
def media_flag_resolve(request, event_id, flag_id):
    """POST events/<event_id>/media-flags/<flag_id>/resolve/ — close a flag (media replaced)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = MediaFlag.objects.filter(event=event, id=flag_id).first()
    if not row:
        return Response({"message": "Flag not found."}, status=404)
    row.resolved = True
    row.save(update_fields=["resolved"])
    return Response({"message": "Flag resolved."}, status=200)


@api_view(["POST"])
def media_opt_out(request, event_id):
    """POST events/<event_id>/media-opt-outs/ {kind, team_id?|user_id?, remove?} — suppress (or
    restore with remove=true) a team logo / player image on THIS event's broadcast surfaces."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    from afc_auth.models import User
    from afc_team.models import Team
    kind = (request.data.get("kind") or "").strip()
    if kind not in ("team_logo", "esports_image"):
        return Response({"message": "kind must be team_logo or esports_image."}, status=400)
    team = Team.objects.filter(team_id=request.data.get("team_id")).first() if kind == "team_logo" else None
    user = User.objects.filter(user_id=request.data.get("user_id")).first() if kind == "esports_image" else None
    if not team and not user:
        return Response({"message": "Target not found."}, status=404)
    if request.data.get("remove"):
        EventMediaOptOut.objects.filter(event=event, kind=kind, team=team, user=user).delete()
        return Response({"message": "Suppression removed - the media shows again."}, status=200)
    EventMediaOptOut.objects.get_or_create(event=event, kind=kind, team=team, user=user)
    return Response({"message": "Suppressed for this event."}, status=201)


# ── UPLOAD (owner 2026-07-02: "admins can add esport images and team logos for teams/players") ──
# AFC ADMINS ONLY (is_stats_admin: role admin/moderator/support or granular platform-admin role):
# organizers keep flag/hide but cannot overwrite another team's media. Writes the SAME fields the
# audit reads (Team.team_logo / UserProfile.esports_pic), so the panel + overlays + exports pick
# the new file up immediately. Consumed by MediaAuditCard.tsx's per-row Upload button.
#
# POST events/<event_id>/media-upload/   multipart form:
#   kind = "team_logo" (+ team_id = Team pk)  |  "player_image" (+ user_id = User pk)
#   file = the image (normalized/re-encoded via afc_auth.image_utils.normalize_image_upload)
# Response: {message, url} — url = the new absolute media URL.
@api_view(["POST"])
def media_upload(request, event_id):
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    from afc_auth.views import is_stats_admin, validate_token
    auth = request.headers.get("Authorization", "")
    viewer = validate_token(auth.split(" ")[1]) if " " in auth else None
    if viewer is None or not is_stats_admin(viewer):
        return Response({"message": "Only AFC admins can upload media for teams or players."},
                        status=403)

    kind = (request.data.get("kind") or "").strip()
    upload = request.FILES.get("file")
    if kind not in ("team_logo", "player_image") or not upload:
        return Response({"message": "kind (team_logo|player_image) and file are required."},
                        status=400)

    from afc_auth.image_utils import normalize_image_upload
    upload = normalize_image_upload(upload)
    if upload is None:
        return Response({"message": "The uploaded file is not a valid image."}, status=400)

    if kind == "team_logo":
        from afc_team.models import Team
        try:
            team = Team.objects.get(team_id=request.data.get("team_id"))
        except (Team.DoesNotExist, ValueError, TypeError):
            return Response({"message": "Team not found."}, status=404)
        team.team_logo = upload
        team.save(update_fields=["team_logo"])
        url = request.build_absolute_uri(team.team_logo.url)
    else:
        from afc_auth.models import UserProfile
        try:
            profile, _ = UserProfile.objects.get_or_create(user_id=request.data.get("user_id"))
        except (ValueError, TypeError):
            return Response({"message": "Player not found."}, status=404)
        profile.esports_pic = upload
        profile.save()
        url = request.build_absolute_uri(profile.esports_pic.url)

    return Response({"message": "Media updated.", "url": url})
