# afc_auth/locale_middleware.py
#
# i18n Phase 1 (owner 2026-06-15): per-request locale resolution.
#
# WHAT THIS IS:
#   A tiny middleware that reads the incoming Accept-Language header and pins request.locale to one of
#   our supported locales (en/fr/pt, default "en"). Downstream views/serializers then localize their
#   output by passing request.locale (via get_locale) into the translation engine.
#
# HOW IT CONNECTS END TO END:
#   - Set by    : LocaleMiddleware (this file), wired into settings.MIDDLEWARE right AFTER
#                 django.middleware.common.CommonMiddleware.
#   - Read by   : get_locale(request) - the helper every view/serializer should use to pick a target
#                 language, instead of re-parsing headers. That value is fed to
#                 afc_auth.translation.translate / translate_batch / translate_richtext as `target`.
#   - Data in   : the frontend sends Accept-Language (the AFC AuthContext sets it from User.language /
#                 the NEXT_LOCALE cookie - see afc_auth Phase 0, User.LANGUAGE_CHOICES). A logged-in
#                 user's saved preference therefore flows through to this header on API calls.
#
# It never raises into the request: a missing/garbled header simply resolves to "en".

# The locales the backend supports, in the SAME set as afc_auth.models.User.LANGUAGE_CHOICES.
SUPPORTED_LOCALES = ("en", "fr", "pt")
DEFAULT_LOCALE = "en"


def _parse_accept_language(header: str) -> str:
    """Return the FIRST supported locale named in an Accept-Language header, else DEFAULT_LOCALE.

    Accept-Language looks like "fr-FR,fr;q=0.9,en;q=0.8". We walk the comma-separated entries IN
    ORDER (clients list them most-preferred first; we honor that order rather than re-sorting by q,
    which is the pragmatic, predictable choice for a 3-locale site) and return the first whose base
    language (the part before any '-') is one we support. Used only by LocaleMiddleware below."""
    if not header:
        return DEFAULT_LOCALE
    for part in header.split(","):
        # Each part may carry a quality value: "fr;q=0.9" -> take the tag before ';'.
        tag = part.split(";", 1)[0].strip().lower()
        if not tag:
            continue
        base = tag.split("-", 1)[0]  # "fr-FR" -> "fr"
        if base in SUPPORTED_LOCALES:
            return base
    return DEFAULT_LOCALE


class LocaleMiddleware:
    """Sets request.locale to one of en/fr/pt from the Accept-Language header (default 'en').

    Wired in settings.MIDDLEWARE after CommonMiddleware. Read downstream via get_locale(request),
    whose result is passed to afc_auth.translation.* as the translation target. Best-effort: any
    failure leaves request.locale as DEFAULT_LOCALE and never breaks the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            header = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
            request.locale = _parse_accept_language(header)
        except Exception:
            # Never let locale parsing break a request; fall back to English.
            request.locale = DEFAULT_LOCALE
        return self.get_response(request)


def get_locale(request) -> str:
    """The canonical way for views/serializers to read the active locale: returns request.locale when
    LocaleMiddleware has set it, else 'en'. Always returns a supported code. Pass its result straight
    into afc_auth.translation.translate(..., target=get_locale(request))."""
    locale = getattr(request, "locale", None)
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE
