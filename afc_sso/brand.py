# ──────────────────────────────────────────────────────────────────────────────
# AFC's brand kit, published for partners.
#
# WHY THIS EXISTS (owner, 2026-08-30)
#     V-ENT put a "Sign in with AFC" button on their login page and it came out as a bare
#     wide button reading "Continue with African Free Fire Community", with no mark, sitting
#     next to a compact Google button that had both. The owner: "you dont send brand kit
#     with the api? logos and the all ... it should simply show cancelled ... so it now
#     looks like this on their page and its ugly".
#
#     They were right, and the cause was not on their side. AFC published NOTHING. The logo
#     traffic ran one way only: partners upload THEIR mark to us (afc_sso/provisioning.py),
#     and AFC served its own nowhere. The only discovery document is the stock OIDC one,
#     and OIDC has no field for a provider's own logo. So a partner had nothing to draw and
#     no short name, and fell back to the full legal name as a text label.
#
#     Worth recording plainly: frontend public/brands/v-ent.svg exists because V-ENT
#     publishes a kit. We had been consuming theirs and offering none back.
#
# WHAT A PARTNER GETS
#     GET /sso/brand/                 the kit as JSON: names, the button label, colours,
#                                     logo urls, and the usage rules
#     GET /sso/brand/logo.svg         the mark as VECTOR. What to use unless a raster is
#                                     required. ?on=dark serves the light-wordmark variant
#     GET /sso/brand/logo/<size>.png  the mark as a raster, at a size that exists
#
#     Both are PUBLIC and unauthenticated on purpose. A partner needs the mark to draw the
#     button that begins the sign-in, which is by definition before anyone has signed in.
#
# ON RESOLUTION, which is a house rule and not a detail
#     UNTIL 2026-08-30 this section said there was no vector of the AFC mark and that a
#     partner must not draw it above 500px. There is one now, and the ceiling is gone.
#
#     Nobody produced an original: AFC has never held one. tools/trace_afc_mark.py TRACES
#     the 500x500 PNG (afc_organizers/assets/afc-logo.png, sha256 3aeb8b14...), feeding the
#     tracer the raw coverage gradient rather than a mask hardened at source resolution, so
#     the antialiasing carries the sub-pixel edge into the curves. The result is measured
#     against the source by rendering it in Chrome, and it is only written when it agrees:
#     0.989 on the silhouette and 0.997 on the green, both against a 0.985 floor. The score
#     and the source hash are published beside it in afc-mark.provenance.json, and
#     afc_sso/tests/test_brand_vector.py re-measures on every CI run.
#
#     SO: the svg is what a partner should reach for, at any size. The PNGs stay, because a
#     link preview, an email client and an OG card all still want a raster, and each is
#     still a DOWNSCALE of that same 500px file. `logo.raster_source_resolution` is
#     published so a partner choosing a PNG can see the ceiling that still applies to THOSE
#     rather than discover it by producing a soft banner.
#
# CONNECTS TO
#     afc_sso/urls.py                     mounts both views under /sso/brand/
#     afc_partner_apply/views_public.py   the integration guide points partners here
#     frontend app/(root)/brand/page.tsx  the human-readable version of this same data
# ──────────────────────────────────────────────────────────────────────────────
import os

from django.conf import settings
from django.http import FileResponse, Http404
from django.views.decorators.cache import cache_control
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "brand_assets")

# Every size that exists on disk. 500 is the SOURCE; the rest are downscales of it. Adding
# a size here without generating the file would publish a url that 404s, so the endpoint
# reads the directory rather than trusting this list blindly (see _logo_urls).
LOGO_SIZES = (500, 256, 128, 64, 32)

# The vector, and its light-wordmark twin. Traced from the same 500px file by
# tools/trace_afc_mark.py, which refuses to write either one unless the render agrees with
# the source. See afc-mark.provenance.json beside them for the score and the source hash.
SVG_FILE = "afc-mark.svg"
SVG_ON_DARK_FILE = "afc-mark-on-dark.svg"

# The default mark's wordmark ("AFRICAN FREEFIRE COMMUNITY") is near-black, so on a dark
# surface it disappears and the mark reads as the letters alone. The on-dark variant is the
# same paths with that half in the site's off-white. A partner's login page is usually dark,
# so this is a real choice and not a nicety.
SVG_VARIANTS = {"light": SVG_FILE, "dark": SVG_ON_DARK_FILE}

# The name a partner should show. Short deliberately: "African Free Fire Community" set as
# a button label is what made V-ENT's row look broken next to Google's.
BRAND_NAME = "AFC"
BRAND_FULL_NAME = "African Free Fire Community"
BUTTON_LABEL = "Continue with AFC"

# Taken from the frontend's own tokens (app/globals.css) and converted to sRGB by Chrome's
# canvas, not by hand, so what a partner paints matches what the site paints.
#   --primary  oklch(0.624 0.170 149.09)
#   --gold     oklch(0.79 0.17 83.63)
#   --background (dark) oklch(0.141 0.005 285.823)
COLORS = {
    "primary": {
        "hex": "#15a249",
        "rgb": "rgb(21, 162, 73)",
        "oklch": "oklch(0.624 0.170 149.09)",
        "use": "The AFC green. Buttons, links, headings. One committed hue, used sparingly.",
    },
    "gold": {
        "hex": "#eeaf00",
        "rgb": "rgb(238, 175, 0)",
        "oklch": "oklch(0.79 0.17 83.63)",
        "use": "Accent for prizes, tiers and winners. Never a second brand colour.",
    },
    "surface_dark": {
        "hex": "#09090b",
        "rgb": "rgb(9, 9, 11)",
        "oklch": "oklch(0.141 0.005 285.823)",
        "use": "The dark page surface AFC is drawn on. Not pure black.",
    },
}

USAGE = {
    "do": [
        "Show the mark beside the words, at the same visual weight as other sign-in buttons.",
        f'Label the button "{BUTTON_LABEL}".',
        "Keep clear space around the mark of at least a quarter of its width.",
        "Use the mark on a dark or light surface with real contrast behind it.",
        "Prefer the svg. It is the same drawing at every size, and it is what the button "
        "should use.",
        "On a dark surface take the on-dark svg. The wordmark in the default mark is "
        "near-black and vanishes against one.",
    ],
    "dont": [
        "Do not stretch, rotate, recolour or add effects to the mark.",
        "Do not draw a PNG above its own size; 500px is the largest that exists and "
        "anything above it goes soft. Use the svg instead, at any size.",
        "Do not use the full name as a button label; it makes the button wider than every "
        "other provider's.",
        "Do not imply AFC endorses your product. The button means a player can sign in, "
        "nothing more.",
    ],
    "min_size_px": 16,
    "clear_space_ratio": 0.25,
}


def _absolute(request, path):
    """An absolute URL for `path`, built from the request so this is correct on the local
    box and in production without a second setting to keep in sync."""
    return request.build_absolute_uri(path)


def _logo_urls(request):
    """The sizes that actually exist on disk, newest-largest first.

    Reads the directory rather than trusting LOGO_SIZES, so a missing file can never be
    published as a url that 404s for a partner.
    """
    urls = {}
    for size in LOGO_SIZES:
        if os.path.exists(os.path.join(_ASSETS_DIR, f"afc-mark-{size}.png")):
            urls[str(size)] = _absolute(request, f"/sso/brand/logo/{size}.png")
    return urls


def _svg_urls(request):
    """The vector, and the on-dark variant, when each exists on disk.

    Same reason _logo_urls reads the directory: a published url that 404s is worse for a
    partner than a key that is simply absent, because absent is something their code can
    branch on.
    """
    urls = {}
    if os.path.exists(os.path.join(_ASSETS_DIR, SVG_FILE)):
        urls["default"] = _absolute(request, "/sso/brand/logo.svg")
    if os.path.exists(os.path.join(_ASSETS_DIR, SVG_ON_DARK_FILE)):
        urls["on_dark"] = _absolute(request, "/sso/brand/logo.svg?on=dark")
    return urls


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def brand_kit(request):
    """AFC's brand kit: what to call us, what colour we are, and where the mark lives.

    PURPOSE: so a partner building "Sign in with AFC" can draw a button that looks like it
    belongs beside Google's, instead of guessing. See the module header for the button that
    prompted it.

    AUTH: none. A partner needs this to draw the button that STARTS a sign-in.

    REQUEST: no parameters.

    RESPONSE 200:
        {"name": "AFC",
         "full_name": "African Free Fire Community",
         "button_label": "Continue with AFC",
         "homepage_url": "...", "brand_page_url": "...",
         "colors": {"primary": {"hex", "rgb", "oklch", "use"}, "gold": {...},
                    "surface_dark": {...}},
         "logo": {"format": "png", "source_resolution": 500,
                  "mark": {"500": "<url>", "256": "<url>", ...},
                  "preferred": "svg",
                  "vector": {"default": "<url>", "on_dark": "<url>"}},
         "usage": {"do": [...], "dont": [...], "min_size_px": 16,
                   "clear_space_ratio": 0.25}}

    CONSUMED BY: partners directly, and the frontend /brand page, which renders this same
    data for a human to read.
    """
    site = getattr(settings, "FRONTEND_URL", "https://africanfreefirecommunity.com")
    return Response(
        {
            "name": BRAND_NAME,
            "full_name": BRAND_FULL_NAME,
            "button_label": BUTTON_LABEL,
            "homepage_url": site,
            "brand_page_url": f"{site}/brand",
            "colors": COLORS,
            "logo": {
                # `format`, `source_resolution` and `mark` describe the RASTER and are left
                # exactly as first published: a partner may already be reading them, and
                # repurposing a published key is how an integration breaks quietly.
                "format": "png",
                # The ceiling that still applies to the PNGs. Each is a downscale of one
                # 500x500 file, so above that they go soft.
                "source_resolution": 500,
                "mark": _logo_urls(request),
                # Added 2026-08-30, when the mark was finally traced to vector.
                "preferred": "svg",
                "vector": _svg_urls(request),
            },
            "usage": USAGE,
        }
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
# A brand mark changes about once a decade. A long cache keeps a partner's login page fast
# and keeps their traffic off this box; `public` because there is nothing per-user here.
@cache_control(public=True, max_age=60 * 60 * 24 * 30)
def brand_logo(request, size):
    """The AFC mark as a PNG, at one of the sizes brand_kit publishes.

    AUTH: none, for the same reason as brand_kit.

    RESPONSE 200: image/png. 404 for a size that does not exist, rather than the nearest
    one: silently returning a different size is how a partner ends up scaling our mark.

    Every file is a downscale of one 500x500 source. See the module header on resolution.
    """
    if size not in LOGO_SIZES:
        raise Http404("No AFC mark at that size.")

    path = os.path.join(_ASSETS_DIR, f"afc-mark-{size}.png")
    if not os.path.exists(path):
        raise Http404("No AFC mark at that size.")

    # FileResponse streams and sets Content-Length; the filename is what a partner gets if
    # they save it, so it says what the file is.
    return FileResponse(
        open(path, "rb"),
        content_type="image/png",
        filename=f"afc-mark-{size}.png",
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
# Same cache as the PNG: a brand mark changes about once a decade, and a partner's login
# page should not wait on this box to draw its button.
@cache_control(public=True, max_age=60 * 60 * 24 * 30)
def brand_logo_svg(request):
    """The AFC mark as VECTOR. What a partner should use unless a raster is required.

    PURPOSE: to remove the ceiling this endpoint's own sibling used to publish. Until
    2026-08-30 the largest AFC mark that existed anywhere was a 500x500 PNG, so the kit had
    to tell partners not to draw it bigger. tools/trace_afc_mark.py traced that file, and
    afc_sso/tests/test_brand_vector.py re-measures the result against it on every run.

    AUTH: none, for the same reason as brand_kit. This is drawn BEFORE anyone signs in.

    REQUEST: `?on=dark` for the light-wordmark variant. Anything else, including no
    parameter at all, serves the default. An unknown value is NOT an error here, unlike the
    PNG size: there are exactly two variants and falling back to the default draws a
    correct mark, whereas returning a PNG at a size the caller did not ask for silently
    changes how big our logo is on their page.

    RESPONSE 200: image/svg+xml. 404 only if the file is missing from the deploy, which
    would mean brand_kit is not publishing the url either (see _svg_urls).

    CONSUMED BY: partners directly, and the frontend /brand page, which offers it as the
    preferred download and renders it in the example button.
    """
    variant = "dark" if request.GET.get("on") == "dark" else "light"
    path = os.path.join(_ASSETS_DIR, SVG_VARIANTS[variant])
    if not os.path.exists(path):
        raise Http404("The AFC mark is not available as a vector on this deploy.")

    return FileResponse(
        open(path, "rb"),
        content_type="image/svg+xml",
        # The name a partner gets if they save it, so it says which variant they took.
        filename=SVG_VARIANTS[variant],
    )
