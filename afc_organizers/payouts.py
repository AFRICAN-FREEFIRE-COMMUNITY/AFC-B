"""
afc_organizers/payouts.py — Organizer payout subsystem + co-owner auto-split (F6-P4, owner 2026-06-19).

Paid-event registration fees are charged via Stripe and HELD by AFC; an admin RELEASES a payment
once the event has run (afc_tournament_and_scrims.event_payments.admin_release_payment). On each
release we (re)SETTLE the event: compute the event's released revenue, deduct the AFC platform fee,
and SPLIT the net among the owning orgs — the PRIMARY org (Event.organization) plus each ACCEPTED
co-owner by its EventCoOrganizer.payout_percent — writing one OrganizationEarning ledger row per org
(idempotent). An AFC admin then RELEASES each earning, which attempts the bank transfer on the org's
rail (Paystack recipient / Stripe Connect) and marks it paid; if no rail/creds, it stays owed/released
for a manual "mark paid" (mirrors the marketplace VendorPayout flow).

AFC platform fee: the documented policy — an org's FIRST 10 paid tournaments are 0% fee, then 2%.

Endpoints (organizers/ prefix):
  POST organizers/<slug>/payout-account/        save_payout_account     (org owner: bank details + recipient)
  GET  organizers/<slug>/earnings/              my_org_earnings         (org owner/can_view_metrics)
  GET  organizers/admin/payouts/                admin_list_org_payouts  (AFC admin)
  POST organizers/admin/payouts/release/        admin_release_org_payout (AFC admin: owed -> transfer -> paid)
  POST organizers/admin/payouts/mark-paid/      admin_mark_org_payout_paid (AFC admin: manual completion)
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import validate_token

from .models import Organization, OrganizationMember, EventCoOrganizer, OrganizationEarning
from .permissions import is_platform_org_admin, org_can


AFC_FEE_FREE_TOURNAMENTS = 10   # an org's first N paid tournaments are fee-free
AFC_FEE_PERCENT = Decimal("2")  # then AFC takes this %


# ── auth ───────────────────────────────────────────────────────────────────────────────────────
def _auth(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Authorization header is required"}, status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def _org_or_404(slug):
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return None, Response({"message": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)
    return org, None


# ── fee + settlement ─────────────────────────────────────────────────────────────────────────
def _afc_fee_rate(org, earning, event):
    """0% for the org's first AFC_FEE_FREE_TOURNAMENTS earning events, then AFC_FEE_PERCENT. The
    bracket must be STABLE per event across re-settles, so we rank this event by its earning row's
    FROZEN created_at: count the org's OTHER earned events whose row was created strictly earlier.
    A later event always has a later created_at, so it can never shift THIS event into a new bracket.
    (Adversarial-review fix, owner 2026-06-19: the old count of "distinct other earned events" grew
    over time and flipped the same event between 0% and 2% on re-settle.)"""
    qs = OrganizationEarning.objects.filter(organization=org).exclude(event=event)
    if earning is not None and earning.created_at:
        qs = qs.filter(created_at__lt=earning.created_at)
    prior = qs.values("event").distinct().count()
    return (Decimal("0") if prior < AFC_FEE_FREE_TOURNAMENTS else AFC_FEE_PERCENT) / Decimal("100")


def settle_event_payouts(event):
    """Recompute the payout split for ONE event from its RELEASED registration revenue and upsert an
    OrganizationEarning per owning org. Idempotent — safe to call on every payment release. Returns
    the list of (organization_id, amount) settled. Never raises into the caller (best-effort)."""
    try:
        from afc_tournament_and_scrims.models import EventRegistrationPayment
        currency = getattr(event, "registration_fee_currency", None) or "USD"
        # Gross = released revenue IN THE EVENT'S CURRENCY ONLY. Summing mixed currencies (Stripe
        # Adaptive Pricing can charge buyers in their local currency) produced a meaningless scalar;
        # filter to the event currency so foreign-currency rows are never silently mixed in.
        # (Adversarial-review fix, owner 2026-06-19.)
        gross = EventRegistrationPayment.objects.filter(
            event=event, status="paid", release_status="released", currency__iexact=currency,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        gross = Decimal(gross)

        # Owning orgs + their share %. Co-owners take their payout_percent (each clamped to 0..100);
        # if the accepted co-owners sum to >100 we SCALE them proportionally so AFC never pays out more
        # than 100% of gross (the old code clamped only the primary, letting two 60% co-owners drain
        # 120%). The PRIMARY org takes the remainder; it also absorbs the rounding remainder so the
        # split reconciles to gross to the cent. A native event (no primary) just settles co-owners.
        # (Adversarial-review fixes, owner 2026-06-19.)
        co = list(EventCoOrganizer.objects.filter(event=event, status="accepted").select_related("organization"))
        co_pairs = []  # (org, clamped pct)
        for c in co:
            pct = Decimal(c.payout_percent or 0)
            pct = max(Decimal("0"), min(Decimal("100"), pct))
            if pct > 0:
                co_pairs.append((c.organization, pct))
        co_total = sum((p for _, p in co_pairs), Decimal("0"))
        if co_total > 100:
            scale = Decimal("100") / co_total
            co_pairs = [(o, p * scale) for o, p in co_pairs]
            co_total = Decimal("100")

        shares = []  # (org, pct, gross_share)
        allocated = Decimal("0")
        for org, pct in co_pairs:
            gs = (gross * pct / Decimal("100")).quantize(Decimal("0.01"))
            shares.append((org, pct, gs))
            allocated += gs
        if event.organization_id:
            primary_pct = Decimal("100") - co_total
            if primary_pct < 0:
                primary_pct = Decimal("0")
            primary_gs = (gross - allocated).quantize(Decimal("0.01"))  # absorbs the rounding remainder
            if primary_gs < 0:
                primary_gs = Decimal("0")
            shares.append((event.organization, primary_pct, primary_gs))

        settled = []
        share_org_ids = set()
        # All-or-nothing: a failure mid-loop must not leave some orgs settled and others stale.
        with transaction.atomic():
            for org, pct, gross_share in shares:
                share_org_ids.add(org.organization_id)
                earning, _ = OrganizationEarning.objects.get_or_create(
                    organization=org, event=event, source="registration_fee",
                    defaults={"currency": currency},
                )
                fee = (gross_share * _afc_fee_rate(org, earning, event)).quantize(Decimal("0.01"))
                net = (gross_share - fee).quantize(Decimal("0.01"))
                # Don't disturb a row already paid out; otherwise refresh the computed amounts.
                if earning.status != "paid":
                    earning.gross_share = gross_share
                    earning.platform_fee = fee
                    earning.amount = net
                    earning.share_percent = pct
                    earning.currency = currency
                    earning.save()
                settled.append((org.organization_id, float(net)))
            # Reconcile: zero out earnings for orgs NO LONGER in the split (co-owner removed / declined /
            # payout dropped to 0, or the primary org changed) so a stale row stops counting as owed.
            # Never touch a row already paid. (Adversarial-review fix, owner 2026-06-19.)
            OrganizationEarning.objects.filter(event=event).exclude(
                organization_id__in=share_org_ids,
            ).exclude(status="paid").update(
                amount=Decimal("0"), gross_share=Decimal("0"),
                platform_fee=Decimal("0"), share_percent=Decimal("0"),
            )
        return settled
    except Exception:
        # Surface the failure to the logs (was silently swallowed) so a release that fails to settle
        # is debuggable instead of silently reporting success with an unwritten ledger.
        logger.exception("settle_event_payouts failed for event %s", getattr(event, "event_id", "?"))
        return []


# ── org self-serve: payout account + earnings ──────────────────────────────────────────────────
@api_view(["POST"])
def save_payout_account(request, slug):
    """Owner saves the org's payout bank details. Best-effort creates a Paystack transfer recipient
    when provider=paystack + bank_code + account_number are given (so admin release can transfer)."""
    user, err = _auth(request)
    if err:
        return err
    org, err = _org_or_404(slug)
    if err:
        return err
    is_owner = OrganizationMember.objects.filter(
        organization=org, user=user, role="owner", status="active",
    ).exists()
    if not (is_owner or is_platform_org_admin(user)):
        return Response({"message": "Only the organization owner can set payout details."}, status=status.HTTP_403_FORBIDDEN)

    org.payout_provider = (request.data.get("payout_provider") or org.payout_provider or "paystack")
    org.bank_code = request.data.get("bank_code", org.bank_code)
    org.account_number = request.data.get("account_number", org.account_number)
    org.account_name = request.data.get("account_name", org.account_name)
    # NOTE: creating the live Paystack transfer recipient / Stripe Connect account from these details
    # is the one remaining wire-up (needs live processor creds + a recipient-creation call). Until
    # then the admin completes payouts via "mark paid" after sending manually (see admin endpoints).
    org.save(update_fields=[
        "payout_provider", "bank_code", "account_number", "account_name", "updated_at",
    ])
    return Response({
        "message": "Payout account saved.",
        "payout_provider": org.payout_provider,
        "account_name": org.account_name,
        "account_number": org.account_number,
        "recipient_ready": bool(org.paystack_recipient_code or org.stripe_account_id),
    }, status=status.HTTP_200_OK)


def _earning_payload(e):
    return {
        "id": e.id,
        "organization_id": e.organization_id,
        "organization_name": e.organization.name,
        "event_id": e.event_id,
        "event_name": e.event.event_name,
        "gross_share": float(e.gross_share),
        "platform_fee": float(e.platform_fee),
        "amount": float(e.amount),
        "share_percent": float(e.share_percent),
        "currency": e.currency,
        "status": e.status,
        "created_at": e.created_at,
        "paid_at": e.paid_at,
    }


@api_view(["GET"])
def my_org_earnings(request, slug):
    """The org's payout-ledger rows (its share of each event's revenue). Owner or can_view_metrics."""
    user, err = _auth(request)
    if err:
        return err
    org, err = _org_or_404(slug)
    if err:
        return err
    if not org_can(user, "can_view_metrics", org):
        return Response({"message": "You do not have permission to view this org's earnings."}, status=status.HTTP_403_FORBIDDEN)
    rows = OrganizationEarning.objects.filter(organization=org).select_related("event", "organization")
    total_owed = sum((float(r.amount) for r in rows if r.status != "paid"), 0.0)
    total_paid = sum((float(r.amount) for r in rows if r.status == "paid"), 0.0)
    return Response({
        "earnings": [_earning_payload(r) for r in rows],
        "summary": {"total_owed": round(total_owed, 2), "total_paid": round(total_paid, 2)},
    }, status=status.HTTP_200_OK)


# ── admin: list + release + mark-paid ──────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_org_payouts(request):
    """AFC admin payout dashboard: every OrganizationEarning (optional ?status= / ?organization_id=)."""
    user, err = _auth(request)
    if err:
        return err
    if not is_platform_org_admin(user):
        return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    rows = OrganizationEarning.objects.select_related("event", "organization").all()
    if request.GET.get("status"):
        rows = rows.filter(status=request.GET["status"])
    if request.GET.get("organization_id"):
        rows = rows.filter(organization_id=request.GET["organization_id"])
    rows = list(rows[:1000])
    summary = {
        "owed": sum((float(r.amount) for r in rows if r.status == "owed"), 0.0),
        "released": sum((float(r.amount) for r in rows if r.status == "released"), 0.0),
        "paid": sum((float(r.amount) for r in rows if r.status == "paid"), 0.0),
    }
    return Response({"payouts": [_earning_payload(r) for r in rows], "summary": summary}, status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_release_org_payout(request):
    """AFC admin approves an owed earning for payout (owed -> released). When automated transfers are
    wired (live Paystack recipient / Stripe Connect), this is where the transfer fires; for now the
    admin sends manually and then calls mark-paid. Mirrors the marketplace owed->released->paid flow."""
    user, err = _auth(request)
    if err:
        return err
    if not is_platform_org_admin(user):
        return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    e = OrganizationEarning.objects.filter(id=request.data.get("earning_id")).select_related("organization").first()
    if not e:
        return Response({"message": "Payout not found."}, status=status.HTTP_404_NOT_FOUND)
    if e.status == "paid":
        return Response({"message": "Already paid."}, status=status.HTTP_400_BAD_REQUEST)
    e.status = "released"
    e.released_at = timezone.now()
    e.save(update_fields=["status", "released_at", "updated_at"])
    return Response({"message": "Released for payout. Mark paid once the transfer is sent.",
                     "status": e.status}, status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_mark_org_payout_paid(request):
    """AFC admin manually completes a payout (e.g. paid out-of-band, or no automated rail)."""
    user, err = _auth(request)
    if err:
        return err
    if not is_platform_org_admin(user):
        return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    e = OrganizationEarning.objects.filter(id=request.data.get("earning_id")).first()
    if not e:
        return Response({"message": "Payout not found."}, status=status.HTTP_404_NOT_FOUND)
    e.status = "paid"
    e.paid_at = timezone.now()
    if request.data.get("transfer_ref"):
        e.transfer_ref = str(request.data["transfer_ref"])
    e.save(update_fields=["status", "paid_at", "transfer_ref", "updated_at"])
    return Response({"message": "Marked paid.", "status": e.status}, status=status.HTTP_200_OK)
