# backend/afc_whatsapp/phone.py
# ──────────────────────────────────────────────────────────────────────────────
# Phone-number normalisation for WhatsApp sends.
#
# THE REAL PROBLEM THIS SOLVES
#   Meta's Cloud API addresses a recipient by their full international number. A
#   large slice of the numbers AFC has stored are LOCAL form instead: an audit of
#   UserProfile.whatsapp_number found 34 of 133 rows shaped like "08051234567"
#   (Nigerian national form with the "0" trunk prefix) rather than
#   "+2348051234567". Sent as-is those either bounce or, worse, resolve to a
#   different country's subscriber. Kapso's normaliser only stripped punctuation,
#   so it turned "08051234567" into "8051234567" and shipped it.
#
#   to_e164() fixes that by inferring the country from the ACCOUNT the number
#   belongs to (User.ip_country / User.country, the same fields afc_auth.fx and
#   afc_auth.language_utils key off) whenever the number is written in local form.
#
# CONSUMED BY
#   afc_whatsapp/tasks.py  (every outbound send normalises here before the row is
#                           written, so WhatsAppMessage.phone is always what Meta
#                           actually received)
#   afc_whatsapp/webhooks.py (inbound sender numbers are normalised the same way
#                           so an inbound "STOP" can be matched against the stored
#                           profile numbers)
#
# LIBRARY NOTE (honest statement of what backs this)
#   `phonenumbers` (Google's libphonenumber port) is NOT in requirements.txt and is
#   NOT installed in the repo venv, so the table below is what actually runs today.
#   If the owner ever adds the package, this module picks it up automatically: the
#   import is optional and, when present, libphonenumber does the parse and the
#   table is only used to resolve a country NAME to its ISO-2 region code. No code
#   change needed, just `pip install phonenumbers`.
# ──────────────────────────────────────────────────────────────────────────────
import logging

logger = logging.getLogger(__name__)

# Optional upgrade path: use Google's libphonenumber when it is installed. It knows
# every national numbering plan, including the ones the table below approximates.
try:  # pragma: no cover - exercised only on machines that have the package
    import phonenumbers
except ImportError:
    phonenumbers = None


# ──────────────────────────────────────────────────────────────────────────────
# Numbering-plan table: ISO-2 region -> (dial code, trunk prefix, min NSN, max NSN)
#
#   dial code    what goes after the "+".
#   trunk prefix the digit(s) a local caller dials before the national number and
#                which must be DROPPED when going international. "0" across most of
#                Africa and Europe; "1" in the NANP; None where the plan has no
#                trunk prefix (most of Francophone West Africa) or where a leading
#                zero is part of the number itself (Italy).
#   min/max NSN  length of the national significant number (after the trunk prefix).
#                Used both to validate and to tell "already international" from
#                "local" when a number happens to start with its own dial code.
#
# Coverage mirrors afc_auth/fx.py _COUNTRY_CCY (the African Free Fire community's
# countries plus the common non-African ones). Ranges are deliberately a little
# permissive where a plan has several number lengths: rejecting a real number is
# worse than passing a slightly odd one to Meta, which validates it again anyway.
# ──────────────────────────────────────────────────────────────────────────────
_DIAL_BY_ISO = {
    # West Africa
    "ng": ("234", "0", 10, 10),
    "gh": ("233", "0", 9, 9),
    "sn": ("221", None, 9, 9),
    "ci": ("225", None, 8, 10),
    "ml": ("223", None, 8, 8),
    "bf": ("226", None, 8, 8),
    "bj": ("229", None, 8, 10),
    "tg": ("228", None, 8, 8),
    "ne": ("227", None, 8, 8),
    "gn": ("224", None, 8, 9),
    "gm": ("220", None, 7, 7),
    "sl": ("232", "0", 8, 8),
    "lr": ("231", "0", 7, 9),
    "cv": ("238", None, 7, 7),
    "gw": ("245", None, 7, 9),
    # East Africa
    "ke": ("254", "0", 9, 9),
    "tz": ("255", "0", 9, 9),
    "ug": ("256", "0", 9, 9),
    "rw": ("250", "0", 9, 9),
    "et": ("251", "0", 9, 9),
    "so": ("252", "0", 7, 9),
    "bi": ("257", None, 8, 8),
    "dj": ("253", None, 8, 8),
    "er": ("291", "0", 7, 7),
    "km": ("269", None, 7, 7),
    "sd": ("249", "0", 9, 9),
    "ss": ("211", "0", 9, 9),
    # Southern Africa
    "za": ("27", "0", 9, 9),
    "zm": ("260", "0", 9, 9),
    "zw": ("263", "0", 9, 9),
    "mz": ("258", None, 9, 9),
    "mg": ("261", "0", 9, 9),
    "mw": ("265", "0", 7, 9),
    "bw": ("267", None, 7, 8),
    "na": ("264", "0", 7, 9),
    "ao": ("244", None, 9, 9),
    "mu": ("230", None, 7, 8),
    "ls": ("266", None, 8, 8),
    "sz": ("268", None, 8, 8),
    # North + Central Africa
    "eg": ("20", "0", 10, 10),
    "ma": ("212", "0", 9, 9),
    "dz": ("213", "0", 9, 9),
    "tn": ("216", None, 8, 8),
    "ly": ("218", "0", 9, 9),
    "cm": ("237", None, 8, 9),
    "td": ("235", None, 8, 8),
    "ga": ("241", None, 7, 9),
    "cg": ("242", None, 9, 9),
    "cd": ("243", "0", 9, 9),
    "cf": ("236", None, 8, 8),
    "gq": ("240", None, 9, 9),
    "st": ("239", None, 7, 7),
    # Common non-African (staff, sponsors, partner vendors)
    "us": ("1", "1", 10, 10),
    "ca": ("1", "1", 10, 10),
    "gb": ("44", "0", 10, 10),
    "ie": ("353", "0", 9, 9),
    "fr": ("33", "0", 9, 9),
    "de": ("49", "0", 6, 12),
    "es": ("34", None, 9, 9),
    "it": ("39", None, 9, 11),  # Italy keeps its leading 0: no trunk prefix to strip
    "pt": ("351", None, 9, 9),
    "nl": ("31", "0", 9, 9),
    "br": ("55", "0", 10, 11),
    "in": ("91", "0", 10, 10),
    "ae": ("971", "0", 9, 9),
}

# Country NAME (lowercased) -> ISO-2 region. Profiles store a human country label
# ("Nigeria", "Cote d'Ivoire") far more often than a code, and the geo lookup stores
# a code, so both must resolve. Spellings are intentionally redundant for the same
# reason afc_auth/language_utils.COUNTRY_TO_LANGUAGE lists several per country: it is
# cheaper than making every upstream agree.
_ISO_BY_NAME = {
    "nigeria": "ng",
    "ghana": "gh",
    "senegal": "sn",
    "ivory coast": "ci", "cote d'ivoire": "ci", "côte d'ivoire": "ci",
    "mali": "ml",
    "burkina faso": "bf",
    "benin": "bj",
    "togo": "tg",
    "niger": "ne",
    "guinea": "gn",
    "gambia": "gm", "the gambia": "gm",
    "sierra leone": "sl",
    "liberia": "lr",
    "cape verde": "cv", "cabo verde": "cv",
    "guinea-bissau": "gw", "guinea bissau": "gw",
    "kenya": "ke",
    "tanzania": "tz", "tanzania, united republic of": "tz",
    "uganda": "ug",
    "rwanda": "rw",
    "ethiopia": "et",
    "somalia": "so",
    "burundi": "bi",
    "djibouti": "dj",
    "eritrea": "er",
    "comoros": "km",
    "sudan": "sd",
    "south sudan": "ss",
    "south africa": "za",
    "zambia": "zm",
    "zimbabwe": "zw",
    "mozambique": "mz",
    "madagascar": "mg",
    "malawi": "mw",
    "botswana": "bw",
    "namibia": "na",
    "angola": "ao",
    "mauritius": "mu",
    "lesotho": "ls",
    "eswatini": "sz", "swaziland": "sz",
    "egypt": "eg",
    "morocco": "ma",
    "algeria": "dz",
    "tunisia": "tn",
    "libya": "ly",
    "cameroon": "cm",
    "chad": "td",
    "gabon": "ga",
    "congo": "cg", "republic of the congo": "cg", "congo-brazzaville": "cg",
    "congo (brazzaville)": "cg",
    "dr congo": "cd", "drc": "cd", "democratic republic of the congo": "cd",
    "congo, the democratic republic of the": "cd", "congo (kinshasa)": "cd",
    "central african republic": "cf",
    "equatorial guinea": "gq",
    "sao tome and principe": "st", "são tomé and príncipe": "st",
    "united states": "us", "united states of america": "us", "usa": "us",
    "canada": "ca",
    "united kingdom": "gb", "uk": "gb", "great britain": "gb",
    "ireland": "ie",
    "france": "fr",
    "germany": "de",
    "spain": "es",
    "italy": "it",
    "portugal": "pt",
    "netherlands": "nl",
    "brazil": "br",
    "india": "in",
    "united arab emirates": "ae", "uae": "ae",
}

# E.164 hard limits: at least 8 digits (country code + a very short national number)
# and never more than 15. Anything outside is not a dialable number.
_E164_MIN_DIGITS = 8
_E164_MAX_DIGITS = 15


def _iso_region(country_code):
    """Resolve whatever the caller has ("NG", "Nigeria", "  nigeria ", None) to a
    lowercase ISO-2 region key in _DIAL_BY_ISO, or None when it cannot be resolved.

    Accepting both shapes matters because the same value can arrive from three
    places: User.ip_country (an ipinfo ISO-2 code), User.country (a frontend label
    from constants/index.ts), and a caller passing a literal "NG"."""
    if not country_code:
        return None
    key = str(country_code).strip().lower().replace("’", "'").replace("ʼ", "'")
    if key in _DIAL_BY_ISO:
        return key
    return _ISO_BY_NAME.get(key)


def _digits(raw):
    """Every digit in `raw`, in order. Drops "+", spaces, dashes, brackets, and the
    stray letters people type ("+234 805 123 4567 (WhatsApp)")."""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def to_e164(raw, country_code=None):
    """Normalise a stored phone number to E.164 ("+2348051234567"), or None.

    Args:
        raw:          the number as stored/typed. Any punctuation, any spacing.
        country_code: the country the number BELONGS to, used only when `raw` is in
                      local form. Accepts an ISO-2 code ("NG") or a country name
                      ("Nigeria"); callers normally pass
                      `user.ip_country or user.country`.

    Returns:
        "+<country code><national number>" on success, or None when the number
        cannot be resolved with confidence.

    Returning None rather than guessing is deliberate: a local number with no
    country to anchor it ("08051234567" from a profile with no country set) could
    belong to any of a dozen numbering plans, and messaging the wrong subscriber is
    worse than not messaging at all. The caller records the failure on the
    WhatsAppMessage row so an organizer can see WHY a player was not reached.

    Handled shapes:
        "+234 805 123 4567" -> "+2348051234567"   already international
        "002348051234567"   -> "+2348051234567"   "00" international prefix
        "2348051234567"     -> "+2348051234567"   international, no "+"
        "08051234567" + NG  -> "+2348051234567"   local with a trunk prefix
        "8051234567"  + NG  -> "+2348051234567"   local without a trunk prefix
        "hello" / "" / None -> None
    """
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    region = _iso_region(country_code)

    # ── libphonenumber path (only when the optional package is installed) ──
    # It parses far more plans than the table below and validates the result, so we
    # prefer it whenever it is available. A parse failure falls through to the table
    # rather than returning None, so behaviour never gets WORSE by installing it.
    if phonenumbers is not None:  # pragma: no cover - not installed in this venv
        try:
            parsed = phonenumbers.parse(text, (region or "").upper() or None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except Exception:
            pass  # fall through to the table below

    digits = _digits(text)
    if not digits:
        return None  # junk: no digits at all

    # ── (1) explicitly international: a leading "+" or the "00" dial-out prefix ──
    # Both mean "the country code is already in here", so the country argument is
    # irrelevant and we only have to sanity-check the length.
    if text.lstrip().startswith("+"):
        return _validated(digits)
    if digits.startswith("00"):
        return _validated(digits[2:])

    # ── (2) local or bare-international form: we need the country to decide ──
    plan = _DIAL_BY_ISO.get(region) if region else None
    if plan is None:
        # No country to anchor a local number. If it is long enough to already carry
        # a country code we still cannot prove which one, so refuse rather than guess.
        logger.info(
            "to_e164: cannot normalise %s digit number without a known country (got %r)",
            len(digits), country_code,
        )
        return None

    dial, trunk, nsn_min, nsn_max = plan

    # (2a) already carries its own country code, e.g. "2348051234567" stored without
    # the "+". Length is what separates this from a national number that merely
    # starts with the same digits.
    if digits.startswith(dial) and nsn_min <= len(digits) - len(dial) <= nsn_max:
        return _validated(digits)

    # (2b) national form WITH the trunk prefix, e.g. "08051234567" in Nigeria. This
    # is the 34-of-133 case: drop the trunk digit, prepend the country code.
    if trunk and digits.startswith(trunk) and nsn_min <= len(digits) - len(trunk) <= nsn_max:
        return _validated(dial + digits[len(trunk):])

    # (2c) national form WITHOUT the trunk prefix, e.g. "8051234567".
    if nsn_min <= len(digits) <= nsn_max:
        return _validated(dial + digits)

    logger.info(
        "to_e164: %s digits do not fit the %s numbering plan (+%s, national %s-%s)",
        len(digits), region, dial, nsn_min, nsn_max,
    )
    return None


def _validated(digits):
    """Wrap a fully international digit string as "+<digits>" if it is a plausible
    E.164 number, else None. The last gate every path above goes through, so the
    length rule lives in exactly one place."""
    if _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS:
        return "+" + digits
    return None


def to_wa_id(e164):
    """Meta addresses recipients WITHOUT the leading "+" (its "wa_id" form), so the
    client strips it at the wire edge while we keep the readable "+234..." on the
    WhatsAppMessage row. Accepts either form; returns digits only."""
    return _digits(e164 or "")


# ──────────────────────────────────────────────────────────────────────────────
# The COUNTRY CODE IS COMPULSORY rule, for the surfaces where a number is TYPED
# (owner 2026-08-08: "if they're inputting WhatsApp number, then country code is
# compulsory").
#
# to_e164() above is deliberately FORGIVING: handed a country it will happily
# resolve "08051234567", which is exactly right at SEND time, where the job is to
# rescue the 34-of-133 rows already sitting in the database in local form. It is
# the wrong rule at WRITE time, and the difference matters:
#
#   * a local number resolved against a country field the person may have got
#     wrong produces a PLAUSIBLE wrong number, and a plausible wrong number
#     messages a real stranger. An obviously bad one never leaves.
#   * this number is now an ACCOUNT RECOVERY factor (afc_auth/views_recovery.py).
#     Storing one that cannot be dialled means discovering it at the exact moment
#     somebody is locked out and needs it.
#   * refusing at the door puts the error in front of the only person who can fix
#     it, while they are still looking at the form.
#
# The frontend control (react-phone-number-input, via components/PhoneNumberInput)
# always emits the international form, so an honest user never meets this error;
# the ones who do are posting to the API directly. A client-side rule alone is not
# a rule, which is why this exists.
#
# CONSUMED BY: afc_auth/views.py signup + edit_profile (the two places a player's
# UserProfile.whatsapp_number is written). afc_partner_apply/views_public.py
# _clean_whatsapp enforces the SAME rule inline for the partner application form,
# written first and left alone here on purpose: it is a different form with its own
# error copy, and rewiring it is not part of this change.
# ──────────────────────────────────────────────────────────────────────────────

# The one sentence every caller shows. Named so the wording cannot drift between
# the signup path and the profile-edit path.
COUNTRY_CODE_REQUIRED_MESSAGE = (
    "That WhatsApp number needs your country code. Give it in international form, "
    "starting with a plus and your country code, for example +234 805 123 4567."
)


def require_international(raw):
    """Normalise a TYPED WhatsApp number to E.164, insisting on a country code.

    Returns (e164, error):
        ("+2348051234567", None)  a usable number
        ("", None)                blank, which is a legitimate answer: the field is
                                  OPTIONAL everywhere it appears and must never block
                                  a signup or a profile save
        (None, "<message>")       given, but not in international form or not dialable

    Accepts a leading "+" or the "00" dial-out prefix. Everything else is refused,
    INCLUDING a national number that would have resolved fine with a country hint:
    see the section header for why that forgiveness belongs at send time only.
    """
    text = str(raw or "").strip()
    if not text:
        return "", None

    # The shape gate runs BEFORE to_e164 so the message can name the actual problem
    # ("we need your country code") instead of a vague "that number is unreadable".
    digits_only = _digits(text)
    if not (text.startswith("+") or digits_only.startswith("00")):
        return None, COUNTRY_CODE_REQUIRED_MESSAGE

    # No country_code argument, deliberately: an international number carries its
    # own, and withholding a fallback is what stops a local number slipping past
    # this second gate.
    e164 = to_e164(text)
    if not e164:
        return None, COUNTRY_CODE_REQUIRED_MESSAGE
    return e164, None


def mask_e164(e164):
    """"+2348051234567" -> "+234 ***** 4567". Safe to show once possession of the
    number has been PROVEN, never before.

    Same job as afc_auth.two_factor.mask_email: enough for the owner to recognise
    their own number, not enough for a stranger at the keyboard to learn it. The
    country code is kept because it is the part that reassures ("yes, that is my
    Nigerian number") while narrowing the number down to about a million people.
    """
    digits = _digits(e164 or "")
    if len(digits) < 4:
        return ""
    tail = digits[-4:]
    head = digits[:-4]
    # Show at most the first 3 digits (the longest dial code we carry) so the mask
    # never widens with the number's length.
    lead = head[:3]
    return f"+{lead} ***** {tail}"
