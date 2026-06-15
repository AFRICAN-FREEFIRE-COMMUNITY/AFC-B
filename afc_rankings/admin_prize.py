"""
Admin write API for tournament prize entry (Phase 2) — feeds quarterly prize_money_pts.

WHAT THIS COVERS
----------------
Admins record the prize money a tournament team was awarded. Those payouts roll up
into a team's quarterly score as ``prize_money_pts`` (spec §6.2 / §7.2) — so every
create / edit / delete here must trigger a quarterly recalc for the affected team.

There is NO inventory or exchange-rate model: prize is entered already-converted to
NGN (Naira) as a plain decimal ``amount``. We do not convert anything.

MODEL MAPPING (IMPORTANT — read before changing field names)
------------------------------------------------------------
The persisted model is ``afc_tournament_and_scrims.models.EventPrizePayout``, NOT a
rankings-local model. Its real fields are::

    event            FK -> Event            (the tournament; related_name="payouts")
    user             FK -> User             (nullable; for solo payouts — unused here)
    tournament_team  FK -> TournamentTeam   (nullable; the team that was paid)
    amount           DecimalField(12,2)     (NGN, default 0)
    created_at       DateTimeField(auto_now_add)  (the awarded date — read-only)

Because the FK is ``event`` (a tournament Event) and the team FK is ``tournament_team``
(a TournamentTeam row, NOT a bare Team), the create body uses ``event_id`` +
``tournament_team_id``. The brief's ``team_id`` is interpreted as the TournamentTeam id
(``tournament_team_id``) — that is what the model + the §18 signal in ``signals.py``
key off (``instance.tournament_team.team_id``). See the create view for the note.

WHY THE RECALC IS QUARTERLY-ONLY
--------------------------------
``prize_money_pts`` only exists on the quarterly score (§6.2), so we enqueue the team's
quarterly recalc on commit. ``tasks.enqueue_team`` fires both monthly + quarterly, which
is harmless — the monthly recalc just re-derives the same monthly figures — and it keeps
us consistent with the existing ``on_prize_payout`` signal. We pass the payout's month
(``recalc.current_month()`` for new rows, the row's ``created_at`` month for edits) and
the active season's id.

This module is built entirely on the shared foundation in ``admin_views.py``
(``_auth`` / ``_require_reason`` / ``_audit`` / ``RANKING_ADMIN_ROLES``) — it does not
reimplement auth or the §16 audit trail. The public *read* rankings API lives in
``views.py``; this is a *write* surface and so every mutation is reason-gated + audited.

Standard mutating shape (see admin_views.py header for the canonical template):
    1. user, err = _auth(request, roles=...)   -> 401/403 short-circuit
    2. reason, err = _require_reason(request)   -> mandatory audit reason
    3. with transaction.atomic(): apply write, snapshot before/after
    4. _audit(...)                              -> one RankingAuditLog row
    5. transaction.on_commit(enqueue recalc)    -> recalc AFTER commit, never inline
"""
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

# Shared auth/audit foundation — do NOT reimplement these here (project rule).
from .admin_views import _auth, _require_reason, _audit, RANKING_ADMIN_ROLES
from . import recalc
from . import tasks
from .serializers import paginate

# The payout model lives in the tournaments app (see header MODEL MAPPING).
# PlayerWinning + TournamentTeamMember are also imported here so prize_create can split a
# team payout into per-player history rows (feature "Prizepool auto-links to event winners'
# history/stats", owner 2026-06-15). See _distribute_payout below and the PlayerWinning model
# docstring; those rows are read back on the player profile via afc_player.views.get_public_player_stats.
from afc_tournament_and_scrims.models import (
    EventPrizePayout, Event, TournamentTeam, PlayerWinning, TournamentTeamMember,
)
from decimal import Decimal


# Data-entry endpoints widen the default ranking-admin set to include event_admin,
# since prize entry is a tournament-operations task (per the brief's auth rule).
PRIZE_ADMIN_ROLES = RANKING_ADMIN_ROLES + ("event_admin",)


# ───────────────────────── season helper ─────────────────────────
def _active_season():
    """The currently-active Season (or None), mirroring recalc.current_season().

    Prize recalcs are quarterly, so we always recalc against the active season. We
    resolve it here rather than importing signals._season_for (a private helper) so
    this module stays self-contained.
    """
    return recalc.current_season()


def _payout_month(payout):
    """The month (day=1) a payout belongs to — its awarded date, else the current month.

    ``created_at`` is auto-set on insert, so for a brand-new row it is None until the
    object is saved; callers pass the saved instance, so this is populated by then.
    """
    if payout.created_at:
        return payout.created_at.date().replace(day=1)
    return recalc.current_month()


# ── mirrors the prize-payout signal in signals.py ──
# Both this helper and the §18 prize-payout signal key recalc off tournament_team.team_id and
# dispatch via tasks.enqueue_team. If one changes how prize feeds the quarterly score, change both.
def _enqueue_prize_recalc(tournament_team):
    """Enqueue the affected team's recalc AFTER commit (never inline).

    ``EventPrizePayout.tournament_team`` is a TournamentTeam; the ranking score keys off
    the underlying ``team_id`` (same as the §18 ``on_prize_payout`` signal). We resolve
    the active season + current month and hand both to ``tasks.enqueue_team`` so the
    quarterly ``prize_money_pts`` is recomputed.
    """
    if not tournament_team:
        return
    team_id = tournament_team.team_id
    if not team_id:
        return
    season = _active_season()
    month = recalc.current_month()
    season_id = season.season_id if season else None
    # on_commit so the recalc reads the just-committed payout state (§18 pattern).
    transaction.on_commit(lambda: tasks.enqueue_team(team_id, month, season_id))


# ───────────────────────── serializer (local, manual-dict style) ─────────────────────────
def serialize_prize(p):
    """One payout as a flat dict (matches serializers.py manual-dict idiom).

    Surfaces the tournament (event) name, the team name, the NGN amount as a string
    (Decimal -> str keeps full precision and avoids float rounding in JSON), the awarded
    date, and the payout id. ``select_related`` in the queryset keeps this query-free.
    """
    event = p.event
    tt = p.tournament_team
    return {
        "payout_id": p.id,
        "event_id": p.event_id,
        "event_name": event.event_name if event else None,        # the tournament name
        "tournament_team_id": p.tournament_team_id,
        "team_id": tt.team_id if tt else None,                    # underlying Team id
        "team_name": (tt.team.team_name if tt and tt.team_id else None),
        "amount": str(p.amount),                                   # NGN, already converted
        "awarded_at": p.created_at.isoformat() if p.created_at else None,
    }


# ───────────────────────── per-player prize distribution ─────────────────────────
# Feature "Prizepool auto-links to event winners' history/stats" (owner 2026-06-15).
#
# WHAT THIS DOES
#   A payout (EventPrizePayout) records what a WHOLE TEAM (or a solo player) was awarded for an
#   event. On its own that only feeds the team's quarterly score + Team.total_earnings — it never
#   lands on the individual players' profiles. This helper writes one PlayerWinning row PER winning
#   player so the prize ALSO shows up in each player's OWN tournament-winnings history, which the
#   public player profile reads back (afc_player.views.get_public_player_stats -> tournament_winnings).
#
# WHEN IT RUNS (baked-in decision)
#   On payout CREATE — i.e. the moment an admin/organizer records the prize, NOT on event completion.
#   It is called from prize_create below, inside that view's existing transaction.atomic() block, so
#   the PlayerWinning rows commit together with the payout (and roll back together on any error).
#
# SPLIT RULE (baked-in decision)
#   TEAM payout: split EQUALLY among the team's ACTIVE roster members
#       (TournamentTeamMember.status == "active"); if the team has no active members we fall back to
#       ALL of its members so the prize is never silently dropped. Each member gets
#       amount / count (NGN, same currency as EventPrizePayout.amount + Team.total_earnings) and a
#       share_percentage of round(100 / count, 2).
#   SOLO payout (payout.user set, no team): the single player gets the FULL amount at 100%.
#
# IDEMPOTENCY
#   Keyed on the source payout: we first DELETE every PlayerWinning whose payout is this one, then
#   recreate. So re-running distribution for the same payout (e.g. an amount edit re-deriving shares)
#   never double-counts a player. (prize_update currently edits amount only and does not re-split; the
#   delete-then-recreate contract here means a future re-split call stays safe.)
#
# SAFETY
#   Wrapped in try/except: a distribution hiccup (bad roster row, etc.) is swallowed so it can NEVER
#   break the core payout save the admin asked for. The payout is the source of truth; PlayerWinning
#   is a derived convenience surface and can be re-derived.
def _distribute_payout(payout):
    """Write per-player PlayerWinning rows from a saved EventPrizePayout (idempotent by payout).

    Called by prize_create after the EventPrizePayout row is saved, inside the same transaction.
    Team payout -> equal split across active roster; solo payout -> full amount to payout.user.
    Connects: reads EventPrizePayout (source) + TournamentTeamMember (roster); writes PlayerWinning
    (consumed by afc_player.views.get_public_player_stats -> player profile tournament_winnings).
    """
    try:
        # Idempotent: clear any prior shares for THIS payout before recreating (no double-count).
        PlayerWinning.objects.filter(payout=payout).delete()

        amount = payout.amount or Decimal("0")

        # ── TEAM payout: split equally among the winning team's roster ──
        if payout.tournament_team_id:
            # Prefer ACTIVE members; fall back to ALL members so the prize is never dropped if a
            # team's statuses were never set to "active".
            members = list(
                TournamentTeamMember.objects.filter(
                    tournament_team=payout.tournament_team, status="active"
                ).select_related("user")
            )
            if not members:
                members = list(
                    TournamentTeamMember.objects.filter(
                        tournament_team=payout.tournament_team
                    ).select_related("user")
                )
            count = len(members)
            if count == 0:
                return  # nothing to split among — leave it to the team total only

            # Equal split. Decimal keeps NGN precision; share_percentage is informational.
            share = (amount / Decimal(count)).quantize(Decimal("0.01"))
            share_pct = round(Decimal(100) / Decimal(count), 2)

            PlayerWinning.objects.bulk_create([
                PlayerWinning(
                    event=payout.event,
                    tournament_team=payout.tournament_team,
                    player=m.user,
                    payout=payout,
                    amount=share,
                    share_percentage=share_pct,
                )
                for m in members
            ])
            return

        # ── SOLO payout: the single player gets the full amount (100%) ──
        if payout.user_id:
            PlayerWinning.objects.create(
                event=payout.event,
                tournament_team=None,
                player=payout.user,
                payout=payout,
                amount=amount,
                share_percentage=Decimal("100"),
            )
    except Exception:
        # A distribution hiccup must NEVER break the payout save (the payout is the source of
        # truth; PlayerWinning is a re-derivable convenience surface). Swallow + move on.
        pass


# ───────────────────────── LIST (read-only) ─────────────────────────
@api_view(["GET"])
def tournament_prizes_list(request):
    """List prize payouts, newest first. Optional ?season_id filter.

    Read-only: no reason + no audit (per the foundation's mutating-vs-read rule).

    EventPrizePayout has no direct Season FK, so we filter by season *date window*:
    a payout falls in a season when its ``created_at`` date is within the season's
    [start_date, end_date]. If ?season_id names an unknown season we return an empty
    page (rather than 404) so the admin UI can show "no payouts this season" cleanly.
    """
    user, err = _auth(request, roles=PRIZE_ADMIN_ROLES)
    if err:
        return err

    qs = (EventPrizePayout.objects
          .select_related("event", "tournament_team", "tournament_team__team")
          .order_by("-created_at", "-id"))

    season_id = request.GET.get("season_id")
    if season_id:
        # Resolve the season window, then filter by created_at date range.
        from .models import Season
        season = Season.objects.filter(pk=season_id).first()
        if not season:
            # Unknown season -> empty page (consistent envelope, no error).
            return Response({"results": [], "pagination": {
                "limit": 25, "offset": 0, "total_count": 0, "has_more": False, "next_offset": None,
            }, "season_id": season_id})
        qs = qs.filter(
            created_at__date__gte=season.start_date,
            created_at__date__lte=season.end_date,
        )

    items, meta = paginate(request, qs)
    return Response({
        "results": [serialize_prize(p) for p in items],
        "pagination": meta,
        "season_id": season_id,
    })


# ───────────────────────── CREATE ─────────────────────────
@api_view(["POST"])
def prize_create(request):
    """Record a new prize payout for a tournament team.

    Body: { event_id, team_id, amount, reason }.
      * ``event_id``  -> the tournament Event the prize is for (FK ``event``).
      * ``team_id``   -> the TournamentTeam id (FK ``tournament_team``). NOTE: the brief
                         calls this ``team_id``; the model's team FK is a TournamentTeam,
                         so we map ``team_id`` -> ``tournament_team_id``. This matches the
                         §18 ``on_prize_payout`` signal, which reads
                         ``instance.tournament_team.team_id``.
      * ``amount``    -> NGN, already converted (no exchange-rate model).

    Audits ``prize_money`` / ``create`` and enqueues the team's quarterly recalc on commit.
    """
    user, err = _auth(request, roles=PRIZE_ADMIN_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    # ── validate the event FK ──
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    event = Event.objects.filter(pk=event_id).first()
    if not event:
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    # ── validate the team FK (a TournamentTeam, mapped from the brief's team_id) ──
    team_id = request.data.get("team_id")
    if not team_id:
        return Response({"message": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    tt = TournamentTeam.objects.filter(pk=team_id).select_related("team").first()
    if not tt:
        return Response({"message": "Tournament team not found."}, status=status.HTTP_404_NOT_FOUND)
    if tt.event_id != event.pk:
        # Guard against pairing a team with the wrong tournament — the payout must belong
        # to the event the team is registered in, or the recalc would attribute it wrongly.
        return Response(
            {"message": "This team is not registered in the given event."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── validate amount (NGN decimal, must be a non-negative number) ──
    amount, err = _parse_amount(request.data.get("amount"))
    if err:
        return err

    with transaction.atomic():
        payout = EventPrizePayout.objects.create(
            event=event,
            tournament_team=tt,
            amount=amount,
        )
        # Auto-link the prize to the winning players' history: split this team payout equally
        # across the team's active roster and write one PlayerWinning row per member, so the
        # prize shows on each player's profile (read back by afc_player.get_public_player_stats).
        # Idempotent (delete-then-recreate by payout) and failure-safe (never breaks the save).
        # Runs inside this transaction so the shares commit/rollback with the payout.
        _distribute_payout(payout)
        after = serialize_prize(payout)
        # §16 audit. season=active season so the log filters by the right quarter.
        _audit(
            user, "prize_money", "create", reason,
            object_ref=payout.id,
            before={},
            after=after,
            season=_active_season(),
        )
        # Recalc AFTER commit — prize feeds the quarterly score (§6.2).
        _enqueue_prize_recalc(tt)

    return Response(after, status=status.HTTP_201_CREATED)


# ───────────────────────── UPDATE (amount only) ─────────────────────────
@api_view(["PATCH"])
def prize_update(request, payout_id):
    """Edit a payout's amount.

    Body: { amount, reason }. Only ``amount`` is editable — event/team are fixed at
    creation (re-targeting a payout to a different team would be a delete + create).
    Audits ``prize_money`` / ``update`` with a before/after snapshot and re-enqueues
    the team's quarterly recalc on commit.
    """
    user, err = _auth(request, roles=PRIZE_ADMIN_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    payout = (EventPrizePayout.objects
              .select_related("event", "tournament_team", "tournament_team__team")
              .filter(pk=payout_id).first())
    if not payout:
        return Response({"message": "Payout not found."}, status=status.HTTP_404_NOT_FOUND)

    amount, err = _parse_amount(request.data.get("amount"))
    if err:
        return err

    with transaction.atomic():
        before = serialize_prize(payout)
        payout.amount = amount
        payout.save(update_fields=["amount"])
        # Re-derive each player's PlayerWinning share from the new amount (idempotent by payout:
        # _distribute_payout deletes this payout's prior rows then recreates them). Keeps player
        # history/stats accurate when an admin edits a prize, not only on first creation.
        _distribute_payout(payout)
        after = serialize_prize(payout)
        _audit(
            user, "prize_money", "update", reason,
            object_ref=payout.id,
            before=before,
            after=after,
            season=_active_season(),
        )
        _enqueue_prize_recalc(payout.tournament_team)

    return Response(after)


# ───────────────────────── DELETE ─────────────────────────
@api_view(["DELETE"])
def prize_delete(request, payout_id):
    """Delete a payout.

    Audits ``prize_money`` / ``delete`` (with the deleted row snapshot in ``before``) and
    re-enqueues the team's quarterly recalc on commit so the removed prize is subtracted.
    We capture the TournamentTeam reference BEFORE delete so the on_commit recalc still
    has a valid team to recompute.
    """
    user, err = _auth(request, roles=PRIZE_ADMIN_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    payout = (EventPrizePayout.objects
              .select_related("event", "tournament_team", "tournament_team__team")
              .filter(pk=payout_id).first())
    if not payout:
        return Response({"message": "Payout not found."}, status=status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        before = serialize_prize(payout)
        tt = payout.tournament_team          # keep the ref for the post-commit recalc
        payout.delete()
        _audit(
            user, "prize_money", "delete", reason,
            object_ref=payout_id,
            before=before,
            after={},
            season=_active_season(),
        )
        _enqueue_prize_recalc(tt)

    return Response({"message": "Payout deleted."}, status=status.HTTP_200_OK)


# ───────────────────────── helpers ─────────────────────────
def _parse_amount(raw):
    """Validate the NGN amount. Returns (Decimal, None) or (None, error Response).

    Prize is already-converted NGN, so we accept any non-negative number and let
    DecimalField(12,2) handle storage precision.
    """
    from decimal import Decimal, InvalidOperation
    if raw is None or raw == "":
        return None, Response({"message": "amount is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None, Response({"message": "amount must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)
    if amount < 0:
        return None, Response({"message": "amount must not be negative."}, status=status.HTTP_400_BAD_REQUEST)
    return amount, None
