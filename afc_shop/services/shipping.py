"""
afc_shop/services/shipping.py
─────────────────────────────
Provider-AGNOSTIC shipping layer: live courier rate-quote at checkout + dispatch
(label booking) on successful payment. Owner ask 2026-06-29.

WHY a shell (and why provider-agnostic):
  AFC sells physical goods but holds no couriers. At checkout we want to show the
  buyer live delivery options (courier + fee + ETA) and charge the chosen fee on top
  of subtotal+tax, then book that courier when payment succeeds. The provider could be
  an aggregator (Terminal Africa, Shipbubble) or a single carrier (GIGL). Rather than
  bake one vendor into the app, everything the rest of AFC touches goes through the
  ShippingProvider interface + the quote_rates()/book_shipment() helpers below. Adding
  or swapping a provider is one subclass; the model (afc_shop.models.Shipment), the
  endpoint (/shop/shipping/quote/), and the checkout views never change.

SAFE-WHEN-DISABLED contract (important):
  Until SHIPPING_PROVIDER + SHIPPING_API_KEY are set (settings.py), get_provider()
  returns None, quote_rates() reports enabled=False, and book_shipment() is a no-op.
  In that state the FE picker renders nothing and Order.shipping_fee stays 0.00, so
  checkout behaves EXACTLY as it does today. Nothing here can break an existing order.

HOW IT CONNECTS:
  - quote_rates(address, items)  -> called by afc_shop.views_shipping.shipping_quote,
    which the FE courier picker (CartDetails.tsx) hits once the delivery address is
    filled in. Returns a RateQuote (couriers + a request_token to reuse when booking).
  - book_shipment(order)         -> called (idempotently) from the payment-success
    points (verify_paystack_payment / paystack_webhook / stripe _mark_paid_and_fulfil)
    beside the voucher fulfilment, once the provider client is wired. Persists an
    afc_shop.models.Shipment row (provider, courier, tracking).

DEFERRED (the "provider client", per owner: confirm provider + supply sandbox key):
  The concrete fetch_rates()/book() HTTP calls for the chosen vendor are intentionally
  NOT written yet — they need the provider decision (Terminal/Shipbubble/GIGL) and a
  sandbox API key. The plug-in point is marked `# >>> PROVIDER CLIENT GOES HERE` in
  get_provider(). Reference client shape: afc_shop.services.mintroute (HMAC) /
  stripe_checkout._stripe (Bearer, returns (ok, json), never raises into the caller).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings


# ── Value objects the FE + checkout read (plain, JSON-serializable) ──────────────
@dataclass
class Courier:
    """One quoted delivery option the buyer can pick at checkout."""
    courier_id: str            # the provider's courier/service id (reused to book)
    name: str                  # human label, e.g. "GIGL Standard"
    fee: Decimal               # delivery cost, charged on top of subtotal+tax
    currency: str = "NGN"
    eta: str = ""              # e.g. "2-3 days" (provider-supplied, optional)
    service_code: str = ""     # optional aggregator service code

    def to_dict(self):
        return {
            "courier_id": self.courier_id,
            "name": self.name,
            "fee": str(self.fee),
            "currency": self.currency,
            "eta": self.eta,
            "service_code": self.service_code,
        }


@dataclass
class RateQuote:
    """Result of a rate lookup. `enabled` False => shipping is off; FE shows nothing."""
    enabled: bool
    couriers: list = field(default_factory=list)   # list[Courier]
    request_token: str = ""    # aggregator quote token, reused to book the chosen courier
    error: str = ""            # set (and enabled stays usable) on a soft provider failure

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "couriers": [c.to_dict() for c in self.couriers],
            "request_token": self.request_token,
            "error": self.error,
        }


# ── Provider interface — one subclass per courier API ────────────────────────────
class ShippingProvider:
    """Every courier adapter implements these two calls; nothing else in AFC needs to
    know which vendor is behind them. Implementations must NEVER raise into the caller
    on a network/credential error — return a RateQuote(enabled=False, error=...) (quote)
    or a {"status": False, "error": ...} dict (book), mirroring services.mintroute."""

    name = "base"

    def fetch_rates(self, address: dict, items: list) -> "RateQuote":
        raise NotImplementedError

    def book(self, order, courier_id: str, request_token: str) -> dict:
        # Returns {"status": True, "shipment_id", "tracking_url", "payload"} or
        # {"status": False, "error"}. Shape mirrors mintroute.purchase_voucher.
        raise NotImplementedError


def get_provider():
    """Return the configured ShippingProvider, or None when shipping is DISABLED.

    Disabled (None) whenever SHIPPING_PROVIDER or SHIPPING_API_KEY is unset, OR the
    named provider has no client wired yet — so an un-keyed / unknown config can never
    crash checkout; it simply means "no shipping options".
    """
    name = (getattr(settings, "SHIPPING_PROVIDER", "") or "").strip().lower()
    key = getattr(settings, "SHIPPING_API_KEY", None)
    if not name or not key:
        return None

    # >>> PROVIDER CLIENT GOES HERE (deferred: needs the provider decision + sandbox key)
    #   if name == "terminal":   return TerminalProvider(key)     # Terminal Africa aggregator
    #   if name == "shipbubble": return ShipbubbleProvider(key)   # Shipbubble aggregator
    #   if name == "gigl":       return GiglProvider(key)         # GIG Logistics single carrier
    # Each subclass implements fetch_rates()/book() against its REST API. Until one is
    # wired we fall through to None => shipping stays disabled (checkout unchanged).
    return None


def quote_rates(address, items):
    """Public entry: get courier options for a delivery address + cart items.

    Always returns a RateQuote and NEVER raises — a provider hiccup degrades to
    enabled=False so the checkout page keeps working without shipping. Called by
    afc_shop.views_shipping.shipping_quote.
    """
    provider = get_provider()
    if provider is None:
        return RateQuote(enabled=False)
    try:
        return provider.fetch_rates(address, items)
    except Exception as exc:  # courier API down/slow -> never break checkout
        return RateQuote(enabled=False, error=str(exc)[:200])


def book_shipment(order):
    """Idempotent post-payment dispatch hook: book the courier the buyer picked.

    No-op (returns None) when shipping is disabled. Idempotent: if the order's Shipment
    is already booked/in-transit/delivered it returns it untouched, so calling this from
    BOTH the Paystack verify and the webhook (or Stripe verify + webhook) books exactly
    once. Wire-up is completed alongside the provider client; today it only ensures the
    idempotency contract holds and never raises into the payment flow.
    """
    from afc_shop.models import Shipment

    provider = get_provider()
    if provider is None:
        return None

    shipment = getattr(order, "shipment", None)
    if shipment and shipment.status in ("booked", "in_transit", "delivered"):
        return shipment  # already dispatched -> idempotent

    try:
        # The chosen courier + quote token are stamped on the pending Shipment at
        # checkout (deferred with the checkout-view wiring). Book via the provider,
        # then persist the result. Guarded so a provider outage logs failed, not 500.
        if shipment is None:
            return None  # nothing to book yet (checkout wiring pending)
        result = provider.book(order, shipment.courier_id, shipment.request_token)
        if result.get("status"):
            shipment.provider = provider.name
            shipment.provider_shipment_id = result.get("shipment_id", "")
            shipment.tracking_url = result.get("tracking_url", "")
            shipment.provider_payload = result.get("payload", {})
            shipment.status = "booked"
        else:
            shipment.status = "failed"
            shipment.provider_payload = {"error": result.get("error", "")}
        shipment.save()
        return shipment
    except Exception:
        # Dispatch must never break a successful payment; leave the order paid, the
        # shipment can be retried/booked manually.
        return shipment
