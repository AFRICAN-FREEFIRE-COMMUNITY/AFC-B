"""
afc_auth/fx.py
──────────────
Multi-currency FX layer (owner 2026-06-30): the platform stores money in USD and shows each user
their own currency. This module fetches + caches USD->currency rates, converts amounts, and
resolves a user's display currency from their country.

Design:
  - Rates come from a FREE, no-key FX API (open.er-api.com, "USD base"), cached in the FxRate table.
  - get_rates() refreshes LAZILY: it re-fetches only when the newest row is older than FX_TTL, so no
    Celery-beat / cron is needed. Fail-soft: a fetch error keeps the last-good rows (or returns an
    empty/USD-only map) so a money render NEVER crashes on FX downtime.
  - Money is STORED in USD on new rows; convert-on-read renders the viewer's currency. Historical
    rows carry their own `currency` column (NGN for pre-cutover data) and are converted from that.

Consumers:
  - /auth/fx-rates/ endpoint (afc_auth.views) serves get_rates() to the frontend (lib/fx.ts), which
    caches it; lib/money.ts formatMoney() does the USD->user-currency conversion for display.
  - Checkout (afc_shop) uses from_usd(amount_usd, "NGN") to charge Paystack in NGN (owner decision 4),
    locking the rate on the order.
"""

import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.utils import timezone

from .models import FxRate

# How long cached rates stay fresh before a lazy re-fetch (≈ daily, per owner decision 1).
FX_TTL = timedelta(hours=12)
# DISPLAY rate source. Default = the FREE, no-key exchangerate-api "open" endpoint (USD base;
# payload.rates = units per USD; 99.99% uptime). Env-overridable so the owner can swap to a KEYED,
# production-grade provider (Open Exchange Rates / Fixer / ExchangeRate-API Pro) for an SLA WITHOUT a
# code change — just set FX_API_URL to a URL whose JSON also has {"result":"success","rates":{...}}
# (USD base), e.g. ExchangeRate-API Pro: https://v6.exchangerate-api.com/v6/<KEY>/latest/USD.
FX_API_URL = os.getenv("FX_API_URL", "https://open.er-api.com/v6/latest/USD")
FX_API_TIMEOUT = 6  # seconds; fail-soft on timeout

# ── FX CHARGE BUFFER (owner 2026-06-30: "so we don't lose money") ──────────────────────────
# The mid-market rate is NOT what we net: when we charge NGN (Paystack) for a USD price and later
# settle/withdraw back to USD, the gateway/bank takes a ~1-3% spread. Charging at the raw rate eats
# that spread. So CHARGING uses a buffered rate = raw rate × (1 + FX_CHARGE_MARKUP); DISPLAY uses the
# raw rate. Default 3%, env-tunable. (Stripe charges the buyer's local currency via its own FX, so
# this buffer only applies to the Paystack USD->NGN charge path; the rate is locked on the order.)
try:
    FX_CHARGE_MARKUP = Decimal(os.getenv("FX_CHARGE_MARKUP", "0.03"))
except (InvalidOperation, TypeError):
    FX_CHARGE_MARKUP = Decimal("0.03")

# Country -> ISO-4217 currency. Covers the African Free Fire community's countries + common ones;
# anything not listed falls back to USD (the storage currency, always a safe default). Keys are
# lowercased country NAMES and ISO-2 codes so a profile country stored either way resolves.
_COUNTRY_CCY = {
    # West Africa
    "nigeria": "NGN", "ng": "NGN",
    "ghana": "GHS", "gh": "GHS",
    "senegal": "XOF", "sn": "XOF", "ivory coast": "XOF", "côte d'ivoire": "XOF", "ci": "XOF",
    "mali": "XOF", "ml": "XOF", "burkina faso": "XOF", "bf": "XOF", "benin": "XOF", "bj": "XOF",
    "togo": "XOF", "tg": "XOF", "niger": "XOF", "ne": "XOF", "guinea-bissau": "XOF",
    "sierra leone": "SLL", "sl": "SLL", "liberia": "LRD", "lr": "LRD", "gambia": "GMD", "gm": "GMD",
    "cape verde": "CVE", "cabo verde": "CVE", "cv": "CVE", "guinea": "GNF", "gn": "GNF",
    # East Africa
    "kenya": "KES", "ke": "KES", "tanzania": "TZS", "tz": "TZS", "uganda": "UGX", "ug": "UGX",
    "rwanda": "RWF", "rw": "RWF", "ethiopia": "ETB", "et": "ETB", "somalia": "SOS", "so": "SOS",
    "burundi": "BIF", "bi": "BIF",
    # Southern Africa
    "south africa": "ZAR", "za": "ZAR", "zambia": "ZMW", "zm": "ZMW", "zimbabwe": "ZWL", "zw": "ZWL",
    "mozambique": "MZN", "mz": "MZN", "madagascar": "MGA", "mg": "MGA", "malawi": "MWK", "mw": "MWK",
    "botswana": "BWP", "bw": "BWP", "namibia": "NAD", "na": "NAD", "angola": "AOA", "ao": "AOA",
    "mauritius": "MUR", "mu": "MUR",
    # North + Central Africa
    "egypt": "EGP", "eg": "EGP", "morocco": "MAD", "ma": "MAD", "algeria": "DZD", "dz": "DZD",
    "tunisia": "TND", "tn": "TND", "cameroon": "XAF", "cm": "XAF", "chad": "XAF", "td": "XAF",
    "gabon": "XAF", "ga": "XAF", "congo": "XAF", "cg": "XAF",
    "dr congo": "CDF", "democratic republic of the congo": "CDF", "cd": "CDF",
    # Common non-African
    "united states": "USD", "usa": "USD", "us": "USD",
    "united kingdom": "GBP", "uk": "GBP", "gb": "GBP",
    "canada": "CAD", "ca": "CAD", "india": "INR", "in": "INR",
    "germany": "EUR", "de": "EUR", "france": "EUR", "fr": "EUR", "spain": "EUR", "es": "EUR",
    "italy": "EUR", "it": "EUR", "portugal": "EUR", "pt": "EUR", "ireland": "EUR", "ie": "EUR",
}


def refresh_fx_rates():
    """Fetch fresh USD->currency rates and upsert them into FxRate. Returns the rates dict or {}.

    Fail-soft: any network/parse error returns {} and leaves the existing rows untouched, so callers
    fall back to the last-good cached rates.
    """
    try:
        resp = requests.get(FX_API_URL, timeout=FX_API_TIMEOUT)
        data = resp.json()
        if data.get("result") != "success":
            return {}
        rates = data.get("rates") or {}
        for ccy, rate in rates.items():
            try:
                FxRate.objects.update_or_create(
                    currency=ccy.upper(), defaults={"rate": Decimal(str(rate))},
                )
            except (InvalidOperation, TypeError, ValueError):
                continue
        return rates
    except Exception:
        return {}


def _rates_are_stale():
    newest = FxRate.objects.order_by("-updated_at").first()
    return (newest is None) or (timezone.now() - newest.updated_at > FX_TTL)


def get_rates():
    """Return {currency: Decimal(rate per USD)}. Lazily refreshes when stale. USD is always 1.

    Never raises: on a failed refresh with no cached rows it returns {"USD": 1} so money still
    renders (everyone sees USD until rates land).
    """
    if _rates_are_stale():
        refresh_fx_rates()
    out = {r.currency: r.rate for r in FxRate.objects.all()}
    out.setdefault("USD", Decimal("1"))
    return out


def to_usd(amount, from_currency):
    """Convert an amount in `from_currency` to USD. Unknown currency -> treated as already USD."""
    amount = Decimal(str(amount or 0))
    cur = (from_currency or "USD").upper()
    if cur == "USD":
        return amount
    rate = get_rates().get(cur)
    if not rate or rate == 0:
        return amount  # no rate -> don't fabricate a conversion
    return amount / Decimal(rate)


def from_usd(amount_usd, to_currency):
    """Convert a USD amount into `to_currency` (units of that currency). Unknown -> USD."""
    amount_usd = Decimal(str(amount_usd or 0))
    cur = (to_currency or "USD").upper()
    if cur == "USD":
        return amount_usd
    rate = get_rates().get(cur)
    if not rate:
        return amount_usd
    return amount_usd * Decimal(rate)


def convert(amount, from_currency, to_currency):
    """Convert `amount` from one currency to another (via USD)."""
    return from_usd(to_usd(amount, from_currency), to_currency)


def from_usd_for_charge(amount_usd, to_currency):
    """USD -> the amount to CHARGE in `to_currency`, with the protective FX buffer applied (so the
    gateway/settlement spread doesn't make us lose money). Use this — NOT from_usd — when computing
    a real charge (e.g. the NGN amount sent to Paystack). USD charges have no buffer (no conversion)."""
    cur = (to_currency or "USD").upper()
    base = from_usd(amount_usd, cur)
    if cur == "USD":
        return base
    return (base * (Decimal("1") + FX_CHARGE_MARKUP)).quantize(Decimal("0.01"))


def country_to_currency(country):
    """Map a country name or ISO-2 code to its ISO-4217 currency. Unknown -> 'USD'."""
    if not country:
        return "USD"
    return _COUNTRY_CCY.get(str(country).strip().lower(), "USD")


def user_currency(user):
    """The display currency for a user: explicit pick -> country-derived -> USD. Never blank."""
    if not user:
        return "USD"
    if getattr(user, "preferred_currency", ""):
        return user.preferred_currency.upper()
    return country_to_currency(getattr(user, "ip_country", "") or getattr(user, "country", ""))
