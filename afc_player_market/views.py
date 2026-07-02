from django.shortcuts import render

# Create your views here.


from rest_framework.decorators import api_view
from rest_framework.response import Response
import calendar  # monthrange() for the one-month expiry cap (add_one_month, below)
import json       # decode JSON-encoded list fields sent as multipart strings (_coerce_list)
import pycountry  # ISO 3166-2 subdivisions for the residential-state picker (location feature)
from datetime import datetime
# timezone is also imported lower in this module (line ~363) for the trial/invite
# expiry logic; we import it at the top too so create_recruitment_post (the first view
# in the file) can compute "today" for the one-active-post rule without depending on the
# later import. Re-importing the same name is harmless in Python.
from django.utils import timezone

from django.db.models import Q, Sum

from afc_auth.models import BannedPlayer, LoginHistory, Notifications
from afc_team.models import Team, TeamMembers
from .models import Country, DirectTrialInvite, PlayerReport, RecruitmentApplication, RecruitmentPost, RecruitmentPostImage, TrialChat, TrialChatMessage, TrialInvite
from afc_auth.views import send_email, validate_token
# Market-ban enforcement guard (feature "J-market-reporting"). Returns the active
# MarketBan blocking a user from acting on the market, or None. Used to block a banned
# poster (or a member of a banned team) before any post/apply/invite row is created.
# _is_market_moderator (same module) gates the admin-only surfaces â€” reused here to let
# AFC staff READ any trial chat (feature "K-admin-chat-read").
from .views_moderation import _active_market_ban, _is_market_moderator
from afc_tournament_and_scrims.models import TournamentPlayerMatchStats, TournamentTeamMatchStats


TRANSFER_WINDOW_STATUS = "OPEN"  # This can be dynamically set based on date or admin input


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Â§J  Player-market posting + tryout rules (feature "J-market-rules")
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# These two caps are surfaced to players on the user-facing market page
# (frontend/app/(user)/player-markets/page.tsx) via InfoTip rules copy, and are
# enforced here on the backend so the rules can't be bypassed by hitting the API
# directly.
#
# MAX_ACTIVE_POSTS â€” a user may have at most ONE active recruitment post at a time
#   (any type: a player-availability post OR a team-recruitment post). "Active" means
#   is_active=True AND post_expiry_date >= today. Enforced in create_recruitment_post
#   below. Close or let the current post expire before creating a new one.
#
# MAX_ACTIVE_TRIALS â€” a player may be in at most TWO active tryouts (trials) at once.
#   "Active" means a RecruitmentApplication in status TRIAL_ONGOING. Enforced in BOTH
#   places a player enters a trial: when a team INVITES an applicant (handle the team's
#   "INVITE" action) and when a player ACCEPTS a direct trial invite. Both checks read
#   this single constant so they can never drift apart.
MAX_ACTIVE_POSTS = 1
MAX_ACTIVE_TRIALS = 2


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Â§L  One-month expiry cap (feature "L-market-expiry-cap", 2026-06-10)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A recruitment post (TEAM_RECRUITMENT or PLAYER_AVAILABLE) may live AT MOST one
# calendar month before it auto-closes. There is NO new DB field and NO celery job:
# RecruitmentPost.is_active is already a @property (post_expiry_date >= today), so a
# post past its expiry already reads inactive (that lapse IS the auto-close). This cap
# just stops a user from setting post_expiry_date further than one month out, so the
# auto-close can never be pushed past a month.
#
# Enforced in BOTH write paths so the API can't be bypassed:
#   â€¢ create_recruitment_post: caps relative to TODAY (a brand-new post may last up to
#     one month from its creation day).
#   â€¢ edit_recruitment_post: caps relative to the post's OWN created_at, so editing
#     can't be used to extend a post past one month of total life.
# Existing far-future rows (2027, December, â€¦) are pulled back in line by the one-off
# backfill command afc_player_market/management/commands/clamp_post_expiries.py.
#
# Surfaced to users on frontend/app/(user)/player-markets/page.tsx: both date inputs
# carry min=today / max=(today+1 month) and a "Posts last up to 1 month" helper line,
# and both submit handlers re-check the bound before POSTing to create-recruitment-post.
def add_one_month(d):
    """Return d advanced by one calendar month, clamping the day to the target month
    length. Used to cap a recruitment post's expiry: a post may last at most one month.

    Day-clamp examples (no external dependency, pure calendar arithmetic):
      â€¢ 2026-01-15 â†’ 2026-02-15
      â€¢ 2026-01-31 â†’ 2026-02-28  (Feb has no 31st; clamp to the last day)
      â€¢ 2026-12-10 â†’ 2027-01-10  (December rolls the year forward)
    """
    y = d.year + (1 if d.month == 12 else 0)
    m = 1 if d.month == 12 else d.month + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Â§K  Country restriction (feature "K-country-gate", 2026-06-08)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A recruitment post can target a set of countries (RecruitmentPost.countries M2M).
# When that set is non-empty, only actors LOCATED in one of those countries may act:
#   â€¢ apply_to_team          â€” the APPLYING PLAYER must be located in the team post's countries
#   â€¢ invite_player_to_trial â€” the RECRUITER must be located in the player post's countries
#
# "Located" = the actor's most-recent login country (LoginHistory.country, an ISO-2 code
# captured per login â€” the freshest location signal we have, chosen by the project owner),
# falling back to User.country (the signup-IP country, also a code). We compare against the
# post's Country rows by CODE first, then NAME (case-insensitive), because this codebase
# holds three country representations that don't always align: User/LoginHistory store
# ISO-2 codes ("NG"), the Country table has name+code, and the FE picker sends names.
# Matching on both makes the gate robust to that drift.
#
# Fail policy: ENFORCE when we can locate the actor (block when their country isn't in the
# set). If the actor has NO location signal at all (no login history AND empty User.country
# â€” rare, since signup captures it) we ALLOW, to avoid false-blocking a legit user. An empty
# post.countries means "open to everyone" â†’ always allow.
#
# Connects to: apply_to_team + invite_player_to_trial (callers below); LoginHistory/User in
# afc_auth (location source); RecruitmentPost.countries (the target set, set in
# create_recruitment_post). The FE shows the same restriction on the post card/dialogs so a
# user sees it before they try to apply (player-markets/page.tsx).

def _resolve_countries(values):
    """Resolve a list of strings (country NAMES or ISO-2 CODES) to Country rows.

    Tolerant by design: the FE country picker sends names while other call sites may send
    codes, so we match each value by code OR name, case-insensitive, de-duped. Used by
    create_recruitment_post / edit_recruitment_post to populate RecruitmentPost.countries.
    """
    out, seen = [], set()
    for v in values or []:
        v = (v or "").strip()
        if not v:
            continue
        c = Country.objects.filter(Q(code__iexact=v) | Q(name__iexact=v)).first()
        if c and c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


def _actor_country_code(user):
    """The actor's current location as a country code/name string.

    Most-recent LoginHistory.country (per-login IP geolocation, the owner-chosen signal),
    else User.country (signup IP). Returns '' when neither is known.
    """
    latest = (
        LoginHistory.objects.filter(user=user)
        .exclude(country__isnull=True)
        .exclude(country="")
        .order_by("-created_at")
        .values_list("country", flat=True)
        .first()
    )
    return (latest or getattr(user, "country", "") or "").strip()


def _country_gate(user, post):
    """Return an error Response if `user` is NOT allowed to act on `post` by country, else
    None. Open post (no target countries) or un-locatable actor â†’ allowed (see fail policy
    in the Â§K header)."""
    target = list(post.countries.all())
    if not target:
        return None  # post is open to everyone
    actor = _actor_country_code(user)
    if not actor:
        return None  # cannot determine the actor's location â€” do not false-block
    actor = actor.upper()
    allowed = {c.code.upper() for c in target if c.code} | {c.name.upper() for c in target if c.name}
    if actor in allowed:
        return None
    names = ", ".join(c.name for c in target)
    return Response(
        {"message": (
            f"This post is open only to players located in: {names}. "
            "Your account location (from your most recent login) does not match."
        )},
        status=403,
    )


# Allowed gameplay-video hosts (owner 2026-06-12: video by LINK, not upload). A strict host
# allowlist - not a format check - so a post can never carry a link that the FE would try to embed
# from an arbitrary site. Mirrors ALLOWED_HOSTS in frontend lib/videoEmbed.ts - keep the two in
# sync, and when adding a platform also update _VIDEO_PLATFORMS_LABEL below + the FE helper text.
_VIDEO_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am",
}

# Human-readable platform list for error messages (owner 2026-06-12: "tell them the platform
# links we are accepting"). The FE shows the same list in the form helper text.
_VIDEO_PLATFORMS_LABEL = "YouTube, TikTok or Instagram"


def _validate_video_url(raw):
    """Normalize + allowlist-check an optional gameplay video link.

    Returns (normalized_url, None) on success - "" stays "" (the field is optional) - or
    (None, error_message) when the value is present but not an http(s) link on an accepted
    platform (see _VIDEO_HOSTS). Used by create_recruitment_post + edit_recruitment_post
    (player posts only)."""
    from urllib.parse import urlparse

    url = (raw or "").strip()
    if not url:
        return "", None
    # Tolerate a missing scheme ("youtube.com/watch?v=..."): default to https.
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host not in _VIDEO_HOSTS:
        return None, f"video_url must be a {_VIDEO_PLATFORMS_LABEL} link."
    if len(url) > 300:
        return None, "video_url is too long (max 300 characters)."
    return url, None


def _resolve_video_url(url):
    """Resolve a TikTok SHARE SHORT link to its canonical video URL so the FE can EMBED it instead
    of linking out (owner 2026-06-30).

    TikTok's share button hands out vm.tiktok.com / vt.tiktok.com / tiktok.com/t/<code> short links
    that carry NO /video/<id> in the path, so lib/videoEmbed.parseVideoEmbed can't build a player and
    falls back to an outbound link. Following the redirect yields the real
    tiktok.com/@user/video/<id> URL (which DOES embed). We strip the tracking query so the stored URL
    stays clean + under the 300-char cap. Everything else (full TikTok/YouTube/Instagram links) passes
    through unchanged. Fail-soft: any network error / non-redirect returns the ORIGINAL url (the FE
    then just renders the plain link, same as before).
    """
    if not url:
        return url
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        is_short = host in ("vm.tiktok.com", "vt.tiktok.com") or (
            host.endswith("tiktok.com") and "/t/" in urlparse(url).path
        )
        if not is_short:
            return url
        import requests
        resp = requests.head(
            url, allow_redirects=True, timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AFCBot/1.0)"},
        )
        final = (resp.url or "").split("?")[0]  # canonical, drop ?_t=... tracking params
        # Only accept a resolved URL that actually points at a video (has /video/<id>); else keep
        # the original (e.g. a profile short link with no specific video can't be embedded).
        if final and "/video/" in final and len(final) <= 300:
            return final
    except Exception:
        pass
    return url


# ==============================================================================
#  "Player Available Post" feature set (owner 2026-06-29)
# ==============================================================================
# Four player-post upgrades share the helpers below:
#   2) up to 3 in-game profile SCREENSHOTS per post  (RecruitmentPostImage)
#   3) optional RESIDENTIAL STATE locked to the player's country (ISO 3166-2)
#   1/4) phone combobox + UID display are pure serialization/FE changes.
#
# Because a post can now carry image files, the create/edit forms post multipart
# instead of JSON. In a multipart body DRF's request.data is a QueryDict, so a list
# field (country_codes, roles_needed) arrives either as repeated keys OR as a single
# JSON-encoded string. _coerce_list normalises ALL three shapes (real list from a JSON
# body, JSON string from multipart, repeated keys) so the existing JSON callers and the
# new multipart callers both work through one code path.

# Per-image raw-upload cap (owner 2026-06-29). NOTE: settings.py sets NEITHER
# DATA_UPLOAD_MAX_MEMORY_SIZE NOR FILE_UPLOAD_MAX_MEMORY_SIZE, so Django's defaults apply
# (2.5 MB / 2621440 bytes) and -- critically -- those defaults do NOT cap uploaded FILE size
# (DATA_UPLOAD_* excludes file fields; FILE_UPLOAD_* only decides memory-vs-tempfile). The
# existing image path (afc_auth.upload_esport_image) has NO explicit byte check at all and
# relies solely on normalize_image_upload downscaling to ~150-300 KB. We add a sane 10 MB
# raw guard here (a screenshot is well under that; a 12 MP phone photo is 8-15 MB) so three
# images can't be abused, then normalise each one down exactly like the esport image.
MAX_POST_IMAGE_BYTES = 10 * 1024 * 1024   # 10 MB per screenshot, pre-normalisation
MAX_POST_IMAGES = 3                        # owner cap: at most 3 screenshots per post


def _coerce_list(value):
    """Return a clean list[str] from a value that may be a real list (JSON body), a
    JSON-encoded string (multipart), or a single scalar string. None -> None so callers can
    distinguish "field absent" from "field present but empty". Used for country_codes /
    roles_needed in create/edit now that those forms can post multipart."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
        return [s] if s else []
    return [value]


def _post_files(request):
    """The uploaded screenshot files for a post, read from EITHER the repeated `images`
    multipart key OR a single `image` key (tolerant to both FE shapes). Empty list on a
    JSON request (no files). Consumed by create_recruitment_post + edit_recruitment_post."""
    if hasattr(request, "FILES"):
        return request.FILES.getlist("images") or request.FILES.getlist("image")
    return []


def _validate_post_image_files(files):
    """Pure validation of an uploaded screenshot batch: at most MAX_POST_IMAGES files, each
    <= MAX_POST_IMAGE_BYTES. Returns an error string or None. Called BEFORE the post row is
    saved (create) so a bad batch never leaves an orphan post, and reused inside
    _save_post_images so the rules live in one place."""
    files = list(files or [])
    if len(files) > MAX_POST_IMAGES:
        return f"You can attach at most {MAX_POST_IMAGES} screenshots per post."
    for f in files:
        if getattr(f, "size", 0) and f.size > MAX_POST_IMAGE_BYTES:
            return "Each screenshot must be 10 MB or smaller."
    return None


def _save_post_images(post, files, *, replace=False):
    """Persist up to MAX_POST_IMAGES screenshots for `post`.

    Each file is size-checked (MAX_POST_IMAGE_BYTES via _validate_post_image_files) then run
    through afc_auth.image_utils.normalize_image_upload(force_jpeg=True) -- the SAME normaliser
    the esport-image upload uses (HEIC->JPEG + downscale), but WITHOUT the face-detection gate
    (a screenshot is a game capture, not a portrait).

    replace=True deletes the post's existing images first (edit "replace the gallery"
    semantics). Returns (saved_count, error_message|None). Validation happens before anything
    is written, so a rejected request leaves the gallery untouched.
    """
    # Local import keeps the heavy Pillow/normalise path out of module import (mirrors how
    # upload_esport_image imports it lazily).
    from afc_auth.image_utils import normalize_image_upload

    files = list(files or [])
    if not files:
        return 0, None
    err = _validate_post_image_files(files)
    if err:
        return 0, err

    if replace:
        post.images.all().delete()

    # Continue ordering after any image kept on the post (0 for a fresh/replaced gallery).
    start = post.images.count()
    saved = 0
    for idx, f in enumerate(files):
        normalized = normalize_image_upload(f, force_jpeg=True)
        RecruitmentPostImage.objects.create(post=post, image=normalized, order=start + idx)
        saved += 1
    return saved, None


def _serialize_post_images(post, request):
    """Ordered list of absolute screenshot URLs for `post`. Absolute (request.build_absolute_uri)
    so the image loads from the API host even though the SPA runs on a different origin -- the
    same reason get_post_details builds an absolute team_logo URL. Used by every post serializer."""
    out = []
    for img in post.images.all().order_by("order", "id"):
        if not img.image:
            continue
        try:
            url = request.build_absolute_uri(img.image.url)
        except Exception:
            url = img.image.url
        out.append({"id": img.id, "url": url, "order": img.order})
    return out


def _player_avatar_url(player, request):
    """Best-effort avatar URL for a player User: their UserProfile.profile_pic, else their
    discord_avatar, else None.

    WHY this helper exists: profile_pic lives on UserProfile (a ForeignKey from User), NOT on the
    User model, so get_post_details's previous expression `post.player.profile_pic` raised
    AttributeError and 500-ed for EVERY player post. That broke the player edit-form prefill
    (frontend openEditPost -> GET /player-market/post-details/) that this feature's Edit Player
    flow relies on, so it had to be made correct + safe here. Returns an ABSOLUTE URL (API host),
    matching how team_logo is built in the same serializer. Never raises."""
    if not player:
        return None
    from afc_auth.models import UserProfile
    prof = UserProfile.objects.filter(user=player).order_by("profile_id").first()
    if prof and prof.profile_pic:
        try:
            return request.build_absolute_uri(prof.profile_pic.url)
        except Exception:
            return prof.profile_pic.url
    return getattr(player, "discord_avatar", None) or None


def _pycountry_alpha2(raw):
    """Normalise a country code OR name to an ISO-3166-1 alpha-2 code ("NG"), or "" if it
    can't be resolved. _actor_country_code may return a code ("NG") or a name; the FE state
    filter passes a country NAME. pycountry.countries.lookup handles alpha-2/3, name, official
    and common names; we fall back to the local Country table (name/code) for anything it
    misses. Feeds _subdivisions_for (the residential-state picker)."""
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return pycountry.countries.lookup(s).alpha_2
    except LookupError:
        pass
    row = Country.objects.filter(Q(code__iexact=s) | Q(name__iexact=s)).first()
    if row and row.code:
        try:
            return pycountry.countries.lookup(row.code).alpha_2
        except LookupError:
            return ""
    return ""


def _subdivisions_for(country_alpha2):
    """[{value, label}] of a country's ISO 3166-2 subdivisions (states/provinces/regions),
    sorted by name. value == label == the subdivision NAME on purpose: RecruitmentPost.
    residential_state stores that name verbatim (it is what the card shows and the recruiter
    filter compares), so the ISO subdivision CODE is never needed in the UI. Returns [] for an
    unknown/empty country. Consumed by location_subdivisions + my_market_context."""
    if not country_alpha2:
        return []
    subs = pycountry.subdivisions.get(country_code=country_alpha2) or []
    items = sorted(subs, key=lambda sd: sd.name)
    return [{"value": sd.name, "label": sd.name} for sd in items]


def _country_display_name(alpha2):
    """A clean, human-friendly display name for an ISO-3166-1 alpha-2 code ("TZ" -> "Tanzania").

    pycountry's `.name` is the OFFICIAL / inverted form for several countries ("Tanzania, United
    Republic of", "Congo, The Democratic Republic of the"), which reads as machine output in the
    recruiter Country filter + the detected-country label. Prefer, in order:
      1. the local Country table's name (the curated label the rest of the site already shows),
      2. pycountry's common_name (e.g. "Tanzania", "Russia"),
      3. the part of `.name` before the first comma (strips the inverted ", X of" suffix),
      4. the raw `.name` as a last resort.
    Returns "" for an empty/unresolvable code. Consumed by _actor_country_name (persisted onto
    RecruitmentPost.residential_country) and my_market_context's country_name echo."""
    code = (alpha2 or "").strip().upper()
    if not code:
        return ""
    row = Country.objects.filter(code__iexact=code).first()
    if row and row.name:
        return row.name
    try:
        country = pycountry.countries.lookup(code)
    except LookupError:
        return ""
    common = getattr(country, "common_name", None)
    if common:
        return common
    name = getattr(country, "name", "") or ""
    return name.split(",")[0].strip() or name


def _actor_country_name(user):
    """The actor's detected country as a clean display NAME ("Nigeria"), or "" if unresolved.

    Resolves _actor_country_code(user) (most-recent login country, else User.country) to an
    alpha-2 code, then to a human-friendly name via _country_display_name (which avoids pycountry's
    inverted official forms). This is the value persisted into RecruitmentPost.residential_country
    on create/edit (country-level location-filter refinement, owner 2026-06-29) so the recruiter's
    players-tab Country filter matches on a single canonical string. Server-derived only -- never
    taken from the client."""
    return _country_display_name(_pycountry_alpha2(_actor_country_code(user)))


@api_view(["POST"])
def create_recruitment_post(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # â”€â”€ MARKET-BAN GUARD (feature "J-market-reporting") â”€â”€
    # A banned player (or a member of a banned team) cannot create a market post.
    # Reporting is NOT gated by this â€” only the create/apply/invite actions are.
    market_ban = _active_market_ban(user)
    if market_ban:
        return Response(
            {"message": f"You are banned from the player market. Reason: {market_ban.reason}"},
            status=403,
        )

    # â”€â”€ ONE-ACTIVE-POST RULE (feature "J-market-rules", J1) â”€â”€
    # A user may have at most MAX_ACTIVE_POSTS (=1) active recruitment post at a time,
    # counting BOTH a player-availability post and a team-recruitment post.
    #
    # "Active" == NOT expired. NOTE: RecruitmentPost.is_active is a model @property
    # (post_expiry_date >= today), NOT a queryable DB field â€” a `.filter(is_active=True)`
    # raises FieldError. So "active" is enforced purely as post_expiry_date >= today,
    # which is exactly what that property means. An expired post no longer counts, so the
    # user can post again once their old one lapses.
    #
    # Ownership: every post sets created_by=user; player posts ALSO set player=user.
    # We match on created_by OR player so a player who already has a live availability
    # post (or a team owner who already has a live recruitment post) is blocked. This
    # mirrors how the FE filters "My Posts" (player == in_game_name / team == own team).
    #
    # Surfaced to users on the Create Post flow + the rules summary on
    # frontend/app/(user)/player-markets/page.tsx (player_market.create_post_rule /
    # player_market.rules_summary InfoTips). The FE shows a friendly toast on this 400.
    today = timezone.now().date()
    active_posts = RecruitmentPost.objects.filter(
        Q(created_by=user) | Q(player=user),
        post_expiry_date__gte=today,
    ).count()
    if active_posts >= MAX_ACTIVE_POSTS:
        return Response(
            {
                "message": (
                    "You already have an active post. You can only have one active "
                    "post at a time. Close or let your current post expire before "
                    "creating a new one."
                )
            },
            status=400,
        )

    data = request.data

    try:
        post_type = data.get("post_type")
        country_code = data.get("country_code")
        expiry = data.get("post_expiry_date")

        if not post_type or not expiry:
            return Response({"message": "post_type and post_expiry_date are required"}, status=400)

        # â”€â”€ ONE-MONTH EXPIRY CAP (feature "L-market-expiry-cap") â”€â”€
        # Parse the user-set expiry, then bound it: a post may last AT MOST one calendar
        # month before it auto-closes (RecruitmentPost.is_active = post_expiry_date >=
        # today). For a brand-new post the window is measured from TODAY, so the expiry
        # must sit in [today, add_one_month(today)]. A past date is rejected too (an
        # already-expired post would be pointless, reading inactive immediately).
        # The FE date input enforces the same min/max, but we re-check here so a direct
        # API call can't slip a 2027 expiry past the cap.
        today = timezone.now().date()
        try:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            return Response({"message": "Invalid date format. Use YYYY-MM-DD."}, status=400)
        if expiry_date < today:
            return Response({"message": "Post expiry cannot be in the past."}, status=400)
        if expiry_date > add_one_month(today):
            return Response({"message": "Post expiry must be within 1 month from today."}, status=400)

        # ðŸŒ Get country
        country = None
        if country_code:
            country = Country.objects.filter(code=country_code).first()
            if not country:
                return Response({"message": "Invalid country code"}, status=400)

        # Build the post in memory and set EVERY field BEFORE the first save(), so a
        # request missing an optional field can never leave a half-written orphan row.
        # (The old code create()d the row first, then set fields + saved again; if an
        # optional NOT NULL text column was missing it 500'd on the second save and left
        # a partial row behind, which would also wrongly count against the one-post cap.)
        post = RecruitmentPost(
            post_type=post_type,
            country=country,
            post_expiry_date=expiry_date,  # already parsed + capped above (one-month rule)
            created_by=user,
        )

        # ---------------- PLAYER POST ----------------
        if post_type == "PLAYER_AVAILABLE":
            post.player = user
            # A player who is ALREADY on a team can't advertise that they're "available" — they have
            # a team (owner 2026-06-30: "it shouldn't allow those in a team create"). They must leave
            # first. Mirrors the apply-to-post guard (which blocks in-team appliers). Returns a CLEAR
            # 400 so the FE shows WHY instead of a generic "Failed to create post". Covers both a
            # rostered member (TeamMembers.member) and a team owner.
            if (TeamMembers.objects.filter(member=user).exists()
                    or Team.objects.filter(team_owner=user).exists()):
                return Response(
                    {"message": "You're already in a team, so you can't post that you're available. "
                                "Leave your team first, then create the post."},
                    status=400,
                )
            # COMPULSORY (owner 2026-06-12): the mobile device the player currently plays on.
            # Recruiters factor device performance into trial decisions, so a player post
            # without it is rejected outright. Shown on the post card + detail dialog.
            mobile_device = (data.get("mobile_device") or "").strip()
            if not mobile_device:
                return Response(
                    {"message": "mobile_device is required: tell teams the phone you currently play on."},
                    status=400,
                )
            post.mobile_device = mobile_device[:80]  # column cap; UI enforces the same limit
            # OPTIONAL gameplay video link, allowlist-validated (YouTube/TikTok only).
            video_url, video_err = _validate_video_url(data.get("video_url"))
            if video_err:
                return Response({"message": video_err}, status=400)
            # Resolve TikTok short links to the embeddable canonical URL (owner 2026-06-30).
            post.video_url = _resolve_video_url(video_url)
            # OPTIONAL residential state (feature 3). Free CharField storing the ISO-3166-2
            # subdivision NAME the FE picker emits; blank when the player skips it. Trimmed to
            # the column cap. Not validated against pycountry here (the FE locks the picker to
            # the player's country list), so a custom value is tolerated rather than 500-ing.
            post.residential_state = (data.get("residential_state") or "").strip()[:120]
            # OPTIONAL residential COUNTRY (refinement): DERIVED server-side from the player's
            # detected location, and set ONLY when a state was given (the two are bound: the form
            # locks the state picker to the player's own country). Powers the recruiter country
            # filter. Never trusts a client-supplied country.
            post.residential_country = _actor_country_name(user) if post.residential_state else ""
            # Coalesce optional text fields to "" : these columns are NOT NULL with no
            # default, so a missing value (None) would raise IntegrityError on save.
            post.primary_role = data.get("primary_role") or ""
            post.secondary_role = data.get("secondary_role") or ""
            post.availability_type = data.get("availability_type") or ""
            post.additional_info = data.get("additional_info") or ""

            # Screenshots (feature 2): validate the batch BEFORE the post row is saved so a bad
            # upload never leaves an orphan post (and never wrongly counts against the one-post
            # cap). Files come from the multipart `images` key; a JSON request has none.
            image_files = _post_files(request)
            img_err = _validate_post_image_files(image_files)
            if img_err:
                return Response({"message": img_err}, status=400)

            post.save()  # single INSERT
            # Now that the post has a PK, persist the screenshots (normalised like the esport image).
            _save_post_images(post, image_files)

            # Target countries (the restriction set). The FE country picker sends a list of
            # country NAMES under "country_codes" (legacy key name); older callers used
            # "country_names"/codes. _resolve_countries matches either by name OR ISO-2 code,
            # so the selection actually persists (previously dropped â€” FE key never matched
            # the BE key, leaving the M2M empty). This M2M drives the country gate
            # (_country_gate) and the FE "Open to" display. Mirror the first into the legacy
            # single `country` FK so existing readers keep working. _coerce_list normalises the
            # value whether it arrives as a JSON array (JSON body) or a JSON string (multipart).
            target = _resolve_countries(
                _coerce_list(data.get("countries"))
                or _coerce_list(data.get("country_names"))
                or _coerce_list(data.get("country_codes"))
            )
            if target:
                post.countries.set(target)
                if not post.country:
                    post.country = target[0]
                    post.save(update_fields=["country"])

        # ---------------- TEAM POST ----------------
        elif post_type == "TEAM_RECRUITMENT":
            try:
                team = Team.objects.get(team_owner=user)
            except Team.DoesNotExist:
                team = None
            if not team:
                return Response({"message": "You must own a team to create a recruitment post"}, status=400)
            post.team = team
            # roles_needed is a JSON list column; _coerce_list keeps it a real list whether the
            # team form posts JSON (array) or multipart (JSON string), then None stays None.
            post.roles_needed = _coerce_list(data.get("roles_needed"))  # JSON (nullable)
            post.minimum_tier_required = data.get("minimum_tier_required") or ""
            post.commitment_type = data.get("commitment_type") or ""
            post.recruitment_criteria = data.get("recruitment_criteria") or ""
            post.save()  # single INSERT

            # Target countries â€” same tolerant resolution as the player branch (see comment
            # above). Drives the country gate + FE "Open to" display for team posts.
            target = _resolve_countries(
                _coerce_list(data.get("countries"))
                or _coerce_list(data.get("country_names"))
                or _coerce_list(data.get("country_codes"))
            )
            if target:
                post.countries.set(target)
                if not post.country:
                    post.country = target[0]
                    post.save(update_fields=["country"])

        else:
            return Response({"message": "Invalid post_type"}, status=400)

        return Response({
            "message": "Recruitment post created successfully",
            "post_id": post.id
        }, status=201)

    except Exception as e:
        return Response({"message": str(e)}, status=500)
    

@api_view(["GET"])
def get_recruitment_posts(request):

    # Expired posts simply disappear from the market (owner 2026-07-02) - both team + player posts.
    # post_expiry_date is a DateField; keep posts whose expiry is today or later. The owner's "My Posts"
    # (get_posts_related_to_me) is NOT filtered so they can still see + renew their own expired post.
    posts = RecruitmentPost.objects.filter(
        post_expiry_date__gte=timezone.localdate()
    ).order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "post_type": post.post_type,
            "country": post.country.name if post.country else None,
            # Target countries (restriction set) so the FE can show "Open to: â€¦" on every
            # surface that lists posts. Same shape as the other list endpoints.
            "countries": list(post.countries.values("name", "code")),
            "expiry": post.post_expiry_date,
            "created_at": post.created_at,

            # Player fields
            "player": post.player.username if post.player else None,
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,
            # The phone the player currently plays on (compulsory on player posts).
            "mobile_device": post.mobile_device,
            # Optional gameplay video link (YouTube/TikTok, allowlist-validated).
            "video_url": post.video_url,
            # Free Fire UID + residential state + screenshots ("Player Available Post" feature set).
            "uid": post.player.uid if post.player else None,
            "residential_state": post.residential_state,
            "residential_country": post.residential_country,
            "images": _serialize_post_images(post, request),

            # Team fields
            "team": post.team.team_name if post.team else None,
            "roles_needed": post.roles_needed,
            "minimum_tier_required": post.minimum_tier_required,
            "commitment_type": post.commitment_type,
        })

    return Response(data, status=200)


def _optional_viewer(request):
    """Resolve the signed-in user from the Authorization header WITHOUT requiring it.

    The browse/list endpoints are public (anyone can read the market), but a logged-in viewer
    needs to know which cards are THEIRS so the UI can show Edit/Delete on them. Returns the User
    when a valid Bearer token is present, else None. The axios instance already forwards the token
    for authenticated users (AuthContext interceptor), so this is set for them and None for guests.
    Consumed by view_all_team_recruitment_post + view_all_player_availability_post (is_owner).
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ")[1])


@api_view(["GET"])
def view_all_team_recruitment_post(request):

    # Optional viewer -> is_owner per card (owner 2026-06-30): drives the My Posts tab (both post types)
    # and the inline Edit/Delete buttons on the "Teams looking" listing for posts the viewer created.
    viewer = _optional_viewer(request)

    posts = RecruitmentPost.objects.filter(
        post_type="TEAM_RECRUITMENT",
        post_expiry_date__gte=timezone.localdate(),  # expired posts disappear from the market
    ).order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "team": post.team.team_name if post.team else None,
            "countries": list(post.countries.values("name", "code")),
            "roles_needed": post.roles_needed,
            "minimum_tier_required": post.minimum_tier_required,
            "commitment_type": post.commitment_type,
            "expiry": post.post_expiry_date,
            # True only for the user who CREATED this post (matches edit_recruitment_post's
            # created_by gate), so the FE shows Edit/Delete on exactly the posts they can change.
            "is_owner": bool(viewer and post.created_by_id == viewer.user_id),
        })

    return Response(data, status=200)


@api_view(["GET"])
def view_all_player_availability_post(request):

    # Optional viewer -> is_owner per card (owner 2026-06-30): drives the My Posts tab (both post types)
    # and the inline Edit/Delete buttons on the "Players open to join" listing for the viewer's own post.
    viewer = _optional_viewer(request)

    posts = RecruitmentPost.objects.filter(
        post_type="PLAYER_AVAILABLE",
        post_expiry_date__gte=timezone.localdate(),  # expired posts disappear from the market
    ).order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "player": post.player.username if post.player else None,
            # True only for the post's creator (matches edit_recruitment_post's created_by gate).
            "is_owner": bool(viewer and post.created_by_id == viewer.user_id),
            "country": post.country.name if post.country else None,
            # Target countries (the restriction set). Returned so the FE can show "Open to: â€¦"
            # on the post card/dialog â€” previously omitted here, so the main market UI never
            # displayed the restriction. Same shape as view_all_team_recruitment_post.
            "countries": list(post.countries.values("name", "code")),
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,
            "additional_info": post.additional_info,
            # The phone the player currently plays on (compulsory on player posts).
            "mobile_device": post.mobile_device,
            # Optional gameplay video link (YouTube/TikTok, allowlist-validated).
            "video_url": post.video_url,
            # Free Fire UID (feature 4): the player's in-game id, shown on the card + dialog so
            # recruiters can look the player up in-game. Read from User.uid (afc_auth).
            "uid": post.player.uid if post.player else None,
            # Optional residential state (feature 3): ISO-3166-2 subdivision NAME, "" when unset.
            # Drives the recruiter STATE FILTER on the browse surface (filtered client-side).
            "residential_state": post.residential_state,
            "residential_country": post.residential_country,
            # Up to 3 in-game profile screenshots (feature 2): ordered absolute URLs. Empty list
            # when the player attached none. Rendered as a gallery on the card + View Player dialog.
            "images": _serialize_post_images(post, request),
            "expiry": post.post_expiry_date,
        })

    return Response(data, status=200)


@api_view(["POST"])
def apply_to_team(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # â”€â”€ MARKET-BAN GUARD (feature "J-market-reporting") â”€â”€
    # A banned player (or a member of a banned team) cannot apply to a team.
    market_ban = _active_market_ban(user)
    if market_ban:
        return Response(
            {"message": f"You are banned from the player market. Reason: {market_ban.reason}"},
            status=403,
        )

    # â”€â”€ INPUT GUARD â”€â”€
    # post_id is required. On an empty/blank body request.data.get returns None, and
    # RecruitmentPost.objects.get(id=None) raises RecruitmentPost.DoesNotExist (a
    # malformed/non-numeric id raises ValueError) â€” both previously bubbled up as an
    # unhandled 500. Validate the field first (missing -> 400), then guard the lookup
    # (not found / bad id -> 404) so bad input returns a clean 4xx instead.
    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except (RecruitmentPost.DoesNotExist, ValueError):
        return Response({"message": "Recruitment post not found."}, status=404)

    # ensure the applier is currently not in a team
    if TeamMembers.objects.filter(member=user).exists():
        return Response({"message": "You must leave your current team before applying"}, status=400)
    
    if post.post_type != "TEAM_RECRUITMENT":
        return Response({"message": "Invalid post"}, status=400)

    # â”€â”€ COUNTRY GATE (feature "K-country-gate") â”€â”€
    # If the team's post targets specific countries, only players LOCATED in one of those
    # countries (by most-recent login country) may apply. See Â§K helper for the policy.
    country_block = _country_gate(user, post)
    if country_block:
        return country_block

    application_message = request.data.get("application_message", "")

    # ensure the user has not already applied to this post
    if RecruitmentApplication.objects.filter(player=user, recruitment_post=post).exists():
        return Response({"message": "Already applied"}, status=400)

    application, created = RecruitmentApplication.objects.get_or_create(
        player=user,
        recruitment_post=post,
        team=post.team,
        application_message=application_message
    )    

    # Retention email at every 5th application milestone (5, 10, 15, ...)
    Notifications.objects.create(
        user=post.team.team_owner,
        message=f"Your Player Market post is getting attention!"
    )
    total_number_of_applications = RecruitmentApplication.objects.filter(team=post.team).count()

    if total_number_of_applications % 5 == 0:
        # i18n (owner 2026-06-15): collect the recipient USER objects (not bare emails) so each gets
        # the milestone email in their OWN saved language. We pull (email, language) pairs from the
        # owner, captain, and the manager/coach memberships, dedupe by email, then send per recipient.
        recipient_users = [post.team.team_owner]
        if post.team.team_captain:
            recipient_users.append(post.team.team_captain)
        recipient_users += [m.member for m in post.team.memberships.filter(management_role__in=["manager", "coach"]).select_related("member") if m.member]
        # Dedupe by email, keeping the first (email, language) seen for each address.
        recipient_targets = {}
        for ru in recipient_users:
            addr = getattr(ru, "email", None)
            if addr and addr not in recipient_targets:
                recipient_targets[addr] = (getattr(ru, "language", "") or "en")

        email_subject = "Your Player Market post is getting attention!"
        email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Your post is getting attention</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Your Post Is Getting Attention!</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{post.team.team_name} Management</strong>, your recruitment post for
                <strong style="color:#ff7a00;">{post.team.team_name}</strong> is attracting players!
              </p>

              <!-- Milestone Counter -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td align="center" style="background-color:#242424;border-radius:10px;border:1px solid #333;padding:28px;">
                    <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#666;">Total Applications</p>
                    <p style="margin:0;font-size:56px;font-weight:800;color:#ff7a00;line-height:1;">{total_number_of_applications}</p>
                    <p style="margin:8px 0 0 0;font-size:13px;color:#888;">players have applied to join your team</p>
                  </td>
                </tr>
              </table>

              <!-- Message -->
              <div style="background-color:#1e1e1e;border-left:3px solid #ff7a00;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px;">
                <p style="margin:0;font-size:14px;color:#bbbbbb;line-height:1.7;">
                  Don&rsquo;t let talent slip away &mdash; log in to review your applications, shortlist the best candidates, and invite players to trial.
                </p>
              </div>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/player-markets?applications=true"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Review Applications
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{post.team.team_name}</strong>.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        for email, lang in recipient_targets.items():
            send_email(email, email_subject, email_body, language=lang)

    return Response({"message": "Application submitted"}, status=201)


from datetime import timedelta
from django.utils import timezone


@api_view(["POST"])
def update_application_status(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    # â”€â”€ INPUT GUARD â”€â”€
    # application_id is required. On an empty/blank body request.data.get returns None,
    # and RecruitmentApplication.objects.get(id=None) raises DoesNotExist (a malformed
    # id raises ValueError) â€” both previously bubbled up as an unhandled 500. Validate
    # first (missing -> 400), then guard the lookup (not found / bad id -> 404).
    application_id = request.data.get("application_id")
    if not application_id:
        return Response({"message": "application_id is required."}, status=400)

    try:
        application = RecruitmentApplication.objects.get(id=application_id)
    except (RecruitmentApplication.DoesNotExist, ValueError):
        return Response({"message": "Application not found."}, status=404)

    # Ensure user owns the team
    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    action = request.data.get("action")

    if action == "REJECT":
        application.reason = request.data.get("reason")
        application.status = "REJECTED"

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been rejected."
        )

        # SEND EMAIL TO PLAYER
        email_subject = f"Application Update from {application.team.team_name}"
        email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Application Update</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#1a1a1a,#2a2a2a);padding:32px 40px;text-align:center;border-bottom:3px solid #333;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:#666;text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Application Update</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{application.player.username}</strong>,
              </p>

              <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
                Thank you for your interest in joining <strong style="color:#ffffff;">{application.team.team_name}</strong>.
                After careful consideration, we regret to inform you that your application was not successful at this time.
                We encourage you to keep honing your skills and consider applying again in the future.
              </p>

              <!-- Reason Box (only shown if reason provided) -->
              {"" if not application.reason else f"""
              <p style="margin:0 0 8px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#555;">Reason</p>
              <div style="background-color:#1e1e1e;border-left:3px solid #555;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px;">
                <p style="margin:0;font-size:14px;color:#999999;line-height:1.7;font-style:italic;">{application.reason}</p>
              </div>
              """}

              <!-- Encouragement Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td style="background-color:#1e1a0e;border:1px solid #ff9500;border-radius:8px;padding:20px 24px;">
                    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">Keep Going</p>
                    <p style="margin:0;font-size:13px;color:#cc9933;line-height:1.6;">
                      Every great player started somewhere. Keep practicing, stay active in the community, and your next opportunity could be just around the corner.
                    </p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/player-market"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Browse Other Teams
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">We wish you the best of luck in your esports journey.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        # i18n: send in the player's saved language; send_email localizes subject + body (falls back to English).
        send_email(application.player.email, email_subject, email_body, language=(getattr(application.player, "language", "") or "en"))

    elif action == "SHORTLIST":
        application.status = "SHORTLISTED"

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been shortlisted."
        )

    elif action == "INVITE":
        player = application.player

        # â”€â”€ MAX-2-CONCURRENT-TRYOUTS RULE (feature "J-market-rules", J2) â”€â”€
        # A player may be in at most MAX_ACTIVE_TRIALS (=2) active tryouts at once.
        # "Active" = a RecruitmentApplication in status TRIAL_ONGOING. This is the
        # TEAM-side gate: a team inviting an applicant into a trial. The player-side
        # gate (accepting a direct trial invite) lives in respond_to_trial_invite and
        # reads the SAME constant + uses the SAME wording so the two can't drift.
        # Surfaced to players via the player_market.tryout_limit InfoTip on the FE.
        active_trials = RecruitmentApplication.objects.filter(
            player=player,
            status="TRIAL_ONGOING"
        ).count()

        if active_trials >= MAX_ACTIVE_TRIALS:
            return Response({
                "message": f"You can be in at most {MAX_ACTIVE_TRIALS} active tryouts at a time."
            }, status=400)

        application.status = "TRIAL_ONGOING"
        application.contact_unlocked = True
        application.invite_expires_at = timezone.now() + timedelta(hours=72)

        TrialInvite.objects.create(
            team=application.team,
            player=player,
            application=application,
            expires_at=application.invite_expires_at,
            status="ACCEPTED"
        )

        chat = TrialChat.objects.create(application=application)

        # Notify player
        Notifications.objects.create(
            user=player,
            message=f"You have been added to a trial with {application.team.team_name}. A trial chat has been created."
        )

        # Email to player
        email_subject = f"You've Been Added to a Trial with {application.team.team_name}!"
        player_email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Trial Started</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Your Trial Has Begun!</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hey <strong style="color:#ffffff;">{player.username}</strong> &mdash;
                <strong style="color:#ff7a00;">{application.team.team_name}</strong> has selected you for a trial!
                A dedicated trial chat has been created where you can communicate directly with the team&rsquo;s management.
              </p>

              <!-- Team Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:24px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Team</p>
                    <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{application.team.team_name}</p>
                  </td>
                </tr>
              </table>

              <!-- Info Box -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td style="background-color:#1e1a0e;border:1px solid #ff6b0044;border-radius:8px;padding:16px 20px;">
                    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">What happens next?</p>
                    <p style="margin:0;font-size:13px;color:#cc9933;line-height:1.6;">
                      Use the trial chat in the AFC app to coordinate with the team. This is your chance to impress &mdash; give it your all!
                    </p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/applications"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Open Trial Chat
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">This trial was started because you applied to <strong style="color:#777;">{application.team.team_name}</strong> on the AFC Player Market.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        # i18n: localized to the player's saved language (send_email translates subject + body).
        send_email(player.email, email_subject, player_email_body, language=(getattr(player, "language", "") or "en"))

        # Notify team staff
        Notifications.objects.create(
            user=application.team.team_owner,
            message=f"{player.username} has been added to a trial. A trial chat has been created."
        )

        # Email to team owner, manager, coach, captain.
        # i18n (owner 2026-06-15): collect recipient USERS so each staff member's email is localized
        # to their own saved language. We dedupe by email into {email: language} pairs.
        staff_users = [application.team.team_owner]
        if application.team.team_captain:
            staff_users.append(application.team.team_captain)
        staff_users += [m.member for m in application.team.memberships.filter(management_role__in=["manager", "coach"]).select_related("member") if m.member]
        recipient_targets = {}
        for su in staff_users:
            addr = getattr(su, "email", None)
            if addr and addr not in recipient_targets:
                recipient_targets[addr] = (getattr(su, "language", "") or "en")

        team_email_subject = f"Trial Started: {player.username} has been added!"
        team_email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Trial Started</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Trial Started</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{application.team.team_name}</strong> Management,
              </p>

              <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
                <strong style="color:#ff7a00;">{player.username}</strong> has been added to a trial with your team.
                A dedicated trial chat is now available to coordinate and evaluate their performance.
              </p>

              <!-- Player Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:28px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player on Trial</p>
                    <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{player.username}</p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/team/trials"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Open Trial Chat
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{application.team.team_name}</strong>.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        for email, lang in recipient_targets.items():
            send_email(email, team_email_subject, team_email_body, language=lang)

        application.save()
        return Response({"message": "Trial started.", "chat_id": chat.id}, status=200)

    else:
        return Response({"message": "Invalid action"}, status=400)

    application.save()

    return Response({"message": "Application updated"}, status=200)


@api_view(["POST"])
def get_player_contact(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # â”€â”€ INPUT GUARD â”€â”€
    # application_id is required. On an empty/blank body request.data.get returns None,
    # and RecruitmentApplication.objects.get(id=None) raises DoesNotExist (a malformed
    # id raises ValueError) â€” both previously bubbled up as an unhandled 500. Validate
    # first (missing -> 400), then guard the lookup (not found / bad id -> 404).
    application_id = request.data.get("application_id")
    if not application_id:
        return Response({"message": "application_id is required."}, status=400)

    try:
        application = RecruitmentApplication.objects.get(id=application_id)
    except (RecruitmentApplication.DoesNotExist, ValueError):
        return Response({"message": "Application not found."}, status=404)

    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    if not application.contact_unlocked:
        return Response({"message": "Contact locked"}, status=403)

    # invite_expires_at is nullable (model: null=True). When it is None, "None < now()"
    # raises TypeError -> 500. Treat a missing expiry as not-yet-unlocked: no live invite
    # window means contact is effectively locked, so return the same 403 as a stale one.
    if application.invite_expires_at is None or application.invite_expires_at < timezone.now():
        return Response({"message": "Invite expired"}, status=403)

    player = application.player

    return Response({
        "discord": player.discord_username,
        "uid": player.uid
    })


def send_trial_invite_notification(application):

    player = application.player
    team = application.team

    message = f"""
    {team.team_name} has invited you to a trial.

    Join their Discord within 72 hours to proceed.
    """

    # Save notification in DB (you should have Notification model)
    Notifications.objects.create(
        user=player,
        message=message
    )

    # Optional:
    send_email(player.email, message)
    # send_discord_dm(player.discord_id, message)


@api_view(["POST"])
def finalize_trial(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    # â”€â”€ INPUT GUARD â”€â”€
    # application_id is required. On an empty/blank body request.data.get returns None,
    # and RecruitmentApplication.objects.get(id=None) raises DoesNotExist (a malformed
    # id raises ValueError) â€” both previously bubbled up as an unhandled 500. Validate
    # first (missing -> 400), then guard the lookup (not found / bad id -> 404).
    application_id = request.data.get("application_id")
    if not application_id:
        return Response({"message": "application_id is required."}, status=400)

    try:
        application = RecruitmentApplication.objects.get(id=application_id)
    except (RecruitmentApplication.DoesNotExist, ValueError):
        return Response({"message": "Application not found."}, status=404)

    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    action = request.data.get("action")

    if action == "ACCEPT":
        application.status = "ACCEPTED"

        # ðŸ”¥ Add player to team logic here

    elif action == "REJECT":
        application.status = "REJECTED"

    elif action == "EXTEND":
        application.status = "TRIAL_EXTENDED"

    else:
        return Response({"message": "Invalid action"}, status=400)

    application.save()

    return Response({"message": "Trial updated"})


@api_view(["GET"])
def view_applications(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # team_owner is a non-unique ForeignKey (a user can own >1 team), so .get() can raise
    # MultipleObjectsReturned -> uncaught 500. Use .filter().first() to deterministically pick one team.
    team = Team.objects.filter(team_owner=user).order_by("team_id").first()
    if not team:
        return Response({"message": "Team not found"}, status=404)


    applications = RecruitmentApplication.objects.filter(team=team).order_by("-created_at")

    data = []

    for app in applications:
        player = app.player

        if player:
            tournament_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="tournament",
                placement=1,
            ).count()

            total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
            ).aggregate(total=Sum("kills"))["total"] or 0

            # Finals appearances = distinct tournament events where player played in a stage named "final"
            tournament_finals_appearances = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
                team_stats__match__leaderboard__stage__stage_name__icontains="final",
            ).values("team_stats__tournament_team__event").distinct().count()

            scrims_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="scrims",
            ).aggregate(total=Sum("kills"))["total"] or 0

            scrims_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="scrims",
                placement=1,
            ).count()
        else:
            tournament_wins = 0
            total_tournament_kills = 0
            tournament_finals_appearances = 0
            scrims_kills = 0
            scrims_wins = 0

        data.append({
            "id": app.id,
            "player": player.username if player else None,
            "team": app.team.team_name if app.team else None,
            "post_id": app.recruitment_post.id,
            "status": app.status,
            "contact_unlocked": app.contact_unlocked,
            "invite_expires_at": app.invite_expires_at,
            "applied_at": app.created_at,
            "uid": player.uid if player else None,
            "discord_username": player.discord_username if player else None,
            "primary_role": app.recruitment_post.primary_role,
            "secondary_role": app.recruitment_post.secondary_role,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "is_banned": True if player and BannedPlayer.objects.filter(banned_player=player, is_active=True).exists() else False,
            "application_message": app.application_message,
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        })

    return Response(data, status=200)


def _is_trial_chat_participant(user, chat):
    """Returns True if user is allowed to access the trial chat."""
    application = chat.application
    if user == application.player:
        return True
    if user == application.team.team_owner:
        return True
    return TeamMembers.objects.filter(
        team=application.team,
        member=user,
        management_role__in=['coach', 'manager']
    ).exists()




@api_view(["GET"])
def get_my_trial_chats(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    trial_chats = TrialChat.objects.filter(
        Q(application__player=user) |
        Q(application__team__team_owner=user) |
        Q(application__team__memberships__member=user, application__team__memberships__management_role__in=['coach', 'manager'])
    ).distinct().select_related("application", "application__team", "application__player")

    data = [
        {
            "chat_id": chat.id,
            "application_id": chat.application.id,
            "team": chat.application.team.team_name,
            "player": chat.application.player.username,
        }
        for chat in trial_chats
    ]

    return Response(data, status=200)


@api_view(["GET"])
def get_trial_chat_messages(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    chat_id = request.query_params.get("chat_id")

    if not chat_id:
        return Response({"message": "chat_id is required."}, status=400)

    try:
        chat = TrialChat.objects.select_related(
            "application__player",
            "application__team",
        ).get(id=chat_id)
    except (TrialChat.DoesNotExist, ValueError):
        return Response({"message": "Chat not found."}, status=404)

    # AFC staff may READ any trial chat for oversight/moderation (feature "K-admin-chat-read",
    # disclosed in the Privacy Policy + Terms). The read gate allows the trial participants
    # (player / team owner / coach / manager) OR a market moderator. Staff get READ access
    # only â€” send_trial_chat_message keeps the participant-only gate, so an admin cannot post
    # into someone's trial conversation, only observe it.
    if not (_is_trial_chat_participant(user, chat) or _is_market_moderator(user)):
        return Response({"message": "Unauthorized."}, status=403)

    messages = chat.messages.select_related("sender").all()

    data = [
        {
            "id": msg.id,
            "sender": msg.sender.username,
            "sender_id": msg.sender.user_id,
            "message": msg.message,
            "sent_at": msg.sent_at,
        }
        for msg in messages
    ]

    app = chat.application
    return Response({
        "chat_id": chat.id,
        "application_id": app.id,
        "status": app.status,
        "team": app.team.team_name,
        # Absolute URL (API host) so the logo loads; bare .url is relative and 404s off the frontend origin.
        "team_logo": request.build_absolute_uri(app.team.team_logo.url) if app.team.team_logo else None,
        "player": app.player.username,
        "messages": data,
    }, status=200)


@api_view(["POST"])
def send_trial_chat_message(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    chat_id = request.data.get("chat_id")
    message_text = request.data.get("message", "").strip()

    if not chat_id:
        return Response({"message": "chat_id is required."}, status=400)

    if not message_text:
        return Response({"message": "Message cannot be empty."}, status=400)

    try:
        chat = TrialChat.objects.select_related(
            "application__player", "application__team"
        ).get(id=chat_id)
    except (TrialChat.DoesNotExist, ValueError):
        return Response({"message": "Chat not found."}, status=404)

    if not _is_trial_chat_participant(user, chat):
        return Response({"message": "Unauthorized."}, status=403)

    msg = TrialChatMessage.objects.create(chat=chat, sender=user, message=message_text)

    return Response({
        "id": msg.id,
        "sender": user.username,
        "message": msg.message,
        "sent_at": msg.sent_at,
    }, status=201)


@api_view(["GET"])
def view_my_applications(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)


    applications = RecruitmentApplication.objects.filter(player=user).order_by("-created_at")

    data = []

    for app in applications:
        player = app.player

        if player:
            tournament_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="tournament",
                placement=1,
            ).count()

            total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
            ).aggregate(total=Sum("kills"))["total"] or 0

            # Finals appearances = distinct tournament events where player played in a stage named "final"
            tournament_finals_appearances = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
                team_stats__match__leaderboard__stage__stage_name__icontains="final",
            ).values("team_stats__tournament_team__event").distinct().count()

            scrims_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="scrims",
            ).aggregate(total=Sum("kills"))["total"] or 0

            scrims_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="scrims",
                placement=1,
            ).count()
        else:
            tournament_wins = 0
            total_tournament_kills = 0
            tournament_finals_appearances = 0
            scrims_kills = 0
            scrims_wins = 0

        data.append({
            "id": app.id,
            "player": player.username if player else None,
            "team": app.team.team_name if app.team else None,
            "post_id": app.recruitment_post.id,
            "status": app.status,
            "contact_unlocked": app.contact_unlocked,
            "invite_expires_at": app.invite_expires_at,
            "applied_at": app.created_at,
            "uid": player.uid if player else None,
            "discord_username": player.discord_username if player else None,
            "primary_role": app.recruitment_post.primary_role,
            "secondary_role": app.recruitment_post.secondary_role,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "is_banned": True if player and BannedPlayer.objects.filter(banned_player=player, is_active=True).exists() else False,
            "application_message": app.application_message,
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        })

    return Response(data, status=200)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DIRECT TRIAL INVITES  (Team â†’ Player from a PLAYER_AVAILABLE post)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@api_view(["POST"])
def invite_player_to_trial(request):
    """
    Team invites a player who posted a PLAYER_AVAILABLE post.
    - Caller must be team owner, manager, or coach
    - Team must have < 4 active (TRIAL_ONGOING) trials
    - No duplicate pending invite allowed
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # â”€â”€ MARKET-BAN GUARD (feature "J-market-reporting") â”€â”€
    # A banned user (or a member of a banned team) cannot send a trial invite.
    market_ban = _active_market_ban(user)
    if market_ban:
        return Response(
            {"message": f"You are banned from the player market. Reason: {market_ban.reason}"},
            status=403,
        )

    post_id = request.data.get("post_id")
    invite_message = request.data.get("message", "")

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    if post.post_type != "PLAYER_AVAILABLE":
        return Response({"message": "This post is not a player availability post."}, status=400)

    # Resolve which team this user represents
    team = None
    if Team.objects.filter(team_owner=user).exists():
        team = Team.objects.get(team_owner=user)
    else:
        membership = TeamMembers.objects.filter(
            member=user, management_role__in=['manager', 'coach']
        ).select_related('team').first()
        if membership:
            team = membership.team

    if not team:
        return Response({"message": "You must be a team owner, manager, or coach to send a trial invite."}, status=403)

    # â”€â”€ COUNTRY GATE (feature "K-country-gate") â”€â”€
    # If the player's availability post targets specific countries (the countries they are
    # open to play for/from), only a recruiter LOCATED in one of those countries (by most-
    # recent login country) may invite them. Mirrors apply_to_team's gate. See Â§K helper.
    country_block = _country_gate(user, post)
    if country_block:
        return country_block

    if TeamMembers.objects.filter(team=team, member=post.player).exists():
        return Response({"message": "This player is already in your team."}, status=400)

    if DirectTrialInvite.objects.filter(team=team, player_post=post, status="PENDING").exists():
        return Response({"message": "You have already sent a pending trial invite to this player."}, status=400)

    active_team_trials = RecruitmentApplication.objects.filter(team=team, status="TRIAL_ONGOING").count()
    if active_team_trials >= 4:
        return Response({"message": "Your team already has 4 active trials. Finalize an existing trial before starting more."}, status=400)

    invite = DirectTrialInvite.objects.create(
        team=team,
        player=post.player,
        player_post=post,
        message=invite_message,
        expires_at=timezone.now() + timedelta(hours=72),
    )

    Notifications.objects.create(
        user=post.player,
        message=f"{team.team_name} has sent you a trial invite!"
    )

    player = post.player
    email_subject = f"Trial Invite from {team.team_name}"
    message_row = (
        f'<tr><td style="padding:0 24px 20px 24px;border-top:1px solid #333;">'
        f'<p style="margin:12px 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Message</p>'
        f'<p style="margin:0;font-size:14px;color:#bbbbbb;line-height:1.6;font-style:italic;">{invite_message}</p>'
        f'</td></tr>'
    ) if invite_message else ""

    email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">A Team Wants You!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
            Hey <strong style="color:#ffffff;">{player.username}</strong> &mdash;
            <strong style="color:#ff7a00;">{team.team_name}</strong> saw your availability post and wants you on their roster for a trial!
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:24px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Team Inviting You</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{team.team_name}</p>
            </td></tr>
            {message_row}
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background-color:#2a1a00;border:1px solid #ff6b0044;border-radius:8px;padding:16px 20px;">
              <table cellpadding="0" cellspacing="0"><tr>
                <td style="padding-right:12px;font-size:22px;">&#9201;</td>
                <td>
                  <p style="margin:0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">72-Hour Window</p>
                  <p style="margin:4px 0 0 0;font-size:13px;color:#cc8800;line-height:1.5;">You must accept or decline within <strong>72 hours</strong>. After that, the invite expires.</p>
                </td>
              </tr></table>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/my-invites"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              View &amp; Respond to Invite
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">This invite was sent because you have an active availability post on the AFC Player Market.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    # i18n: send the direct trial invite in the invited player's saved language.
    send_email(player.email, email_subject, email_body, language=(getattr(player, "language", "") or "en"))
    return Response({"message": "Trial invite sent.", "invite_id": invite.id}, status=201)


@api_view(["GET"])
def view_my_trial_invites(request):
    """Player views all direct trial invites received from teams."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invites = DirectTrialInvite.objects.filter(player=user).select_related(
        "team", "player_post"
    ).order_by("-created_at")

    data = []
    for invite in invites:
        if invite.status == "PENDING" and invite.expires_at < timezone.now():
            invite.status = "EXPIRED"
            invite.save(update_fields=["status"])

        data.append({
            "invite_id": invite.id,
            "team": invite.team.team_name,
            "team_id": invite.team.team_id,
            # Absolute URL (API host) so the logo loads; bare .url is relative and 404s off the frontend origin.
            "team_logo": request.build_absolute_uri(invite.team.team_logo.url) if invite.team.team_logo else None,
            "message": invite.message,
            "status": invite.status,
            "post_id": invite.player_post.id,
            "expires_at": invite.expires_at,
            "created_at": invite.created_at,
        })

    return Response(data, status=200)


@api_view(["POST"])
def respond_to_direct_trial_invite(request):
    """
    Player accepts or declines a DirectTrialInvite.
    ACCEPT:  player < 2 active trials, team < 4 active trials â†’ creates RecruitmentApplication + TrialChat
    DECLINE: marks invite rejected, notifies team
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invite_id = request.data.get("invite_id")
    action = request.data.get("action")  # ACCEPT or DECLINE

    try:
        invite = DirectTrialInvite.objects.select_related("team", "player", "player_post").get(id=invite_id)
    except DirectTrialInvite.DoesNotExist:
        return Response({"message": "Invite not found."}, status=404)

    if invite.player != user:
        return Response({"message": "Unauthorized."}, status=403)

    if invite.status != "PENDING":
        return Response({"message": f"This invite has already been {invite.status.lower()}."}, status=400)

    if invite.expires_at < timezone.now():
        invite.status = "EXPIRED"
        invite.save(update_fields=["status"])
        return Response({"message": "This invite has expired."}, status=400)

    if action == "DECLINE":
        invite.status = "REJECTED"
        invite.save()
        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} has declined your trial invite."
        )
        return Response({"message": "Invite declined."}, status=200)

    elif action == "ACCEPT":
        # â”€â”€ MAX-2-CONCURRENT-TRYOUTS RULE (feature "J-market-rules", J2) â”€â”€
        # Player-side gate: a player accepting a DIRECT trial invite. Same cap and same
        # wording as the team-side gate in handle_application_action so the rule is
        # consistent everywhere a player can enter a trial. See MAX_ACTIVE_TRIALS.
        player_active_trials = RecruitmentApplication.objects.filter(
            player=user, status="TRIAL_ONGOING"
        ).count()
        if player_active_trials >= MAX_ACTIVE_TRIALS:
            return Response({
                "message": f"You can be in at most {MAX_ACTIVE_TRIALS} active tryouts at a time."
            }, status=400)

        team_active_trials = RecruitmentApplication.objects.filter(
            team=invite.team, status="TRIAL_ONGOING"
        ).count()
        if team_active_trials >= 4:
            return Response({
                "message": f"{invite.team.team_name} already has 4 active trials and cannot start more right now."
            }, status=400)

        invite.status = "ACCEPTED"
        invite.save()

        # Unified: RecruitmentApplication + TrialChat so all existing chat logic works
        application = RecruitmentApplication.objects.create(
            player=user,
            recruitment_post=invite.player_post,
            team=invite.team,
            status="TRIAL_ONGOING",
            contact_unlocked=True,
        )
        chat = TrialChat.objects.create(application=application)

        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} accepted your trial invite. A trial chat has been created."
        )

        team = invite.team
        # i18n (owner 2026-06-15): collect recipient USERS so each staff member gets the email in
        # their own saved language. Deduped by email into {email: language} pairs.
        staff_users = [team.team_owner]
        if team.team_captain:
            staff_users.append(team.team_captain)
        staff_users += [m.member for m in team.memberships.filter(management_role__in=["manager", "coach"]).select_related("member") if m.member]
        recipient_targets = {}
        for su in staff_users:
            addr = getattr(su, "email", None)
            if addr and addr not in recipient_targets:
                recipient_targets[addr] = (getattr(su, "language", "") or "en")

        team_email_subject = f"{user.username} accepted your trial invite!"
        team_email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">Trial Accepted!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">Hi <strong style="color:#ffffff;">{team.team_name}</strong> Management,</p>
          <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
            <strong style="color:#ff7a00;">{user.username}</strong> has accepted your trial invite. A dedicated trial chat is now open.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player on Trial</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{user.username}</p>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/team/trials"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              Open Trial Chat
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{team.team_name}</strong>.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
        for email, lang in recipient_targets.items():
            send_email(email, team_email_subject, team_email_body, language=lang)

        return Response({"message": "Trial accepted.", "chat_id": chat.id}, status=200)

    else:
        return Response({"message": "Invalid action. Use ACCEPT or DECLINE."}, status=400)


@api_view(["GET"])
def view_application_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    application_id = request.query_params.get("application_id")

    try:
        app = RecruitmentApplication.objects.select_related(
            "player", "team", "recruitment_post", "recruitment_post__country"
        ).get(id=application_id)
    except RecruitmentApplication.DoesNotExist:
        return Response({"message": "Application not found."}, status=404)

    if app.player != user and app.team.team_owner != user and not TeamMembers.objects.filter(
        team=app.team, member=user, management_role__in=['coach', 'manager']
    ).exists():
        return Response({"message": "Unauthorized."}, status=403)

    player = app.player

    tournament_wins = TournamentTeamMatchStats.objects.filter(
        tournament_team__members__user=player,
        tournament_team__event__competition_type="tournament",
        placement=1,
    ).count()

    total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
        player=player,
        team_stats__tournament_team__event__competition_type="tournament",
    ).aggregate(total=Sum("kills"))["total"] or 0

    tournament_finals_appearances = TournamentPlayerMatchStats.objects.filter(
        player=player,
        team_stats__tournament_team__event__competition_type="tournament",
        team_stats__match__leaderboard__stage__stage_name__icontains="final",
    ).values("team_stats__tournament_team__event").distinct().count()

    scrims_kills = TournamentPlayerMatchStats.objects.filter(
        player=player,
        team_stats__tournament_team__event__competition_type="scrims",
    ).aggregate(total=Sum("kills"))["total"] or 0

    scrims_wins = TournamentTeamMatchStats.objects.filter(
        tournament_team__members__user=player,
        tournament_team__event__competition_type="scrims",
        placement=1,
    ).count()

    # Trial chat if one exists for this application
    chat_id = None
    try:
        chat_id = app.trial_chat.id
    except Exception:
        pass

    return Response({
        "id": app.id,
        "status": app.status,
        "applied_at": app.created_at,
        "updated_at": app.updated_at,
        "application_message": app.application_message,
        "reason": app.reason,
        "invite_expires_at": app.invite_expires_at,
        "contact_unlocked": app.contact_unlocked,

        "team": {
            "id": app.team.team_id,
            "name": app.team.team_name,
            "tag": app.team.team_tag,
            # Absolute URL (API host) so the logo loads; bare .url is relative and 404s off the frontend origin.
            "logo": request.build_absolute_uri(app.team.team_logo.url) if app.team.team_logo else None,
            "tier": app.team.team_tier,
            "country": app.team.country,
        },

        "post": {
            "id": app.recruitment_post.id,
            "roles_needed": app.recruitment_post.roles_needed,
            "commitment_type": app.recruitment_post.commitment_type,
            "minimum_tier_required": app.recruitment_post.minimum_tier_required,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "expiry": app.recruitment_post.post_expiry_date,
        },

        "stats": {
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        },

        "chat_id": chat_id,
    }, status=200)


@api_view(["GET"])
def get_post_details(request):
    """Public endpoint â€” no auth required."""
    post_id = request.query_params.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)

    try:
        post = RecruitmentPost.objects.select_related("player", "team", "country", "created_by").get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    data = {
        "id": post.id,
        "post_type": post.post_type,
        "post_expiry_date": post.post_expiry_date,
        "created_at": post.created_at,
        "created_by": post.created_by.username,
        "is_active": post.is_active,

        # Player fields
        "player": post.player.username if post.player else None,
        # Avatar resolved from UserProfile (where profile_pic actually lives) with a discord
        # fallback; see _player_avatar_url. Previously read post.player.profile_pic, which does
        # not exist on User and 500-ed this endpoint for every player post.
        "player_avatar": _player_avatar_url(post.player, request),
        "primary_role": post.primary_role,
        "secondary_role": post.secondary_role,
        "availability_type": post.availability_type,
        "additional_info": post.additional_info,
        # The phone the player currently plays on (compulsory on player posts).
        "mobile_device": post.mobile_device,
        # Optional gameplay video link (YouTube/TikTok, allowlist-validated).
        "video_url": post.video_url,
        # Free Fire UID + residential state + screenshots ("Player Available Post" feature set).
        # uid + residential_state feed the player card/dialog + edit form prefill; images is the
        # ordered absolute-URL gallery (also re-loaded into the edit form so the user sees what's there).
        "uid": post.player.uid if post.player else None,
        "residential_state": post.residential_state,
        "residential_country": post.residential_country,
        "images": _serialize_post_images(post, request),
        "country": post.country.name if post.country else None,
        "countries": list(post.countries.values("name", "code")),

        # Team fields
        "team": post.team.team_name if post.team else None,
        # Absolute URL (API host) so the logo loads; bare .url is relative and 404s off the frontend origin.
        "team_logo": request.build_absolute_uri(post.team.team_logo.url) if post.team and post.team.team_logo else None,
        "roles_needed": post.roles_needed,
        "minimum_tier_required": post.minimum_tier_required,
        "commitment_type": post.commitment_type,
        "recruitment_criteria": post.recruitment_criteria,
    }

    return Response(data, status=200)


@api_view(["GET"])
def get_posts_related_to_me(request):
    """Returns all recruitment posts created by the authenticated user."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    posts = RecruitmentPost.objects.filter(created_by=user).order_by("-created_at")

    data = []
    for post in posts:
        data.append({
            "id": post.id,
            "post_type": post.post_type,
            "post_expiry_date": post.post_expiry_date,
            "created_at": post.created_at,
            "is_active": post.is_active,

            # Player fields
            "player": post.player.username if post.player else None,
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,
            "additional_info": post.additional_info,
            # The phone the player currently plays on (compulsory on player posts).
            "mobile_device": post.mobile_device,
            # Optional gameplay video link (YouTube/TikTok, allowlist-validated).
            "video_url": post.video_url,
            # Free Fire UID + residential state + screenshots ("Player Available Post" feature set).
            "uid": post.player.uid if post.player else None,
            "residential_state": post.residential_state,
            "residential_country": post.residential_country,
            "images": _serialize_post_images(post, request),
            "country": post.country.name if post.country else None,
            "countries": list(post.countries.values("name", "code")),

            # Team fields
            "team": post.team.team_name if post.team else None,
            "roles_needed": post.roles_needed,
            "minimum_tier_required": post.minimum_tier_required,
            "commitment_type": post.commitment_type,
            "recruitment_criteria": post.recruitment_criteria,
        })

    return Response(data, status=200)


@api_view(["PATCH"])
def edit_recruitment_post(request):
    """Edit a recruitment post. Only the creator can edit it."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    if post.created_by != user:
        return Response({"message": "Unauthorized."}, status=403)

    data = request.data

    # Common fields
    if "post_expiry_date" in data:
        try:
            new_expiry = datetime.strptime(data["post_expiry_date"], "%Y-%m-%d").date()
        except ValueError:
            return Response({"message": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        # â”€â”€ ONE-MONTH EXPIRY CAP (feature "L-market-expiry-cap") â”€â”€
        # An edit must not extend a post past one calendar month of TOTAL life, so the cap
        # is measured from the post's OWN start (created_at), NOT from today. Otherwise a
        # user could keep editing every day and roll the expiry forward indefinitely. We
        # still reject a past date (an already-expired expiry reads inactive at once).
        # Same bound the create path enforces from today; see add_one_month above.
        today = timezone.now().date()
        if new_expiry < today:
            return Response({"message": "Post expiry cannot be in the past."}, status=400)
        if new_expiry > add_one_month(post.created_at.date()):
            return Response({"message": "Post expiry must be within 1 month of the post's start date."}, status=400)

        post.post_expiry_date = new_expiry

    if "country_code" in data:
        country = Country.objects.filter(code=data["country_code"]).first()
        if not country:
            return Response({"message": "Invalid country code."}, status=400)
        post.country = country

    # Player post fields
    if post.post_type == "PLAYER_AVAILABLE":
        for field in ("primary_role", "secondary_role", "availability_type", "additional_info"):
            if field in data:
                setattr(post, field, data[field])

        # mobile_device is COMPULSORY on player posts: an edit may change it but never clear it
        # (same rule the create path enforces).
        if "mobile_device" in data:
            device = (data.get("mobile_device") or "").strip()
            if not device:
                return Response(
                    {"message": "mobile_device cannot be empty: tell teams the phone you currently play on."},
                    status=400,
                )
            post.mobile_device = device[:80]

        # OPTIONAL gameplay video link: editable, clearable (send ""), allowlist-validated.
        if "video_url" in data:
            video_url, video_err = _validate_video_url(data.get("video_url"))
            if video_err:
                return Response({"message": video_err}, status=400)
            # Resolve TikTok short links to the embeddable canonical URL (owner 2026-06-30).
            post.video_url = _resolve_video_url(video_url)

        # OPTIONAL residential state (feature 3): present-key-wins, so sending "" clears it.
        # residential_country (refinement) follows the state: we only (re)derive the country from the
        # actor's detected location when the STATE ACTUALLY CHANGES. Re-deriving on every save desynced
        # the pair when the editor's own login country had since changed (e.g. they travelled): an edit
        # that merely touched the bio would silently rewrite a Lagos/Nigeria post to the editor's new
        # country while keeping the Nigerian state. Computing state_changed BEFORE we overwrite the
        # field keeps an unchanged state's stored country intact; a cleared state still clears both.
        if "residential_state" in data:
            new_state = (data.get("residential_state") or "").strip()[:120]
            state_changed = new_state != (post.residential_state or "")
            post.residential_state = new_state
            if new_state:
                # Only refresh the derived country when the state changed; otherwise keep what's stored
                # (and if the actor's country can't be resolved, preserve the existing value either way).
                if state_changed:
                    post.residential_country = _actor_country_name(user) or post.residential_country
            else:
                post.residential_country = ""

        # Screenshots (feature 2 + deferred-removal refinement, owner 2026-06-29). The gallery is
        # recomputed ATOMICALLY on save from three inputs sent together:
        #   * remove_image_ids -> the screenshots the user MARKED for removal in the Edit form (the
        #     per-thumbnail X marks locally and only sends ids here on Save, so cancelling deletes
        #     nothing). Scoped to this post (a stray id from another post is ignored by the filter).
        #   * clear_images=true -> remove every existing screenshot (kept for robustness/other callers).
        #   * new image files  -> ADDED to whatever survives (NOT a wholesale replace), so the result
        #     is exactly (existing - marked-removed) + new.
        # The combined result must stay within MAX_POST_IMAGES; we reject up-front (before any write)
        # so a too-big result never partially applies. Per-file size is validated by
        # _validate_post_image_files.
        image_files = _post_files(request)
        img_err = _validate_post_image_files(image_files)
        if img_err:
            return Response({"message": img_err}, status=400)

        clear_all = str(data.get("clear_images", "")).lower() in ("1", "true", "yes")
        remove_ids = [
            int(i) for i in (_coerce_list(data.get("remove_image_ids")) or [])
            if str(i).strip().isdigit()
        ]
        existing_qs = post.images.all()
        if clear_all:
            kept_count = 0
        else:
            removing_count = existing_qs.filter(id__in=remove_ids).count() if remove_ids else 0
            kept_count = existing_qs.count() - removing_count
        if kept_count + len(image_files) > MAX_POST_IMAGES:
            return Response(
                {"message": f"You can attach at most {MAX_POST_IMAGES} screenshots per post."},
                status=400,
            )
        # Apply removals first, then ADD the new files (order continues after the kept ones).
        if clear_all:
            post.images.all().delete()
        elif remove_ids:
            post.images.filter(id__in=remove_ids).delete()
        if image_files:
            _save_post_images(post, image_files)  # replace=False -> ADD

        # Target countries â€” tolerant to the FE key ("country_codes", holding names) and the
        # legacy "country_names"/codes. _resolve_countries matches by name OR code. Setting
        # only when a key is present lets an edit clear the restriction (empty list).
        # _coerce_list normalises arrays whether the form posts JSON or multipart.
        if any(k in data for k in ("countries", "country_names", "country_codes")):
            post.countries.set(_resolve_countries(
                _coerce_list(data.get("countries"))
                or _coerce_list(data.get("country_names"))
                or _coerce_list(data.get("country_codes"))
            ))

    # Team post fields
    elif post.post_type == "TEAM_RECRUITMENT":
        for field in ("minimum_tier_required", "commitment_type", "recruitment_criteria"):
            if field in data:
                setattr(post, field, data[field])
        # roles_needed is a JSON list column; _coerce_list keeps it a real list whether the form
        # posts a JSON array or a multipart JSON string.
        if "roles_needed" in data:
            post.roles_needed = _coerce_list(data.get("roles_needed"))

        # Target countries â€” tolerant to the FE key ("country_codes", holding names) and the
        # legacy "country_names"/codes. _resolve_countries matches by name OR code. Setting
        # only when a key is present lets an edit clear the restriction (empty list).
        if any(k in data for k in ("countries", "country_names", "country_codes")):
            post.countries.set(_resolve_countries(
                _coerce_list(data.get("countries"))
                or _coerce_list(data.get("country_names"))
                or _coerce_list(data.get("country_codes"))
            ))

    post.save()
    return Response({"message": "Post updated successfully."}, status=200)


@api_view(["DELETE"])
def delete_recruitment_post(request):
    """Delete a recruitment post. Only the creator can delete it."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    post_id = request.query_params.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    if post.created_by != user:
        return Response({"message": "Unauthorized."}, status=403)

    post.delete()
    return Response({"message": "Post deleted successfully."}, status=200)


@api_view(["POST"])
def remove_post_image(request):
    """Delete one (or several) screenshots from a recruitment post, in place (refinement 2,
    owner 2026-06-29).

    PURPOSE
        Backs the per-thumbnail X on the Edit Player form: remove a single saved screenshot
        immediately without re-uploading the rest or submitting the whole form. "Remove all" uses
        the same endpoint with every id. Complements (does not replace) edit-post's
        replace/clear_images/remove_image_ids paths.

    REQUEST  : POST /player-market/remove-post-image/  (Bearer)
               body { "post_id": <id>, "image_id": <id> }  OR  { "post_id": <id>, "image_ids": [..] }
    RESPONSE : 200 { "message": ..., "images": [{id,url,order}, ...] }  (the post's REMAINING images)
               400 missing post_id/image id ; 404 post not found ; 403 not the post's creator.
    AUTH     : Bearer SessionToken; CREATOR-ONLY (same gate as edit/delete).
    GATE     : deletion is scoped to the post (filter id__in=ids, post=post), so an id from another
               post is ignored; the <=3 cap can never be exceeded by a delete.

    FRONTEND CONSUMER
        app/(user)/player-markets/page.tsx -> handleRemovePostImage (Edit Player gallery X +
        "Remove all"), which replaces editPlayerExistingImages with the returned `images`.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    post_id = request.data.get("post_id")
    if not post_id:
        return Response({"message": "post_id is required."}, status=400)
    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except (RecruitmentPost.DoesNotExist, ValueError):
        return Response({"message": "Post not found."}, status=404)
    if post.created_by != user:
        return Response({"message": "Unauthorized."}, status=403)

    # Accept a single image_id or a list (image_ids); normalise to a list of ints.
    raw_ids = _coerce_list(request.data.get("image_ids"))
    if raw_ids is None and request.data.get("image_id") is not None:
        raw_ids = [request.data.get("image_id")]
    ids = [int(i) for i in (raw_ids or []) if str(i).strip().isdigit()]
    if not ids:
        return Response({"message": "image_id (or image_ids) is required."}, status=400)

    # Scoped delete: only rows that belong to THIS post are touched.
    post.images.filter(id__in=ids).delete()
    return Response(
        {"message": "Screenshot(s) removed.", "images": _serialize_post_images(post, request)},
        status=200,
    )


@api_view(["GET"])
def location_subdivisions(request):
    """List a country's states/provinces/regions for the residential-location pickers (feature 3).

    PURPOSE
        Powers the residential-state dropdown on the Create/Edit Player form (locked to the
        player's own country) AND the recruiter STATE FILTER on the browse surface (one call
        per country the recruiter selects).

    REQUEST  : GET /player-market/location-subdivisions/?country=<code-or-name>
               `country` accepts an ISO-3166-1 code ("NG") OR a country name ("Nigeria") -
               _pycountry_alpha2 normalises either to an alpha-2 code.
    RESPONSE : 200 {"country_code": "NG",
                    "subdivisions": [{"value": "Abia", "label": "Abia"}, ...]}  (sorted by name)
               An unknown/blank country returns 200 with an empty subdivisions list (never 404,
               so the FE just shows "no states" rather than erroring).
    AUTH     : none (read-only public reference data, like a country list). No user data leaks.

    FRONTEND CONSUMER
        app/(user)/player-markets/page.tsx - the state Select in the player form and the state
        multi-select in the players-tab filter. value == label == the subdivision NAME, which is
        exactly what RecruitmentPost.residential_state stores and the filter compares against.
    """
    raw = request.query_params.get("country", "")
    alpha2 = _pycountry_alpha2(raw)
    return Response(
        {"country_code": alpha2, "subdivisions": _subdivisions_for(alpha2)},
        status=200,
    )


@api_view(["GET"])
def my_market_context(request):
    """Bootstrap for the Create/Edit Player form's residential-location field (feature 3).

    PURPOSE
        The residential-state picker must be LOCKED to the player's own country. The FE doesn't
        reliably know that country, so this resolves it server-side from the same signal the
        country gate uses (_actor_country_code: most-recent LoginHistory.country, else
        User.country) and returns it together with that country's subdivisions in ONE call.

    REQUEST  : GET /player-market/my-market-context/   (Bearer SessionToken)
    RESPONSE : 200 {"country_code": "NG", "country_name": "Nigeria",
                    "subdivisions": [{"value","label"}, ...], "uid": "<player uid>"}
               When the actor's country can't be resolved, country_code/country_name are "" and
               subdivisions is [] - the FE then hides the optional state field.
    AUTH     : Bearer SessionToken (validate_token, same as create_recruitment_post).

    FRONTEND CONSUMER
        app/(user)/player-markets/page.tsx - fetched when the Create/Edit Player form opens to
        populate the country-locked state dropdown (and to echo the player's own UID, feature 4).
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    alpha2 = _pycountry_alpha2(_actor_country_code(user))
    # Clean display name (avoids pycountry's inverted "Tanzania, United Republic of" form); same
    # helper persists residential_country, so the locked picker label matches what gets stored/filtered.
    country_name = _country_display_name(alpha2)
    return Response(
        {
            "country_code": alpha2,
            "country_name": country_name,
            "subdivisions": _subdivisions_for(alpha2),
            "uid": getattr(user, "uid", None),
        },
        status=200,
    )


@api_view(["GET"])
def view_all_trials_and_applications(request):
    """Admin view to see all trials and applications in the system."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    if user.role not in ["admin", "moderator"]:
        return Response({"message": "Unauthorized."}, status=403)

    # Optional filters via query params
    status_filter = request.query_params.get("status")    # e.g. ?status=TRIAL_ONGOING
    team_filter   = request.query_params.get("team_id")   # e.g. ?team_id=5
    player_filter = request.query_params.get("player_id") # e.g. ?player_id=12

    applications = RecruitmentApplication.objects.select_related(
        "player", "team", "recruitment_post"
    ).order_by("-created_at")

    if status_filter:
        applications = applications.filter(status=status_filter)
    if team_filter:
        applications = applications.filter(team__team_id=team_filter)
    if player_filter:
        applications = applications.filter(player__id=player_filter)

    from django.db.models import Count
    status_summary = list(
        RecruitmentApplication.objects
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )

    data = []
    for app in applications:
        chat_id = None
        try:
            chat_id = app.trial_chat.id
        except Exception:
            pass

        data.append({
            "id": app.id,
            "status": app.status,
            "applied_at": app.created_at,
            "updated_at": app.updated_at,
            "reason": app.reason,
            "invite_expires_at": app.invite_expires_at,
            "contact_unlocked": app.contact_unlocked,
            "chat_id": chat_id,

            "player": {
                "id": app.player.user_id,
                "username": app.player.username,
                "uid": app.player.uid,
                "discord": app.player.discord_username,
                "is_banned": BannedPlayer.objects.filter(banned_player=app.player, is_active=True).exists(),
            },

            "team": {
                "id": app.team.team_id,
                "name": app.team.team_name,
                "tag": app.team.team_tag,
                "tier": app.team.team_tier,
            },

            "post": {
                "id": app.recruitment_post.id,
                "post_type": app.recruitment_post.post_type,
                "roles_needed": app.recruitment_post.roles_needed,
                "commitment_type": app.recruitment_post.commitment_type,
            },
        })

    return Response({
        "summary": status_summary,
        "total": len(data),
        "applications": data,
    }, status=200)
