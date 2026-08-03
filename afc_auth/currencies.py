"""
afc_auth/currencies.py
──────────────────────
THE single source of truth for "which currencies may a human pick on AFC" (owner backlog item 28,
2026-08-03: "Currency lists are incomplete: some currencies are missing when entering a prize pool,
and when sending notifications or announcements").

WHY THIS FILE EXISTS
    Before this module the platform carried FOUR different hand-maintained currency arrays that had
    drifted apart, so the same user saw a different menu depending on which screen they were on:
      - frontend components/CurrencyPicker.tsx            13 codes  (display-currency picker)
      - frontend .../create/_components/types.ts          7 codes   (registration fee)
      - frontend .../create/_components/Step5PrizePool.tsx 20 codes (prize pool)
      - frontend .../_components/BroadcastTokenInserts.tsx 8 codes  (notifications/announcements)
    The broadcast list was the shortest AND the oddest (it carried BRL but not XOF/XAF, the currencies
    most of francophone West and Central Africa actually use). This module, plus its frontend twin
    `lib/currencies.ts`, replaces all four. Add a currency HERE and in lib/currencies.ts, nowhere else.

WHAT IS IN THE LIST
    Every African ISO-4217 currency AFC's community plausibly transacts in, plus USD (the platform's
    storage/base currency) and EUR, plus the small set of non-African majors that were already
    selectable somewhere and therefore may already be persisted on real rows (GBP, CAD, INR, BRL).
    Nothing was REMOVED from any existing picker: dropping a code would orphan events/broadcasts that
    already store it. Order is deliberate, not alphabetical - AFC's highest-volume currencies first,
    so the common pick is at the top of a phone-sized dropdown.

FX SAFETY (the trap this file is designed to avoid)
    Prize/money conversion goes through afc_auth.fx (get_rates -> the FxRate table, populated from
    open.er-api.com, USD base). fx.from_usd()/to_usd() deliberately DO NOT fabricate a rate: a code
    with no FxRate row passes the amount through unconverted, which silently renders a wrong number
    rather than raising. So every code below MUST have an FxRate row. All of them were verified
    present against the live table (166 rows) on 2026-08-03; `check_currency_fx_coverage()` below
    re-checks at runtime and is asserted by the test suite so a future addition cannot regress this.

HOW IT CONNECTS
    - afc_auth.fx.get_rates() supplies the rates these codes are converted with; fx._COUNTRY_CCY maps
      a user's country to their DEFAULT display currency (a smaller map: only countries AFC sees).
    - afc_shop / afc_organizers and the event prize + registration-fee fields all store a 3-letter
      code; is_supported_currency() is the guard for any endpoint accepting one from a client.
    - Frontend twin: frontend/lib/currencies.ts (same codes, same order). The two are kept identical by
      afc_auth.tests.CurrencySourceOfTruthTests, which parses the .ts file and diffs the code lists.
"""

# ── the canonical menu ────────────────────────────────────────────────────────────────────────
# (code, English name). Names stay English on purpose: ISO codes are universal and currency names are
# not user-generated content, so they are not routed through the i18n catalogue (same call the
# existing CurrencyPicker made). Grouped by region, highest AFC volume first within each group.
AFC_CURRENCIES = [
    # ── platform base + the currencies AFC actually settles in most often ──
    ("USD", "US Dollar"),
    ("NGN", "Nigerian Naira"),
    ("GHS", "Ghanaian Cedi"),
    ("KES", "Kenyan Shilling"),
    ("ZAR", "South African Rand"),
    ("XOF", "West African CFA Franc"),
    ("XAF", "Central African CFA Franc"),
    ("EGP", "Egyptian Pound"),
    ("MAD", "Moroccan Dirham"),

    # ── rest of West Africa ──
    ("SLE", "Sierra Leonean Leone"),
    ("LRD", "Liberian Dollar"),
    ("GMD", "Gambian Dalasi"),
    ("GNF", "Guinean Franc"),
    ("CVE", "Cape Verdean Escudo"),
    ("MRU", "Mauritanian Ouguiya"),

    # ── rest of East Africa + Indian Ocean ──
    ("TZS", "Tanzanian Shilling"),
    ("UGX", "Ugandan Shilling"),
    ("RWF", "Rwandan Franc"),
    ("ETB", "Ethiopian Birr"),
    ("BIF", "Burundian Franc"),
    ("SOS", "Somali Shilling"),
    ("DJF", "Djiboutian Franc"),
    ("ERN", "Eritrean Nakfa"),
    ("SSP", "South Sudanese Pound"),
    ("MUR", "Mauritian Rupee"),
    ("SCR", "Seychellois Rupee"),
    ("MGA", "Malagasy Ariary"),
    ("KMF", "Comorian Franc"),

    # ── rest of Southern Africa ──
    ("ZMW", "Zambian Kwacha"),
    ("ZWG", "Zimbabwe Gold"),
    ("MZN", "Mozambican Metical"),
    ("MWK", "Malawian Kwacha"),
    ("BWP", "Botswana Pula"),
    ("NAD", "Namibian Dollar"),
    ("AOA", "Angolan Kwanza"),
    ("LSL", "Lesotho Loti"),
    ("SZL", "Swazi Lilangeni"),

    # ── rest of North Africa ──
    ("DZD", "Algerian Dinar"),
    ("TND", "Tunisian Dinar"),
    ("LYD", "Libyan Dinar"),
    ("SDG", "Sudanese Pound"),

    # ── rest of Central Africa ──
    ("CDF", "Congolese Franc"),
    ("STN", "Sao Tome and Principe Dobra"),

    # ── non-African majors. EUR is an owner requirement; the rest were already selectable in one of
    # the four legacy pickers (GBP/CAD/INR in the prize list, BRL in the broadcast list) and are kept
    # so no already-saved event or broadcast token points at a code that vanished from the menu. ──
    ("EUR", "Euro"),
    ("GBP", "British Pound"),
    ("CAD", "Canadian Dollar"),
    ("INR", "Indian Rupee"),
    ("BRL", "Brazilian Real"),
]

# Fast membership test for request validation. Set, not list: this is hit per-request by every
# endpoint that accepts a client-supplied currency.
CURRENCY_CODES = frozenset(code for code, _name in AFC_CURRENCIES)

# The default whenever nothing else is known. USD is the platform's storage currency, so it is always
# a safe fallback (see afc_auth.fx: a USD amount needs no conversion and therefore cannot be wrong).
DEFAULT_CURRENCY = "USD"

# ── DEPRECATED ISO codes we still accept on READ ──────────────────────────────────────────────
# Two redenominations happened after some AFC rows were written, so historical data can carry the old
# code. We keep ACCEPTING these (so an old event still validates and still converts) but they are NOT
# offered in the menu above, so nothing new can be created with them.
#   SLL -> SLE  Sierra Leone redenominated 1000:1 in 2022. An SLL rate row still exists (~24,167/USD
#               vs SLE ~24.17/USD), so an SLL amount renders 1000x the SLE figure.
#   ZWL -> ZWG  Zimbabwe replaced the dollar with Zimbabwe Gold in April 2024.
LEGACY_CURRENCY_CODES = frozenset({"SLL", "ZWL"})


def is_supported_currency(code):
    """True if `code` may be STORED on a new row. Case-insensitive; legacy codes are excluded so a
    client cannot newly create data on a redenominated currency.

    Use this in any endpoint that accepts a currency from a client (the alternative, trusting the
    payload, is what let the four pickers drift apart in the first place)."""
    return str(code or "").strip().upper() in CURRENCY_CODES


def is_known_currency(code):
    """True if `code` is supported OR a legacy code we still convert on read. Use this when VALIDATING
    EXISTING data (for example re-saving an old event) rather than when accepting a fresh pick."""
    cur = str(code or "").strip().upper()
    return cur in CURRENCY_CODES or cur in LEGACY_CURRENCY_CODES


def normalize_currency(code, default=DEFAULT_CURRENCY):
    """Coerce arbitrary client input to a supported ISO code, falling back to `default`.

    Deliberately falls back rather than raising: a money render must never 500 because a stale client
    sent a code we retired (see the fail-soft contract in afc_auth.fx)."""
    cur = str(code or "").strip().upper()
    return cur if cur in CURRENCY_CODES else default


def check_currency_fx_coverage():
    """Return the list of menu codes that currently have NO FxRate row.

    An empty list is the healthy state. A non-empty list means those currencies would convert
    INCORRECTLY-BUT-SILENTLY (afc_auth.fx passes the amount through unconverted rather than raising),
    which is exactly the trap the owner asked us not to leave behind. Asserted by the test suite, so
    adding a code with no rate data fails CI rather than shipping a wrong prize figure."""
    from .models import FxRate  # local import: keeps this module importable without the app registry

    have = set(FxRate.objects.values_list("currency", flat=True))
    return [code for code in CURRENCY_CODES if code not in have]
