"""
Admin write API — Ghost teams + ghost players + claim lifecycle (Phase 2).

A *ghost team* is a placeholder for an off-platform squad that shows up in tournament
results before its real members have AFC accounts (§19.4). Admins create the ghost +
its in-game roster (``GhostPlayer`` rows) so off-platform results can be attributed; a
real ``afc_team.Team`` can later *claim* the ghost, at which point its history maps onto
that team.

This module owns the admin CRUD + claim transitions for that lifecycle:

    list/detail/create/update/delete  ghost-teams/[<uuid>/]
    approve-claim / revoke-claim       ghost-teams/<uuid>/{approve,revoke}-claim/

Standalone ("parked") ghost players
-----------------------------------
``GhostPlayer.ghost_team`` is NULLABLE (see the model), so a ghost player can also exist on
its own — a provisional in-game name that is not yet attached to any ghost team. The flat
ghost-player surface here owns that case (so an admin no longer has to pick a ghost team
just to park an IGN):

    list/create        ghost-players/                      (GET list, POST flat create)
    detail/update/del  ghost-players/<int:player_id>/      (GET / PATCH / DELETE)

A standalone player is INERT for scoring: nothing in the scoring/aggregation/recalc path
reads GhostPlayer (result attribution is GhostTeam-keyed via
TeamMonthlyScore.ghost_team / TeamQuarterlyScore.ghost_team), so a team-less ghost player
is purely parked IGN data. The freeze guard (claim_status != "unclaimed" -> 400) therefore
only applies when the player IS attached to a team; standalone rows are always editable and
deletable. The flat create is consumed by the FE CreateGhostPlayerModal
(``rankingsAdminApi.createGhostPlayerFlat``); the nested
``ghost-teams/<uuid>/players/`` append route below is kept for the existing roster-append flow.

It is function-based DRF (``@api_view``) with manual-dict serialization, matching
``views.py`` / ``serializers.py``. The auth + audit foundation is reused verbatim from
``admin_views.py`` — DO NOT reimplement it here:

    user, err = _auth(request)              # 401/403 short-circuit (head_admin | metrics_admin)
    if err: return err
    reason, err = _require_reason(request)  # mandatory >=10-char audit reason on every mutation
    if err: return err
    with transaction.atomic():
        ... the write ...
    _audit(user, "ghost_claim", "<action>", reason, object_ref=..., before=..., after=...)

Audit bucket: every write here logs under ``object_type="ghost_claim"`` (one of
``RankingAuditLog.OBJECT_TYPES``) so the audit log filters cleanly to ghost activity.

Recalc note: claiming a ghost re-attributes historical results to the claiming team and
therefore needs a retroactive, cross-period recalc. That spans many months/seasons and is
NOT a single enqueue_team() call, so it is intentionally left to the coordinator — see the
``# TODO(recalc)`` marker in ``approve_claim``. We do NOT enqueue inline here.
"""
from django.db import models, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

# Reuse the shared auth/audit foundation (admin_views.py) — never re-implemented locally.
from .admin_views import _auth, _require_reason, _audit
# validate_token is the house Bearer-token -> User resolver (afc_auth.views). The user-facing
# request-claim endpoints below use it directly (a normal logged-in user, NOT the admin gate).
from afc_auth.views import validate_token
from .models import GhostTeam, GhostPlayer
from .serializers import paginate
# the re-attribution service (the core of claim approval) + its conflict exception / pre-checks.
from . import claims


# ───────────────────────── local serializers (manual-dict, like serializers.py) ─────────────────────────
def serialize_ghost_player(p, team_name=...):
    """One ghost roster slot → dict. ``slot`` is 1-based display order.

    ``ghost_team_id`` / ``ghost_team_name`` are NULL for a standalone ("parked") player —
    one created on its own, not attached to any ghost team. They are additive: the existing
    ``id`` / ``ign`` / ``slot`` keys are unchanged, so the nested-roster callers (serialize_ghost)
    keep working as before while the flat ghost-player surface gets the team context it needs.

    ``team_name`` is an optional N+1 guard: serialize_ghost already holds the parent ghost,
    so it passes the name in to avoid dereferencing ``p.ghost_team`` once per roster row (the
    reverse FK is not cached by ``prefetch_related("players")``). The flat surface omits it and
    relies on ``select_related("ghost_team")`` instead. ``...`` (Ellipsis) = "not provided".
    """
    if team_name is ...:
        # no hint from the caller: read the team name off the (select_related) relation,
        # null-safe — None for a standalone player.
        team_name = p.ghost_team.team_name if p.ghost_team_id else None
    return {
        "id": p.id,
        "ign": p.ign,
        "slot": p.slot,
        # null-safe: read the FK id without a DB hit; None means standalone (no team).
        "ghost_team_id": str(p.ghost_team_id) if p.ghost_team_id else None,
        "ghost_team_name": team_name,
        # claim lifecycle (ghost-claim-process-design.md §7) — mirrors serialize_ghost's claim block,
        # but claimed_by / claim_requested_by / claim_approved_by are User ids (a player claims itself).
        # Consumed by the admin pending-claim queue (ghost_players_list?claim_status=pending) and the
        # FE claim dialog (to hide the Claim action on an already-pending/claimed ghost).
        "claim_status": p.claim_status,
        "claimed_by": p.claimed_by_id,                       # afc_auth.User id (or None)
        "claim_requested_by": p.claim_requested_by_id,       # User id (or None)
        "claim_requested_at": p.claim_requested_at.isoformat() if p.claim_requested_at else None,
        "claimed_at": p.claimed_at.isoformat() if p.claimed_at else None,
        "claim_approved_by": p.claim_approved_by_id,         # User id (or None)
        "claim_revoked_at": p.claim_revoked_at.isoformat() if p.claim_revoked_at else None,
        "claim_note": p.claim_note,
    }


def serialize_ghost(g):
    """A ghost team → dict, with its nested roster + the full claim-lifecycle fields.

    ``players`` is ordered by slot (the model's default ``ordering``), so the related
    manager already returns them in roster order.
    """
    return {
        "ghost_team_id": str(g.ghost_team_id),
        "team_name": g.team_name,
        "country": g.country,
        "external_id": g.external_id,
        "is_provisional": g.is_provisional,
        "is_active": g.is_active,
        # claim lifecycle (§19.4)
        "claim_status": g.claim_status,
        "claimed_by": g.claimed_by_id,                       # afc_team.Team id (or None)
        "claim_requested_by": g.claim_requested_by_id,       # User id (or None)
        "claim_requested_at": g.claim_requested_at.isoformat() if g.claim_requested_at else None,
        "claimed_at": g.claimed_at.isoformat() if g.claimed_at else None,
        "claim_approved_by": g.claim_approved_by_id,         # User id (or None)
        "claim_revoked_at": g.claim_revoked_at.isoformat() if g.claim_revoked_at else None,
        "claim_note": g.claim_note,
        # provenance
        "created_by": g.created_by_id,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        # pass g.team_name in as the team-name hint so the roster serializer never has to
        # re-query the reverse FK per player (these are all this ghost's own players).
        "players": [serialize_ghost_player(p, team_name=g.team_name) for p in g.players.all()],
    }


# ───────────────────────── helpers (local to this surface) ─────────────────────────
def _get_ghost_or_404(ghost_team_id):
    """Fetch a ghost team (prefetching its roster) or return ``(None, Response 404)``.

    Returns ``(ghost, None)`` on hit so callers mirror the auth/reason pattern::

        ghost, err = _get_ghost_or_404(ghost_team_id)
        if err: return err
    """
    ghost = GhostTeam.objects.prefetch_related("players").filter(pk=ghost_team_id).first()
    if not ghost:
        return None, Response({"message": "Ghost team not found."}, status=status.HTTP_404_NOT_FOUND)
    return ghost, None


def _clean_players(raw):
    """Validate + normalise the inbound ``players`` list into ``[{"ign": str}, ...]``.

    Body shape is ``[{"ign": "..."}, ...]`` with >=1 non-blank ign. Returns
    ``(cleaned_list, None)`` or ``(None, Response 400)``. Slot numbers are NOT taken from
    the body — they are assigned positionally (index+1) by the caller so the roster is
    always a clean 1..N sequence.
    """
    if not isinstance(raw, list) or len(raw) < 1:
        return None, Response(
            {"message": "At least one player is required (players: [{\"ign\": \"...\"}])."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cleaned = []
    for entry in raw:
        # accept either {"ign": "..."} dicts or bare strings, for caller convenience.
        ign = (entry.get("ign") if isinstance(entry, dict) else entry) or ""
        ign = str(ign).strip()
        if not ign:
            return None, Response(
                {"message": "Every player must have a non-empty ign."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cleaned.append({"ign": ign})
    return cleaned, None


# ───────────────────────── user-facing auth + team-role gate (claim REQUESTS) ─────────────────────────
# The claim-REQUEST endpoints below are USER actions (a real player/team owner initiates a claim), NOT
# admin actions, so they use the normal Bearer-token user gate instead of admin_views._auth. The
# approve/reject endpoints stay on _auth (admin-only).
def _auth_user(request):
    """Resolve the Bearer SessionToken to any logged-in User (no role gate).

    Returns ``(user, None)`` on success or ``(None, Response)`` on a missing/bad header (400) or an
    invalid/expired token (401). Same shape + idiom as afc_leaderboard.views._auth_user, so the
    request endpoints keep the auth/reason short-circuit pattern the rest of this module uses.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."},
                              status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ", 1)[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


# Roster management roles allowed to claim a ghost team ON BEHALF of a real team. Mirrors the house
# team-management gate used elsewhere (afc_player_market.views._is_trial_chat_participant and friends:
# team.team_owner == user OR a TeamMembers row with a management role). "manager" + "team_captain"
# are the design's owner/captain/manager set (the model's MANAGEMENT_ROLE_CHOICES names).
_TEAM_CLAIM_ROLES = ("team_captain", "manager")


def _user_can_act_for_team(user, team):
    """True if ``user`` may act for ``team`` (owner / captain / manager), reusing the house gate.

    The check mirrors afc_player_market.views (e.g. ``_is_trial_chat_participant``): the team owner FK,
    the team captain FK, OR a TeamMembers roster row whose management_role is captain/manager. Read by
    ghost_team_request_claim to 403 a requester who does not run the team they are claiming for.
    """
    from afc_team.models import TeamMembers
    if team.team_owner_id == user.pk:
        return True
    if team.team_captain_id == user.pk:
        return True
    return TeamMembers.objects.filter(
        team=team, member=user, management_role__in=_TEAM_CLAIM_ROLES,
    ).exists()


# ───────────────────────── LIST + DETAIL (read-only) ─────────────────────────
@api_view(["GET"])
def ghost_list(request):
    """GET ghost-teams/ — paginated list with nested rosters.

    Filters:
      ?claim_status=unclaimed|pending|claimed|revoked   exact match on claim_status
      ?q=<text>                                         case-insensitive substring on team_name

    Read-only: skips the reason + audit steps (per the admin_views.py contract).
    """
    user, err = _auth(request)
    if err:
        return err

    qs = GhostTeam.objects.prefetch_related("players").all()  # default ordering: -created_at

    claim_status = request.GET.get("claim_status")
    if claim_status:
        # guard against silently returning everything on a typo'd filter value.
        valid = {c[0] for c in GhostTeam.CLAIM_STATUS}
        if claim_status not in valid:
            return Response(
                {"message": f"Invalid claim_status. Expected one of: {', '.join(sorted(valid))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = qs.filter(claim_status=claim_status)

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(team_name__icontains=q)

    items, meta = paginate(request, qs)
    return Response({"results": [serialize_ghost(g) for g in items], "pagination": meta})


@api_view(["GET"])
def ghost_detail(request, ghost_team_id):
    """GET ghost-teams/<uuid>/ — one ghost team with its roster + claim fields. Read-only."""
    user, err = _auth(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err
    return Response(serialize_ghost(ghost))


# ───────────────────────── CREATE ─────────────────────────
@api_view(["POST"])
def ghost_create(request):
    """POST ghost-teams/ — create a ghost team + its initial roster.

    Body:
      team_name   (required)
      country     (required)
      external_id (optional)
      players     (required, >=1) — [{"ign": "..."}]
      reason      (required, >=10 chars — the audit reason)

    Roster slots are assigned positionally (index+1). ``created_by`` is the acting admin.
    Audit: object_type="ghost_claim", action="create".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    team_name = (request.data.get("team_name") or "").strip()
    country = (request.data.get("country") or "").strip()
    external_id = (request.data.get("external_id") or "").strip() or None
    if not team_name:
        return Response({"message": "team_name is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not country:
        return Response({"message": "country is required."}, status=status.HTTP_400_BAD_REQUEST)

    players, err = _clean_players(request.data.get("players"))
    if err:
        return err

    with transaction.atomic():
        ghost = GhostTeam.objects.create(
            team_name=team_name,
            country=country,
            external_id=external_id,
            created_by=user,
            # is_provisional / is_active / claim_status keep their model defaults
            # (True / True / "unclaimed") — a freshly created ghost is always unclaimed.
        )
        # slot = index + 1 → clean 1..N roster ordering.
        GhostPlayer.objects.bulk_create([
            GhostPlayer(ghost_team=ghost, ign=p["ign"], slot=i + 1)
            for i, p in enumerate(players)
        ])
        # re-fetch with the roster prefetched so the response + audit snapshot include players.
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "create", reason,
            object_ref=ghost.ghost_team_id, before={}, after=after,
        )

    return Response(after, status=status.HTTP_201_CREATED)


# ───────────────────────── ADD SINGLE PLAYER ─────────────────────────
@api_view(["POST"])
def ghost_player_create(request, ghost_team_id):
    """POST ghost-teams/<uuid>/players/ — append ONE ghost player to an existing ghost team.

    This is the surface the admin *Players* page uses to "create a ghost player": a ghost
    player is always a roster slot on a ghost team (``GhostPlayer.ghost_team`` is non-null),
    so the body must say which ghost team it belongs to (the <uuid> in the path).

    Body:
      ign     (required) — the in-game name for the new slot
      reason  (required, >=10 chars — the audit reason)

    Unlike ``ghost_update`` (which REPLACES the whole roster), this only APPENDS one slot, so
    the existing roster is left untouched. The new slot number is ``max(existing slots) + 1``
    so the roster stays a clean 1..N sequence even after repeated single adds.

    Same freeze rule as ``ghost_update``: only allowed while ``claim_status == 'unclaimed'`` —
    a pending/claimed ghost is frozen (adding to a claimed ghost would silently rewrite a real
    team's roster), so we 400 instead. Audit: object_type="ghost_claim", action="add_player".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    # freeze guard — mirror ghost_update: only unclaimed ghosts accept roster edits.
    if ghost.claim_status != "unclaimed":
        return Response(
            {"message": f"Cannot add a player to a ghost team with claim_status '{ghost.claim_status}'. "
                        "Only unclaimed ghost teams are editable."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # one ign, non-blank — same normalisation rule _clean_players applies per entry.
    ign = (request.data.get("ign") or "").strip()
    if not ign:
        return Response({"message": "ign is required."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        before = serialize_ghost(ghost)
        # next slot = highest current slot + 1 (1 when the roster is empty) → clean 1..N order.
        next_slot = (ghost.players.aggregate(m=models.Max("slot"))["m"] or 0) + 1
        GhostPlayer.objects.create(ghost_team=ghost, ign=ign, slot=next_slot)

        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "add_player", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )

    return Response(after, status=status.HTTP_201_CREATED)


# ═════════════════════ FLAT ghost-player surface (team-OPTIONAL) ═════════════════════
# These handlers own the standalone / "parked" ghost player: a GhostPlayer whose ghost_team
# FK is NULL (a provisional in-game name not yet attached to any ghost team). They sit under
# the flat ``ghost-players/`` URL space (urls.py), keyed on the integer GhostPlayer.id, and
# are CONSUMED BY the FE CreateGhostPlayerModal (rankingsAdminApi.createGhostPlayerFlat) so an
# admin can park an IGN without being forced to pick a ghost team. Standalone rows are inert
# for scoring (nothing in the scoring/recalc path reads GhostPlayer), so the freeze guard only
# bites when the player is attached to a non-unclaimed team. Auth/reason/audit reuse the same
# admin_views.py foundation as the rest of this module.
def _get_player_or_404(player_id):
    """Fetch a GhostPlayer (with its ghost team select_related) or return ``(None, Response 404)``.

    Mirrors ``_get_ghost_or_404`` so the flat handlers keep the auth/reason short-circuit shape::

        player, err = _get_player_or_404(player_id)
        if err: return err
    """
    player = GhostPlayer.objects.select_related("ghost_team").filter(pk=player_id).first()
    if not player:
        return None, Response({"message": "Ghost player not found."}, status=status.HTTP_404_NOT_FOUND)
    return player, None


@api_view(["POST"])
def ghost_player_create_flat(request):
    """POST ghost-players/ — create ONE ghost player, with the ghost team OPTIONAL.

    This is the surface the admin *Players* page uses to "create a ghost player" without being
    forced onto a ghost team. CONSUMED BY the FE CreateGhostPlayerModal
    (``rankingsAdminApi.createGhostPlayerFlat``).

    Request body:
      ign           (required, non-blank) — the in-game name for the new player
      ghost_team_id (optional, uuid)      — if given, attach the player to that ghost team;
                                            if absent/blank, create a STANDALONE (team-less) player
      reason        (required, >=10 chars — the audit reason; KEPT for every write)

    Behaviour:
      - attached (ghost_team_id given): re-apply the freeze guard (the team must be
        ``claim_status == 'unclaimed'`` or 400), then slot = max(existing slots) + 1 — mirrors
        ``ghost_player_create`` so the roster stays a clean 1..N sequence.
      - standalone (no ghost_team_id): create GhostPlayer(ghost_team=None, slot=1); slot is not
        meaningful with no roster, so it is a fixed 1. The freeze guard is skipped (a team-less
        player is inert for scoring).

    Response: 201 with serialize_ghost_player(player) (includes ghost_team_id / ghost_team_name,
    both NULL for a standalone player).
    Auth: head_admin | metrics_admin (via _auth). Audit: object_type="ghost_claim",
    action="add_player_flat", object_ref = the player id (standalone) or the team id (attached).
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)   # KEEP the >=10-char reason gate on every write.
    if err:
        return err

    # one ign, non-blank — same normalisation rule the nested route + _clean_players apply.
    ign = (request.data.get("ign") or "").strip()
    if not ign:
        return Response({"message": "ign is required."}, status=status.HTTP_400_BAD_REQUEST)

    # ghost_team_id is OPTIONAL: blank/absent => standalone parked player.
    ghost_team_id = (request.data.get("ghost_team_id") or "").strip() or None

    with transaction.atomic():
        if ghost_team_id:
            # ── attached path: mirror ghost_player_create (freeze guard + append slot) ──
            ghost, err = _get_ghost_or_404(ghost_team_id)
            if err:
                return err
            if ghost.claim_status != "unclaimed":
                return Response(
                    {"message": f"Cannot add a player to a ghost team with claim_status "
                                f"'{ghost.claim_status}'. Only unclaimed ghost teams are editable."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # next slot = highest current slot + 1 (1 when empty) → clean 1..N roster order.
            next_slot = (ghost.players.aggregate(m=models.Max("slot"))["m"] or 0) + 1
            player = GhostPlayer.objects.create(ghost_team=ghost, ign=ign, slot=next_slot)
            object_ref = str(ghost.ghost_team_id)
        else:
            # ── standalone path: team-less, inert, freeze skipped, fixed slot 1 ──
            player = GhostPlayer.objects.create(ghost_team=None, ign=ign, slot=1)
            object_ref = str(player.id)

        # re-fetch with the team select_related so the response is null-safe + query-free.
        player = GhostPlayer.objects.select_related("ghost_team").get(pk=player.pk)
        after = serialize_ghost_player(player)
        _audit(
            user, "ghost_claim", "add_player_flat", reason,
            object_ref=object_ref, before={}, after=after,
        )

    return Response(after, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def ghost_players_list(request):
    """GET ghost-players/ — paginated list of ALL ghost players (attached + standalone).

    Read-only (skips the reason + audit steps, per the admin_views.py contract).

    Query filters:
      ?unattached=true                                  only standalone players (ghost_team IS NULL)
      ?ghost_team_id=<uuid>                             only players on that one ghost team
      ?claim_status=unclaimed|pending|claimed|revoked   exact match on claim_status

    The ``claim_status`` filter drives the admin PENDING-CLAIM queue: the FE fetches
    ``ghost-players/?claim_status=pending`` (and ``ghost-teams/?claim_status=pending``) to build one
    combined queue of ghost claims awaiting review.

    Sort: ``-id`` (creation order) — for standalone rows ``slot`` is always 1, so it is not a
    meaningful sort key; newest-first by id is the predictable order across both kinds.

    Response: ``{results: [serialize_ghost_player...], pagination}`` (the canonical envelope).
    Auth: head_admin | metrics_admin (via _auth).
    """
    user, err = _auth(request)
    if err:
        return err

    # select_related so serialize_ghost_player can read ghost_team_name without an N+1.
    qs = GhostPlayer.objects.select_related("ghost_team").all()

    # ?unattached=true → standalone players only (team-less parked IGNs).
    if (request.GET.get("unattached") or "").strip().lower() == "true":
        qs = qs.filter(ghost_team__isnull=True)

    # ?ghost_team_id=<uuid> → narrow to one ghost team's roster.
    ghost_team_id = (request.GET.get("ghost_team_id") or "").strip()
    if ghost_team_id:
        qs = qs.filter(ghost_team_id=ghost_team_id)

    # ?claim_status=<value> → the admin pending-claim queue (mirrors ghost_list's claim_status filter,
    # same valid-value guard so a typo'd filter 400s instead of silently returning everything).
    claim_status = (request.GET.get("claim_status") or "").strip()
    if claim_status:
        valid = {c[0] for c in GhostPlayer.CLAIM_STATUS}
        if claim_status not in valid:
            return Response(
                {"message": f"Invalid claim_status. Expected one of: {', '.join(sorted(valid))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = qs.filter(claim_status=claim_status)

    # -id = creation order; slot isn't meaningful for standalone rows (always 1).
    qs = qs.order_by("-id")

    items, meta = paginate(request, qs)
    return Response({"results": [serialize_ghost_player(p) for p in items], "pagination": meta})


@api_view(["GET"])
def ghost_player_detail(request, player_id):
    """GET ghost-players/<int:player_id>/ — one ghost player (attached or standalone). Read-only.

    Response: serialize_ghost_player(player). Auth: head_admin | metrics_admin (via _auth).
    """
    user, err = _auth(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err
    return Response(serialize_ghost_player(player))


@api_view(["PATCH"])
def ghost_player_update(request, player_id):
    """PATCH ghost-players/<int:player_id>/ — edit ign and/or slot of one ghost player.

    Request body (all optional except reason; only present fields are patched):
      ign     (optional, non-blank if present) — new in-game name
      slot    (optional, positive int)         — new display order
      reason  (required, >=10 chars — the audit reason)

    Freeze: applied ONLY when the player is attached to a team whose ``claim_status`` is not
    "unclaimed" (editing a claimed team's roster would rewrite a real team's history → 400).
    A standalone player (ghost_team NULL) is ALWAYS editable.

    First cut: this does NOT move a standalone player onto a team (no ghost_team_id field is
    accepted here) — attaching/claiming is a separate, later step.

    Response: serialize_ghost_player(player). Auth: head_admin | metrics_admin (via _auth).
    Audit: object_type="ghost_claim", action="update_player".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err

    # freeze guard — only bites when attached to a non-unclaimed team; standalone is free.
    if player.ghost_team_id and player.ghost_team.claim_status != "unclaimed":
        return Response(
            {"message": f"Cannot edit a player on a ghost team with claim_status "
                        f"'{player.ghost_team.claim_status}'. Only unclaimed ghost teams are editable."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost_player(player)

        # patch only the fields present in the body.
        if "ign" in request.data:
            new_ign = (request.data.get("ign") or "").strip()
            if not new_ign:
                return Response({"message": "ign cannot be blank."}, status=status.HTTP_400_BAD_REQUEST)
            player.ign = new_ign
        if "slot" in request.data:
            try:
                new_slot = int(request.data.get("slot"))
            except (TypeError, ValueError):
                return Response({"message": "slot must be a positive integer."},
                                status=status.HTTP_400_BAD_REQUEST)
            if new_slot < 1:
                return Response({"message": "slot must be a positive integer."},
                                status=status.HTTP_400_BAD_REQUEST)
            player.slot = new_slot
        player.save()

        player = GhostPlayer.objects.select_related("ghost_team").get(pk=player.pk)
        after = serialize_ghost_player(player)
        _audit(
            user, "ghost_claim", "update_player", reason,
            object_ref=str(player.id), before=before, after=after,
        )

    return Response(after)


@api_view(["DELETE"])
def ghost_player_delete(request, player_id):
    """DELETE ghost-players/<int:player_id>/ — remove one ghost player.

    Request body:
      reason  (required, >=10 chars — the audit reason)

    Freeze: applied ONLY when the player is attached to a team whose ``claim_status`` is not
    "unclaimed" (deleting a claimed team's slot would rewrite a real team's roster → 400). A
    standalone player (ghost_team NULL) is ALWAYS deletable.

    Response: ``{message}``. Auth: head_admin | metrics_admin (via _auth).
    Audit: object_type="ghost_claim", action="delete_player".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err

    # freeze guard — only bites when attached to a non-unclaimed team; standalone is free.
    if player.ghost_team_id and player.ghost_team.claim_status != "unclaimed":
        return Response(
            {"message": f"Cannot delete a player on a ghost team with claim_status "
                        f"'{player.ghost_team.claim_status}'. Only unclaimed ghost teams are editable."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost_player(player)
        ref = str(player.id)
        player.delete()
        _audit(
            user, "ghost_claim", "delete_player", reason,
            object_ref=ref, before=before, after={},
        )

    return Response({"message": "Ghost player deleted."}, status=status.HTTP_200_OK)


# ───────────────────────── UPDATE ─────────────────────────
@api_view(["PATCH"])
def ghost_update(request, ghost_team_id):
    """PATCH ghost-teams/<uuid>/ — edit name/country/external_id AND replace the roster.

    Only allowed while ``claim_status == 'unclaimed'`` — once a claim is pending/claimed the
    ghost is frozen (editing a claimed ghost would silently rewrite a real team's history),
    so we 400 instead. The roster is fully REPLACED from body ``players[]`` (>=1), re-slotted
    1..N. Audit: object_type="ghost_claim", action="update".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    if ghost.claim_status != "unclaimed":
        return Response(
            {"message": f"Cannot edit a ghost team with claim_status '{ghost.claim_status}'. "
                        "Only unclaimed ghost teams are editable."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    players, err = _clean_players(request.data.get("players"))
    if err:
        return err

    with transaction.atomic():
        before = serialize_ghost(ghost)

        # patch the editable scalar fields (only those present in the body).
        if "team_name" in request.data:
            team_name = (request.data.get("team_name") or "").strip()
            if not team_name:
                return Response({"message": "team_name cannot be blank."}, status=status.HTTP_400_BAD_REQUEST)
            ghost.team_name = team_name
        if "country" in request.data:
            country = (request.data.get("country") or "").strip()
            if not country:
                return Response({"message": "country cannot be blank."}, status=status.HTTP_400_BAD_REQUEST)
            ghost.country = country
        if "external_id" in request.data:
            ghost.external_id = (request.data.get("external_id") or "").strip() or None
        ghost.save()

        # full roster replacement — delete the old slots, recreate 1..N from the body.
        ghost.players.all().delete()
        GhostPlayer.objects.bulk_create([
            GhostPlayer(ghost_team=ghost, ign=p["ign"], slot=i + 1)
            for i, p in enumerate(players)
        ])

        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "update", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )

    return Response(after)


# ───────────────────────── DELETE ─────────────────────────
@api_view(["DELETE"])
def ghost_delete(request, ghost_team_id):
    """DELETE ghost-teams/<uuid>/ — remove a ghost team (and its roster, via FK cascade).

    Blocked if ``claim_status == 'claimed'`` (deleting a claimed ghost would orphan a real
    team's attributed history) → 400. Audit: object_type="ghost_claim", action="delete".
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    if ghost.claim_status == "claimed":
        return Response(
            {"message": "Cannot delete a claimed ghost team. Revoke the claim first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost(ghost)
        ref = ghost.ghost_team_id
        ghost.delete()  # GhostPlayer rows cascade (FK on_delete=CASCADE)
        _audit(
            user, "ghost_claim", "delete", reason,
            object_ref=ref, before=before, after={},
        )

    return Response({"message": "Ghost team deleted."}, status=status.HTTP_200_OK)


# ───────────────────────── CLAIM LIFECYCLE ─────────────────────────
@api_view(["POST"])
def ghost_approve_claim(request, ghost_team_id):
    """POST ghost-teams/<uuid>/approve-claim/ — approve a PENDING team claim + re-attribute history.

    Request: body { reason (>=10 chars) }. Auth: head_admin | metrics_admin (_auth).
    FE consumer: the admin claim queue's Approve button (a section under admin rankings listing
    ghost-teams/?claim_status=pending).

    Precondition: the ghost must be ``pending`` with a ``claimed_by`` (the requested team, set by
    ghost_team_request_claim). Otherwise there is nothing to approve -> 400.

    The re-attribution (the core): claims.reattribute_ghost_team re-points every standalone
    LeaderboardParticipant from this ghost onto the requested team, deletes the ghost's score rows,
    and recomputes the real team for every affected month + season so it inherits the ghost's points,
    rank, and tier. If the real team is already a participant alongside the ghost in some leaderboard,
    the service raises claims.ClaimConflict and we 400 with its message (nothing is committed — the
    raise rolls back the whole transaction). On success we flip claim_status='claimed', stamp
    claimed_at + claim_approved_by, and audit ghost_claim/approve with the service summary in `after`.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    # there must be a PENDING claim with a target team to approve (set by the request endpoint).
    if ghost.claim_status != "pending" or not ghost.claimed_by_id:
        return Response(
            {"message": "No pending claim to approve."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost(ghost)
        # RE-ATTRIBUTE first (it has its own conflict guard). A ClaimConflict aborts the whole
        # transaction (the atomic block rolls back), so nothing is half-applied.
        try:
            summary = claims.reattribute_ghost_team(ghost, ghost.claimed_by, user)
        except claims.ClaimConflict as conflict:
            return Response({"message": str(conflict)}, status=status.HTTP_400_BAD_REQUEST)

        # only after a clean re-attribution: mark the ghost claimed.
        ghost.claim_status = "claimed"
        ghost.claimed_at = timezone.now()
        ghost.claim_approved_by = user
        ghost.save(update_fields=["claim_status", "claimed_at", "claim_approved_by"])
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        # put the re-attribution summary (counts of moved participants + affected periods) in the
        # audit `after` so the log explains what the approval actually moved.
        after["reattribution"] = summary
        _audit(
            user, "ghost_claim", "approve", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )

    return Response(after)


# ───────────────────────── CLAIM REQUEST (user-facing — the initiate step) ─────────────────────────
@api_view(["POST"])
def ghost_team_request_claim(request, ghost_team_id):
    """POST ghost-teams/<uuid>/request-claim/ — a real team OWNER/CAPTAIN/MANAGER requests a claim.

    Request body:
      team_id   (required) — the real afc_team.Team the requester wants to map this ghost onto
      evidence  (optional) — free-text the admin reads when reviewing (stored in claim_note)

    Auth: a normal logged-in user (Bearer SessionToken via _auth_user) — this is a USER action, NOT
    an admin action, so it is NOT gated by _auth. The requester MUST run team_id (owner/captain/manager
    via _user_can_act_for_team) or 403.

    Guards (the request can only enter review if it could plausibly be approved):
      - the ghost must be ``unclaimed`` (one pending claim per ghost) else 400,
      - the conflict pre-check (claims.conflict_for_team_claim): if team_id already shares a
        leaderboard with the ghost, the claim could never be approved -> 400 up front.

    On success: claim_status='pending', claim_requested_by=user, claim_requested_at=now,
    claimed_by=team (the target, confirmed on approve), claim_note=evidence. Returns the serialized
    ghost. FE consumer: the "Claim" action on a ghost row in the public rankings ladders.
    No admin audit row (this is a user request, not an admin write); the admin approve/reject is audited.
    """
    user, err = _auth_user(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    # resolve + authorize the target team.
    from afc_team.models import Team
    team_id = request.data.get("team_id")
    if not team_id:
        return Response({"message": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    team = Team.objects.filter(pk=team_id).first()
    if not team:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _user_can_act_for_team(user, team):
        return Response(
            {"message": "You must be the owner, captain, or a manager of this team to claim for it."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # one pending claim per ghost — only an unclaimed ghost can be requested.
    if ghost.claim_status != "unclaimed":
        return Response(
            {"message": f"This ghost team already has a claim ({ghost.claim_status}); "
                        "it cannot be requested again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # conflict pre-check (no mutation): a request that could never be approved is rejected now.
    conflict_lb = claims.conflict_for_team_claim(ghost, team)
    if conflict_lb:
        return Response(
            {"message": f"Cannot claim: your team is already a participant alongside this ghost in "
                        f"leaderboard '{conflict_lb}'. An admin must resolve the duplicate first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    evidence = (request.data.get("evidence") or "").strip()
    with transaction.atomic():
        ghost.claim_status = "pending"
        ghost.claim_requested_by = user
        ghost.claim_requested_at = timezone.now()
        ghost.claimed_by = team            # the target, confirmed (or cleared) on approve/reject
        ghost.claim_note = evidence
        ghost.save(update_fields=[
            "claim_status", "claim_requested_by", "claim_requested_at", "claimed_by", "claim_note",
        ])
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)

    return Response(serialize_ghost(ghost), status=status.HTTP_200_OK)


# ───────────────────────── REJECT a pending TEAM claim (admin) ─────────────────────────
@api_view(["POST"])
def ghost_reject_claim(request, ghost_team_id):
    """POST ghost-teams/<uuid>/reject-claim/ — reject a PENDING team claim (no re-attribution).

    Request: body { reason (>=10 chars) }. Auth: head_admin | metrics_admin (_auth). FE consumer: the
    admin claim queue's Reject button.

    Distinct from revoke-claim: REJECT is for a request that has not yet been approved (pending ->
    back to unclaimed); REVOKE undoes an already-APPROVED claim. Requires claim_status=='pending' else
    400. Resets to 'unclaimed', clears the request fields (claim_requested_by / claimed_by /
    claim_requested_at), stamps claim_revoked_at=now, and audits ghost_claim/reject. The ghost is
    immediately re-claimable.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    if ghost.claim_status != "pending":
        return Response(
            {"message": "No pending claim to reject."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost(ghost)
        ghost.claim_status = "unclaimed"
        ghost.claim_requested_by = None
        ghost.claimed_by = None
        ghost.claim_requested_at = None
        ghost.claim_revoked_at = timezone.now()
        ghost.save(update_fields=[
            "claim_status", "claim_requested_by", "claimed_by", "claim_requested_at", "claim_revoked_at",
        ])
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "reject", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )

    return Response(after)


# ───────────────────────── PLAYER claim lifecycle (request / approve / reject) ─────────────────────────
@api_view(["POST"])
def ghost_player_request_claim(request, player_id):
    """POST ghost-players/<int:player_id>/request-claim/ — a real user claims this IGN as THEMSELVES.

    Request body:
      evidence  (optional) — free-text the admin reads when reviewing (stored in claim_note)

    Auth: a normal logged-in user (Bearer SessionToken via _auth_user). Unlike the team request,
    there is no role gate: a player claims their OWN account (claimed_by = the requester).

    Guards: the ghost must be ``unclaimed`` (one pending claim per ghost) else 400; the conflict
    pre-check (claims.conflict_for_player_claim) 400s if the requester already shares a solo
    leaderboard with the ghost. On success: claim_status='pending', claim_requested_by=user,
    claimed_by=user, claim_requested_at=now, claim_note=evidence. Returns the serialized ghost player.
    FE consumer: the "This is me" claim action on a ghost player row in the public player ladder.
    """
    user, err = _auth_user(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err

    if player.claim_status != "unclaimed":
        return Response(
            {"message": f"This ghost player already has a claim ({player.claim_status}); "
                        "it cannot be requested again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # conflict pre-check (no mutation): the requester already shares a solo LB with the ghost.
    conflict_lb = claims.conflict_for_player_claim(player, user)
    if conflict_lb:
        return Response(
            {"message": f"Cannot claim: you are already a participant alongside this ghost in "
                        f"leaderboard '{conflict_lb}'. An admin must resolve the duplicate first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    evidence = (request.data.get("evidence") or "").strip()
    with transaction.atomic():
        player.claim_status = "pending"
        player.claim_requested_by = user
        player.claimed_by = user           # a self-claim: requester == target
        player.claim_requested_at = timezone.now()
        player.claim_note = evidence
        player.save(update_fields=[
            "claim_status", "claim_requested_by", "claimed_by", "claim_requested_at", "claim_note",
        ])
        player = GhostPlayer.objects.select_related("ghost_team").get(pk=player.pk)

    return Response(serialize_ghost_player(player), status=status.HTTP_200_OK)


@api_view(["POST"])
def ghost_player_approve_claim(request, player_id):
    """POST ghost-players/<int:player_id>/approve-claim/ — approve a PENDING player claim + re-attribute.

    Request: body { reason (>=10 chars) }. Auth: head_admin | metrics_admin (_auth). FE consumer: the
    admin claim queue's Approve button (combined teams + players queue).

    Mirror of ghost_approve_claim for the solo side. Precondition: claim_status=='pending' with a
    ``claimed_by`` (the requesting user) else 400. claims.reattribute_ghost_player re-points the ghost's
    standalone solo LeaderboardParticipant rows onto the user, deletes the ghost's player score rows,
    and recomputes the user for every affected month + season. ClaimConflict -> 400 (nothing committed).
    On success: claim_status='claimed', claimed_at + claim_approved_by stamped, audit ghost_claim/approve
    with the service summary in `after`.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err

    if player.claim_status != "pending" or not player.claimed_by_id:
        return Response(
            {"message": "No pending claim to approve."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost_player(player)
        try:
            summary = claims.reattribute_ghost_player(player, player.claimed_by, user)
        except claims.ClaimConflict as conflict:
            return Response({"message": str(conflict)}, status=status.HTTP_400_BAD_REQUEST)

        player.claim_status = "claimed"
        player.claimed_at = timezone.now()
        player.claim_approved_by = user
        player.save(update_fields=["claim_status", "claimed_at", "claim_approved_by"])
        player = GhostPlayer.objects.select_related("ghost_team").get(pk=player.pk)
        after = serialize_ghost_player(player)
        after["reattribution"] = summary
        _audit(
            user, "ghost_claim", "approve", reason,
            object_ref=str(player.id), before=before, after=after,
        )

    return Response(after)


@api_view(["POST"])
def ghost_player_reject_claim(request, player_id):
    """POST ghost-players/<int:player_id>/reject-claim/ — reject a PENDING player claim.

    Request: body { reason (>=10 chars) }. Auth: head_admin | metrics_admin (_auth). FE consumer: the
    admin claim queue's Reject button. Mirror of ghost_reject_claim for the solo side: requires
    claim_status=='pending' else 400, resets to 'unclaimed', clears the request fields, stamps
    claim_revoked_at=now, audits ghost_claim/reject.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    player, err = _get_player_or_404(player_id)
    if err:
        return err

    if player.claim_status != "pending":
        return Response(
            {"message": "No pending claim to reject."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost_player(player)
        player.claim_status = "unclaimed"
        player.claim_requested_by = None
        player.claimed_by = None
        player.claim_requested_at = None
        player.claim_revoked_at = timezone.now()
        player.save(update_fields=[
            "claim_status", "claim_requested_by", "claimed_by", "claim_requested_at", "claim_revoked_at",
        ])
        player = GhostPlayer.objects.select_related("ghost_team").get(pk=player.pk)
        after = serialize_ghost_player(player)
        _audit(
            user, "ghost_claim", "reject", reason,
            object_ref=str(player.id), before=before, after=after,
        )

    return Response(after)


@api_view(["POST"])
def ghost_revoke_claim(request, ghost_team_id):
    """POST ghost-teams/<uuid>/revoke-claim/ — undo a claim.

    Sets claim_status back to 'unclaimed' (the editable state), clears claimed_by / claimed_at /
    claim_approved_by, and stamps claim_revoked_at=now. Audit: object_type="ghost_claim",
    action="revoke".

    (The model also offers a 'revoked' status; we reset to 'unclaimed' so the ghost is
    immediately re-editable/re-claimable, and record the revocation via claim_revoked_at.)
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    ghost, err = _get_ghost_or_404(ghost_team_id)
    if err:
        return err

    if ghost.claim_status == "unclaimed":
        return Response(
            {"message": "Ghost team is not claimed; nothing to revoke."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost(ghost)
        ghost.claim_status = "unclaimed"
        ghost.claimed_by = None
        ghost.claimed_at = None
        ghost.claim_approved_by = None
        ghost.claim_revoked_at = timezone.now()
        ghost.save(update_fields=[
            "claim_status", "claimed_by", "claimed_at", "claim_approved_by", "claim_revoked_at",
        ])
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "revoke", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )

    return Response(after)
