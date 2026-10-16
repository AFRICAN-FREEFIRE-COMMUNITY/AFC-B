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
  GET|POST mail/preferences/            {marketing_opt_in}: the consent toggle on the account page
HEAD ADMIN (afc_auth.views_account_deletion._require_head_admin):
  GET|POST mail/campaigns/              list (limit/offset, stats) | create a draft
  GET|PATCH mail/campaigns/<token>/     detail with the tracker numbers and per-link clicks | edit a draft
  GET|POST mail/campaigns/<token>/audience/   how many it would reach (POST: try another audience)
  GET  mail/campaigns/<token>/preview/?language=fr   the email as a reader in that language gets it
  POST mail/campaigns/<token>/test/     send it to the admin's own address (tracking off)
  POST mail/campaigns/<token>/send/     freeze the recipients and start sending
  POST mail/campaigns/<token>/cancel/   stop; unsent rows become skipped
Envelope: success bodies are plain objects; refusals are {"message", "code"} (owner rule R35).
Frontend callers (built after the owner approves mockups/bulk-email/): the account settings consent
toggle, /email/unsubscribe/[token], and the admin composer + tracker under /a/.
"""
import base64

from django.db.models import Count
from django.http import HttpResponse, HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.audience import parse_audience_spec, spec_is_empty
from afc_auth.views_account_deletion import _require_head_admin, _require_user

from . import sns
from .content import extract_links, render_preview, render_test
from .models import Campaign, CampaignClick, CampaignRecipient, EmailSubscription
from .recipients import audience_count, campaign_spec, freeze_recipients, set_consent, suppress

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
MAX_BODY = 20000
LANGS = ("en", "fr", "pt")


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
        CampaignRecipient.objects.filter(pk=row.pk).update(open_count=row.open_count + 1)
        if row.first_opened_at is None:
            CampaignRecipient.objects.filter(pk=row.pk, first_opened_at__isnull=True).update(first_opened_at=now)
    resp = HttpResponse(PIXEL, content_type="image/gif")
    resp["Cache-Control"] = "no-store, private"
    return resp


@api_view(["GET"])
def click(request, token, index):
    row = CampaignRecipient.objects.select_related("campaign").filter(token=token).first()
    links = (row.campaign.links if row else None) or []
    if row is None or not (0 <= index < len(links)):
        return _refuse("This link is not valid.", "mail_link_not_found", status.HTTP_404_NOT_FOUND)
    url = links[index]
    if not (isinstance(url, str) and url.startswith(("https://", "http://"))):
        return _refuse("This link is not valid.", "mail_link_not_found", status.HTTP_404_NOT_FOUND)
    now = timezone.now()
    CampaignClick.objects.create(recipient=row, link_index=index)
    CampaignRecipient.objects.filter(pk=row.pk).update(click_count=row.click_count + 1)
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
    })


# ── head admin: campaigns ─────────────────────────────────────────────────────────────────────

FIELDS = {
    "name": 120,
    "subject_en": 150, "body_en": MAX_BODY,
    "subject_fr": 150, "body_fr": MAX_BODY,
    "subject_pt": 150, "body_pt": MAX_BODY,
}


def _apply_fields(campaign, data, *, creating):
    """Write the named fields only (owner rule R65). Returns a refusal Response or None."""
    for field, cap in FIELDS.items():
        if field not in data:
            continue
        value = data.get(field)
        if not isinstance(value, str):
            return _refuse(f"{field} must be text.", "mail_field_invalid")
        if len(value) > cap:
            return _refuse(f"{field} is longer than {cap} characters.", "mail_field_too_long")
        setattr(campaign, field, value)
    if "audience" in data:
        if not isinstance(data.get("audience"), dict):
            return _refuse("audience must be an object.", "mail_audience_invalid")
        campaign.audience = parse_audience_spec({"audience": data["audience"]})
    if creating and not (campaign.name or "").strip():
        return _refuse("Give the campaign a name.", "mail_name_required")
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


def serialize(campaign, *, full=False):
    out = {
        "token": campaign.token,
        "name": campaign.name,
        "subject_en": campaign.subject_en,
        "status": campaign.status,
        "created_at": campaign.created_at,
        "queued_at": campaign.queued_at,
        "finished_at": campaign.finished_at,
        "created_by": getattr(campaign.created_by, "username", None),
        "stats": stats_for(campaign),
    }
    if full:
        out.update({
            "body_en": campaign.body_en,
            "subject_fr": campaign.subject_fr, "body_fr": campaign.body_fr,
            "subject_pt": campaign.subject_pt, "body_pt": campaign.body_pt,
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


@api_view(["GET", "POST"])
def campaigns(request):
    admin, err = _require_head_admin(request)
    if err:
        return err
    if request.method == "POST":
        campaign = Campaign(created_by=admin)
        refusal = _apply_fields(campaign, request.data, creating=True)
        if refusal:
            return refusal
        campaign.save()
        return Response(serialize(campaign, full=True), status=status.HTTP_201_CREATED)
    limit, offset = _paging(request)
    if limit is None:
        return _refuse("limit and offset must be numbers.", "mail_paging_invalid")
    qs = Campaign.objects.select_related("created_by")
    total = qs.count()
    page = list(qs[offset:offset + limit])
    has_more = offset + len(page) < total
    return Response({
        "results": [serialize(c) for c in page],
        "total_count": total,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
    })


def _campaign_or_404(token):
    return Campaign.objects.select_related("created_by").filter(token=token).first()


@api_view(["GET", "PATCH"])
def campaign_detail(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        if campaign.status != "draft":
            return _refuse("Only a draft can be edited.", "mail_campaign_not_draft", status.HTTP_409_CONFLICT)
        refusal = _apply_fields(campaign, request.data, creating=False)
        if refusal:
            return refusal
        campaign.save()
    return Response(serialize(campaign, full=True))


@api_view(["GET", "POST"])
def campaign_audience(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
    spec = parse_audience_spec(request.data) if request.method == "POST" else campaign_spec(campaign)
    return Response(audience_count(spec))


@api_view(["GET"])
def campaign_preview(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
    language = request.query_params.get("language", "en")
    subject, html_body = render_preview(campaign, language if language in LANGS else "en")
    return Response({"subject": subject, "html": html_body})


def _ready_to_send(campaign):
    if not (campaign.subject_en or "").strip() or not (campaign.body_en or "").strip():
        return _refuse("The English subject and text are required.", "mail_english_required")
    for lang in ("fr", "pt"):
        subject = (getattr(campaign, f"subject_{lang}") or "").strip()
        body = (getattr(campaign, f"body_{lang}") or "").strip()
        if bool(subject) != bool(body):
            return _refuse(f"The {lang} version needs both a subject and a text, or neither.", "mail_version_incomplete")
    return None


@api_view(["POST"])
def campaign_test(request, token):
    admin, err = _require_head_admin(request)
    if err:
        return err
    campaign = _campaign_or_404(token)
    if campaign is None:
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
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
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
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
    campaign.links = extract_links(campaign.body_en, campaign.body_fr, campaign.body_pt)
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
        return _refuse("No such campaign.", "mail_campaign_not_found", status.HTTP_404_NOT_FOUND)
    if campaign.status not in ("queued", "sending"):
        return _refuse("Only a campaign that is sending can be stopped.", "mail_campaign_not_sending", status.HTTP_409_CONFLICT)
    from .tasks import cancel
    cancel(campaign)
    return Response({"message": "Stopped.", "code": "mail_cancelled"})
