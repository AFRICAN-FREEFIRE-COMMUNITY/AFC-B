"""
afc_auth/cbn.py
───────────────
Central Bank of Nigeria (CBN) official exchange rates, for the platform's money conversions.

Owner ask (27 Nov 2026, AFC inbox #649): "the next thing I want you to add is the cbn API for money
conversions to naira, we can use that to be converting currency on the platform instead of whatever we
have been using." Owner decision the same day: "CBN where it can": every currency the CBN publishes
uses the CBN rate, the other currencies keep the existing provider, and every converted amount can
show its source and date.

WHAT THE CBN OFFERS
    There is no documented CBN API. Its Exchange Rates page loads open JSON endpoints (no key, no
    CORS header, so SERVER-SIDE only). We read the two small "GRAPH" ones (about 18 business days):
      - GetAllExchangeRatesGRAPH : per currency per day, buying / central / selling rate, in naira
        per ONE unit of that currency, values as strings ("1387.8883").
      - GetAllNFEM_RatesGRAPH    : the dollar's official daily rate (NFEM volume weighted average).
    Full details and the research behind them: projectsmanagement/PERSONAL/AFC/cbn-api-details.md and
    CBN-RATES/RESEARCH.md. This module is a Python port of the tested helpers in CBN-RATES/cbn-rates.mjs
    (CURRENCIES, nfemDate, buildRates, buildNfem, ngnPer).

THE RATE RULE
    - USD: the NFEM weightedAvgRate for the date (the CBN calls it the official rate of the day). If
      that day has no usable NFEM row, the USD selling rate.
    - Every other currency: the central rate.
    - Latest date each currency has. A day where a currency has two DIFFERENT rows (a known CBN data
      fault) is refused and the day before is used; two identical rows are simply merged.

TERMS: copying is allowed provided the CBN is stated expressly as the source and figures are not
altered, so CBN_CREDIT travels with every answer that carries these rates (see afc_auth.views.fx_rates).

HOW IT CONNECTS
    - Called by: afc_auth.fx.refresh_fx_rates(), which converts the naira-per-unit figures into the
      FxRate convention (units per 1 USD) and stores them with source="cbn" and the rate date.
    - Calls: requests.get on the two CBN endpoints only. Never called per page view: fx.get_rates()
      refreshes lazily at most once an hour.
    - FAIL-SOFT: every network or parse problem returns {} and is logged, never raised, so a CBN
      outage leaves the last good FxRate rows serving.
"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import requests

logger = logging.getLogger(__name__)

CBN_RATES_URL = "https://www.cbn.gov.ng/api/GetAllExchangeRatesGRAPH"
CBN_NFEM_URL = "https://www.cbn.gov.ng/api/GetAllNFEM_RatesGRAPH"
CBN_TIMEOUT = 8  # seconds; fail-soft on timeout
# Plain User-Agent naming the client, no personal data (the CBN publishes no terms, this is courtesy).
CBN_USER_AGENT = "afc-site-rates/1.0"
# The credit the CBN's terms require wherever a rate or a converted amount is shown.
CBN_CREDIT = "Source: Central Bank of Nigeria (www.cbn.gov.ng)"

# The CBN's text labels (trimmed, whitespace collapsed, upper-cased) -> ISO-4217. From CBN-RATES/
# cbn-rates.mjs. "CFA" does not say which franc; XOF and XAF are both fixed to the euro at the same
# rate, and the site's currency list carries XOF, so XOF it is. WAUA (no ISO code) and SDR (XDR) are
# left out on purpose: they are not on the site's currency list. Rows labelled NAIRA or POESO are
# not currencies we convert and fall out as unmapped.
CBN_LABELS = {
    "US DOLLAR": "USD",
    "POUNDS STERLING": "GBP",
    "POUND STERLING": "GBP",
    "EURO": "EUR",
    "SWISS FRANC": "CHF",
    "YEN": "JPY",
    "JAPANESE YEN": "JPY",
    "CFA": "XOF",
    "RIYAL": "SAR",
    "YUAN/RENMINBI": "CNY",
    "DANISH KRONA": "DKK",
    "DANISH KRONER": "DKK",
    "SOUTH AFRICAN RAND": "ZAR",
    "UAE DIRHAM": "AED",
}

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _norm_label(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).upper()


def _dec(v):
    """A CBN string like "1387.8883" -> positive Decimal, else None."""
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def nfem_date(s):
    """NFEM dates look like "November-03-2026"; returns a date or None."""
    m = re.match(r"^([A-Za-z]+)-(\d{1,2})-(\d{4})$", str(s or "").strip())
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
    except ValueError:
        return None


def _iso_date(s):
    try:
        return date.fromisoformat(str(s or "").strip())
    except ValueError:
        return None


def _day_rates(rows):
    """{code: {date: (buy, central, sell) or None}}; None marks a day with two DIFFERENT rows."""
    table = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = CBN_LABELS.get(_norm_label(r.get("currency")))
        d = _iso_date(r.get("ratedate"))
        vals = tuple(_dec(r.get(k)) for k in ("buyingrate", "centralrate", "sellingrate"))
        if not code or not d or None in vals:
            continue
        days = table.setdefault(code, {})
        if d in days and days[d] != vals:
            days[d] = None  # conflict: never guess which one is right
        else:
            days.setdefault(d, vals)
    return table


def _nfem_days(rows):
    """{date: weighted average rate or None (conflict)} from the NFEM payload."""
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = nfem_date(r.get("ratedate"))
        v = _dec(r.get("weightedAvgRate"))
        if not d or v is None:
            continue
        if d in out and out[d] != v:
            out[d] = None
        else:
            out.setdefault(d, v)
    return out


def parse_rates(rate_rows, nfem_rows):
    """Turn the two CBN payloads into {ISO code: (naira per 1 unit as Decimal, rate date)}.

    Pure function (no network), so it is tested directly. Latest usable date per currency; a day with
    conflicting rows is skipped and the previous day tried.
    """
    table = _day_rates(rate_rows)
    nfem = _nfem_days(nfem_rows)
    out = {}
    for code, days in table.items():
        for d in sorted(days, reverse=True):
            vals = days[d]
            if vals is None:
                continue  # two different rows that day: refuse it, try the day before
            buy, central, sell = vals
            if code == "USD":
                # Official dollar rate = NFEM weighted average; selling rate when that day has no NFEM row.
                value = nfem.get(d) or sell
            else:
                value = central
            out[code] = (value, d)
            break
    return out


def _get_json(url):
    resp = requests.get(url, timeout=CBN_TIMEOUT, headers={"User-Agent": CBN_USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_cbn_rates():
    """Fetch both CBN endpoints and return parse_rates(...). {} on any failure (logged, never raised).

    The NFEM endpoint failing alone is survivable (USD falls back to the selling rate); the rates
    endpoint failing means no CBN data at all.
    """
    try:
        rate_rows = _get_json(CBN_RATES_URL)
    except Exception as exc:
        logger.warning("CBN rates fetch failed: %s", exc)
        return {}
    try:
        nfem_rows = _get_json(CBN_NFEM_URL)
    except Exception as exc:
        logger.warning("CBN NFEM fetch failed (USD falls back to the selling rate): %s", exc)
        nfem_rows = []
    try:
        return parse_rates(rate_rows, nfem_rows)
    except Exception as exc:  # shape drift must never reach a money render
        logger.warning("CBN rates parse failed: %s", exc)
        return {}
