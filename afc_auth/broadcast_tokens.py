# ── Per-recipient broadcast tokens (owner 2026-07-04) ────────────────────────────
# A broadcast (email OR in-app notification) can embed a TIME or a MONEY amount that must render in
# EACH recipient's own timezone / currency. The composer inserts tokens; deliver_broadcast calls
# resolve_broadcast_tokens(message, user) once PER recipient so every person sees their own local time
# and their own currency.
#
#   {{time:<ISO8601>}}          - an absolute instant (e.g. 2026-07-04T17:00:00Z). Rendered in the
#                                 recipient's timezone (derived from their country) + locale.
#   {{money:<amount>:<CUR>}}    - an amount in currency CUR (e.g. {{money:5000:NGN}}). Converted to the
#                                 recipient's currency via the FX system and formatted.
#
# There is no per-user timezone field, so the recipient tz is a REPRESENTATIVE zone for their country
# (ip_country || country); unknown -> the AFC base zone (Africa/Lagos). Fail-open: a token that cannot
# be resolved is replaced with a plain readable fallback, never left raw and never raising.

import re
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9
    ZoneInfo = None

from .fx import convert, user_currency

_TIME_RE = re.compile(r"\{\{\s*time:([^}]+?)\s*\}\}")
_MONEY_RE = re.compile(r"\{\{\s*money:([0-9]+(?:\.[0-9]+)?):([A-Za-z]{3})\s*\}\}")

_DEFAULT_TZ = "Africa/Lagos"  # AFC base zone (fallback when a country has no mapping)

# Representative IANA timezone per country (name OR ISO-2, lowercased). Focused on AFC's regions plus
# common others; extend freely. Only ONE zone per country - broadcasts don't need sub-national tz.
_COUNTRY_TZ = {
    "nigeria": "Africa/Lagos", "ng": "Africa/Lagos",
    "ghana": "Africa/Accra", "gh": "Africa/Accra",
    "south africa": "Africa/Johannesburg", "za": "Africa/Johannesburg",
    "kenya": "Africa/Nairobi", "ke": "Africa/Nairobi",
    "egypt": "Africa/Cairo", "eg": "Africa/Cairo",
    "morocco": "Africa/Casablanca", "ma": "Africa/Casablanca",
    "senegal": "Africa/Dakar", "sn": "Africa/Dakar",
    "cameroon": "Africa/Douala", "cm": "Africa/Douala",
    "ivory coast": "Africa/Abidjan", "cote d'ivoire": "Africa/Abidjan", "ci": "Africa/Abidjan",
    "tanzania": "Africa/Dar_es_Salaam", "tz": "Africa/Dar_es_Salaam",
    "uganda": "Africa/Kampala", "ug": "Africa/Kampala",
    "ethiopia": "Africa/Addis_Ababa", "et": "Africa/Addis_Ababa",
    "algeria": "Africa/Algiers", "dz": "Africa/Algiers",
    "tunisia": "Africa/Tunis", "tn": "Africa/Tunis",
    "angola": "Africa/Luanda", "ao": "Africa/Luanda",
    "cape verde": "Atlantic/Cape_Verde", "cabo verde": "Atlantic/Cape_Verde", "cv": "Atlantic/Cape_Verde",
    "mozambique": "Africa/Maputo", "mz": "Africa/Maputo",
    "zambia": "Africa/Lusaka", "zm": "Africa/Lusaka",
    "zimbabwe": "Africa/Harare", "zw": "Africa/Harare",
    "brazil": "America/Sao_Paulo", "br": "America/Sao_Paulo",
    "portugal": "Europe/Lisbon", "pt": "Europe/Lisbon",
    "united states": "America/New_York", "usa": "America/New_York", "us": "America/New_York",
    "united kingdom": "Europe/London", "uk": "Europe/London", "gb": "Europe/London",
    "france": "Europe/Paris", "fr": "Europe/Paris",
    "india": "Asia/Kolkata", "in": "Asia/Kolkata",
}

# Minimal currency symbol map for formatting; unknown -> the code + a space.
_CUR_SYMBOL = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€",
               "ZAR": "R", "GHS": "GH₵", "KES": "KSh", "BRL": "R$"}


def _user_tz_name(user):
    country = (getattr(user, "ip_country", "") or getattr(user, "country", "") or "").strip().lower()
    return _COUNTRY_TZ.get(country, _DEFAULT_TZ)


def _fmt_money(amount, code):
    code = code.upper()
    sym = _CUR_SYMBOL.get(code)
    try:
        n = float(amount)
    except (TypeError, ValueError):
        n = 0.0
    body = f"{n:,.2f}".rstrip("0").rstrip(".") if n % 1 else f"{int(round(n)):,}"
    return f"{sym}{body}" if sym else f"{body} {code}"


def resolve_broadcast_tokens(message, user):
    """Return `message` with every {{time:...}}/{{money:...}} token rendered for THIS user's timezone
    + currency. Never raises; an unresolvable token degrades to a plain string."""
    if not message or "{{" not in message:
        return message
    lang = (getattr(user, "language", "") or "en")

    def _time_sub(m):
        raw = m.group(1).strip()
        try:
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if ZoneInfo is not None:
                tzname = _user_tz_name(user)
                local = dt.astimezone(ZoneInfo(tzname))
                # e.g. "Fri, 4 Jul, 6:00 PM WAT" - built manually so it works on Windows too
                # (%-d / %-I are POSIX-only and would raise on the Windows dev box).
                hour12 = local.hour % 12 or 12
                ampm = "AM" if local.hour < 12 else "PM"
                return (f"{local:%a}, {local.day} {local:%b}, "
                        f"{hour12}:{local.minute:02d} {ampm} {local:%Z}")
            return raw
        except Exception:
            return raw

    def _money_sub(m):
        amt, code = m.group(1), m.group(2).upper()
        try:
            target = (user_currency(user) or "NGN").upper()
            converted = convert(float(amt), code, target)
            return _fmt_money(converted, target)
        except Exception:
            return _fmt_money(amt, code)

    out = _TIME_RE.sub(_time_sub, message)
    out = _MONEY_RE.sub(_money_sub, out)
    return out
