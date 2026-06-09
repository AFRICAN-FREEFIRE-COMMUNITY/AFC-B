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
from .models import GhostTeam, GhostPlayer
from .serializers import paginate


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
      ?unattached=true        only standalone players (ghost_team IS NULL)
      ?ghost_team_id=<uuid>   only players on that one ghost team

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
    """POST ghost-teams/<uuid>/approve-claim/ — mark the ghost as claimed.

    Sets claim_status='claimed', claimed_at=now, claim_approved_by=acting admin.
    Audit: object_type="ghost_claim", action="approve".

    NOTE: approving a claim should retroactively re-attribute the ghost's historical results
    to the claiming team and recalc every affected month/season. That cross-period recalc is
    deliberately NOT done here (see module docstring) — the coordinator owns it.
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
            {"message": "Ghost team is already claimed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = serialize_ghost(ghost)
        ghost.claim_status = "claimed"
        ghost.claimed_at = timezone.now()
        ghost.claim_approved_by = user
        ghost.save(update_fields=["claim_status", "claimed_at", "claim_approved_by"])
        ghost = GhostTeam.objects.prefetch_related("players").get(pk=ghost.pk)
        after = serialize_ghost(ghost)
        _audit(
            user, "ghost_claim", "approve", reason,
            object_ref=ghost.ghost_team_id, before=before, after=after,
        )
        # TODO(recalc): retroactive recalc on claim handled by coordinator
        # (re-attribute the ghost's historical results to ghost.claimed_by and recalc every
        #  affected month + season). NOT a single enqueue_team() call — do not implement here.

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
