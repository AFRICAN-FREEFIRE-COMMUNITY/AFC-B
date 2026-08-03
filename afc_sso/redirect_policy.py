# ──────────────────────────────────────────────────────────────────────────────
# AFC's policy for partner redirect URIs. ONE module, TWO callers, so the rule
# cannot be enforced in one place and skipped in the other.
#
# WHO CALLS THIS
#   * afc_sso/models.py  AFCSSOApplication.clean()   - the model-level gate, which is
#     what the Django admin runs (ModelForm calls full_clean). A superuser editing a
#     row through /admin/ therefore gets the same answer as the API.
#   * afc_sso/admin_api.py  create + update          - the surface AFC staff actually
#     use, the "Sign in with AFC" tab of the admin API Keys page. It calls this
#     directly so it can return a 400 naming the offending URI rather than a generic
#     model error.
#
# THE POLICY (owner 2026-08-03), deliberately MORE generous and MORE strict at once:
#   1. SEVERAL URIs per partner. A partner needs production, staging and a local
#      development machine, and having to ask AFC for each one was friction with no
#      security benefit: every URI is checked by the same rules whatever it is for.
#   2. HTTPS everywhere a real player will ever be sent.
#   3. Plain http ONLY for loopback (localhost, 127.0.0.1, [::1]). This is what makes
#      rule 1 safe to give away: a developer can point at their own machine, and
#      nobody can register a plaintext redirect that carries an authorization code
#      across the internet. Loopback never leaves the developer's machine, so there is
#      no network to intercept.
#   4. NO wildcards. django-oauth-toolkit matches redirect URIs exactly, so a "*" was
#      never going to work as the partner imagined; it would silently register a URI
#      that can never match. Refusing it says so instead of failing at sign-in time.
#   5. NO fragments. The fragment is where an implicit-flow response would put tokens,
#      and RFC 6749 section 3.1.2 requires the endpoint URI to have none. A registered
#      fragment is either a mistake or an attempt to smuggle one.
#
# WHY NOT ENFORCE IN save(): Model.save() does not call full_clean(), and adding it
# would mean any row already in the database with a now-illegal URI could never be
# saved again, including by the very edit that would fix it. clean() is the hook the
# admin and forms run, and the API calls the validator directly, so both paths a human
# can reach are covered.
#
# APPLIES TO POST-LOGOUT URIs TOO: RP-initiated logout (afc_sso/views.py
# AFCRPInitiatedLogoutView) redirects a player to post_logout_redirect_uris after
# ending their session, which is the same "AFC sends a player to a partner URL"
# problem, so it is the same policy. See validate_redirect_uris(required=False).
# ──────────────────────────────────────────────────────────────────────────────
from urllib.parse import urlsplit

# Hosts allowed to use plain http. Loopback only: traffic to these never leaves the
# developer's own machine, so there is no network hop to intercept. "[::1]" is the IPv6
# spelling of the same address and is accepted for the same reason.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

ALLOWED_SCHEMES = ("http", "https")

# Characters that mean the partner is trying to register a pattern rather than a URI.
WILDCARD_CHARACTERS = ("*", "?")


class RedirectURIPolicyError(ValueError):
    """One URI failed the policy. `message` names which URI and why.

    A ValueError subclass so callers that only care about the text can catch either.
    afc_sso/models.py converts it to a django ValidationError; afc_sso/admin_api.py
    returns str(err) as the 400 body.
    """


def _host_of(parts):
    """Hostname without the port, in the spelling LOOPBACK_HOSTS uses.

    urlsplit().hostname lowercases and strips the brackets from an IPv6 literal, so
    "[::1]" arrives here as "::1"; both spellings are in LOOPBACK_HOSTS rather than
    normalising, because the bracketed form is what a developer actually types.
    """
    return (parts.hostname or "").lower()


def validate_one(uri):
    """Check a SINGLE redirect URI against the policy.

    Returns the cleaned URI. Raises RedirectURIPolicyError naming the URI and the
    reason, because an admin who pasted three URIs into a textarea needs to know which
    one of the three is wrong, not merely that something is.
    """
    uri = (uri or "").strip()
    if not uri:
        raise RedirectURIPolicyError("A redirect URI cannot be empty.")

    # Checked on the RAW string, before parsing: a wildcard is legal in several URI
    # components, so urlsplit would happily accept it and the mistake would survive to
    # sign-in time, where it fails as an unhelpful "redirect uri mismatch".
    for character in WILDCARD_CHARACTERS:
        if character in uri:
            raise RedirectURIPolicyError(
                f"Redirect URI '{uri}' contains '{character}'. Wildcards are not allowed: "
                "AFC matches redirect URIs exactly, so register each address in full."
            )

    parts = urlsplit(uri)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise RedirectURIPolicyError(
            f"Redirect URI '{uri}' must start with https:// (or http:// for localhost)."
        )

    if not parts.netloc:
        raise RedirectURIPolicyError(
            f"Redirect URI '{uri}' is not a full address. Include the host, "
            "for example https://partner.example/auth/afc/callback."
        )

    # RFC 6749 3.1.2: "The endpoint URI MUST NOT include a fragment component."
    if parts.fragment or uri.endswith("#"):
        raise RedirectURIPolicyError(
            f"Redirect URI '{uri}' must not contain a '#' fragment."
        )

    if parts.scheme.lower() == "http" and _host_of(parts) not in LOOPBACK_HOSTS:
        raise RedirectURIPolicyError(
            f"Redirect URI '{uri}' uses http. Plain http is only allowed for localhost "
            "and 127.0.0.1; every other address must use https."
        )

    return uri


def validate_redirect_uris(value, *, required=True, label="redirect URI"):
    """Check a WHOLE list and return it as the single space-separated string
    django-oauth-toolkit stores.

    `value` may be a list or a string with the URIs separated by any whitespace, so the
    admin UI can use a textarea with one URI per line and the Django admin can use the
    library's own space-separated field, without either caller reformatting first.

    `required=True` (redirect_uris) rejects an empty list: an application with no
    redirect URI can never complete a sign-in. `required=False`
    (post_logout_redirect_uris) allows one, because a partner that does not use
    RP-initiated logout has no reason to register anything.
    """
    if isinstance(value, (list, tuple)):
        candidates = [str(item).strip() for item in value]
    else:
        candidates = str(value or "").split()
    candidates = [item for item in candidates if item]

    if not candidates:
        if required:
            raise RedirectURIPolicyError(f"At least one {label} is required.")
        return ""

    return " ".join(validate_one(uri) for uri in candidates)
