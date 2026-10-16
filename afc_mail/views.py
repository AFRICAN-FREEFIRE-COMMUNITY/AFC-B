"""
afc_mail/views.py - every address of bulk marketing email (prefix mail/, afc/urls.py).

PUBLIC (no session; the opaque recipient token IS the key, 32 hex characters):
  GET  mail/o/<token>.gif               the open pixel: counts the open, answers a 1x1 GIF
  GET  mail/c/<token>/<i>/              a tracked link: counts the click, 302 to the campaign's own
                                        link number i (never anywhere else: R85, no open redirect)
  GET  mail/unsubscribe/<token>/        what the site's unsubscribe page shows (masked address, state);
                                        changes NOTHING, because mail scanners open links
  POST mail/unsubscribe/<token>/        unsubscribe: the page's button AND RFC 8058 one-click from the
                                        mail app (body "List-Unsubscribe=One-Click")
  POST mail/resubscribe/<token>/        undo, from the same page
  POST mail/ses-events/                 Amazon SNS: SES delivery/bounce/complaint (afc_mail.sns, R72)
SIGNED-IN USER:
  GET|POST mail/preferences/            {marketing_opt_in}: the consent toggle on the account page and
                                        the one-time question
HEAD ADMIN (afc_auth.views_account_deletion._require_head_admin), the composer and tracker under
app/(a)/a/email-news/ in AFC_Frontend (lib/mailApi.ts):
  GET|POST   mail/campaigns/                       list (limit/offset, stats) | create a draft
  GET|PATCH|DELETE mail/campaigns/<token>/         detail + tracker numbers | edit a draft | discard a draft
  POST       mail/campaigns/<token>/start-from/    replace a draft's content with a template's or a sent
                                                   email's ({"template"} | {"campaign"} | {"blank": true})
  POST|DELETE mail/campaigns/<token>/design/       upload (multipart file + language) | remove own design
  GET|POST   mail/campaigns/<token>/audience/      how many it would reach
  GET        mail/campaigns/<token>/preview/?language=fr   the email exactly as a reader gets it
  POST       mail/campaigns/<token>/test/          send it to the admin's own address (tracking off)
  POST       mail/campaigns/<token>/send/          freeze the recipients and start sending
  POST       mail/campaigns/<token>/cancel/        stop; unsent rows become skipped
  POST       mail/audience/                        counts AND the picked players/teams left out, and why
  POST       mail/assets/                          upload a picture or a font (multipart file + kind)
  GET        mail/assets/<token>/                  its bytes, for the composer (a font to show, a picture to
                                                   draw on the designed-picture canvas: the media host sends no
                                                   CORS headers, this API does)
  GET|POST   mail/templates/                       list | save a campaign's content as a template
  DELETE     mail/templates/<token>/               delete a template
Envelope: success bodies are plain objects; refusals are {"message", "code"} (owner rule R35).
"""
import base64

from django.db.models import Count, F
from django.http import FileResponse, HttpResponse, HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.audience import parse_audience_spec, spec_is_empty
from afc_auth.views_account_deletion import _require_head_admin, _require_user

from . import assets as asset_store
from . import sns
from .addresses import frontend_base
from .content import campaign_links, render_preview, render_test
from .designs import DesignError, clean_design
from .document import DocError, doc_is_empty, doc_tokens, parse_doc
from .models import LANGS, Campaign, CampaignClick, CampaignRecipient, EmailSubscription, EmailTemplate
from .recipients import audience_count, campaign_spec, freeze_recipients, left_out, picked, set_consent, suppress
from .style import font_tokens, parse_style

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _refuse(message, code, http=status.HTTP_400_BAD_REQUEST):
    return Response({"message": message, "code": code}, status=http)


def _mask(email):
    local, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"


# ── public: tracking ──────────────────────────────────────────────────────────────────────────

@api_view(["GET"])
def open_pixel(request, token):
    row = CampaignRecipient.objects.filter(token=token).first()
    if row is not None:
        now = timezone.now()
        CampaignRecipient.objects.filter(pk=row.pk).update(open_count=F("open_count") + 1)  # in the database: parallel loads all count
        if row.first_opened_at is None:
            CampaignRecipient.objects.filter(pk=row.pk, first_opened_at__isnull=True).update(first_opened_at=now)
    resp = HttpResponse(PIXEL, content_type="image/gif")
    resp["Cache-Control"] = "no-store, private"
    return resp


@api_view(["GET"])
def click(request, token, index):
    row = CampaignRecipient.objects.select_related("campaign").filter(token=token).first()
    links = (row.campaign.links if row else None) or []
    url = links[index] if row is not None and 0 <= index < len(links) else None
    if not (isinstance(url, str) and url.startswith(("https://", "http://"))):
        # A reader who clicks a link we cannot place (mistyped, or from an email we no longer hold)
        # lands on AFC's home page, never on a bare error from the API host. Nothing is counted.
        return HttpResponseRedirect(frontend_base() + "/")
    now = timezone.now()
    CampaignClick.objects.create(recipient=row, link_index=index)
    # Counted in the database, so two clicks at the same moment both count.
    CampaignRecipient.objects.filter(pk=row.pk).update(click_count=F("click_count") + 1)
    if row.first_clicked_at is None:
        CampaignRecipient.objects.filter(pk=row.pk, first_clicked_at__isnull=True).update(first_clicked_at=now)
    # A click proves the message was opened, even when images were blocked.
    if row.first_opened_at is None:
        CampaignRecipient.objects.filter(pk=row.pk, first_opened_at__isnull=True).update(first_opened_at=now)
    return HttpResponseRedirect(url)


# ── public: unsubscribe ───────────────────────────────────────────────────────────────────────

def _subscribed(row):
    if row.user_id is None:
        return False
    sub = EmailSubscription.objects.filter(user_id=row.user_id).first()
    return bool(sub and sub.marketing_opt_in)


@api_view(["GET", "POST"])
def unsubscribe(request, token):
    row = CampaignRecipient.objects.filter(token=token).first()
    if row is None:
        return _refuse("This unsubscribe link is not valid.", "mail_unsubscribe_not_found", status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        return Response({"email": _mask(row.email), "subscribed": _subscribed(row)})
    if row.user_id:
        set_consent(row.user, False, "unsubscribe_link")
    else:
        suppress(row.email, "unsubscribe")
    if row.unsubscribed_at is None:
        CampaignRecipient.objects.filter(pk=row.pk).update(unsubscribed_at=timezone.now())
    return Response({"message": "You are unsubscribed.", "code": "unsubscribed", "subscribed": False})


@api_view(["POST"])
def resubscribe(request, token):
    row = CampaignRecipient.objects.filter(token=token).first()
    if row is None or row.user_id is None:
        return _refuse("This link is not valid.", "mail_unsubscribe_not_found", status.HTTP_404_NOT_FOUND)
    set_consent(row.user, True, "resubscribe_link")
    return Response({"message": "You are subscribed again.", "code": "resubscribed", "subscribed": True})


# ── public: SES events through SNS ────────────────────────────────────────────────────────────

@api_view(["POST"])
def ses_events(request):
    try:
        result = sns.handle(request.body)
    except sns.Refused as refused:
        return _refuse("Not accepted.", refused.code)
    return Response({"result": result})


# ── signed-in: consent ────────────────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
def preferences(request):
    user, err = _require_user(request)
    if err:
        return err
    if request.method == "POST":
        value = request.data.get("marketing_opt_in")
        if not isinstance(value, bool):
            return _refuse("marketing_opt_in must be true or false.", "mail_preference_invalid")
        source = request.data.get("source")
        source = source if source in ("settings", "prompt") else "settings"
        set_consent(user, value, source)
    sub = EmailSubscription.objects.filter(user=user).first()
    return Response({
        "marketing_opt_in": bool(sub and sub.marketing_opt_in),
        "opted_in_at": sub.opted_in_at if sub else None,
        # Has this person ever answered (yes or no)? The home page asks once, until they do.
        "answered": bool(sub and (sub.opted_in_at or sub.opted_out_at)),
    })


# ── head admin: campaigns ─────────────────────────────────────────────────────────────────────

LANG_WORD = {"en": "English", "fr": "French", "pt": "Portuguese"}
TEXT_FIELDS = {"name": 120}
TEXT_FIELDS.update({f"subject_{lang}": 150 for lang in LANGS})
TEXT_FIELDS.update({f"preheader_{lang}": 160 for lang in LANGS})
CONTENT_KEYS = ("mode", "docs", "style", "designs") + tuple(f"subject_{l}" for l in LANGS) + tuple(f"preheader_{l}" for l in LANGS)


def _clean_style(raw):
    return parse_style(raw, font_assets=asset_store.load(font_tokens(raw), kind="font"))


def _apply_fields(campaign, data, *, creating):
    """Write the named fields only (owner rule R65), each through its parser. Returns a refusal
    Response or None."""
    for field, cap in TEXT_FIELDS.items():
        if field not in data:
            continue
        value = data.get(field)
        if not isinstance(value, str):
            return _refuse(f"{field} must be text.", "mail_field_invalid")
        if len(value) > cap:
            return _refuse(f"{field} is longer than {cap} characters.", "mail_field_too_long")
        setattr(campaign, field, value)
    # The composer has no separate name field: the campaign is listed by its English subject.
    if "name" not in data and "subject_en" in data and (data.get("subject_en") or "").strip():
        campaign.name = data["subject_en"].strip()[:120]
    if "mode" in data:
        if data.get("mode") not in ("write", "design"):
            return _refuse("mode must be write or design.", "mail_mode_invalid")
        campaign.mode = data["mode"]
    if "docs" in data:
        docs = data.get("docs")
        if not isinstance(docs, dict):
            return _refuse("docs must be an object.", "mail_doc_invalid")
        clean = dict(campaign.docs or {})
        for lang, raw in docs.items():
            if lang not in LANGS:
                continue
            try:
                clean[lang], _tokens = parse_doc(raw)
            except DocError as err:
                return _refuse(str(err), err.code)
        campaign.docs = clean
    if "style" in data:
        if not isinstance(data.get("style"), dict):
            return _refuse("style must be an object.", "mail_style_invalid")
        campaign.style = _clean_style(data["style"])
    if "designs" in data:
        # Only REMOVING a language's design is done here; a design arrives through the upload
        # endpoint, so it is always cleaned by the server before it is stored.
        designs = data.get("designs")
        if not isinstance(designs, dict) or any(v is not None for v in designs.values()):
            return _refuse("Upload a design through the design address.", "mail_design_upload_only")
        kept = dict(campaign.designs or {})
        for lang in designs:
            kept.pop(lang, None)
        campaign.designs = kept
    if "audience" in data:
        if not isinstance(data.get("audience"), dict):
            return _refuse("audience must be an object.", "mail_audience_invalid")
        campaign.audience = parse_audience_spec({"audience": data["audience"]})
    if creating and not (campaign.name or "").strip():
        campaign.name = (campaign.subject_en or "").strip()[:120] or "Untitled email"
    return None


def stats_for(campaign):
    rows = CampaignRecipient.objects.filter(campaign=campaign)
    by_status = dict(rows.values_list("status").annotate(n=Count("pk")))
    total = sum(by_status.values())
    accepted = sum(by_status.get(s, 0) for s in ("sent", "delivered", "bounced", "complained"))
    return {
        "total": total,
        "queued": by_status.get("queued", 0),
        "sent": accepted,
        "delivered": by_status.get("delivered", 0),
        "bounced": by_status.get("bounced", 0),
        "complained": by_status.get("complained", 0),
        "failed": by_status.get("failed", 0),
        "skipped": by_status.get("skipped", 0),
        "opened": rows.filter(first_opened_at__isnull=False).count(),
        "clicked": rows.filter(first_clicked_at__isnull=False).count(),
        "unsubscribed": rows.filter(unsubscribed_at__isnull=False).count(),
    }


def link_stats(campaign):
    clicks = dict(
        CampaignClick.objects.filter(recipient__campaign=campaign)
        .values_list("link_index").annotate(n=Count("pk"))
    )
    people = dict(
        CampaignClick.objects.filter(recipient__campaign=campaign)
        .values_list("link_index").annotate(n=Count("recipient", distinct=True))
    )
    return [{"index": i, "url": url, "clicks": clicks.get(i, 0), "people": people.get(i, 0)}
            for i, url in enumerate(campaign.links or [])]


def _assets_of(content):
    """{token: {url, ...}} for every picture and font the content names, so the composer can show
    them (documents hold tokens, never addresses)."""
    tokens = []
    for doc in (content.get("docs") or {}).values():
        tokens += doc_tokens(doc)
    tokens += font_tokens(content.get("style") or {})
    found = asset_store.load(tokens)
    return {key: asset_store.serialize(a) for key, a in found.items()}


def _content(obj):
    """The content fields of a campaign (or a template's data) as one dict."""
    if isinstance(obj, Campaign):
        data = {k: getattr(obj, k) for k in CONTENT_KEYS}
    else:
        data = {k: obj.get(k) for k in CONTENT_KEYS if k in obj}
    return data


def _designs_out(designs):
    return {lang: {"name": d.get("name"), "size": d.get("size"), "html": d.get("html"), "report": d.get("report")}
            for lang, d in (designs or {}).items() if d}


def serialize(campaign, *, full=False):
    out = {
        "token": campaign.token,
        "name": campaign.name,
        "subject_en": campaign.subject_en,
        "mode": campaign.mode,
        "status": campaign.status,
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
        "queued_at": campaign.queued_at,
        "finished_at": campaign.finished_at,
        "created_by": getattr(campaign.created_by, "username", None),
        "stats": stats_for(campaign),
    }
    if full:
        out.update({k: getattr(campaign, k) for k in TEXT_FIELDS})
        out.update({
            "docs": campaign.docs or {},
            "style": campaign.style or _clean_style({}),
            "designs": _designs_out(campaign.designs),
            "assets": _assets_of(_content(campaign)),
            "audience": campaign.audience,
            "links": link_stats(campaign),
        })
    return out


def _paging(request):
    try:
        limit = min(50, max(1, int(request.query_params.get("limit", 20))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        return None, None
    return limit, offset


def _page(qs, request, serializer):
    limit, offset = _paging(request)
    if limit is None:
        return _refuse("limit and offset must be numbers.", "mail_paging_invalid")
    total = qs.count()
    page = list(qs[offset:offset + limit])
    has_more = offset + len(page) < total
    return Response({
        "results": [serializer(x) for x in page],
        "total_count": total,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
    })


@api_view(["GET", "POST"])
def campaigns(request):
    admin, err = _require_head_admin(request)
    if err:
        return err
    if request.method == "POST":
        campaign = Campaign(created_by=admin, style=_clean_style({}))
        refusal = _apply_fields(campaign, request.data, creating=True)
        if refusal:
            return refusal
        campaign.save()
        return Response(serialize(campaign, full=True), status=status.HTTP_201_CREATED)
    return _page(Campaign.objects.select_related("created_by"), request, serialize)


def _campaign_or_404(token):
    return Campaign.objects.select_related("created_by").filter(token=token).first()


def _not_found():
    return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)


def _not_draft():
    return _refuse("Only a draft can be changed.", "mail_campaign_not_draft", status.HTTP_409_CONFLICT)


@api_view(["GET", "PATCH", "DELETE"])
def campaign_detail(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    if request.method == "DELETE":
        if campaign.status != "draft":
            return _not_draft()
        campaign.delete()
        return Response({"message": "Draft deleted.", "code": "mail_draft_deleted"})
    if request.method == "PATCH":
        if campaign.status != "draft":
            return _not_draft()
        refusal = _apply_fields(campaign, request.data, creating=False)
        if refusal:
            return refusal
        campaign.save()
    return Response(serialize(campaign, full=True))


@api_view(["POST"])
def campaign_start_from(request, token):
    """Replace a draft's content (subjects, preview lines, mode, documents, style, designs) with a
    template's, a sent email's, or nothing. The audience is kept: it is chosen per email."""
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    if campaign.status != "draft":
        return _not_draft()
    if request.data.get("blank"):
        source = {"mode": "write", "docs": {}, "designs": {}, "style": _clean_style({}),
                  **{k: "" for k in TEXT_FIELDS if k != "name"}}
    elif request.data.get("template"):
        tpl = EmailTemplate.objects.filter(token=request.data["template"]).first()
        if tpl is None:
            return _refuse("No such template.", "mail_template_not_found", status.HTTP_404_NOT_FOUND)
        source = _content(tpl.data or {})
    elif request.data.get("campaign"):
        other = Campaign.objects.filter(token=request.data["campaign"]).first()
        if other is None:
            return _not_found()
        source = _content(other)
    else:
        return _refuse("Say what to start from.", "mail_start_from_invalid")
    # Stored content was checked when it was stored; it is copied as it is, designs included (an
    # uploaded design is only ever stored after clean_design).
    for key in CONTENT_KEYS:
        if key in source and source[key] is not None:
            setattr(campaign, key, source[key])
    if (campaign.subject_en or "").strip():
        campaign.name = campaign.subject_en.strip()[:120]
    campaign.save()
    return Response(serialize(campaign, full=True))


@api_view(["POST", "DELETE"])
def campaign_design(request, token):
    """POST (multipart: file, language): upload this language's own design; the answer carries what
    was done to it. DELETE ?language=: remove it."""
    admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    if campaign.status != "draft":
        return _not_draft()
    language = (request.data.get("language") if request.method == "POST" else request.query_params.get("language")) or "en"
    if language not in LANGS:
        return _refuse("language must be en, fr or pt.", "mail_language_invalid")
    designs = dict(campaign.designs or {})
    if request.method == "DELETE":
        designs.pop(language, None)
        campaign.designs = designs
        campaign.save(update_fields=["designs", "updated_at"])
        return Response(serialize(campaign, full=True))
    upload = request.FILES.get("file")
    if upload is None:
        return _refuse("Choose a file.", "mail_design_empty")
    if upload.size > 2 * 1024 * 1024:
        return _refuse("That file is bigger than 2 MB.", "mail_design_too_big")
    try:
        cleaned = clean_design(upload.name, upload.read(), admin)
    except DesignError as err:
        return _refuse(str(err), err.code)
    designs[language] = cleaned
    campaign.designs = designs
    campaign.save(update_fields=["designs", "updated_at"])
    return Response(serialize(campaign, full=True))


@api_view(["GET", "POST"])
def campaign_audience(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    spec = parse_audience_spec(request.data) if request.method == "POST" else campaign_spec(campaign)
    return Response({**audience_count(spec), "left_out": left_out(spec), "picked": picked(spec)})


@api_view(["POST"])
def audience(request):
    """The To line's numbers before anything is saved: {selected, recipients, left_out, picked}."""
    _admin, err = _require_head_admin(request)
    if err:
        return err
    spec = parse_audience_spec(request.data)
    return Response({**audience_count(spec), "left_out": left_out(spec), "picked": picked(spec)})


@api_view(["GET"])
def campaign_preview(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    language = request.query_params.get("language", "en")
    subject, html_body = render_preview(campaign, language if language in LANGS else "en")
    return Response({"subject": subject, "html": html_body, "size_kb": max(1, round(len(html_body.encode("utf-8")) / 1024))})


def _ready_to_send(campaign):
    if not (campaign.subject_en or "").strip():
        return _refuse("The English subject is required.", "mail_english_required")
    if campaign.mode == "design":
        if not (campaign.designs or {}).get("en"):
            return _refuse("Upload the English design.", "mail_english_required")
    elif doc_is_empty((campaign.docs or {}).get("en")):
        return _refuse("Write the English email.", "mail_english_required")
    for lang in ("fr", "pt"):
        subject = bool((getattr(campaign, f"subject_{lang}") or "").strip())
        if campaign.mode == "design":
            content = bool((campaign.designs or {}).get(lang))
        else:
            content = not doc_is_empty((campaign.docs or {}).get(lang))
        if subject != content:
            return _refuse(f"The {LANG_WORD[lang]} version needs both a subject and an email, or neither.", f"mail_version_incomplete_{lang}")
    return None


@api_view(["POST"])
def campaign_test(request, token):
    admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    refusal = _ready_to_send(campaign)
    if refusal:
        return refusal
    if not (admin.email or "").strip():
        return _refuse("Your account has no email address.", "mail_admin_no_email")
    language = request.data.get("language", "en")
    from .transport import send_one
    subject, html_body, text_body = render_test(campaign, language if language in LANGS else "en")
    ok = send_one(admin.email, f"[TEST] {subject}", html_body, text_body,
                  one_click_url="https://africanfreefirecommunity.com", page_url="https://africanfreefirecommunity.com",
                  tags={"campaign": campaign.token, "recipient": "test"})
    if not ok:
        return _refuse("The mail server refused the test message.", "mail_test_refused", status.HTTP_502_BAD_GATEWAY)
    return Response({"message": "Test sent.", "code": "mail_test_sent", "to": _mask(admin.email)})


@api_view(["POST"])
def campaign_send(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    if campaign.status != "draft":
        return _refuse("This campaign has already been sent or cancelled.", "mail_campaign_not_draft", status.HTTP_409_CONFLICT)
    refusal = _ready_to_send(campaign)
    if refusal:
        return refusal
    if spec_is_empty(campaign_spec(campaign)):
        return _refuse("Choose who should receive it.", "mail_audience_empty")
    # Claim the draft atomically, so a double click cannot freeze the recipients twice.
    claimed = Campaign.objects.filter(pk=campaign.pk, status="draft").update(status="queued", queued_at=timezone.now())
    if not claimed:
        return _refuse("This campaign has already been sent or cancelled.", "mail_campaign_not_draft", status.HTTP_409_CONFLICT)
    campaign.refresh_from_db()
    campaign.links = campaign_links(campaign)
    campaign.save(update_fields=["links"])
    count = freeze_recipients(campaign)
    if count == 0:
        Campaign.objects.filter(pk=campaign.pk).update(status="draft", queued_at=None)
        return _refuse("Nobody in this audience has agreed to receive email yet.", "mail_no_recipients")
    from .tasks import send_campaign_chunk
    send_campaign_chunk.delay(campaign.pk)
    return Response({"message": "Sending started.", "code": "mail_sending", "recipients": count})


@api_view(["POST"])
def campaign_cancel(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _not_found()
    if campaign.status not in ("queued", "sending"):
        return _refuse("Only a campaign that is sending can be stopped.", "mail_campaign_not_sending", status.HTTP_409_CONFLICT)
    from .tasks import cancel
    cancel(campaign)
    return Response({"message": "Stopped.", "code": "mail_cancelled"})


# ── head admin: pictures and fonts ────────────────────────────────────────────────────────────

ASSET_REFUSALS = {
    asset_store.NOT_AN_IMAGE: ("That file is not a picture AFC can use (JPEG, PNG, WEBP or GIF).", "mail_not_an_image"),
    asset_store.IMAGE_TOO_LARGE: ("That picture is bigger than 5 MB.", "mail_image_too_large"),
    asset_store.NOT_A_FONT: ("That is not a font file (WOFF2, WOFF, TTF or OTF).", "mail_not_a_font"),
    asset_store.FONT_TOO_LARGE: ("That font is bigger than 5 MB.", "mail_font_too_large"),
}


@api_view(["POST"])
def assets(request):
    """multipart: file, kind ("image" | "font"). 201 {token, kind, url, ...}."""
    admin, err = _require_head_admin(request)
    if err:
        return err
    kind = request.data.get("kind")
    upload = request.FILES.get("file")
    if kind not in ("image", "font"):
        return _refuse("kind must be image or font.", "mail_asset_kind_invalid")
    if upload is None:
        return _refuse("Choose a file.", "mail_asset_missing")
    asset, code = (asset_store.store_image if kind == "image" else asset_store.store_font)(upload, admin)
    if code:
        message, refusal_code = ASSET_REFUSALS.get(code, ("That file could not be used.", "mail_asset_refused"))
        return _refuse(message, refusal_code)
    return Response(asset_store.serialize(asset), status=status.HTTP_201_CREATED)


@api_view(["GET"])
def asset_file(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    asset = asset_store.load([token]).get(token)
    if asset is None:
        return _refuse("No such file.", "mail_asset_not_found", status.HTTP_404_NOT_FOUND)
    resp = FileResponse(asset.file.open("rb"), content_type=asset.content_type)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, max-age=3600"
    return resp


# ── head admin: templates ─────────────────────────────────────────────────────────────────────

def serialize_template(tpl):
    data = tpl.data or {}
    return {
        "token": tpl.token,
        "name": tpl.name,
        "mode": data.get("mode", "write"),
        "subject_en": data.get("subject_en", ""),
        "source_campaign": tpl.source_campaign.token if tpl.source_campaign_id else None,
        "created_by": getattr(tpl.created_by, "username", None),
        "created_at": tpl.created_at,
    }


@api_view(["GET", "POST"])
def templates(request):
    """GET: list (paged). POST {campaign, name?}: save that campaign's content (a draft being written,
    or an email already sent) as a template."""
    admin, err = _require_head_admin(request)
    if err:
        return err
    if request.method == "GET":
        return _page(EmailTemplate.objects.select_related("created_by", "source_campaign"), request, serialize_template)
    campaign = Campaign.objects.filter(token=request.data.get("campaign")).first()
    if campaign is None:
        return _not_found()
    name = request.data.get("name")
    if name is not None and not isinstance(name, str):
        return _refuse("name must be text.", "mail_field_invalid")
    name = (name or "").strip()[:120] or (campaign.subject_en or campaign.name or "Template").strip()[:120]
    tpl = EmailTemplate.objects.create(name=name, data=_content(campaign), source_campaign=campaign, created_by=admin)
    return Response(serialize_template(tpl), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def template_detail(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    deleted, _ = EmailTemplate.objects.filter(token=token).delete()
    if not deleted:
        return _refuse("No such template.", "mail_template_not_found", status.HTTP_404_NOT_FOUND)
    return Response({"message": "Template deleted.", "code": "mail_template_deleted"})
