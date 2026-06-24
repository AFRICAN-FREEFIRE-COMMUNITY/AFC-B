"""
afc_tournament_and_scrims/event_payments.py
================================================================================
Pay-to-register for PAID events (feature "paid-events", Phase 1). The entry fee is charged via
Stripe Checkout and HELD in AFC's Stripe balance (Stripe is the custodian, so AFC never stores
the money in its own bank, and Adaptive Pricing shows + charges each buyer in their local
currency). A registration is only allowed for a paid event once a payment row is "paid"
(register_for_event enforces this), so a user who pays can always finish registering.

FLOW
  1. init_registration_payment  -> validate the critical gates, create EventRegistrationPayment
     (pending) + a Stripe Checkout Session, return the checkout URL. The FE redirects there.
  2. user pays on Stripe Checkout -> Stripe redirects to the FE success page with the session id.
  3. verify_registration_payment -> retrieve the session from Stripe; if paid, mark the row paid.
     The FE then calls the normal register-for-event endpoint, which now passes the paid guard.
  4. stripe_webhook (checkout.session.completed) -> a server-side backstop that marks the row
     paid even if the user closed the tab (idempotent with verify).
  ESCROW: the row stays release_status="held". An AFC admin RELEASES it after the event runs
  (admin_release_payment) or REFUNDS it (admin_refund_payment -> Stripe refund). The actual
  organizer transfer (Stripe Connect) is a later phase.

Uses raw `requests` against the Stripe REST API (no SDK dep, mirroring afc_shop's Paystack code).
Keys come from settings.STRIPE_SECRET_KEY (env-driven: TEST locally, LIVE on prod).

CONSUMED BY: afc_tournament_and_scrims/urls.py (the events/ routes) and the FE registration modal
+ the admin paid-events escrow dashboard.
"""

import hashlib
import hmac
import json
import uuid

import requests
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_team.models import Team
from .models import Event, EventRegistrationPayment, RegisteredCompetitors

STRIPE_API = "https://api.stripe.com/v1"
# Zero-decimal currencies charge in whole units; everything else (our USD/NGN/GHS/...) in cents.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "XOF", "XAF"}


# ── helpers ──────────────────────────────────────────────────────────────────
def _auth(request):
    """(user, error_response). Bearer token -> validate_token, mirroring the rest of this app."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _is_payments_admin(user) -> bool:
    """AFC staff who can view/release/refund event payments. Coarse staff role only.
    NARROWED 2026-06-19 (adversarial review): no longer authorizes any user merely holding ANY granular
    UserRoles row (e.g. a player who is also a news_admin). Releasing/refunding escrow now (re)triggers
    the ORGANIZER PAYOUT settlement (OrganizationEarning ledger), so the act that CREATES financial
    liabilities must not be gated more loosely than the payout-admin endpoints — those use the narrow
    is_platform_org_admin. A non-finance granular role is no longer a payments credential."""
    return getattr(user, "role", None) in ("admin", "moderator", "support")


def _stripe(method, path, data=None):
    """Call the Stripe REST API with the secret key. Returns (ok, json). Never raises."""
    key = getattr(settings, "STRIPE_SECRET_KEY", None)
    if not key:
        return False, {"error": {"message": "Stripe is not configured (no STRIPE_SECRET_KEY)."}}
    try:
        fn = requests.post if method == "POST" else requests.get
        r = fn(f"{STRIPE_API}{path}", headers={"Authorization": f"Bearer {key}"}, data=data, timeout=30)
        return r.status_code == 200, r.json()
    except Exception as e:  # network/Stripe down -> caller surfaces a clean error
        return False, {"error": {"message": f"Stripe request failed: {e}"}}


def _amount_minor(amount, currency):
    """Stripe charges in the smallest currency unit (cents for 2-decimal currencies)."""
    if currency.upper() in _ZERO_DECIMAL:
        return int(round(float(amount)))
    return int(round(float(amount) * 100))


# ── 1. init payment ──────────────────────────────────────────────────────────
@api_view(["POST"])
def init_registration_payment(request):
    """POST /events/init-registration-payment/  {event_id, team_id?}
    Validate the critical gates, create a pending EventRegistrationPayment + a Stripe Checkout
    Session, and return its url. The remaining registration gates (discord/invite/sponsor) are
    handled by the FE registration steps BEFORE this call + re-checked at register-for-event."""
    user, err = _auth(request)
    if err:
        return err
    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)

    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    if event.registration_type != "paid" or not event.registration_fee or event.registration_fee <= 0:
        return Response({"message": "This event is not a paid event."}, status=400)

    # reg window
    today = timezone.now().date()
    if not (event.registration_open_date <= today <= event.registration_end_date):
        return Response({"message": "Registration is closed."}, status=403)

    # already registered?
    if RegisteredCompetitors.objects.filter(event=event, user=user, status="registered").exists():
        return Response({"message": "You are already registered for this event."}, status=409)

    from .views import resolve_registration_fee, determine_team_country, _user_can_register_team
    from afc_auth.models import User as AfcUser

    team = None
    team_id = request.data.get("team_id")
    if team_id:
        team = Team.objects.filter(team_id=team_id).first()
        # Membership/ownership check (review finding #3): the fee is priced off this team's country, so
        # a client must NOT be able to price (or get {free}) off a team they don't belong to.
        if team and not _user_can_register_team(user, team):
            return Response({"message": "You cannot register this team.", "code": "not_team_member"}, status=403)

    # PER-COUNTRY payment (owner 2026-06-24, hardened after adversarial review): the amount + whether
    # this registrant pays is resolved SERVER-SIDE from country_payment_rules + the registrant's country.
    # Squad: use the SUBMITTED-roster country (determine_team_country over roster_member_ids), the SAME
    # basis register_for_event uses, so init + register agree and a stale Team.country can't be gamed.
    # Solo: the registrant's own country. Never trust a client-sent amount. resolve_registration_fee is
    # the single source of truth (also used by register_for_event + the event-details echo).
    roster_ids = request.data.get("roster_member_ids") or []
    if isinstance(roster_ids, str):
        try:
            roster_ids = json.loads(roster_ids)
        except (ValueError, TypeError):
            roster_ids = []
    if team is not None:
        # Defensive int() coercion: skip any non-numeric id so a malformed client can't 500 here
        # (the real roster validation lives in register_for_event). Empty roster -> determine_team_country
        # falls back to the team's stored country (not just the owner) so a stale client that omits the
        # roster still prices off the team, not the lone owner.
        rid = []
        for x in roster_ids:
            try:
                if x is not None:
                    rid.append(int(x))
            except (TypeError, ValueError):
                continue
        roster_users = list(AfcUser.objects.filter(user_id__in=rid)) if rid else []
        reg_country = determine_team_country(roster_users, user) or (getattr(team, "country", "") or "")
        fee = resolve_registration_fee(event, country=reg_country)
    else:
        fee = resolve_registration_fee(event, user=user)
    # FREE for this registrant's country -> no Stripe; the FE registers directly (register_for_event
    # mirrors this and skips the paid-row gate for a free-country registrant).
    if not fee["pays"]:
        return Response({"free": True, "message": "No entry fee for your country. You can register directly."}, status=200)

    amount = fee["amount"]
    currency = (fee["currency"] or "USD").upper()

    # Already paid + not refunded? Reuse it ONLY when the existing row SATISFIES this registration's
    # resolved fee (review pass-2): same currency, at least the resolved amount, and (squad) the same
    # team. Otherwise (e.g. a cheaper/other-team row, or the operator raised the fee after payment) we
    # fall through to a FRESH checkout for the now-correct amount, so the user is never dead-ended on a
    # row the hardened register gate would reject. Mirrors the register_for_event binding exactly.
    existing_q = EventRegistrationPayment.objects.filter(
        event=event, user=user, status="paid", currency=currency, amount__gte=amount,
    ).exclude(release_status="refunded")
    if team is not None:
        existing_q = existing_q.filter(team=team)
    existing = existing_q.first()
    if existing:
        return Response({"message": "You have already paid for this event. You can complete your registration.",
                         "payment_id": str(existing.payment_id), "already_paid": True}, status=200)

    # capacity (best-effort; re-checked at register time)
    if RegisteredCompetitors.objects.filter(event=event, status="registered").count() >= event.max_teams_or_players:
        return Response({"message": "Registration limit reached."}, status=403)

    payment = EventRegistrationPayment.objects.create(
        event=event, user=user, team=team, amount=amount, currency=currency, provider="stripe",
    )

    base = getattr(settings, "FRONTEND_URL", "https://africanfreefirecommunity.com").rstrip("/")
    success_url = f"{base}/tournaments/{event.slug}/register/success?session_id={{CHECKOUT_SESSION_ID}}&payment_id={payment.payment_id}"
    cancel_url = f"{base}/tournaments/{event.slug}"
    data = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "customer_email": user.email,
        "client_reference_id": str(payment.payment_id),
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][product_data][name]": f"Entry fee: {event.event_name}",
        "line_items[0][price_data][unit_amount]": _amount_minor(amount, currency),
        "line_items[0][quantity]": "1",
        # Adaptive Pricing: Stripe shows + charges in the buyer's local currency (verified working).
        "adaptive_pricing[enabled]": "true",
        "metadata[payment_id]": str(payment.payment_id),
        "metadata[event_id]": str(event.event_id),
        "metadata[user_id]": str(user.user_id),
    }
    ok, resp = _stripe("POST", "/checkout/sessions", data)
    if not ok:
        payment.status = "failed"
        payment.save(update_fields=["status"])
        return Response({"message": "Could not start payment.", "detail": resp.get("error", {}).get("message", "")}, status=502)

    payment.stripe_session_id = resp.get("id", "")
    payment.save(update_fields=["stripe_session_id"])
    return Response({"payment_id": str(payment.payment_id), "checkout_url": resp.get("url"),
                     "session_id": resp.get("id")}, status=200)


# ── 2. verify (FE success-page callback) ─────────────────────────────────────
@api_view(["POST"])
def verify_registration_payment(request):
    """POST /events/verify-registration-payment/  {session_id} or {payment_id}
    Retrieve the Checkout Session from Stripe; if paid, mark the row paid. Idempotent."""
    user, err = _auth(request)
    if err:
        return err
    session_id = request.data.get("session_id")
    payment_id = request.data.get("payment_id")
    payment = None
    if payment_id:
        payment = EventRegistrationPayment.objects.filter(payment_id=payment_id).first()
    elif session_id:
        payment = EventRegistrationPayment.objects.filter(stripe_session_id=session_id).first()
    if not payment:
        return Response({"message": "Payment not found."}, status=404)
    if payment.user_id != user.user_id and not _is_payments_admin(user):
        return Response({"message": "Unauthorized."}, status=403)
    if payment.status == "paid":
        return Response({"status": "paid", "already": True}, status=200)

    ok, sess = _stripe("GET", f"/checkout/sessions/{payment.stripe_session_id}")
    if not ok:
        return Response({"message": "Could not verify payment.", "detail": sess.get("error", {}).get("message", "")}, status=502)
    if sess.get("payment_status") == "paid":
        _mark_paid(payment, sess.get("payment_intent"))
        return Response({"status": "paid"}, status=200)
    return Response({"status": sess.get("payment_status", "unpaid")}, status=200)


def _mark_paid(payment, payment_intent=None):
    """Idempotently flip a payment to paid (used by verify + the webhook)."""
    if payment.status == "paid":
        return
    payment.status = "paid"
    payment.paid_at = timezone.now()
    if payment_intent:
        payment.stripe_payment_intent = payment_intent
    payment.save(update_fields=["status", "paid_at", "stripe_payment_intent"])


# ── 3. webhook (backstop) ────────────────────────────────────────────────────
@api_view(["POST"])
def stripe_webhook(request):
    """POST /events/stripe-webhook/  Stripe -> us. Verifies the signature (HMAC-SHA256 of
    "timestamp.body" with STRIPE_WEBHOOK_SECRET) and marks the payment paid on
    checkout.session.completed. A backstop for a closed tab. Returns 200 quickly; never 500s."""
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
    sig_header = request.headers.get("Stripe-Signature", "")
    body = request.body
    if secret:
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
            signed = f"{parts.get('t','')}.".encode() + body
            expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, parts.get("v1", "")):
                return Response({"message": "Bad signature."}, status=400)
        except Exception:
            return Response({"message": "Bad signature."}, status=400)
    # else: no secret configured yet -> accept (test setup); set the secret in prod.

    try:
        event = json.loads(body.decode())
    except Exception:
        return Response({"message": "Bad payload."}, status=400)

    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        pid = (obj.get("metadata") or {}).get("payment_id") or obj.get("client_reference_id")
        if pid:
            payment = EventRegistrationPayment.objects.filter(payment_id=pid).first()
            if payment and obj.get("payment_status") == "paid":
                _mark_paid(payment, obj.get("payment_intent"))
    return Response({"received": True}, status=200)


# ── 4. admin escrow: list / release / refund ─────────────────────────────────
@api_view(["GET"])
def admin_list_event_payments(request):
    """GET /events/admin/event-payments/?event_id=  -> paid-event payments for the escrow
    dashboard. Staff only. Lists who paid, amount, status, release status."""
    user, err = _auth(request)
    if err:
        return err
    if not _is_payments_admin(user):
        return Response({"message": "Unauthorized."}, status=403)
    qs = EventRegistrationPayment.objects.select_related("event", "user", "team").all()
    event_id = request.GET.get("event_id")
    if event_id:
        qs = qs.filter(event__event_id=event_id)
    status_f = request.GET.get("status")
    if status_f:
        qs = qs.filter(status=status_f)
    data = [{
        "payment_id": str(p.payment_id),
        "event_id": p.event_id, "event_name": p.event.event_name,
        "user": p.user.username, "user_id": p.user_id,
        "team": p.team.team_name if p.team_id else None,
        "amount": p.amount, "currency": p.currency,
        "status": p.status, "release_status": p.release_status,
        "paid_at": p.paid_at, "released_at": p.released_at, "created_at": p.created_at,
    } for p in qs[:500]]
    # quick escrow totals per the current filter
    held = qs.filter(status="paid", release_status="held")
    summary = {"held_count": held.count(), "total_payments": qs.count()}
    return Response({"payments": data, "summary": summary}, status=200)


@api_view(["POST"])
def admin_release_payment(request):
    """POST /events/admin/event-payments/release/  {payment_id}
    Mark a held, paid payment as RELEASED (event ran -> organizer is owed). The actual Stripe
    Connect transfer to the organizer is a later phase; this records the release decision."""
    user, err = _auth(request)
    if err:
        return err
    if not _is_payments_admin(user):
        return Response({"message": "Unauthorized."}, status=403)
    payment = EventRegistrationPayment.objects.filter(payment_id=request.data.get("payment_id")).first()
    if not payment:
        return Response({"message": "Payment not found."}, status=404)
    if payment.status != "paid":
        return Response({"message": "Only a paid payment can be released."}, status=400)
    if payment.release_status != "held":
        return Response({"message": f"Already {payment.release_status}."}, status=400)
    payment.release_status = "released"
    payment.released_at = timezone.now()
    payment.released_by = user
    payment.save(update_fields=["release_status", "released_at", "released_by"])
    # F6-P4 (owner 2026-06-19): releasing revenue (re)settles the event's organizer payout split —
    # the event's released revenue is divided among the owning orgs (primary + accepted co-owners by
    # payout_percent), minus the AFC fee, into OrganizationEarning ledger rows. Best-effort/idempotent.
    try:
        from afc_organizers.payouts import settle_event_payouts
        settle_event_payouts(payment.event)
    except Exception:
        pass
    return Response({"message": "Released.", "release_status": payment.release_status}, status=200)


@api_view(["POST"])
def admin_refund_payment(request):
    """POST /events/admin/event-payments/refund/  {payment_id}
    Refund a paid payment via Stripe (e.g. the event was cancelled). Marks it refunded + frees the
    user to re-register elsewhere. Held funds go back to the payer."""
    user, err = _auth(request)
    if err:
        return err
    if not _is_payments_admin(user):
        return Response({"message": "Unauthorized."}, status=403)
    payment = EventRegistrationPayment.objects.filter(payment_id=request.data.get("payment_id")).first()
    if not payment:
        return Response({"message": "Payment not found."}, status=404)
    if payment.status != "paid":
        return Response({"message": "Only a paid payment can be refunded."}, status=400)
    if payment.release_status == "released":
        return Response({"message": "Already released to the organizer; cannot auto-refund."}, status=400)
    if not payment.stripe_payment_intent:
        return Response({"message": "No charge reference to refund."}, status=400)

    ok, resp = _stripe("POST", "/refunds", {"payment_intent": payment.stripe_payment_intent})
    if not ok:
        return Response({"message": "Refund failed.", "detail": resp.get("error", {}).get("message", "")}, status=502)
    payment.status = "refunded"
    payment.release_status = "refunded"
    payment.refunded_at = timezone.now()
    payment.save(update_fields=["status", "release_status", "refunded_at"])
    # Drop the user's registration if they had completed one (so a refunded slot frees up).
    RegisteredCompetitors.objects.filter(event=payment.event, user=payment.user, status="registered").update(status="withdrawn")
    # F6-P4 (adversarial-review fix, owner 2026-06-19): a refund changes the event's released revenue,
    # so RE-SETTLE the organizer payout split. settle recomputes purely from the remaining
    # paid+released rows (the just-refunded row now drops out), so the OrganizationEarning ledger
    # converges to the corrected gross instead of keeping the old, too-high amount. Best-effort.
    try:
        from afc_organizers.payouts import settle_event_payouts
        settle_event_payouts(payment.event)
    except Exception:
        pass
    return Response({"message": "Refunded.", "status": payment.status}, status=200)
