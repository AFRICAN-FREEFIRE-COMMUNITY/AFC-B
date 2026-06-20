# afc_auth/translation.py
#
# i18n Phase 1 (owner 2026-06-15): the machine-translation ENGINE for the AFC backend.
#
# WHAT THIS IS:
#   A small, failure-safe layer that translates English UI strings and content into French / Portuguese
#   using DeepL, with a persistent DB cache so repeat translations cost zero API calls. It is the
#   single place the rest of the backend goes through to localize text - no other module should call
#   the translation engine directly. (Gemini is used ONLY for OCR now, in afc_ocr/services/gemini.py.)
#
# PUBLIC API (what callers import):
#   translate(text, target, source="en") -> str            single string, cached
#   translate_batch(texts, target, source="en") -> list    many strings in ONE DeepL batch (rate-friendly)
#   translate_richtext(doc, target, source="en") -> doc    Tiptap JSON doc, translates only text leaves
#   translate_html(html, target, source="en") -> str       HTML email body, translates only visible text
#   localize_field(data, key, value, target, ...) -> data  TRANSLATE-ON-READ: write data[key] + data[key_en]
#                                                           + data['translated'] for the "Show original" toggle
#   lang_name(code) -> str                                  "fr" -> "French", "pt" -> "Portuguese"
#
# HOW IT CONNECTS END TO END:
#   - Engine     : DeepL v2 (POST {api-free|api}.deepl.com/v2/translate, header Authorization:
#                  DeepL-Auth-Key), using settings.DEEPL_API_KEY. A FREE key ends in ":fx" and is
#                  auto-routed to the free host. Native batch (<=50 texts/request); fr -> FR, pt -> PT-PT.
#   - Cache      : afc_auth.models.TranslationCache. Key = sha256(source_text + target_lang). A hit
#                  returns instantly and NEVER calls the API; a miss calls DeepL, stores, returns.
#   - Locale in  : callers decide the target language from afc_auth.locale_middleware.get_locale(request)
#                  (Accept-Language header) or from afc_auth.models.User.language (the user's saved
#                  preference, set in afc_auth Phase 0 - see User.LANGUAGE_CHOICES / language_utils.py).
#   - Consumers  : request-time view localization, the build-time UI-catalog script (translate_batch),
#                  and bulk content localization for news/products (translate_richtext for Tiptap bodies).
#
# FAILURE-SAFE CONTRACT (project rule): translation must NEVER raise into a request. Every error path
# - missing key, network error, HTTP error, bad/blocked response, parse failure - returns the ORIGINAL
# English text (or the original list / doc). The site stays in English rather than 500-ing.

import hashlib
import json
import logging

import requests  # same HTTP client afc_ocr/services/gemini.py uses, kept consistent on purpose
from django.conf import settings
from django.core.cache import cache  # circuit-breaker store (Redis db 1) — see _engine_down/_trip_breaker

logger = logging.getLogger(__name__)

# Translation engine = DeepL (owner 2026-06-20). Gemini is used ONLY for OCR now (afc_ocr/services/
# gemini.py); this module no longer calls Gemini at all. DeepL is purpose-built for translation, has a
# generous free tier, and (cache-first below) costs ~nothing. The host depends on the KEY TYPE: a FREE
# key ends in ":fx" and MUST use api-free.deepl.com; a Pro key uses api.deepl.com (wrong host -> 403).
DEEPL_FREE_HOST = "https://api-free.deepl.com"
DEEPL_PRO_HOST = "https://api.deepl.com"
# DeepL accepts up to 50 `text` params per request; we chunk cache-misses to stay under that.
DEEPL_MAX_BATCH = 50
# Our 2-letter locale -> DeepL target-language code. African Lusophone (Angola/Mozambique) uses
# European Portuguese, so pt -> PT-PT. English is never a target (we only translate AWAY from English).
DEEPL_LANG = {"fr": "FR", "pt": "PT-PT"}

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ENGINE RESILIENCE (owner 2026-06-20): an expired / invalid / over-quota key must NEVER stall the site
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# This layer runs INSIDE request handling (translate-on-read: localize_field is called by the news /
# events / notifications read views). The failure-safe contract already returns English on ANY error,
# but two latency gaps turned a DEAD KEY into "Impossible de charger cette page":
#   1. The old single 60s timeout let one bad call hold a gunicorn worker past the ~30s prod request
#      timeout (afc_leaderboard/models.py) -> the worker is killed -> the navigation gets no response.
#   2. With no breaker, EVERY cache-miss string re-hit the dead key (one network round trip each, per
#      field, per request, forever), so fr/pt reads stayed slow until the key was fixed.
# The two guards below make the site instant + English the moment the engine is unhealthy:
#   • _DEEPL_TIMEOUT   - (connect, read) seconds: fail fast instead of hanging a worker.
#   • circuit breaker  - the first engine-level failure (bad/expired key, quota, network, timeout)
#     "trips" a flag in the Django cache for _ENGINE_DOWN_TTL seconds; while tripped, translate() /
#     translate_batch() skip the API entirely and return the ORIGINAL English with ZERO network cost.
#     It SELF-HEALS: once the TTL lapses the next miss tries the API again, so dropping in a working
#     key resumes translation within ~5 min (or clear the cache key "afc:translation:engine_down").
# IMPORTANT - clearing or expiring the key does NOT wipe existing translations. translate() /
# translate_batch() always check TranslationCache FIRST and serve any stored translation with NO API
# call (the key is only needed to CREATE a new translation). So an empty / expired / over-quota key
# only affects cache MISSES (brand-new, never-translated content), which fall back to the original
# English; set a valid key again and new content translates on first view and caches. There is no
# cache-skipping kill switch - that would needlessly blank already-translated content.
_DEEPL_TIMEOUT = (5, 15)           # (connect, read) seconds: fail fast; never hang a worker
_ENGINE_DOWN_KEY = "afc:translation:engine_down"
_ENGINE_DOWN_TTL = 300             # seconds the breaker stays open after a failure (5 min, self-heals)


def _engine_down() -> bool:
    """True while the circuit breaker is open (a recent engine-level failure). Best-effort: if the cache
    backend itself is unreachable we return False and let the call fall through to the API guard."""
    try:
        return cache.get(_ENGINE_DOWN_KEY) is True
    except Exception:
        return False


def _trip_breaker():
    """Open the breaker for _ENGINE_DOWN_TTL seconds so the next reads skip the engine and serve the
    original English instantly (no per-request network cost). Called ONLY for engine-level failures
    (bad/expired key, quota, rate-limit, network error, timeout, 5xx) - never a one-off content reject.
    Best-effort: a cache write failure is swallowed (we just keep paying round trips until it recovers)."""
    try:
        cache.set(_ENGINE_DOWN_KEY, True, _ENGINE_DOWN_TTL)
    except Exception:
        pass


# Map our 2-letter locale codes to a human language name. Mirrors afc_auth.models.User.LANGUAGE_CHOICES
# (en/fr/pt). Used by lang_name() for display/logging (DeepL itself uses the codes in DEEPL_LANG above).
LANG_NAMES = {
    "en": "English",
    "fr": "French",
    "pt": "Portuguese",
}


def lang_name(code) -> str:
    """'fr' -> 'French', 'pt' -> 'Portuguese', 'en' -> 'English'. Falls back to the raw code for any
    unknown value so the prompt still reads sensibly. Consumed by the prompt builders below and is
    handy for callers that want to show the language label in a UI/log."""
    return LANG_NAMES.get((code or "").lower(), code or "English")


def _cache_key(text: str, target: str) -> str:
    """sha256 hex of (source_text + target_lang). Folding the target lang into the hash input means
    the same English string requested for 'fr' and 'pt' produce different keys, so the two cached
    rows never collide. This is the value stored in TranslationCache.source_hash."""
    return hashlib.sha256(f"{text}{target}".encode("utf-8")).hexdigest()


def _call_deepl(texts, target, source="en"):
    """Low-level DeepL batch call: translate `texts` (a list of non-empty strings) into `target`,
    returning a list of translations aligned 1:1 with the input order. Raises on ANY failure - the
    callers (translate / translate_batch) own the failure-safe fallback to the original text.

    Trips the circuit breaker on engine-level failures (bad/expired key, quota, rate-limit, outage,
    network/timeout) so a dead key stops costing a network round trip per request. NEVER echoes the
    raw exception (it can carry the auth key): always raises a key-free RuntimeError."""
    api_key = getattr(settings, "DEEPL_API_KEY", "") or ""
    if not api_key:
        # No key configured -> let the caller fall back to the original text(s).
        raise ValueError("DEEPL_API_KEY is not configured in settings.")

    # Circuit breaker: a recent engine-level failure (e.g. an expired key) is "open" for a few minutes;
    # while open we do NOT pay another round trip - bail so the caller falls back to English. Self-heals
    # when the cache TTL lapses (the next miss tries the API again).
    if _engine_down():
        raise RuntimeError("DeepL translate skipped: engine circuit-breaker is open.")

    # A FREE key (":fx" suffix) MUST use the free host; a Pro key the pro host. Wrong host -> 403.
    host = DEEPL_FREE_HOST if api_key.endswith(":fx") else DEEPL_PRO_HOST
    target_code = DEEPL_LANG.get((target or "").lower(), (target or "").upper())
    data = {
        # requests serialises a list value as repeated `text=` params -> DeepL's native batch (<=50).
        "text": list(texts),
        "target_lang": target_code,
        "source_lang": (source or "en").upper(),
        # Keep our exact spacing/case around short UI strings; don't let DeepL "tidy" them.
        "preserve_formatting": "1",
    }
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}

    # Short (connect, read) timeout so a slow/unreachable engine fails fast and can never hold a worker
    # past the prod request timeout. A network-level failure means the engine is unreachable -> trip.
    try:
        resp = requests.post(f"{host}/v2/translate", data=data, headers=headers, timeout=_DEEPL_TIMEOUT)
    except requests.RequestException:
        _trip_breaker()
        raise RuntimeError("DeepL translate request failed: engine unreachable.") from None

    if resp.status_code != 200:
        # Engine-level codes that open the breaker: 403 (bad key / wrong free-vs-pro host), 429 (rate
        # limit), 456 (monthly quota exhausted), 5xx (outage). A 400 is usually a bad param/content
        # issue, not the engine being down, so it does NOT trip.
        code = resp.status_code
        if code in (403, 429, 456) or code >= 500:
            _trip_breaker()
        detail = ""
        try:
            detail = (resp.json().get("message") or "")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"DeepL translate failed with HTTP {code}" + (f": {detail}" if detail else "")
        ) from None

    # DeepL returns translations in input order, so no fragile re-matching is needed.
    try:
        out = [t["text"] for t in resp.json()["translations"]]
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("DeepL returned an unreadable response.") from None

    if len(out) != len(texts):
        # A mismatched count means we cannot safely map results back -> fail so callers keep originals.
        raise RuntimeError("DeepL returned a mismatched number of translations.")
    return out


def translate(text, target, source="en") -> str:
    """
    Translate a single UI string from `source` (default English) into `target` ("fr"/"pt").

    Behavior:
      - No-op (returns `text` unchanged) when target == source, or text is blank/None.
      - Cache: sha256(text+target) -> TranslationCache lookup. A HIT returns the cached translation
        and makes NO API call. A MISS calls DeepL, stores the row, and returns the result.
      - Failure-safe: ANY error (no key, network, HTTP, quota, parse) returns the ORIGINAL `text`.
        Translation never raises into a request.

    Connects to: afc_auth.models.TranslationCache (cache rows) and DeepL (engine). Callers pick
    `target` from afc_auth.locale_middleware.get_locale(request) or User.language.
    """
    # ── 1. no-op guards ──────────────────────────────────────────────────────────────────────────
    if not text or not str(text).strip():
        return text
    if target == source:
        return text

    text = str(text)
    key = _cache_key(text, target)

    # ── 2. cache lookup (the cheap, common path) ─────────────────────────────────────────────────
    # Import the model lazily so this module imports cleanly even before the app registry is ready
    # (e.g. during management commands), matching afc_ocr's lazy-model-import style.
    from afc_auth.models import TranslationCache

    try:
        hit = TranslationCache.objects.filter(source_hash=key, target_lang=target).first()
        if hit is not None:
            return hit.translated_text
    except Exception:
        # A DB read failure must not break translation; fall through and try the API instead.
        logger.warning("TranslationCache lookup failed; falling through to API", exc_info=True)

    # ── 3. cache miss -> call DeepL (single-item batch) ──────────────────────────────────────────
    try:
        translated = _call_deepl([text], target, source=source)[0]
    except Exception:
        # Failure-safe: any engine error -> original English text, never raise.
        logger.warning("DeepL translate failed for target=%s; returning original text", target, exc_info=True)
        return text

    if not translated:
        return text

    # ── 4. store the result (best-effort) ────────────────────────────────────────────────────────
    # get_or_create keeps this idempotent under the unique_together (concurrent identical misses
    # collapse to one row). A store failure still returns a valid translation to the caller.
    try:
        TranslationCache.objects.get_or_create(
            source_hash=key,
            target_lang=target,
            defaults={
                "source_lang": source,
                "translated_text": translated,
            },
        )
    except Exception:
        logger.warning("TranslationCache store failed for target=%s", target, exc_info=True)

    return translated


def translate_batch(texts, target, source="en") -> list:
    """
    Translate many strings via DeepL's native batch to respect rate limits, returning a list aligned
    1:1 with the input order.

    Used by: the build-time UI-catalog script and any bulk content path. Cheaper than N calls because
    the misses go up together (chunked to DeepL's <=50-texts-per-request limit).

    Behavior:
      - No-op (returns the input unchanged) when target == source, or `texts` is empty.
      - Cache-first: each text is checked against TranslationCache; only the cache MISSES are sent to
        DeepL, then their results are stored. Hits never hit the API.
      - DeepL returns translations 1:1 in input order, so results map straight back (no parsing). A
        failed chunk keeps its ORIGINAL English for those slots; the rest still translate.
      - Failure-safe: anything that goes wrong yields the ORIGINAL English text for that slot.
    """
    # ── no-op guards ─────────────────────────────────────────────────────────────────────────────
    if not texts:
        return list(texts) if texts is not None else []
    if target == source:
        return list(texts)

    from afc_auth.models import TranslationCache

    texts = [("" if t is None else str(t)) for t in texts]
    results = list(texts)  # start as identity; we overwrite the slots we successfully translate

    # ── 1. resolve cache hits, collect misses (skip blank strings entirely) ──────────────────────
    miss_indexes = []   # positions in `texts` that still need translating
    for i, t in enumerate(texts):
        if not t.strip():
            continue  # blank -> leave as-is (results[i] already == t)
        key = _cache_key(t, target)
        try:
            hit = TranslationCache.objects.filter(source_hash=key, target_lang=target).first()
        except Exception:
            hit = None
        if hit is not None:
            results[i] = hit.translated_text
        else:
            miss_indexes.append(i)

    if not miss_indexes:
        return results  # everything was cached; zero API calls

    miss_texts = [texts[i] for i in miss_indexes]

    # ── 2. translate the misses via DeepL, chunked to its per-request batch limit ─────────────────
    # DeepL returns translations 1:1 in input order (no fragile numbered-list parsing). If a chunk
    # fails, that chunk's slots keep their ORIGINAL English (failure-safe) and the rest still proceed.
    translated_misses = []
    for start in range(0, len(miss_texts), DEEPL_MAX_BATCH):
        chunk = miss_texts[start:start + DEEPL_MAX_BATCH]
        try:
            translated_misses.extend(_call_deepl(chunk, target, source=source))
        except Exception:
            logger.warning("DeepL batch translate failed for target=%s; chunk kept as English", target, exc_info=True)
            translated_misses.extend(chunk)  # keep originals for this chunk -> stays length-aligned

    # ── 3. write results back + store the freshly-translated rows (best-effort) ────────────────────
    for n, idx in enumerate(miss_indexes):
        translated = translated_misses[n] or texts[idx]
        results[idx] = translated
        if translated and translated != texts[idx]:
            try:
                TranslationCache.objects.get_or_create(
                    source_hash=_cache_key(texts[idx], target),
                    target_lang=target,
                    defaults={"source_lang": source, "translated_text": translated},
                )
            except Exception:
                logger.warning("TranslationCache batch store failed (idx=%s)", idx, exc_info=True)
    return results


def translate_richtext(doc, target, source="en"):
    """
    Translate a Tiptap rich-text document, translating ONLY the text in {type:'text', text:...} leaf
    nodes and preserving the full structure + marks (bold/italic/links/etc.).

    Used by: bulk content localization (news bodies, product descriptions) where the editor stores a
    Tiptap JSON document. The marks, node types, attrs and tree shape are all kept intact; only the
    human-visible text is swapped to `target`.

    Input/output: accepts a JSON STRING or a dict and returns the SAME type (string in -> string out,
    dict in -> dict out). It does NOT mutate the input - it walks into a new structure.

    Behavior:
      - No-op (returns the input unchanged) when target == source.
      - Collects every text leaf, translates them all in ONE translate_batch() call (cache-first,
        rate-friendly), then rebuilds the doc with the translated text in place.
      - Failure-safe: on any error (bad JSON, etc.) it returns the ORIGINAL input unchanged.
    """
    if target == source:
        return doc
    if doc is None:
        return doc

    # Remember whether we were handed a JSON string so we can return the same type.
    was_string = isinstance(doc, (str, bytes))
    try:
        parsed = json.loads(doc) if was_string else doc
    except Exception:
        # Not valid JSON we can walk -> return untouched (failure-safe).
        return doc

    # ── 1. collect every text leaf in document order ─────────────────────────────────────────────
    leaves = []  # references to the dict nodes whose 'text' we will replace

    def _collect(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                leaves.append(node)
            for child in node.get("content", []) or []:
                _collect(child)
        elif isinstance(node, list):
            for child in node:
                _collect(child)

    try:
        # Deep-copy so we never mutate the caller's object; we edit the copy's leaves in place.
        import copy
        new_doc = copy.deepcopy(parsed)
        _collect(new_doc)
    except Exception:
        return doc

    if not leaves:
        # No translatable text -> return in the original type (re-serialize if we were given a string).
        return json.dumps(new_doc) if was_string else new_doc

    # ── 2. translate all leaves in one batched, cached call ──────────────────────────────────────
    originals = [n["text"] for n in leaves]
    try:
        translated = translate_batch(originals, target, source=source)
    except Exception:
        # translate_batch is already failure-safe, but guard anyway: on error keep originals.
        return doc

    # ── 3. write the translations back onto the matching leaves ───────────────────────────────────
    for node, new_text in zip(leaves, translated):
        node["text"] = new_text

    return json.dumps(new_doc) if was_string else new_doc


def localize_field(data: dict, key: str, value, target, *, richtext=False, source="en"):
    """
    TRANSLATE-ON-READ helper for the "Show original" frontend toggle. Localizes one field of a
    response dict IN PLACE and, when it actually translated, records the original English alongside
    plus a `translated` flag, so the frontend can offer a "Show original" switch.

    WHY THIS EXISTS (i18n Phase 1, owner 2026-06-15): the read endpoints (news / events /
    notifications) each emit several user-visible fields. Every one needs the SAME shape when
    localized - the translated value, an `_en` companion holding the source English, and a boolean
    `translated` on the item. Centralizing that shape here keeps all the call sites identical and
    means the "Show original" contract is defined in exactly one place.

    WHAT IT WRITES into `data`:
      - data[key]            -> the translated value (or the original, unchanged, when target == 'en',
                                the value is blank, or translation failed - failure-safe per project rule).
      - data[f"{key}_en"]    -> the ORIGINAL English value, but ONLY when a translation actually
                                happened AND it differs from the source (so the FE can show the original).
                                Not added for English requests or when nothing changed - the FE then just
                                reads data[key] as-is.
      - data["translated"]   -> set True (and only ever flipped to True, never back to False) when ANY
                                field on this item was translated. The item-level flag is the FE's single
                                signal that a "Show original" affordance is warranted for the item.

    Args:
      data     : the per-item response dict being built (mutated in place).
      key      : the field name in `data` to localize (e.g. "news_title", "event_name", "content").
      value    : the source English value (CharField text, or a Tiptap JSON doc/string when richtext).
      target   : the active locale, from afc_auth.locale_middleware.get_locale(request) ("en"/"fr"/"pt").
      richtext : when True, translate `value` with translate_richtext (Tiptap JSON), else translate().
      source   : the source language of `value` (default "en").

    Connects to: translate() / translate_richtext() above (engine + TranslationCache cache-first, so
    the same item in the same locale is a free DB hit on the 2nd+ read), and the read views
    afc_auth.views.get_all_news / get_news_detail / get_notifications and
    afc_tournament_and_scrims.views.get_all_events / get_event_details / get_event_details_not_logged_in.

    Failure-safe: translate*/ never raise (they return the original on error), and richtext compares
    use json dumps under a guard, so this helper itself never raises into a request.
    """
    # English (or no target) -> store the original untouched, add no companion/flag. Nothing to do.
    if not target or target == source:
        data[key] = value
        return data

    # Blank values have nothing to translate; keep them as-is with no companion/flag.
    if value is None or (isinstance(value, str) and not value.strip()):
        data[key] = value
        return data

    if richtext:
        # Tiptap JSON: translate only the text leaves; keep the doc shape. translate_richtext returns
        # the SAME type it was given (str in -> str out, dict in -> dict out).
        translated = translate_richtext(value, target, source=source)
        # "Did it change?" for arbitrary JSON: compare a stable JSON serialization of both sides.
        try:
            changed = json.dumps(translated, sort_keys=True) != json.dumps(value, sort_keys=True)
        except Exception:
            # Non-serializable edge case: fall back to identity comparison (best-effort).
            changed = translated is not value
    else:
        # Plain text field.
        translated = translate(value, target, source=source)
        changed = translated != value

    data[key] = translated
    if changed:
        # Only expose the original + flag when a real translation occurred, so the FE only offers
        # "Show original" when there genuinely is a different original to show.
        data[f"{key}_en"] = value
        data["translated"] = True
    return data


# Tags whose inner text is NOT human-readable copy and must NEVER be translated. <style>/<script>
# hold CSS/JS, <title>/<head> are not body copy. translate_html() skips any text living inside these.
_HTML_SKIP_TAGS = {"style", "script", "head", "title", "meta", "link"}


def translate_html(html, target, source="en") -> str:
    """
    Translate the HUMAN-VISIBLE TEXT inside an HTML email body, leaving all tags, attributes,
    inline CSS, links (href), and embedded codes/tokens untouched.

    WHY THIS EXISTS (i18n Phase 1, owner 2026-06-15): every AFC transactional email is assembled as
    one HTML string (the branded _email_shell builders in afc_auth.views / afc_shop.emails, plus the
    large inline templates in afc_tournament_and_scrims / afc_sponsors / afc_player_market). Rather
    than rewrite every builder to wrap each English sentence in translate(), we localize ONCE at the
    email chokepoint: afc_auth.views.send_email(..., language=...) calls this on the finished body. So
    one function localizes the body of EVERY email in the system regardless of which app built it.

    HOW IT WORKS:
      - No-op (returns `html` unchanged) when target == source, or html is blank/None.
      - Parses the HTML with BeautifulSoup, collects every visible text node (NavigableString) that
        is not inside <style>/<script>/<head>/<title> and is not an HTML comment, then sends all of
        them to translate_batch() in ONE cached, rate-friendly call and writes the results back.
      - Whitespace-only nodes (the indentation between tags) are skipped, so we never waste an API
        slot translating "\n      ". A node's leading/trailing whitespace is preserved around the
        translated core so the surrounding layout/spacing is unchanged.
      - Verification codes / reset tokens that the builders render as their OWN text node (e.g. the
        6-digit code) are pure digits; translate() / translate_batch() pass digit-only strings through
        unchanged (DeepL preserves formatting, and a number has nothing to translate), so codes
        survive. Links live in href attributes, which we never touch.

    FAILURE-SAFE (project rule): ANY error - BeautifulSoup import/parse failure, batch failure -
    returns the ORIGINAL `html` so an email always sends (in English) rather than failing. Mirrors the
    contract of translate() / translate_richtext() above.

    Connects to: translate_batch() (engine + TranslationCache) for the actual work, and
    afc_auth.views.send_email (the sole caller) which passes the recipient's User.language as `target`.
    """
    # ── no-op guards ─────────────────────────────────────────────────────────────────────────────
    if not html or not str(html).strip():
        return html
    if target == source:
        return html

    html = str(html)

    # Parse with BeautifulSoup (bs4 is already a project dependency). Import lazily + guarded so a
    # missing parser can never break email sending - we just fall back to the English body.
    try:
        # Comment / Doctype / Declaration / CData / ProcessingInstruction are all NavigableString
        # SUBCLASSES that find_all(string=True) returns but which are NOT human-visible copy (e.g. the
        # "html" text of <!DOCTYPE html>, the body of an HTML comment, a CDATA block). We must skip all
        # of them - translating <!DOCTYPE html>'s "html" token would corrupt the doctype. We gather the
        # subclasses defensively (any that exist in this bs4 version) and skip nodes that are instances.
        from bs4 import BeautifulSoup, NavigableString
        from bs4 import Comment
        _NON_TEXT = [Comment]
        for _name in ("Doctype", "Declaration", "CData", "ProcessingInstruction"):
            _cls = getattr(__import__("bs4", fromlist=[_name]), _name, None)
            if _cls is not None:
                _NON_TEXT.append(_cls)
        _NON_TEXT = tuple(_NON_TEXT)
    except Exception:
        logger.warning("BeautifulSoup unavailable; sending email body untranslated", exc_info=True)
        return html

    try:
        # "html.parser" is the stdlib parser bundled with Python, so this works without lxml and
        # does not reformat the markup (important: email HTML is whitespace/layout sensitive).
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("Email HTML parse failed; sending untranslated", exc_info=True)
        return html

    # ── 1. collect translatable text nodes (skip CSS/JS, comments/doctype/etc + whitespace-only) ──
    nodes = []          # the NavigableString objects we will replace
    cores = []          # the stripped text we actually send to DeepL (aligned 1:1 with `nodes`)
    affixes = []        # (leading_ws, trailing_ws) to re-wrap around each translated core
    for text_node in soup.find_all(string=True):
        # Never translate code/style or non-text string nodes (comments, the doctype token, etc).
        if isinstance(text_node, _NON_TEXT):
            continue
        parent = text_node.parent
        if parent is not None and getattr(parent, "name", None) in _HTML_SKIP_TAGS:
            continue
        raw = str(text_node)
        core = raw.strip()
        if not core:
            continue  # pure whitespace between tags - leave exactly as-is
        # Preserve the exact leading/trailing whitespace so spacing/indentation is untouched; only
        # the visible core is swapped.
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        nodes.append(text_node)
        cores.append(core)
        affixes.append((lead, trail))

    if not nodes:
        return html  # nothing visible to translate (rare) - return untouched

    # ── 2. one batched, cached translate call for every visible string ───────────────────────────
    try:
        translated = translate_batch(cores, target, source=source)
    except Exception:
        # translate_batch is already failure-safe, but guard anyway: on error keep the English body.
        logger.warning("Email body batch translate failed for target=%s; sending untranslated", target, exc_info=True)
        return html

    # ── 3. write each translation back onto its node, restoring the original whitespace affixes ──
    try:
        for node, new_core, (lead, trail) in zip(nodes, translated, affixes):
            node.replace_with(NavigableString(f"{lead}{new_core}{trail}"))
        return str(soup)
    except Exception:
        # If re-serialization somehow fails, never send a broken body - fall back to the original.
        logger.warning("Email body re-render failed for target=%s; sending untranslated", target, exc_info=True)
        return html
