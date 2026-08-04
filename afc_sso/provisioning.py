# ──────────────────────────────────────────────────────────────────────────────
# What it MEANS to stand up a "Sign in with AFC" partner, in one module.
#
# WHY THIS EXISTS: there are now TWO ways a partner application comes into being, and
# they must produce identical rows or the second one is a hole in the first one's rules:
#   * afc_sso/admin_api.py  sso_applications POST   - AFC staff typing it in by hand on
#     the "Sign in with AFC" tab of the admin API Keys page.
#   * afc_partner_apply/views_admin.py approve_application - the owner approving an
#     application an organisation submitted themselves (owner 2026-08-04, "the partner
#     sends you their details and I have to input them on my end? HOW CAN WE AUTOMATE
#     THIS?").
# The same reasoning that pulled the URI rules out into afc_sso/redirect_policy.py
# applies one level up: ONE module, TWO callers, so a rule cannot be enforced on the
# hand-typed path and skipped on the approval path.
#
# WHAT LIVES HERE
#   * the value cleaners every write path shares (_clean_url, _clean_redirect_uris,
#     _clean_logo_upload). They used to live in admin_api.py; they moved here so the
#     approval path can reach them without importing a view module, and admin_api.py now
#     imports them back under the same private names, which is why nothing else in that
#     file had to change.
#   * provision_sso_application(), the one function that actually writes the row and
#     hands back the plaintext client secret.
#
# WHAT DELIBERATELY DOES NOT LIVE HERE: the auth gate. Who is allowed to provision is a
# property of the SURFACE (staff API vs approval queue), not of provisioning, and each
# caller applies its own. This module assumes it is only ever reached by a caller that
# has already decided the request is legitimate.
# ──────────────────────────────────────────────────────────────────────────────
import ipaddress
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import get_application_model

from .models import SSO_FIELD_TOGGLES
from .redirect_policy import RedirectURIPolicyError, validate_redirect_uris

Application = get_application_model()  # AFCSSOApplication, via OAUTH2_PROVIDER_APPLICATION_MODEL


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────
# Everything a partner tells us that ends up in a browser redirect or an outbound
# request is validated here rather than trusted. A redirect URI in particular is the
# field that turns a mistake into a phishing tool, so it is the strictest.
_url_validator = URLValidator(schemes=["http", "https"])

# http:// is allowed ONLY for these hosts, so a partner can develop against their own
# machine. Everything a real player is ever redirected to must be https.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _clean_outbound_url(value, field_label):
    """Validate a URL that AFC'S OWN SERVER will fetch, not one a browser is sent to.

    The distinction matters and is the whole reason this exists beside _clean_url. A redirect
    URI is followed by the PLAYER's browser, so pointing it at localhost reaches the player's
    own machine and is a normal thing for a developer to want. A webhook URL is fetched by AFC,
    from inside AFC's network, so the same value reaches AFC's own infrastructure. An
    organisation applying to be a partner can put anything in that field, and it is a public
    unauthenticated form.

    So a webhook must be https and must resolve to a PUBLIC address. Refused here rather than
    at send time because an admin should not be able to approve a partner whose webhook was
    never deliverable, and because a refusal at the door explains itself while a silently
    failing webhook does not.

    This is one half of the defence. The other is that afc_sso/tasks.py does not follow
    redirects: a host that is public today can answer with a 302 to a private address tomorrow,
    and no amount of validation at registration time can see that coming.
    """
    cleaned, err = _clean_url(value, field_label, require_https=True)
    if err or not cleaned:
        return cleaned, err

    host = urlsplit(cleaned).hostname or ""
    if host.lower() in ("localhost",) or host.lower().endswith(".localhost"):
        return None, f"{field_label} must be a public address, not localhost."

    # A literal IP is checked directly. A NAME is deliberately NOT resolved here: DNS can
    # answer differently later, so resolving now would buy a false sense of safety and add a
    # network call to a form submission. The no-redirects rule in tasks.py is what covers the
    # name case.
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return cleaned, None

    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified):
        return None, (
            f"{field_label} must be a public address. "
            "Private, loopback and link-local addresses are not reachable from AFC."
        )
    return cleaned, None


def _clean_url(value, field_label, require_https=True):
    """Validate one optional absolute URL. Returns (cleaned, error_message).

    An empty value is legal for every optional URL field on the model (they are all
    blank=True), and comes back as "" so the caller can store it as-is.
    """
    value = (value or "").strip()
    if not value:
        return "", None
    try:
        _url_validator(value)
    except ValidationError:
        return None, f"{field_label} must be a valid URL."
    if require_https and not value.lower().startswith("https://"):
        host = value.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host not in _LOCAL_HOSTS:
            return None, f"{field_label} must use https."
    return value, None


def _clean_redirect_uris(value, *, required=True, label="redirect URI"):
    """Validate a redirect URI list against AFC policy. Returns (cleaned, error_message).

    django-oauth-toolkit stores redirect URIs as one whitespace-separated string and
    matches the incoming redirect_uri against that list exactly (afc_sso/views.py refuses
    anything that does not match, without redirecting). We accept either a list or a
    string from the client and normalise to the single space-separated string the library
    expects, so the admin UI can use a textarea with one URI per line.

    THE RULES LIVE IN afc_sso/redirect_policy.py, not here, because
    AFCSSOApplication.clean() has to apply exactly the same ones: several URIs per
    partner, https everywhere except loopback http, no wildcards, no fragments. Keeping
    one copy is what stops the API and the Django admin drifting into disagreeing about
    what a legal URI is. The error text names the offending URI, which matters when an
    admin has pasted three of them into one textarea.

    THE PUBLIC APPLICATION FORM CALLS THIS TOO (afc_partner_apply/views_public.py), at
    SUBMIT time rather than at approval time, so an applicant who typed a wildcard fixes
    it themselves in the moment instead of the owner discovering it days later.
    """
    try:
        return validate_redirect_uris(value, required=required, label=label), None
    except RedirectURIPolicyError as err:
        return None, str(err)


# ──────────────────────────────────────────────────────────────────────────────
# Logo upload validation
# ──────────────────────────────────────────────────────────────────────────────
# DELIBERATELY STRICTER THAN EVERY OTHER UPLOAD GUARD IN THIS CODEBASE. The file
# validated here is rendered on the CONSENT SCREEN - the page a player reads before
# deciding to trust a partner with their data - and it is served from AFC's own media
# origin. A file that is not really an image is therefore stored XSS on a security
# page, not a broken picture.
#
# PRECEDENT FOLLOWED: afc_ocr/services/image_validate.py, the strictest existing guard
# (explicit format allowlist + a per-file byte cap + one client-safe message per
# failure). Two deliberate departures from it, both TIGHTER, because the risk here is
# not the same:
#   * it decides the format from the browser-supplied content_type. We decode the bytes
#     with Pillow and believe only what Pillow says the file is - a .png filename and an
#     image/png header cost an attacker nothing, so neither is evidence.
#   * it fails OPEN on an unexpected error, which is right there (the extraction engine
#     downstream degrades to a clean 503 anyway). Nothing downstream saves us here, so
#     anything we cannot positively identify as one of three raster formats is REFUSED.
#
# THE PUBLIC APPLICATION FORM USES THIS UNCHANGED. An applicant uploading their own logo
# is an ANONYMOUS write of a binary file, which is the most abusable thing on the new
# public surface, so it gets the guard written for the most security-critical one rather
# than a relaxed copy. Rate limiting bounds how often it can be attempted.
ALLOWED_LOGO_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}

# 2 MB. Far below the 10-15 MB caps elsewhere in the app because the consent screen
# renders this at 48x48: anything approaching the cap is already a mistake. The frontend
# also downscales before sending (lib/imageCompress.ts), so a normal logo is nowhere near.
MAX_LOGO_BYTES = 2 * 1024 * 1024

# Decompression-bomb guard: a few-KB PNG can legally decode to a gigapixel canvas, which
# would exhaust memory in the re-encode below. Size on disk alone does not catch that.
MAX_LOGO_EDGE = 5000

# Extensions we are willing to see on a stored file. "jpeg" is here only because it is a
# legitimate alias a re-encode may produce; the canonical values are the ones above.
_SAFE_LOGO_EXTS = frozenset(ALLOWED_LOGO_FORMATS.values()) | {"jpeg"}


def _clean_logo_upload(uploaded):
    """Validate one uploaded partner logo. Returns (file_to_store, error_message).

    Same (cleaned, error) contract as _clean_url above, so the views read alike: exactly
    one of the two is non-None.

    CHECKS, IN ORDER:
      1. SIZE, first because it costs nothing and an enormous file should never reach a
         decoder at all.
      2. IDENTITY, by decoding. Pillow must open the bytes, agree they are PNG / JPEG /
         WEBP, and verify() the file. This is the check a filename and a Content-Type
         header cannot fake, and it is the reason an SVG (which can carry <script>) or an
         HTML page named logo.png is refused HERE rather than being served to players
         from AFC's origin later.
      3. DIMENSIONS, so a small file that decodes to a huge canvas cannot exhaust memory
         during the re-encode.

    ON SUCCESS the file goes through afc_auth.image_utils.normalize_image_upload - the
    same downscale/recompress every other logo upload in the app uses (team logo, profile
    picture) - and is then RENAMED. The rename is not cosmetic: the original filename is
    the one part of the upload an attacker fully controls, and rebuilding it is what
    guarantees no attacker-chosen extension (logo.html) can reach the media directory,
    whatever normalize_image_upload did or did not manage to do with the bytes.
    """
    # ── 1. size, before any decode ──
    size = getattr(uploaded, "size", 0) or 0
    if size > MAX_LOGO_BYTES:
        return None, f"The logo must be {MAX_LOGO_BYTES // (1024 * 1024)} MB or smaller."

    # Local import, mirroring how every other view in this codebase reaches for Pillow:
    # it keeps the module importable on a host where the image stack is unavailable.
    from PIL import Image

    # ── 2. identity: what the BYTES are, not what the request claims ──
    try:
        uploaded.seek(0)
        probe = Image.open(uploaded)
        # .format and .size come from the header, so they are readable before verify().
        image_format = (probe.format or "").upper()
        width, height = probe.size
        # verify() reads the rest of the file and raises on corruption. It leaves `probe`
        # unusable afterwards, which is fine - nothing below touches it again.
        probe.verify()
    except Exception:  # noqa: BLE001 - ANY decode failure is a refusal. Never a pass.
        return None, "That file is not a readable image. Upload a PNG, JPG or WEBP."
    finally:
        # Rewind for whoever reads the file next, whether we passed or failed.
        try:
            uploaded.seek(0)
        except Exception:  # noqa: BLE001 - an exotic file-like without seek is not fatal.
            pass

    if image_format not in ALLOWED_LOGO_FORMATS:
        return None, "The logo must be a PNG, JPG or WEBP image."

    # ── 3. dimensions ──
    if width > MAX_LOGO_EDGE or height > MAX_LOGO_EDGE:
        return None, f"The logo must be {MAX_LOGO_EDGE} pixels or smaller on each side."

    from afc_auth.image_utils import normalize_image_upload

    cleaned = normalize_image_upload(uploaded)

    # Rebuild the stored name from scratch. normalize_image_upload re-encodes to .png or
    # .jpg when it succeeds and returns the ORIGINAL upload, original name and all, when
    # it does not - so trust ITS extension only when it is one of ours, and otherwise fall
    # back to the format Pillow actually decoded above. Either way the name that reaches
    # storage is ours, not the partner's.
    ext = (getattr(cleaned, "name", "") or "").rsplit(".", 1)[-1].lower()
    if ext not in _SAFE_LOGO_EXTS:
        ext = ALLOWED_LOGO_FORMATS[image_format]
    cleaned.name = f"partner-logo.{ext}"
    return cleaned, None


# ──────────────────────────────────────────────────────────────────────────────
# The one provisioning entry point
# ──────────────────────────────────────────────────────────────────────────────
def provision_sso_application(
    *,
    name,
    redirect_uris,
    post_logout_redirect_uris="",
    display_name="",
    logo_url="",
    homepage_url="",
    deletion_webhook_url="",
    logo_file=None,
    toggles=None,
    created_by=None,
):
    """Create ONE partner application. Returns (application, plaintext_secret, error).

    Exactly one of `application` and `error` is non-None; `error` is a plain sentence
    already worded for a human, which both callers hand back as the body of a 400.

    CALLERS
      * afc_sso/admin_api.py sso_applications POST      - staff creating one by hand.
      * afc_partner_apply/views_admin.py approve_application - the owner approving a
        submitted application, passing the fields as the review screen left them.

    EVERY VALUE IS RE-VALIDATED HERE even when the caller has already checked it. The
    approval path validated the applicant's URIs at SUBMIT time, days earlier, and an
    owner can edit them on the review screen in between; re-running the policy at the
    moment of the write is what makes "a provisioned application always satisfies the
    policy" true rather than merely likely.

    `toggles` is a dict of {share_* field: bool}, applied only for keys in
    SSO_FIELD_TOGGLES. It exists for the APPROVAL path, where granting data is part of
    the same decision. The staff create endpoint passes nothing and every toggle stays
    False, which is the older and stricter behaviour: create first, grant deliberately
    afterwards from the edit form.

    THE PLAINTEXT SECRET IS RETURNED AND NEVER STORED. django-oauth-toolkit hashes
    client_secret on save (ClientSecretField.pre_save), so it is generated here rather
    than left to the model default: by the time create() returns, the model's own default
    would already have been hashed away and there would be nothing to hand back.
    """
    name = (name or "").strip()
    if not name:
        return None, None, "Partner name is required."

    cleaned_redirect_uris, err = _clean_redirect_uris(redirect_uris)
    if err:
        return None, None, err

    # Optional: a partner only needs these if it sends players to the end session
    # endpoint, and the same policy applies because it is the same "AFC sends a player to
    # a partner URL" problem.
    cleaned_post_logout, err = _clean_redirect_uris(
        post_logout_redirect_uris, required=False, label="post-logout redirect URI")
    if err:
        return None, None, err

    # display_name is what the PLAYER reads on the consent screen, so it must never be
    # empty; fall back to the internal name rather than showing them a blank sentence.
    cleaned_display_name = (display_name or "").strip() or name

    cleaned_logo_url, err = _clean_url(logo_url, "Logo URL")
    if err:
        return None, None, err
    cleaned_homepage_url, err = _clean_url(homepage_url, "Homepage URL")
    if err:
        return None, None, err
    # The webhook is the one URL AFC'S OWN SERVER fetches, so it goes through the stricter
    # outbound check (public address only), not the browser-facing one the other two use.
    cleaned_webhook_url, err = _clean_outbound_url(deletion_webhook_url, "Deletion webhook URL")
    if err:
        return None, None, err

    secret = generate_client_secret()

    application = Application.objects.create(
        name=name,
        display_name=cleaned_display_name,
        logo_url=cleaned_logo_url,
        homepage_url=cleaned_homepage_url,
        deletion_webhook_url=cleaned_webhook_url,
        redirect_uris=cleaned_redirect_uris,
        post_logout_redirect_uris=cleaned_post_logout,
        client_secret=secret,
        # ── Protocol shape, fixed by AFC and not admin-editable ──
        # CONFIDENTIAL + authorization-code + RS256 is the only combination AFC supports:
        # a server-side partner exchanging a code (with PKCE, see settings PKCE_REQUIRED)
        # for an RS256-signed ID token. Implicit and password grants are not offered.
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        algorithm=Application.RS256_ALGORITHM,
        # Audit trail: which AFC staff member provisioned this partner. `user` on the
        # library's Application model is a plain nullable FK with no other meaning here.
        user=created_by,
        # status defaults to "active" and every share_* toggle defaults to False, so a
        # brand-new partner can sign a player in and learn nothing about them but that
        # the sign-in succeeded.
    )

    # ── The logo AFC hosts itself, when one was supplied ──
    # Saved AFTER create() rather than passed into it, because an ImageField wants a
    # saved row to attach a file to. Failing to store a logo must never lose the
    # application: a partner with no logo simply renders without one (authorize.html
    # tests resolved_logo_url() before drawing an <img>).
    if logo_file is not None:
        application.logo = logo_file
        application.save(update_fields=["logo"])

    # ── Data grants, only on the approval path ──
    if toggles:
        granted = [f for f in SSO_FIELD_TOGGLES if toggles.get(f)]
        if granted:
            for field in granted:
                setattr(application, field, True)
            application.save(update_fields=granted)

    return application, secret, None
