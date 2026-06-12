# utils/search_utils.py — shared, punctuation-insensitive text search for the server-side typeaheads.
#
# WHY THIS EXISTS
# The typeahead endpoints (GET /team/search-teams/ and GET /auth/search-users/) matched with plain
# `field__icontains=q`. That is punctuation-sensitive: a team literally named "V-E" never matched the
# query "ve", because the hyphen sits between the letters. Owners reported exactly this on the teams
# search. This module collapses the common separators (hyphen, space, underscore, dot, apostrophe, ...)
# on BOTH the column and the query so "v-e", "v e", "v_e" and "ve" all match each other.
#
# HOW IT CONNECTS
# - Backend callers: afc_team.views.search_teams and afc_auth.views.search_users import
#   `normalized_column` + `separator_stripped` and OR them onto their existing icontains filters, so the
#   new behavior only ever WIDENS matches (nothing that matched before stops matching).
# - Frontend twin: frontend/lib/search.ts (normalizeSearch / matchesSearch) applies the same idea to the
#   in-browser list filters (teams, rankings, tournaments, ...). Keeping the two in sync means a search
#   behaves the same whether the list is filtered in the browser or on the server.
#
# NOTE ON ACCENTS: MySQL's default utf8mb4 collations are accent-insensitive, so "André" already matches
# "andre" at the DB layer. `normalize_search_text` (used by tests / any pure-Python matching) strips
# accents explicitly so it mirrors the frontend collapse exactly.

import re
import unicodedata

from django.db.models import Value
from django.db.models.functions import Lower, Replace

# Separator characters we collapse so punctuation never blocks a match. Mirrors the frontend, which
# strips every non-alphanumeric; here we enumerate the realistic separators that appear in team names,
# tags, usernames, UIDs and emails. (Full non-alnum stripping is not expressible in portable MySQL SQL,
# so we strip this explicit set on both sides instead.)
SQL_SEPARATORS = ["-", "_", ".", " ", "'", "/", "&", ",", "(", ")", "[", "]"]

# LEET/LOOK-ALIKE DIGITS folded into the letters they imitate, on BOTH the column and the query
# (owner 2026-06-12: searching "SHEDOO" could not find the user "SHED005" - the OCR read letter Os
# where the real name uses zeros). Folding both sides keeps matching consistent: a fully numeric
# query still matches the same numeric name because the two fold identically. 2/6/9 stay digits
# (no convincing letter form). Mirrors LEET_DIGITS in frontend/lib/search.ts.
LEET_DIGITS = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b"}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# CONFUSABLES: stylized letterforms NFKD does NOT fold, mapped to plain letters. Mirror of the same
# map in frontend/lib/search.ts. Covers Latin small-caps/phonetic letters and Cyrillic/Greek
# look-alikes that show up constantly in Free Fire in-game names. (NFKD already handles the math/
# fullwidth/circled/squared/fraktur families, so those are not listed.) Regional-indicator flag
# letters are handled by code-point range in _fold_confusables.
_CONFUSABLES = {
    # Latin small-caps / phonetic capitals
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i",
    "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    # Cyrillic look-alikes
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ѵ": "v",
    # Greek look-alikes
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x",
    # Cherokee + Georgian look-alikes used as Latin capitals by "Cherokee/aesthetic" text generators.
    # Data-driven: real AFC team names spell words in Cherokee letters (e.g. "SUPREME" = Ꮪ Ⴎ Ꮲ Ꭱ Ꭼ Ꮇ).
    # Normal names never contain these code points, so a mapping here can only help a stylized name.
    "Ꭺ": "a", "Ᏼ": "b", "Ꮯ": "c", "Ꭼ": "e", "Ꮐ": "g", "Ꮋ": "h", "Ꭻ": "j", "Ꮶ": "k", "Ꮮ": "l",
    "Ꮇ": "m", "Ꭱ": "r", "Ꮪ": "s", "Ꭲ": "t", "Ꮙ": "v", "Ꮃ": "w", "Ꮲ": "p", "Ꮓ": "z", "Ꭹ": "y",
    "Ⴎ": "u",
}


def _fold_confusables(text):
    """Map stylized letterforms (small-caps, Cyrillic/Greek look-alikes, flag letters) to plain
    letters before NFKD runs, so a normal-keyboard query finds a stylized name."""
    out = []
    for ch in text:
        cp = ord(ch)
        if 0x1F1E6 <= cp <= 0x1F1FF:  # regional-indicator 🇦..🇿 -> a..z
            out.append(chr(97 + (cp - 0x1F1E6)))
            continue
        out.append(_CONFUSABLES.get(ch) or _CONFUSABLES.get(ch.lower()) or ch)
    return "".join(out)


def normalize_search_text(value):
    """Pure-Python collapse: fancy-font-folded, accent-stripped, lower-case, alphanumerics only.

    normalize_search_text("V-E Nigeria!") -> "venigeria"; normalize_search_text("ᴠᴇ") -> "ve". Mirrors
    frontend normalizeSearch(). Used in tests and anywhere a Python-side comparison is needed (the
    SQL/ORM typeahead path uses normalized_column, which folds separators + accents but NOT stylized
    fonts — deep font folding is applied client-side, where the full list is already loaded).
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", _fold_confusables(str(value)))
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    collapsed = _NON_ALNUM.sub("", no_accents.lower())
    # Fold look-alike digits last (see LEET_DIGITS): "shed005" -> "shedoos", same as "shedoo s".
    return "".join(LEET_DIGITS.get(c, c) for c in collapsed)


def separator_stripped(value):
    """Strip the SQL_SEPARATORS set, fold LEET_DIGITS, and lower-case a query string, for comparison
    against normalized_column(). Returns "" for falsy input. "V-E" -> "ve"; "SHED005" -> "shedoos"."""
    out = str(value or "").lower()
    for sep in SQL_SEPARATORS:
        out = out.replace(sep, "")
    return "".join(LEET_DIGITS.get(c, c) for c in out)


def normalized_column(field_name):
    """Build an ORM expression that lower-cases `field_name` and strips SQL_SEPARATORS in the database,
    so `.annotate(x=normalized_column("team_name")).filter(x__icontains=separator_stripped(q))` matches
    punctuation-insensitively. Produces nested REPLACE(...LOWER(col)...) SQL.

    A NULL column stays NULL (so it simply never matches), which is the desired behavior for optional
    fields like team_tag.
    """
    expr = Lower(field_name)
    for sep in SQL_SEPARATORS:
        expr = Replace(expr, Value(sep), Value(""))
    # Fold LEET_DIGITS in SQL too, so the DB-side haystack matches the folded query: REPLACE chains
    # are cheap and this keeps server typeaheads consistent with normalize_search_text / the FE.
    for digit, letter in LEET_DIGITS.items():
        expr = Replace(expr, Value(digit), Value(letter))
    return expr
