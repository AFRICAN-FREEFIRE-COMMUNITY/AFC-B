# afc_auth/language_utils.py
#
# i18n Phase 0 (owner 2026-06-15): map a country to a default UI/email/content language.
#
# WHY: on a user's FIRST login we want to greet Francophone and Lusophone African players in their
# own language without making them dig through settings. We only ever AUTO-pick a default; the user's
# explicit choice (set via the profile language selector, afc_auth.views.edit_profile) always wins
# and is never overwritten.
#
# HOW IT CONNECTS:
#   - Caller : afc_auth.views.login -> after the geo lookup resolves a country, if the user has no
#              language yet it calls language_for_country(country) and saves the result on User.language.
#   - Data in: the country string can arrive in several shapes - an ipinfo ISO alpha-2 code ("CI",
#              "CD", "NG"), a pycountry canonical name ("congo, the democratic republic of the"), or
#              a frontend label from constants/index.ts ("Cote d'Ivoire", "Congo (Kinshasa)",
#              "Sao Tome and Principe"). We normalize all of them to a single lowercase key, then look
#              it up in COUNTRY_TO_LANGUAGE.
#   - Data out: a 2-letter code ("en"|"fr"|"pt") that matches afc_auth.models.User.LANGUAGE_CHOICES.
#
# Locked decisions (see tasks/i18n-localization-plan.md):
#   - Francophone Africa + Madagascar -> "fr" (Madagascar is officially Francophone; Malagasy dropped).
#   - Lusophone Africa (Angola, Mozambique, Cabo Verde, Guinea-Bissau, Sao Tome and Principe) -> "pt".
#   - Everything else (incl. Anglophone Africa and the rest of the world) -> "en" (the default).

# Default language when a country is blank, unknown, or not in the map below.
DEFAULT_LANGUAGE = "en"


def _simple_normalize(country):
    """Last-resort normalizer used when afc_tournament_and_scrims.views.normalize_country cannot be
    imported (it pulls in a heavy views module that itself imports afc_auth, so a module-load import
    would risk a circular import). Lowercases, strips, and folds the curly apostrophe (U+2019) into a
    straight one so the frontend label "Cote d'Ivoire" matches our keys regardless of which quote it
    uses. Note: this does NOT resolve ISO codes - that is what the pycountry-backed normalize_country
    handles when it is importable (see language_for_country)."""
    if not country:
        return ""
    return str(country).strip().lower().replace("’", "'").replace("ʼ", "'")


# COUNTRY_TO_LANGUAGE: normalized-lowercase country name -> language code.
#
# Keys are intentionally redundant: for each country we list every spelling we can plausibly receive
# (pycountry canonical name, ipinfo full name, frontend constants/index.ts label, and common informal
# variants) so the lookup hits no matter the source. This is cheaper and far more predictable than
# trying to make every upstream spelling agree.
COUNTRY_TO_LANGUAGE = {
    # ── Francophone Africa -> French ───────────────────────────────────────────────────────────
    "benin": "fr",
    "burkina faso": "fr",
    "cameroon": "fr",
    "chad": "fr",
    # Republic of the Congo (Congo-Brazzaville). pycountry canonical = "congo".
    "congo": "fr",
    "republic of the congo": "fr",
    "congo-brazzaville": "fr",
    "congo (brazzaville)": "fr",
    # Democratic Republic of the Congo (Congo-Kinshasa, the DRC). pycountry canonical =
    # "congo, the democratic republic of the".
    "congo, the democratic republic of the": "fr",
    "democratic republic of the congo": "fr",
    "dr congo": "fr",
    "drc": "fr",
    "congo-kinshasa": "fr",
    "congo (kinshasa)": "fr",
    # Cote d'Ivoire. pycountry canonical carries an accented "o" + a straight apostrophe; _simple_normalize
    # folds the curly apostrophe, but the accented "o" only matches via normalize_country, so we list both.
    "cote d'ivoire": "fr",
    "côte d'ivoire": "fr",
    "ivory coast": "fr",
    "djibouti": "fr",
    "equatorial guinea": "fr",
    "gabon": "fr",
    "guinea": "fr",
    "mali": "fr",
    "niger": "fr",
    "senegal": "fr",
    "togo": "fr",
    "comoros": "fr",
    "central african republic": "fr",
    "mauritania": "fr",
    # Madagascar -> French (locked owner decision; Malagasy intentionally not a locale).
    "madagascar": "fr",

    # ── Lusophone Africa -> Portuguese ─────────────────────────────────────────────────────────
    "angola": "pt",
    "mozambique": "pt",
    # Cabo Verde / Cape Verde. pycountry canonical = "cabo verde".
    "cabo verde": "pt",
    "cape verde": "pt",
    "guinea-bissau": "pt",
    "guinea bissau": "pt",
    # Sao Tome and Principe. pycountry canonical = "sao tome and principe" (ASCII); the frontend label
    # carries accents ("São Tomé and Príncipe"), which normalize_country resolves to the ASCII form.
    "sao tome and principe": "pt",
    "são tomé and príncipe": "pt",
    "sao tome": "pt",
}


def language_for_country(country_name):
    """Return the default language code ("en"|"fr"|"pt") for a country name or ISO code.

    Resolution order:
      1. Normalize the incoming value. We prefer afc_tournament_and_scrims.views.normalize_country
         (pycountry-backed: it resolves ISO alpha-2 codes like "CI"/"CD" AND fuzzy full names to a
         single canonical lowercase name). It is imported LAZILY inside this function, never at module
         load, because that views module imports afc_auth and would create a circular import otherwise.
      2. If that import is unavailable (or it raises), fall back to _simple_normalize (lowercase/strip
         + fold the curly apostrophe). ISO codes will not resolve in the fallback, but the common
         full-name + frontend-label spellings still will.
      3. Look the normalized name up in COUNTRY_TO_LANGUAGE. Miss / blank -> DEFAULT_LANGUAGE ("en").

    This is intentionally total + side-effect free: it never raises, so the login() caller can use it
    without extra guarding (it still wraps the whole detect-and-save block in try/except for safety).
    """
    if not country_name:
        return DEFAULT_LANGUAGE

    # Step 1: prefer the pycountry-backed normalizer (handles ISO codes + fuzzy names). Lazy import.
    normalized = None
    try:
        from afc_tournament_and_scrims.views import normalize_country
        normalized = normalize_country(country_name)
    except Exception:
        # Circular import, pycountry hiccup, or anything else: fall back to the simple normalizer.
        normalized = None

    # Step 2: fallback / belt-and-suspenders. Even when normalize_country succeeded we re-run the
    # apostrophe fold so a canonical "cote d'ivoire" with a straight quote always matches our keys.
    if not normalized:
        normalized = _simple_normalize(country_name)
    else:
        normalized = _simple_normalize(normalized)

    # Step 3: map lookup, default to English on a miss.
    return COUNTRY_TO_LANGUAGE.get(normalized, DEFAULT_LANGUAGE)
