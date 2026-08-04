"""
afc_partner_apply.views_public - the surface an organisation uses, with no AFC account.

PURPOSE
    Let an organisation apply to become an AFC partner without emailing anybody, track their own
    application afterwards, fix it when AFC asks, and collect their credentials once approved.

THE OPEN WRITE
    submit_application accepts an unauthenticated POST that writes a row and can carry a file. It
    is the second such endpoint on AFC, after afc_feedback's, and it follows that one closely and
    deliberately: per-sender rate limiting on a hashed IP, every value validated server-side
    against the real rules rather than trusted, and the sender's address stored only as a salted
    hash. The differences are all TIGHTER, because this endpoint accepts an image and creates work
    for a human:
      * a lower hourly ceiling (3 rather than 5) and a longer cooldown,
      * one PENDING application per contact email, so a double-clicked form returns the existing
        reference rather than a second row in the queue,
      * the logo goes through afc_sso/provisioning.py _clean_logo_upload, the strictest upload
        guard in the codebase, because that same file may end up on the consent screen.

HOW THE APPLICANT PROVES WHO THEY ARE
    They have no AFC account, so there is no session to check. Each application carries a random
    access token, issued at submission and mailed to the CONTACT ADDRESS THEY GAVE. Holding it is
    what proves the applicant is the person who filled the form in (or someone they forwarded the
    email to, which is their business). The reference alone is never enough: references travel in
    emails and get quoted in tickets. Only a salted hash of the token is stored.

ENDPOINTS (mounted at partner-apply/ via afc/urls.py)
    POST   partner-apply/applications/                    submit_application   PUBLIC, rate limited
    GET    partner-apply/applications/<reference>/        application_status   token in the query
    PATCH  partner-apply/applications/<reference>/        application_status   token, changes only
    POST   partner-apply/applications/<reference>/claim/  claim_credentials    token, ONCE

CONSUMED BY
    frontend app/(root)/partners/apply/page.tsx            -> submit_application
    frontend app/(root)/partners/apply/status/page.tsx     -> application_status (GET + PATCH)
    frontend app/(root)/partners/apply/credentials/page.tsx -> claim_credentials
"""
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from afc_sso.provisioning import (
    _clean_logo_upload, _clean_outbound_url, _clean_redirect_uris, _clean_url,
)

from . import emails
from .models import PartnerApplication, generate_reference, hash_token

# ── RATE LIMIT ────────────────────────────────────────────────────────────────────────────────
# Same Redis cache and the same add()-then-incr() idiom as afc_feedback/views.py and
# afc_partner_api/ratelimit.py (django_redis' incr() raises on a missing key, so the bucket must
# be add()ed first). Two limits again, each catching what the other misses.
#
# The sender is always the hashed IP: an applicant is by definition not signed in, so unlike the
# feedback endpoint there is no user id to prefer. Deliberately tighter than feedback, because a
# real organisation applies ONCE, and the endpoint accepts an image upload.
APPLY_RATE_LIMIT_PER_HOUR = 3
APPLY_COOLDOWN_SECONDS = 60

# Length caps on the two prose answers. Enforced here, not merely marked in the UI, because the
# client is a public web page. Generous enough for a real answer, small enough that the queue
# cannot be filled with a megabyte of text.
MAX_PROSE_LENGTH = 4000
MIN_PROSE_LENGTH = 30


def _client_ip(request):
    """Best-effort client IP. Honours X-Forwarded-For's FIRST entry (the original client) because
    AFC runs behind a load balancer, and falls back to REMOTE_ADDR."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _client_ip_hash(request):
    """A salted, one-way hash of the client IP.

    We need to RECOGNISE a repeat sender for the rate limit and to spot one abusive source in the
    queue. We do not need to KNOW their address. SECRET_KEY is the salt, so the hash is meaningless
    outside this deployment. Identical reasoning to afc_feedback/views.py _client_ip_hash.
    """
    ip = _client_ip(request)
    if not ip:
        return ""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode("utf-8")).hexdigest()


def _hour_key(ip_hash):
    """Per-sender fixed clock-hour bucket. The hour stamp in the key self-rotates the window, so
    no reset job is needed."""
    return f"papply_hr:{ip_hash or 'unknown'}:{timezone.now().strftime('%Y%m%d%H')}"


def _cooldown_key(ip_hash):
    return f"papply_cd:{ip_hash or 'unknown'}"


def _next_hour_iso():
    nxt = timezone.now().replace(minute=0, second=0, microsecond=0) + timezone.timedelta(hours=1)
    return nxt.isoformat()


def check_apply_rate(ip_hash):
    """May this sender submit RIGHT NOW? Read-only: does NOT consume a slot.

    Returns (allowed, info). When blocked, `info` carries `reason`, `resets_at` and a `message`
    the frontend can show as-is.
    """
    cooldown_until = cache.get(_cooldown_key(ip_hash))
    if cooldown_until:
        return False, {
            "reason": "cooldown",
            "resets_at": cooldown_until,
            "message": "You just sent an application. Please wait a moment before sending another.",
        }
    count = cache.get(_hour_key(ip_hash), 0) or 0
    if count >= APPLY_RATE_LIMIT_PER_HOUR:
        return False, {
            "reason": "hourly",
            "resets_at": _next_hour_iso(),
            "message": (
                "You have sent several applications this hour. Please try again later, or reply "
                "to the email you received about your existing application."
            ),
        }
    return True, {"remaining": APPLY_RATE_LIMIT_PER_HOUR - count}


def record_apply_send(ip_hash):
    """Consume one slot. Called ONLY after a row is actually stored, so a rejected or invalid
    attempt never costs a real applicant their allowance."""
    cooldown_end = (
        timezone.now() + timezone.timedelta(seconds=APPLY_COOLDOWN_SECONDS)
    ).isoformat()
    cache.set(_cooldown_key(ip_hash), cooldown_end, APPLY_COOLDOWN_SECONDS)

    hk = _hour_key(ip_hash)
    cache.add(hk, 0, 3600)
    try:
        cache.incr(hk)
    except ValueError:
        # TTL-boundary race: the bucket expired between add() and incr(). Re-seed rather than 500.
        cache.set(hk, 1, 3600)


# ── validation ────────────────────────────────────────────────────────────────────────────────
def _clean_prose(raw, label):
    """One of the two free-text answers. Returns (cleaned, error_message).

    A MINIMUM length is enforced, not just a maximum, and that is the point of the field: "we need
    data" is not an answer the owner can make a grant decision from, and letting it through would
    turn every application into a follow-up email, which is the thing this app exists to remove.
    """
    text = str(raw or "").strip()
    if not text:
        return None, f"{label} is required."
    if len(text) < MIN_PROSE_LENGTH:
        return None, f"Please give a little more detail for {label.lower()}."
    return text[:MAX_PROSE_LENGTH], None


def _clean_email(raw):
    text = str(raw or "").strip()
    if not text:
        return None, "A contact email is required."
    try:
        validate_email(text)
    except DjangoValidationError:
        return None, "That contact email does not look like a valid address."
    return text, None


def _clean_whatsapp(raw, country=None):
    """Normalise the applicant's WhatsApp number to E.164, or say why it could not be.

    OPTIONAL FIELD: blank is a legitimate answer and returns ("", None). An applicant who would
    rather only be emailed is not blocked.

    WHY IT IS NORMALISED HERE RATHER THAN AT SEND TIME. The owner's requirement is that somebody
    can actually be messaged on this number, so a value that cannot be dialled is not worth
    storing. The two phone fields already in this codebase, UserProfile.whatsapp_number and
    Vendor.whatsapp_number, both keep whatever was typed and normalise when a message goes out,
    and the result is 34 of 133 stored player numbers sitting in a local form that cannot be
    resolved without knowing the country (see afc_whatsapp/phone.py's own header). Repeating that
    here would mean discovering a bad number at the moment AFC needed it. Refusing at the door
    also puts the error in front of the person who can fix it, while they are still on the form.

    `country` is the country the applicant typed on the same form, used only to anchor a number
    written in local form ("08051234567"). to_e164 returns None rather than guessing when it has
    no country to anchor to, which is why the message below asks for the international form.
    """
    from afc_whatsapp.phone import to_e164

    text = str(raw or "").strip()
    if not text:
        return "", None

    e164 = to_e164(text, country_code=country or None)
    if not e164:
        return None, (
            "That WhatsApp number could not be read. Please give it in international form, "
            "starting with your country code, for example +234 805 123 4567."
        )
    return e164, None


def _bool(raw):
    """Multipart sends booleans as the strings "true"/"false"; JSON sends real booleans. One
    reader for both so the endpoint behaves identically whether or not a logo is attached."""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("true", "1", "yes", "on")


# ── serializers ───────────────────────────────────────────────────────────────────────────────
def _serialize_for_applicant(application):
    """What the APPLICANT may see about their own application.

    Deliberately NOT the admin serializer. `internal_note` is absent because it is AFC's note to
    itself, and no credential of any kind appears here: the claim endpoint is the only thing that
    ever returns one, once. `decision_note` IS here, because it was written to be read by them.
    """
    return {
        "reference": application.reference,
        "status": application.status,
        "organisation_name": application.organisation_name,
        "display_name": application.display_name,
        "homepage_url": application.homepage_url,
        "country": application.country,
        "contact_name": application.contact_name,
        "contact_email": application.contact_email,
        "contact_role": application.contact_role,
        # Echoed back NORMALISED, so an applicant who typed a local number sees the E.164 AFC
        # actually stored and can tell it resolved to the country they meant.
        "contact_whatsapp": application.contact_whatsapp,
        "wants_sso": application.wants_sso,
        "wants_data_api": application.wants_data_api,
        "redirect_uris": application.redirect_uris,
        "post_logout_redirect_uris": application.post_logout_redirect_uris,
        "deletion_webhook_url": application.deletion_webhook_url,
        "use_case": application.use_case,
        "data_needed": application.data_needed,
        "decision_note": application.decision_note,
        "is_editable": application.is_editable_by_applicant(),
        # Whether there is anything to collect. The credentials page reads this to decide between
        # showing the collect button and explaining that the link has already been used.
        "claim_is_open": application.claim_is_open(),
        "claimed_at": application.claimed_at.isoformat() if application.claimed_at else None,
        # The public half of an approved SSO application. Safe to show repeatedly, unlike the
        # secret: client_id travels in every authorize URL by design.
        "client_id": (
            application.sso_application.client_id if application.sso_application else None
        ),
        "created_at": application.created_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
    }


def _load_by_token(reference, token):
    """Resolve (application, error_response) from a reference plus the applicant's access token.

    ONE failure message for every failure mode, and one status code: an unknown reference, a
    reference with the wrong token, and a well-formed guess all return the same 404. Telling an
    unauthenticated caller "that reference exists but your token is wrong" would turn this
    endpoint into an oracle for which organisations have applied to AFC.
    """
    not_found = Response(
        {"message": "We could not find that application. Check the link in your email."},
        status=status.HTTP_404_NOT_FOUND,
    )
    token = (token or "").strip()
    if not token:
        return None, not_found

    application = PartnerApplication.objects.filter(
        reference=(reference or "").strip().upper()).first()
    if application is None or not application.access_token_hash:
        return None, not_found
    # Compare HASHES, never the token itself: the plaintext is not stored, which is the whole
    # point of storing the hash.
    if hash_token(token) != application.access_token_hash:
        return None, not_found
    return application, None


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 1) submit_application  (POST partner-apply/applications/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def submit_application(request):
    """Receive one partner application from an organisation nobody at AFC has met yet.

    PURPOSE: replace the "email us your details and we retype them" loop. Everything an AFC admin
        used to transcribe by hand arrives here already validated against the rules that will be
        applied at approval, so a mistake is the applicant's to fix while they are still looking
        at the form.

    REQUEST: multipart (when a logo is attached) or JSON. Fields:
        organisation_name  required
        homepage_url       required, https
        contact_name       required
        contact_email      required, and where every decision email goes
        contact_role       optional
        contact_whatsapp   optional, stored in E.164, refused if it cannot be normalised
        display_name       optional, what a player would see on the consent screen
        country            optional
        wants_sso          bool, wants_data_api bool. AT LEAST ONE must be true.
        redirect_uris      required when wants_sso. String (newline separated) or list.
        post_logout_redirect_uris, deletion_webhook_url   optional
        use_case           required prose, what they are building
        data_needed        required prose, what they need from AFC and why
        locale             optional, the language the form was filled in ("en"/"fr"/"pt")
        logo               optional file (PNG/JPG/WEBP, 2 MB)
      NOTE WHAT IS NOT ACCEPTED: any share_* toggle, any scope, any status. What data a partner
      receives is AFC's decision, taken at review time. See the long comment on
      PartnerApplication.use_case for why an applicant is not offered a scope checklist.

    RESPONSE 201: {"message", "reference", "status"}
             200: {"message", "reference", "status", "already_pending": true} when this contact
                  email already has an open application. Not an error: a double-submitted form
                  should hand back the reference, not a second row in the owner's queue.
             400: {"message"} one validation failure, worded for the applicant
             429: {"message", "reason", "resets_at"}
    AUTH: none. An applicant has no AFC account, and requiring one would exclude exactly the
        organisations this is for.
    CONSUMED BY: frontend app/(root)/partners/apply/page.tsx.

    THE ACCESS TOKEN IS EMAILED, NOT RETURNED. The response deliberately does not contain it: it
    is the applicant's key to their own application, and mailing it to the address they typed is
    what makes it evidence that the address is theirs. A caller who mistyped their email gets a
    reference and no way in, which is the correct outcome and is why the form asks them to check.
    """
    ip_hash = _client_ip_hash(request)
    allowed, info = check_apply_rate(ip_hash)
    if not allowed:
        return Response(
            {"message": info["message"], "reason": info["reason"], "resets_at": info["resets_at"]},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    organisation_name = str(request.data.get("organisation_name") or "").strip()
    if not organisation_name:
        return Response({"message": "Your organisation name is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    contact_name = str(request.data.get("contact_name") or "").strip()
    if not contact_name:
        return Response({"message": "A contact name is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    contact_email, err = _clean_email(request.data.get("contact_email"))
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    # Anchored on the country typed on this same form, which is the only country AFC knows about
    # an applicant. A number already in international form ignores it.
    contact_whatsapp, err = _clean_whatsapp(
        request.data.get("contact_whatsapp"), str(request.data.get("country") or "").strip())
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    # ── One open application per contact email ──
    # Checked BEFORE any other work: a second submission from an organisation that already has one
    # in the queue is almost always a double-clicked button or an impatient refresh, and answering
    # with their existing reference is more useful than either a duplicate or an error.
    existing = PartnerApplication.objects.filter(
        contact_email__iexact=contact_email,
        status__in=(PartnerApplication.PENDING, PartnerApplication.CHANGES_REQUESTED),
    ).first()
    if existing is not None:
        return Response(
            {
                "message": (
                    "You already have an application with us. We have sent the link to "
                    f"{contact_email}."
                ),
                "reference": existing.reference,
                "status": existing.status,
                "already_pending": True,
            },
            status=status.HTTP_200_OK,
        )

    homepage_url, err = _clean_url(request.data.get("homepage_url"), "Your website")
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)
    if not homepage_url:
        return Response({"message": "Your website address is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    wants_sso = _bool(request.data.get("wants_sso"))
    wants_data_api = _bool(request.data.get("wants_data_api"))
    if not (wants_sso or wants_data_api):
        return Response(
            {"message": "Choose at least one product: Sign in with AFC, the Data API, or both."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── The redirect URIs, validated NOW against the real policy ──
    # This is the single biggest reason this app exists. Under the old flow a wildcard or a query
    # string in a redirect URI was discovered by the owner days later, or worse, at the partner's
    # first failed sign-in. afc_sso/redirect_policy.py runs here, with the applicant still on the
    # page, and its message already names the offending URI and the rule it broke.
    redirect_uris = ""
    post_logout_redirect_uris = ""
    if wants_sso:
        redirect_uris, err = _clean_redirect_uris(request.data.get("redirect_uris"))
        if err:
            return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)
        post_logout_redirect_uris, err = _clean_redirect_uris(
            request.data.get("post_logout_redirect_uris"),
            required=False, label="post-logout redirect URI")
        if err:
            return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    # _clean_outbound_url, NOT _clean_url. This form is public and unauthenticated, and this is
    # the one field on it that AFC's own server later fetches from inside AFC's network, so it
    # must resolve to a public address. Every other URL here is followed by a browser.
    deletion_webhook_url, err = _clean_outbound_url(
        request.data.get("deletion_webhook_url"), "Disconnection webhook URL")
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    use_case, err = _clean_prose(request.data.get("use_case"), "What you are building")
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)
    data_needed, err = _clean_prose(request.data.get("data_needed"), "What you need from AFC")
    if err:
        return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    # ── The logo, through the consent-screen-grade guard ──
    logo_file = None
    uploaded = request.FILES.get("logo") if hasattr(request, "FILES") else None
    if uploaded is not None:
        logo_file, err = _clean_logo_upload(uploaded)
        if err:
            return Response({"message": err}, status=status.HTTP_400_BAD_REQUEST)

    application = PartnerApplication.objects.create(
        reference=generate_reference(),
        organisation_name=organisation_name[:160],
        display_name=str(request.data.get("display_name") or "").strip()[:120],
        homepage_url=homepage_url,
        country=str(request.data.get("country") or "").strip()[:80],
        contact_name=contact_name[:120],
        contact_email=contact_email,
        contact_role=str(request.data.get("contact_role") or "").strip()[:120],
        # Already E.164 or already empty; _clean_whatsapp refused anything else above.
        contact_whatsapp=contact_whatsapp,
        wants_sso=wants_sso,
        wants_data_api=wants_data_api,
        redirect_uris=redirect_uris,
        post_logout_redirect_uris=post_logout_redirect_uris,
        deletion_webhook_url=deletion_webhook_url,
        use_case=use_case,
        data_needed=data_needed,
        locale=str(request.data.get("locale") or "")[:8],
        ip_hash=ip_hash,
    )
    if logo_file is not None:
        application.logo = logo_file
        application.save(update_fields=["logo"])

    # Consume the slot only now that the row exists.
    record_apply_send(ip_hash)

    # The plaintext token exists only here, for exactly as long as it takes to put it in an email.
    access_token = application.issue_access_token()
    emails.send_received(application, access_token)

    return Response(
        {
            "message": (
                "Thanks. Your application is with AFC, and we have emailed "
                f"{contact_email} a link to track it."
            ),
            "reference": application.reference,
            "status": application.status,
        },
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 2 + 3) application_status  (GET = read, PATCH = fix what AFC asked about)
#        at partner-apply/applications/<reference>/
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "PATCH"])
@authentication_classes([])
def application_status(request, reference):
    """The applicant's own view of their application, and the only way they can edit it.

    AUTH: `?token=` on the query string, matched against the salted hash on the row. There is no
        AFC account to authenticate against. Every failure returns the same 404, so this cannot be
        used to discover which organisations have applied.

    ── GET ──
    REQUEST: partner-apply/applications/AFC-P-7K3MQX/?token=...
    RESPONSE 200: {"application": {...applicant view...}}, 404 on any auth failure.
    CONSUMED BY: frontend app/(root)/partners/apply/status/page.tsx.

    ── PATCH ──
    PURPOSE: the "changes requested" loop. The owner asked for something to be fixed, and this is
        where the applicant fixes it, in place, without starting again.
    REQUEST: the same editable fields as submission (the two prose answers, the URI lists, the
        identity fields). Every one is optional; send only what changed.
    RESPONSE 200: {"message", "application"}
             400 validation, 404 auth, 409 when the application is not in changes_requested.
    CONSUMED BY: the same status page, in its edit mode.

    ONLY IN changes_requested. A pending application is deliberately frozen: the owner may be
    reading it at that moment, and an answer that changes underneath them is worse than a second
    application. An approved or rejected one is history and is never rewritten.

    SUBMITTING THE FIX RETURNS THE APPLICATION TO pending, which is what puts it back in the
    owner's queue. Without that, a fixed application would sit in changes_requested forever and
    nobody would look at it again.
    """
    application, err = _load_by_token(reference, request.GET.get("token"))
    if err:
        return err

    if request.method == "GET":
        return Response({"application": _serialize_for_applicant(application)},
                        status=status.HTTP_200_OK)

    # ── PATCH ──
    if not application.is_editable_by_applicant():
        return Response(
            {
                "message": (
                    "This application cannot be edited right now. It is only editable while AFC "
                    "has asked you for changes."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    data = request.data
    updated = []

    # Identity fields: trimmed and length-capped, same as at submission.
    for field, cap in (
        ("organisation_name", 160), ("display_name", 120), ("country", 80),
        ("contact_name", 120), ("contact_role", 120),
    ):
        if field in data:
            setattr(application, field, str(data.get(field) or "").strip()[:cap])
            updated.append(field)

    # NOT in the loop above, because this one is normalised rather than merely trimmed. It runs
    # the same cleaner the create path uses, for the same reason the webhook URL does: a rule
    # applied at submission and not on the edit beside it is one PATCH away from not existing.
    # The country is read from the edit if it is being changed in the same request, so an
    # applicant correcting both at once is anchored on the country they are moving TO.
    if "contact_whatsapp" in data:
        cleaned, err_msg = _clean_whatsapp(
            data.get("contact_whatsapp"),
            str(data.get("country", application.country) or "").strip())
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        application.contact_whatsapp = cleaned
        updated.append("contact_whatsapp")

    # contact_email is deliberately NOT editable here. It is the address the access token was
    # mailed to, so letting a token holder change it would let them redirect every future decision
    # email, including the credentials link, to an address AFC never verified. An applicant who
    # gave the wrong address applies again.

    if "homepage_url" in data:
        cleaned, err_msg = _clean_url(data.get("homepage_url"), "Your website")
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        if not cleaned:
            return Response({"message": "Your website address is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        application.homepage_url = cleaned
        updated.append("homepage_url")

    if "redirect_uris" in data:
        # Required only when they want SSO at all; the policy itself is unchanged.
        cleaned, err_msg = _clean_redirect_uris(
            data.get("redirect_uris"), required=application.wants_sso)
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        application.redirect_uris = cleaned
        updated.append("redirect_uris")

    if "post_logout_redirect_uris" in data:
        cleaned, err_msg = _clean_redirect_uris(
            data.get("post_logout_redirect_uris"), required=False,
            label="post-logout redirect URI")
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        application.post_logout_redirect_uris = cleaned
        updated.append("post_logout_redirect_uris")

    if "deletion_webhook_url" in data:
        # Same stricter cleaner the create path uses, for the same reason: an applicant editing
        # their draft must not be able to reach an address the create path refused.
        cleaned, err_msg = _clean_outbound_url(
            data.get("deletion_webhook_url"), "Disconnection webhook URL")
        if err_msg:
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        application.deletion_webhook_url = cleaned
        updated.append("deletion_webhook_url")

    for field, label in (
        ("use_case", "What you are building"), ("data_needed", "What you need from AFC"),
    ):
        if field in data:
            cleaned, err_msg = _clean_prose(data.get(field), label)
            if err_msg:
                return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
            setattr(application, field, cleaned)
            updated.append(field)

    if not updated:
        return Response({"message": "Nothing was changed."}, status=status.HTTP_400_BAD_REQUEST)

    # Back into the owner's queue, and clear the note: it described the old answers, and leaving
    # it on screen would tell the applicant they still have something to fix.
    application.status = PartnerApplication.PENDING
    application.decision_note = ""
    updated.extend(["status", "decision_note", "updated_at"])
    application.save(update_fields=sorted(set(updated)))

    return Response(
        {
            "message": "Thanks, your changes are with AFC.",
            "application": _serialize_for_applicant(application),
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 4) claim_credentials  (POST partner-apply/applications/<reference>/claim/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
def claim_credentials(request, reference):
    """Hand an approved applicant their credentials, exactly once.

    PURPOSE: solve the problem that a client secret is shown once and stored hashed, so it cannot
        be re-read, re-sent, or recovered, and yet it has to reach an organisation AFC has no
        account for.

    WHY NOT JUST EMAIL THE SECRET. It would be simpler and it would be wrong. An email sits in an
        inbox permanently, gets forwarded to whoever else at the organisation needs "the AFC
        thing", and is searchable by anyone who later gains access to that mailbox. A secret with
        no expiry in a channel with no expiry is the worst place to put it.

    WHAT HAPPENS INSTEAD. Approval emails a single-use link. Opening it calls this endpoint, which
        ROTATES the client secret and returns the fresh plaintext once. Three properties follow:
          * the link is worth nothing after it is used (claimed_at is stamped),
          * it is worth nothing after CLAIM_WINDOW_HOURS,
          * and because it rotates rather than reveals, a link that WAS intercepted is detectable:
            the real applicant finds their credentials already collected and tells AFC.
        The owner can mint a fresh link at any time from the admin page, which rotates again, so a
        lost or expired link is a two-click fix rather than a re-provisioning.

    REQUEST: POST, no body. `?token=` carries the CLAIM token (not the access token: they are
        separate credentials with separate lifetimes, and the long-lived one must not be able to
        mint secrets).
    RESPONSE 200: {"message", "client_id", "client_secret", "api_key", "guide_url"}
                  client_secret is present only for an SSO application, api_key only for a Data
                  API partner, and this is the ONLY response in the whole codebase that carries
                  either to an unauthenticated caller.
             404 unknown reference or wrong token
             409 already claimed, expired, or the application is not approved
    AUTH: the claim token alone. There is no account to check.
    CONSUMED BY: frontend app/(root)/partners/apply/credentials/page.tsx.
    """
    token = (request.GET.get("token") or "").strip()
    not_found = Response(
        {"message": "We could not find that credentials link. Check the link in your email."},
        status=status.HTTP_404_NOT_FOUND,
    )
    if not token:
        return not_found

    application = PartnerApplication.objects.filter(
        reference=(reference or "").strip().upper()).first()
    # Same one-message-for-every-failure rule as _load_by_token, for the same reason.
    if application is None or not application.claim_token_hash:
        return not_found
    if hash_token(token) != application.claim_token_hash:
        return not_found

    if application.status != PartnerApplication.APPROVED:
        return Response(
            {"message": "This application has not been approved."},
            status=status.HTTP_409_CONFLICT,
        )
    if not application.claim_is_open():
        already = application.claimed_at is not None
        return Response(
            {
                "message": (
                    "These credentials have already been collected. Ask AFC to send a new link "
                    "if you need to reissue them."
                    if already else
                    "This credentials link has expired. Ask AFC to send a new one."
                ),
                "reason": "claimed" if already else "expired",
            },
            status=status.HTTP_409_CONFLICT,
        )

    payload = {
        "message": "Copy these now. They will not be shown again.",
        "reference": application.reference,
    }

    # ── Sign in with AFC ──
    # ROTATE rather than reveal. The secret minted at approval was hashed the moment it was saved
    # and is unrecoverable by design, so there is nothing to reveal even if we wanted to; rotating
    # here is what makes the claim link the thing that carries the credential.
    sso_application = application.sso_application
    if sso_application is not None:
        from oauth2_provider.generators import generate_client_secret

        secret = generate_client_secret()
        sso_application.client_secret = secret
        sso_application.save(update_fields=["client_secret"])
        payload["client_id"] = sso_application.client_id
        payload["client_secret"] = secret

    # ── Data API ──
    # A fresh key rather than a stored one, for the same reason: PartnerApiKey stores only a hash.
    # Any key issued earlier stays valid, exactly as it does when an admin issues a second key
    # from the admin page; revoking one is a separate, deliberate action.
    data_partner = application.data_partner
    if data_partner is not None:
        from afc_partner_api import auth as partner_auth
        from afc_partner_api.models import PartnerApiKey

        full_key, prefix, key_hash = partner_auth.generate_key()
        PartnerApiKey.objects.create(
            partner=data_partner,
            key_prefix=prefix,
            key_hash=key_hash,
            label=f"Issued on approval ({application.reference})",
        )
        payload["api_key"] = full_key

    # Burn the link. Stamped AFTER the credentials exist, so a failure above does not consume the
    # applicant's one chance.
    application.claimed_at = timezone.now()
    application.save(update_fields=["claimed_at", "updated_at"])

    return Response(payload, status=status.HTTP_200_OK)
