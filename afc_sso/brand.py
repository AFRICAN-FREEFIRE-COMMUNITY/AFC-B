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
#     GET /sso/brand/logo/<size>.png  the mark itself, at a size that exists
#
#     Both are PUBLIC and unauthenticated on purpose. A partner needs the mark to draw the
#     button that begins the sign-in, which is by definition before anyone has signed in.
#
# ON RESOLUTION, which is a house rule and not a detail
#     The mark AFC holds is a 500x500 PNG, and there is no vector of it anywhere in either
#     repository. Every size served here is a DOWNSCALE of that one file
#     (afc_organizers/assets/afc-logo.png, sha256 3aeb8b14...), generated with Lanczos and
#     committed under brand_assets/. Nothing is ever drawn above its own resolution, which
#     is why the list stops at 500 rather than offering a rounder 512.
#
#     `logo.source_resolution` is published so a partner can see the ceiling rather than
#     discover it by producing a soft banner. If a real vector is ever produced, add an
#     `svg` key here and the page at /brand picks it up.
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
    ],
    "dont": [
        "Do not stretch, rotate, recolour or add effects to the mark.",
        "Do not draw the mark larger than 500px; there is no vector, so it will go soft.",
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
                  "mark": {"500": "<url>", "256": "<url>", ...}},
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
                "format": "png",
                # Published so a partner can see the ceiling rather than discover it by
                # producing a soft banner. There is no vector of the AFC mark.
                "source_resolution": 500,
                "mark": _logo_urls(request),
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
