"""
afc_partner_apply.views_admin - the owner's queue, and the decision that provisions a partner.

PURPOSE
    Read what organisations have applied for, and turn one of those applications into a real
    partner in a single action, with the fields already filled in and still editable.

WHY A REVIEW SCREEN RATHER THAN AN APPROVE BUTTON
    A one-click approve was the obvious design and it is the wrong one, for a reason that has
    nothing to do with convenience: every field on an application is UNTRUSTED INPUT typed by
    somebody AFC has not met, and two of those fields are player-facing. `display_name` is
    rendered on the consent screen, the page where a player decides whether to trust this
    organisation with their data. `redirect_uris` decides where AFC hands over an authorization
    code. One click means whatever the applicant typed goes live on both.
    So decide_application takes the fields IN ITS BODY, the frontend prefills them from the
    application, and the owner approves what is on the screen rather than what is in the row. In
    the common case they change nothing and it is still one action.

    The data grants ride in the same body for the same reason: what a partner may see is the
    decision, and it belongs in the same moment as "yes". Every toggle still starts OFF, and an
    approval that ticks none produces a partner who can sign a player in and learn nothing about
    them beyond the fact that it worked.

PROVISIONING GOES THROUGH THE SHARED PATH
    afc_sso/provisioning.py provision_sso_application, the same function the staff create endpoint
    calls, so an approved application and a hand-typed one cannot end up with different rules
    applied. Data API partners are created with the same Partner row shape as
    afc_partner_api/views_admin.py create_partner.

ENDPOINTS (mounted at partner-apply/ via afc/urls.py)
    GET   partner-apply/admin/applications/                    list_applications
    GET   partner-apply/admin/applications/<id>/               application_detail
    POST  partner-apply/admin/applications/<id>/decide/        decide_application
    POST  partner-apply/admin/applications/<id>/resend-credentials/  resend_credentials

CONSUMED BY
    frontend app/(a)/a/partners/_components/PartnerApplicationsPanel.tsx, the "Applications" tab
    of app/(a)/a/partners/page.tsx, through frontend/lib/partnerApply.ts.
"""
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from afc_partner_api.models import PARTNER_TOGGLE_FIELDS, Partner
from afc_sso.models import SSO_FIELD_TOGGLES
from afc_sso.provisioning import provision_sso_application

from . import emails
from .models import PartnerApplication

# The same two roles that manage both partner products manage the queue that feeds them, because
# approving an application IS provisioning a partner. Defined here rather than imported from
# afc_sso: each app in this codebase declares its own gate with the same shape
# (afc_sso.admin_api._is_sso_admin, afc_partner_api.views_admin._is_partner_admin,
# afc_feedback.views.is_feedback_admin), and reaching into another app's private helper to save
# four lines would be the one place the pattern broke.
APPLY_ADMIN_ROLES = ("head_admin", "partner_admin")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _is_apply_admin(user) -> bool:
    """True for AFC staff entitled to read and decide partner applications.

    The role name lives on the related Roles row, reached through the UserRoles join, so we filter
    ``role__role_name__in`` - NEVER ``role_name__in`` (UserRoles itself has no role_name column;
    that field is on Roles).
    """
    return bool(user) and \
        user.userroles.filter(role__role_name__in=APPLY_ADMIN_ROLES).exists()


def _require_apply_admin(request):
    """Header parse + token validation + role check, resolved once for every view.

    Returns (user, error_response): exactly one is non-None. Status codes and wording match
    afc_sso.admin_api._require_sso_admin so the frontend's one error-toast idiom covers both tabs
    of the same page.
    """
    session_token = request.headers.get("Authorization")
    if not session_token:
        return None, Response({"message": "Authorization header is required"},
                             status=status.HTTP_400_BAD_REQUEST)
    if not session_token.startswith("Bearer "):
        return None, Response({"message": "Invalid token format"},
                             status=status.HTTP_400_BAD_REQUEST)

    from afc_auth.views import validate_token  # local import: avoids an app-loading cycle

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."},
                             status=status.HTTP_401_UNAUTHORIZED)
    if not _is_apply_admin(user):
        return None, Response(
            {"message": "You do not have permission to review partner applications."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return user, None


def _application_or_404(application_id):
    application = PartnerApplication.objects.filter(pk=application_id).first()
    if not application:
        return None, Response({"message": "Application not found."},
                              status=status.HTTP_404_NOT_FOUND)
    return application, None


# ── serializers ───────────────────────────────────────────────────────────────────────────────
def _serialize_summary(application):
    """Lean row for the queue table. Enough to triage without opening anything."""
    return {
        "id": application.id,
        "reference": application.reference,
        "organisation_name": application.organisation_name,
        "contact_email": application.contact_email,
        "country": application.country,
        "wants_sso": application.wants_sso,
        "wants_data_api": application.wants_data_api,
        "status": application.status,
        "created_at": application.created_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
    }


def _serialize_detail(application, request=None):
    """Everything the review screen prefills its form from, plus the two things that inform the
    decision but are not fields on it.

    `earlier_applications` is the count of OTHER applications from the same contact address. It is
    the signal that matters when deciding whether a rejection is being appealed by resubmission,
    which is the one thing the rejected-is-terminal rule makes possible.

    `internal_note` IS here and is deliberately absent from the applicant serializer.
    """
    logo_url = ""
    if application.logo:
        try:
            logo_url = application.logo.url
            if request is not None:
                logo_url = request.build_absolute_uri(logo_url)
        except ValueError:
            # FileField.url raises when no file is associated. A missing logo is never fatal.
            logo_url = ""

    out = _serialize_summary(application)
    out.update({
        "display_name": application.display_name,
        "homepage_url": application.homepage_url,
        "contact_name": application.contact_name,
        "contact_role": application.contact_role,
        "redirect_uris": application.redirect_uris,
        "post_logout_redirect_uris": application.post_logout_redirect_uris,
        "deletion_webhook_url": application.deletion_webhook_url,
        "use_case": application.use_case,
        "data_needed": application.data_needed,
        "locale": application.locale,
        "logo_url": logo_url,
        "decision_note": application.decision_note,
        "internal_note": application.internal_note,
        "reviewed_by": application.reviewed_by.username if application.reviewed_by else None,
        "reviewed_at": application.reviewed_at.isoformat() if application.reviewed_at else None,
        # What approval produced, so the owner can jump straight to the provisioned partner.
        "sso_application_id": application.sso_application_id,
        "client_id": (
            application.sso_application.client_id if application.sso_application else None
        ),
        "data_partner_slug": (
            application.data_partner.slug if application.data_partner else None
        ),
        "claim_is_open": application.claim_is_open(),
        "claimed_at": application.claimed_at.isoformat() if application.claimed_at else None,
        "claim_expires_at": (
            application.claim_expires_at.isoformat() if application.claim_expires_at else None
        ),
        "earlier_applications": PartnerApplication.objects.filter(
            contact_email__iexact=application.contact_email
        ).exclude(pk=application.pk).count(),
    })
    return out


def _paginate(request, queryset):
    """?limit (default 25, max 100) + ?offset -> (page, total_count, has_more).

    Same shape as afc_sso.admin_api._paginate, so the admin table binds to one response envelope
    across all three tabs of the partners page.
    """
    try:
        limit = int(request.GET.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 1) list_applications  (GET partner-apply/admin/applications/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def list_applications(request):
    """The queue, newest first.

    REQUEST: optional ?status=pending|changes_requested|approved|rejected, ?product=sso|data_api,
        ?search= (organisation, contact email, contact name or reference), ?limit=, ?offset=.
    RESPONSE 200: {results, total_count, has_more, pending_count}
        `pending_count` is the whole queue's outstanding work, NOT the filtered count, because it
        drives the badge on the tab and a badge that changes when you filter is a lie.
    AUTH: Bearer SessionToken, head_admin or partner_admin.
    CONSUMED BY: frontend PartnerApplicationsPanel.tsx (the table and the tab badge).
    """
    user, err = _require_apply_admin(request)
    if err:
        return err

    qs = PartnerApplication.objects.select_related(
        "reviewed_by", "sso_application", "data_partner")

    status_filter = (request.GET.get("status") or "").strip()
    if status_filter in dict(PartnerApplication.STATUS_CHOICES):
        qs = qs.filter(status=status_filter)

    product = (request.GET.get("product") or "").strip()
    if product == "sso":
        qs = qs.filter(wants_sso=True)
    elif product == "data_api":
        qs = qs.filter(wants_data_api=True)

    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(organisation_name__icontains=search)
            | Q(contact_email__icontains=search)
            | Q(contact_name__icontains=search)
            | Q(reference__icontains=search)
        )

    page, total_count, has_more = _paginate(request, qs)
    return Response(
        {
            "results": [_serialize_summary(a) for a in page],
            "total_count": total_count,
            "has_more": has_more,
            "pending_count": PartnerApplication.objects.filter(
                status__in=(PartnerApplication.PENDING, PartnerApplication.CHANGES_REQUESTED)
            ).count(),
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 2) application_detail  (GET partner-apply/admin/applications/<id>/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def application_detail(request, application_id):
    """One application in full: what the review screen prefills its editable form from.

    RESPONSE 200: {"application": {...detail...}}, 404 when the id is unknown.
    AUTH: Bearer SessionToken, head_admin or partner_admin.
    CONSUMED BY: frontend PartnerApplicationsPanel.tsx (the review sheet).
    """
    user, err = _require_apply_admin(request)
    if err:
        return err
    application, err = _application_or_404(application_id)
    if err:
        return err
    return Response({"application": _serialize_detail(application, request)},
                    status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 3) decide_application  (POST partner-apply/admin/applications/<id>/decide/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
def decide_application(request, application_id):
    """Approve, reject, or ask the applicant for changes. The one action that matters here.

    REQUEST: {"action": "approve" | "reject" | "request_changes",
              "note": "...",                # to the APPLICANT: the reason, or what to fix
              "internal_note": "...",       # to AFC, never shown to them
              # approve only, all optional, all prefilled by the frontend from the application:
              "name": "...", "display_name": "...",
              "redirect_uris": "...", "post_logout_redirect_uris": "...",
              "homepage_url": "...", "deletion_webhook_url": "...",
              "share_profile": true, "share_email": false, ...}   # the eight grants

    RESPONSE 200: {"message", "application": {...detail...}}
             400 unknown action, a missing required note, or a provisioning validation failure
             409 the application has already been decided
             401/403/404 from the shared gate

    AUTH: Bearer SessionToken, head_admin or partner_admin.
    CONSUMED BY: frontend PartnerApplicationsPanel.tsx (the review sheet's three buttons).

    WHY A NOTE IS REQUIRED TO REJECT OR REQUEST CHANGES, AND OPTIONAL TO APPROVE. A rejection
    without a reason is the thing that generates the email AFC did not want to receive, and
    "please fix it" without saying what is worse than silence. An approval explains itself.

    APPROVAL IS NOT IDEMPOTENT AND MUST NOT BE. It creates a partner and mints credentials, so a
    second approve on an already-decided application is refused with a 409 rather than quietly
    provisioning a second one. That is also why the status check happens before any write.
    """
    user, err = _require_apply_admin(request)
    if err:
        return err
    application, err = _application_or_404(application_id)
    if err:
        return err

    action = str(request.data.get("action") or "").strip()
    if action not in ("approve", "reject", "request_changes"):
        return Response(
            {"message": "Action must be approve, reject or request_changes."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not application.is_open():
        return Response(
            {"message": f"This application has already been {application.status}."},
            status=status.HTTP_409_CONFLICT,
        )

    note = str(request.data.get("note") or "").strip()
    internal_note = str(request.data.get("internal_note") or "").strip()
    if action in ("reject", "request_changes") and not note:
        return Response(
            {
                "message": (
                    "Tell the applicant why. This note is the whole email they receive."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── The two decisions that do not provision anything ──
    if action in ("reject", "request_changes"):
        application.status = (
            PartnerApplication.REJECTED if action == "reject"
            else PartnerApplication.CHANGES_REQUESTED
        )
        application.decision_note = note
        if internal_note:
            application.internal_note = internal_note
        application.reviewed_by = user
        application.reviewed_at = timezone.now()
        application.save(update_fields=[
            "status", "decision_note", "internal_note", "reviewed_by", "reviewed_at",
            "updated_at",
        ])

        if action == "reject":
            emails.send_rejected(application)
        else:
            # A fresh access token: the applicant is being asked to come back and edit, and this
            # is the moment their link has to work. Re-issuing rather than reusing means a
            # forwarded older email stops working, which is the safer default for a link that now
            # grants write access.
            emails.send_changes_requested(application, application.issue_access_token())

        return Response(
            {
                "message": (
                    "Application rejected." if action == "reject"
                    else "Changes requested. The applicant has been emailed."
                ),
                "application": _serialize_detail(application, request),
            },
            status=status.HTTP_200_OK,
        )

    # ── APPROVE: provision, then tell them how to collect ──
    # Fields come from the REQUEST BODY, falling back to what the applicant submitted, because the
    # owner approves what is on their screen. See the module header for why that is not merely a
    # convenience.
    def field(name, fallback):
        value = request.data.get(name)
        return fallback if value is None else value

    sso_application = None
    if application.wants_sso:
        # The eight grants, read only from the whitelist so a hostile or mistyped body cannot set
        # anything else on the model.
        toggles = {f: bool(request.data.get(f)) for f in SSO_FIELD_TOGGLES}

        sso_application, _secret, err_msg = provision_sso_application(
            name=field("name", application.organisation_name),
            display_name=field("display_name", application.display_name),
            redirect_uris=field("redirect_uris", application.redirect_uris),
            post_logout_redirect_uris=field(
                "post_logout_redirect_uris", application.post_logout_redirect_uris),
            homepage_url=field("homepage_url", application.homepage_url),
            deletion_webhook_url=field(
                "deletion_webhook_url", application.deletion_webhook_url),
            # The applicant's own file, carried straight across so nobody re-uploads it. `.file`
            # unwraps the stored FieldFile into something ImageField will save as a new copy;
            # `None` when they never sent one.
            logo_file=application.logo.file if application.logo else None,
            toggles=toggles,
            created_by=user,
        )
        if err_msg:
            # Nothing has been written yet, so the application stays exactly as it was and the
            # owner can fix the offending value on the review screen and press approve again.
            return Response({"message": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        # The secret generated during provisioning is deliberately DISCARDED. It was hashed on
        # save and is unrecoverable anyway; the applicant's real secret is minted when they open
        # the claim link (afc_partner_apply/views_public.py claim_credentials).

    data_partner = None
    if application.wants_data_api:
        # Same row shape as afc_partner_api/views_admin.py create_partner, including the slug
        # derivation, so a partner provisioned here is indistinguishable from one created there.
        base_slug = slugify(field("name", application.organisation_name)) or "partner"
        slug = base_slug
        suffix = 2
        while Partner.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        data_partner = Partner.objects.create(
            name=str(field("name", application.organisation_name))[:120],
            slug=slug,
            contact_email=application.contact_email,
            created_by=user,
        )
        # Data API grants ride in the same body as the SSO ones, from the same whitelist, and
        # default OFF in exactly the same way.
        granted = [f for f in PARTNER_TOGGLE_FIELDS if request.data.get(f)]
        if granted:
            for toggle in granted:
                setattr(data_partner, toggle, True)
            data_partner.save(update_fields=granted)

    application.status = PartnerApplication.APPROVED
    application.decision_note = note
    if internal_note:
        application.internal_note = internal_note
    application.reviewed_by = user
    application.reviewed_at = timezone.now()
    application.sso_application = sso_application
    application.data_partner = data_partner
    application.save(update_fields=[
        "status", "decision_note", "internal_note", "reviewed_by", "reviewed_at",
        "sso_application", "data_partner", "updated_at",
    ])

    claim_token = application.issue_claim_token()
    emails.send_approved(application, application.issue_access_token(), claim_token)

    return Response(
        {
            "message": (
                "Approved and provisioned. The applicant has been emailed a one-time link to "
                "collect their credentials."
            ),
            "application": _serialize_detail(application, request),
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 4) resend_credentials  (POST partner-apply/admin/applications/<id>/resend-credentials/)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@authentication_classes([])
def resend_credentials(request, application_id):
    """Mint a fresh single-use credentials link and email it again.

    PURPOSE: the answer to "we lost the secret" and "the link expired", both of which will happen.
        Without this the only remedy would be for the owner to rotate the secret on the SSO edit
        form and then find a way to get it to the partner, which is the manual step this whole app
        exists to remove.

    REQUEST: POST, no body.
    RESPONSE 200: {"message", "application"}
             409 when the application is not approved (there is nothing to collect)
    AUTH: Bearer SessionToken, head_admin or partner_admin.
    CONSUMED BY: frontend PartnerApplicationsPanel.tsx ("Send a new credentials link").

    SAFE TO PRESS TWICE. Each press invalidates the previous link, because issue_claim_token
    replaces the stored hash. Only the newest email works, which is the behaviour somebody
    pressing it twice actually expects.

    NOTE THIS DOES NOT ROTATE ANYTHING BY ITSELF. The rotation happens when the link is opened, so
    an owner who presses this and is then told the partner found their old credentials working has
    learned something true: nobody opened the new link.
    """
    user, err = _require_apply_admin(request)
    if err:
        return err
    application, err = _application_or_404(application_id)
    if err:
        return err

    if application.status != PartnerApplication.APPROVED:
        return Response(
            {"message": "Only an approved application has credentials to collect."},
            status=status.HTTP_409_CONFLICT,
        )

    claim_token = application.issue_claim_token()
    emails.send_approved(application, application.issue_access_token(), claim_token)

    return Response(
        {
            "message": f"A new credentials link has been emailed to {application.contact_email}.",
            "application": _serialize_detail(application, request),
        },
        status=status.HTTP_200_OK,
    )
