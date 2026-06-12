"""
afc_sponsors.engagements — ENGAGEMENT CONFIG + SUBMISSIONS + APPROVAL (P2/P3/P4).

PURPOSE
    Everything the sponsor redesign adds ON TOP of the P1 entities:
      P2  per-sponsorship engagement CONFIG (the wizard's engagement builder) + the public
          read the registration page uses to render inputs.
      P3  SponsorEngagementSubmission writes at registration time + the per-engagement
          submission tables (and CSV) in the sponsor portal.
      P4  the approval gate: pending submissions, sponsor decide (approve / reject with a
          MANDATORY reason / final reject / undo), the player rejection loop (email + in-app
          notification + re-input prompt -> resubmission returns to the pending queue).
    Spec: WEBSITE/tasks/sponsors-redesign-design.md (sections 2, 4, 5, 9).

HOW IT CONNECTS
    - Models: afc_sponsors.models (EventSponsorship.engagements/requires_approval +
      SponsorEngagementSubmission).
    - Registration: afc_tournament_and_scrims.views.register_for_event calls
      create_submissions_for_registration() right after it creates the registration rows; the
      payload rides the register body under "sponsorships" (see that helper's docstring).
    - Activation: _sync_registration_state() flips the registration active once every
      approval-requiring submission of that registrant is approved — solo via
      RegisteredCompetitors.status, team via the member row + the EXISTING
      check_and_activate_team() (the legacy sponsored-team machinery, reused not duplicated).
    - Consumed by frontend lib/sponsors.ts -> the event wizard sponsor tab (configure), the
      registration sponsor step (for-event), and the sponsor portal queue (submissions list +
      decide + the player's resubmit).

ENDPOINTS (mounted at sponsors/ via afc/urls.py)
    PATCH sponsors/<sponsor_id>/events/<event_id>/configure/   configure_sponsorship
    GET   sponsors/for-event/<event_id>/                       sponsorships_for_event (public)
    GET   sponsors/<sponsor_id>/events/<event_id>/engagement-submissions/
                                                               sponsorship_submissions
                                                               (?engagement=&status=&csv=1)
    POST  sponsors/submissions/<submission_id>/decide/         decide_submission
                                                               (approve|reject|reject_final|undo)
    POST  sponsors/submissions/<submission_id>/resubmit/       resubmit_submission (the player)
    GET   sponsors/my-submissions/<event_id>/                  my_event_submissions (the player)
"""
import csv

from django.http import HttpResponse
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import Notifications
from afc_auth.views import send_email, _email_shell

from .models import Sponsor, EventSponsorship, SponsorEngagementSubmission
from .views import _auth_user, _is_sponsor_admin, _membership, _page_params


# ── engagement schema (spec section 2) ────────────────────────────────────────────────────────
ENGAGEMENT_TYPES = ("collect_id", "follow_social", "create_account", "join_group")
FOLLOW_ACTIONS = ("follow", "like", "share")
JOIN_PLATFORMS = ("whatsapp", "discord")


def validate_engagements(engagements):
    """Validate a sponsorship's engagements list against the spec schema. Returns an error
    STRING (first problem found) or None when valid. Kept dependency-free so the wizard
    configure endpoint AND the registration-time payload validation share one source of truth."""
    if not isinstance(engagements, list):
        return "engagements must be a list."
    for i, e in enumerate(engagements):
        if not isinstance(e, dict):
            return f"engagements[{i}] must be an object."
        etype = e.get("type")
        if etype not in ENGAGEMENT_TYPES:
            return f"engagements[{i}].type must be one of {', '.join(ENGAGEMENT_TYPES)}."
        if etype == "collect_id":
            if not (e.get("label") or "").strip():
                return f"engagements[{i}]: collect_id needs a label."
        elif etype == "follow_social":
            if not (e.get("platform") or "").strip():
                return f"engagements[{i}]: follow_social needs a platform."
            if not (e.get("url") or "").strip():
                return f"engagements[{i}]: follow_social needs the sponsor page url."
            actions = e.get("actions") or []
            if not actions or not all(a in FOLLOW_ACTIONS for a in actions):
                return f"engagements[{i}]: follow_social actions must be from {', '.join(FOLLOW_ACTIONS)}."
        elif etype == "create_account":
            if not (e.get("label") or "").strip():
                return f"engagements[{i}]: create_account needs a label."
            if not (e.get("signup_url") or "").strip():
                return f"engagements[{i}]: create_account needs a signup_url."
        elif etype == "join_group":
            if e.get("platform") not in JOIN_PLATFORMS:
                return f"engagements[{i}]: join_group platform must be whatsapp or discord."
            if not (e.get("invite_url") or "").strip():
                return f"engagements[{i}]: join_group needs an invite_url."
    return None


def validate_payload(engagement, payload):
    """Validate ONE registrant payload against ONE engagement entry. Returns error str|None.
    Shapes (spec section 2): collect_id {value}; follow_social {profile_link};
    create_account {username}; join_group whatsapp {phone, country_code} / discord
    {discord_username}."""
    if not isinstance(payload, dict):
        return "payload must be an object."
    etype = engagement.get("type")
    if etype == "collect_id":
        if not str(payload.get("value") or "").strip():
            return f"\"{engagement.get('label')}\" is required."
    elif etype == "follow_social":
        # The registrant pastes THEIR OWN profile link on the engaged platform (only asked
        # when the sponsor configured collect_profile_link).
        if engagement.get("collect_profile_link") and not str(payload.get("profile_link") or "").strip():
            return f"Your {engagement.get('platform')} profile link is required."
    elif etype == "create_account":
        if not str(payload.get("username") or "").strip():
            return f"\"{engagement.get('label')}\" is required."
    elif etype == "join_group":
        if engagement.get("platform") == "whatsapp":
            if not str(payload.get("phone") or "").strip() or not str(payload.get("country_code") or "").strip():
                return "Phone number and country code are required for the WhatsApp group."
        else:
            if not str(payload.get("discord_username") or "").strip():
                return "Your Discord username is required for the Discord group."
    return None


def _payload_display(engagement, payload):
    """One human-readable string per submission for tables/CSV (the 'value' column)."""
    etype = (engagement or {}).get("type")
    p = payload or {}
    if etype == "collect_id":
        return p.get("value") or ""
    if etype == "follow_social":
        return p.get("profile_link") or "(actions done)"
    if etype == "create_account":
        return p.get("username") or ""
    if etype == "join_group":
        if (engagement or {}).get("platform") == "whatsapp":
            return f"{p.get('country_code') or ''} {p.get('phone') or ''}".strip()
        return p.get("discord_username") or ""
    return ""


# ── P2: configure a sponsorship (the wizard's engagement builder save) ────────────────────────
@api_view(["PATCH"])
def configure_sponsorship(request, sponsor_id, event_id):
    """PATCH sponsors/<sponsor_id>/events/<event_id>/configure/
    body: {requires_approval?: bool, engagements?: [...]}

    Saves the wizard's per-sponsorship config. Auth: sponsor-admin OR an organizer who can
    edit this event (org_can_event "can_edit_events" — organizers configure sponsors on their
    OWN events; they pick from existing sponsors only, creation stays admin)."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sp = EventSponsorship.objects.select_related("event", "sponsor").get(
            sponsor_id=sponsor_id, event_id=event_id,
        )
    except EventSponsorship.DoesNotExist:
        return Response({"message": "That sponsor is not attached to this event."}, status=404)

    if not _is_sponsor_admin(user):
        # Lazy import (afc_organizers <-> afc_sponsors would otherwise risk a cycle).
        from afc_organizers.permissions import org_can_event
        if not org_can_event(user, "can_edit_events", sp.event):
            return Response({"message": "You do not have permission to configure this sponsorship."}, status=403)

    if "engagements" in request.data:
        engagements = request.data.get("engagements")
        error = validate_engagements(engagements)
        if error:
            return Response({"message": error}, status=400)
        sp.engagements = engagements
    if "requires_approval" in request.data:
        sp.requires_approval = bool(request.data.get("requires_approval"))
    sp.save(update_fields=["engagements", "requires_approval"])
    return Response({"message": "Sponsorship updated.", "sponsorship": _serialize_sponsorship(sp)})


def _serialize_sponsorship(sp):
    return {
        "sponsorship_id": sp.id,
        "sponsor": {
            "id": sp.sponsor.id,
            "name": sp.sponsor.name,
            "slug": sp.sponsor.slug,
            "logo": sp.sponsor.logo.url if sp.sponsor.logo else None,
            "website": sp.sponsor.website,
            "socials": sp.sponsor.socials or [],
        },
        "requires_approval": sp.requires_approval,
        "engagements": sp.engagements or [],
    }


@api_view(["GET"])
def sponsorships_for_event(request, event_id):
    """GET sponsors/for-event/<event_id>/  — PUBLIC read of an event's sponsorships + their
    engagement config. Two consumers: the registration page (renders the engagement inputs)
    and the wizard sponsor tab (rehydrates the builder). Engagement config is not secret
    (registrants see it anyway), so no auth."""
    rows = [
        _serialize_sponsorship(sp)
        for sp in EventSponsorship.objects.filter(event_id=event_id)
        .select_related("sponsor").order_by("created_at")
        if sp.sponsor.status == "active"
    ]
    return Response({"results": rows, "total_count": len(rows)})


# ── P3: registration-time submission writes ──────────────────────────────────────────────────
def create_submissions_for_registration(event, user_payloads, acting_user):
    """Create SponsorEngagementSubmission rows for one registration. Called by
    register_for_event INSIDE its transaction, after the registration rows exist.

    `user_payloads`: {user_id: [{sponsorship_id, engagement_index, payload}, ...]} — solo has
    one key (the registrant), squad has one per rostered player (captain fills per-player
    values, spec section 4).

    Returns (error_message | None, requires_approval_bool). Validates EVERY payload against
    the sponsorship's engagement config BEFORE writing anything; on error the caller's
    transaction is expected to abort (the registration fails atomically with a 400)."""
    sponsorships = {
        sp.id: sp
        for sp in EventSponsorship.objects.filter(event=event).select_related("sponsor")
    }
    if not sponsorships:
        return None, False  # event has no entity sponsorships: nothing to do (legacy path)

    rows = []
    any_approval = False
    for uid, entries in (user_payloads or {}).items():
        for entry in entries or []:
            sp = sponsorships.get(entry.get("sponsorship_id"))
            if sp is None:
                return "Unknown sponsorship in submission payload.", False
            engagements = sp.engagements or []
            idx = entry.get("engagement_index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(engagements):
                return "Unknown engagement in submission payload.", False
            engagement = engagements[idx]
            error = validate_payload(engagement, entry.get("payload") or {})
            if error:
                return error, False
            # collect_id duplicate guard: the same submitted id must not already be used by
            # ANOTHER registrant of this event under the same engagement (mirrors the legacy
            # per-event sponsor-id uniqueness check).
            if engagement.get("type") == "collect_id":
                value = str(entry["payload"].get("value")).strip()
                clash = SponsorEngagementSubmission.objects.filter(
                    sponsorship=sp, engagement_index=idx, payload__value=value,
                ).exclude(user_id=uid).exists()
                if clash:
                    return f"\"{value}\" is already registered for {sp.sponsor.name} in this event.", False
            status = "pending" if sp.requires_approval else "not_required"
            if sp.requires_approval:
                any_approval = True
            rows.append(SponsorEngagementSubmission(
                sponsorship=sp, event=event, user_id=uid,
                engagement_index=idx, payload=entry.get("payload") or {},
                approval_status=status,
            ))

    # MISSING-ANSWER guard: every engagement of every sponsorship must be answered by every
    # registrant in user_payloads (the FE enforces it too; server is the source of truth).
    expected = []
    for sp in sponsorships.values():
        for idx, engagement in enumerate(sp.engagements or []):
            # follow_social without collect_profile_link still expects an (empty) submission
            # row so the sponsor sees WHO confirmed doing the actions.
            expected.append((sp.id, idx))
    if expected:
        for uid, entries in (user_payloads or {}).items():
            got = {(e.get("sponsorship_id"), e.get("engagement_index")) for e in entries or []}
            missing = [k for k in expected if k not in got]
            if missing:
                sp = sponsorships[missing[0][0]]
                eng = (sp.engagements or [])[missing[0][1]]
                return f"Missing sponsor requirement: {eng.get('label') or eng.get('type')} ({sp.sponsor.name}).", False

    # update_or_create semantics for re-registration edge cases (a withdrawn player coming
    # back): the UNIQUE constraint would 500 on bulk_create, so upsert row by row (these are
    # small lists: engagements x roster, worst case a few dozen).
    for row in rows:
        SponsorEngagementSubmission.objects.update_or_create(
            sponsorship=row.sponsorship, user_id=row.user_id,
            engagement_index=row.engagement_index,
            defaults={
                "event": event, "payload": row.payload,
                "approval_status": row.approval_status,
                "reason": "", "decided_by": None, "decided_at": None,
                "prev_status": "", "prev_reason": "",
            },
        )
    return None, any_approval


def _sync_registration_state(submission):
    """Re-derive the registrant's registration state from their submissions (P4 activation).

    Rule: a registrant is APPROVED for the event when every approval-requiring submission of
    theirs (across ALL of the event's approval-requiring sponsorships) is "approved".
      - solo: RegisteredCompetitors.status pending <-> registered
      - team: the member row status pending <-> active, then the EXISTING
        check_and_activate_team() re-derives the team + RC state (reused, not duplicated).
    Rejected submissions keep the registrant pending (they are expected to resubmit); the
    FINAL reject path (decide action reject_final) withdraws the registration instead."""
    from afc_tournament_and_scrims.models import (
        RegisteredCompetitors, TournamentTeamMember,
    )
    from afc_tournament_and_scrims.views import check_and_activate_team

    event, user_id = submission.event, submission.user_id
    pending_exists = SponsorEngagementSubmission.objects.filter(
        event=event, user_id=user_id, approval_status__in=("pending", "rejected"),
    ).exists()

    member = TournamentTeamMember.objects.filter(
        tournament_team__event=event, user_id=user_id,
    ).select_related("tournament_team").first()
    if member:
        new_status = "pending" if pending_exists else "active"
        if member.status != new_status and member.status not in ("rejected",):
            member.status = new_status
            member.save(update_fields=["status"])
        check_and_activate_team(member.tournament_team)
        return

    rc = RegisteredCompetitors.objects.filter(event=event, user_id=user_id).first()
    if rc and rc.status in ("pending", "registered"):
        new_status = "pending" if pending_exists else "registered"
        if rc.status != new_status:
            rc.status = new_status
            rc.save(update_fields=["status"])


# ── P3: the portal's per-engagement submission tables ─────────────────────────────────────────
@api_view(["GET"])
def sponsorship_submissions(request, sponsor_id, event_id):
    """GET sponsors/<sponsor_id>/events/<event_id>/engagement-submissions/
        ?engagement=<index>&status=<pending|approved|rejected|not_required>&limit=&offset=&csv=1

    The NEW per-engagement submissions read for the sponsor portal (the P1 endpoint keeps
    serving the legacy single-id data). Privacy: usernames + submitted values only, never
    account emails/phones. CSV streams the same filtered rows."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sponsor = Sponsor.objects.get(id=sponsor_id)
    except Sponsor.DoesNotExist:
        return Response({"message": "Sponsor not found."}, status=404)
    if not (_is_sponsor_admin(user) or _membership(user, sponsor)):
        return Response({"message": "You do not have access to this sponsor."}, status=403)
    try:
        sp = EventSponsorship.objects.select_related("event").get(
            sponsor=sponsor, event_id=event_id,
        )
    except EventSponsorship.DoesNotExist:
        return Response({"message": "That event is not attached to this sponsor."}, status=404)

    qs = (
        SponsorEngagementSubmission.objects.filter(sponsorship=sp)
        .select_related("user")
        .order_by("approval_status", "-updated_at")  # pending sorts first (alphabetical luck)
    )
    engagement_param = request.GET.get("engagement")
    if engagement_param not in (None, ""):
        try:
            qs = qs.filter(engagement_index=int(engagement_param))
        except (TypeError, ValueError):
            pass
    status_param = request.GET.get("status")
    if status_param:
        qs = qs.filter(approval_status=status_param)

    engagements = sp.engagements or []

    def _row(s):
        engagement = engagements[s.engagement_index] if s.engagement_index < len(engagements) else {}
        return {
            "id": s.id,
            "username": s.user.username,
            "engagement_index": s.engagement_index,
            "engagement_label": engagement.get("label") or engagement.get("platform") or engagement.get("type"),
            "engagement_type": engagement.get("type"),
            "value": _payload_display(engagement, s.payload),
            "payload": s.payload,
            "approval_status": s.approval_status,
            "reason": s.reason,
            "can_undo": bool(s.prev_status),
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }

    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="submissions.csv"'
        writer = csv.writer(resp)
        writer.writerow(["username", "engagement", "value", "status", "reason", "updated_at"])
        for s in qs:
            r = _row(s)
            writer.writerow([r["username"], r["engagement_label"], r["value"],
                             r["approval_status"], r["reason"], r["updated_at"]])
        return resp

    limit, offset = _page_params(request)
    total = qs.count()
    rows = [_row(s) for s in qs[offset:offset + limit]]
    return Response({
        "event": {"event_id": sp.event.event_id, "event_name": sp.event.event_name},
        "engagements": engagements,
        "requires_approval": sp.requires_approval,
        "results": rows,
        "total_count": total,
        "has_more": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
    })


# ── P4: decide + undo + the rejection loop ────────────────────────────────────────────────────
def _notify_rejection(submission, reason, final=False):
    """The rejection loop's player-facing side (spec section 9): an IN-APP notification AND a
    branded EMAIL carrying the sponsor's reason + the re-input prompt (or the final-rejection
    notice). Best-effort: a failed email never blocks the decision."""
    sp = submission.sponsorship
    event = submission.event
    player = submission.user
    engagement = (sp.engagements or [{}])[submission.engagement_index] if submission.engagement_index < len(sp.engagements or []) else {}
    label = engagement.get("label") or engagement.get("platform") or engagement.get("type") or "submission"

    if final:
        title = f"Registration rejected for {event.event_name}"
        body = (
            f"{sp.sponsor.name} rejected your registration for {event.event_name}. "
            f"Reason: {reason}. Your slot has been released."
        )
    else:
        title = f"Action needed: fix your {label} for {event.event_name}"
        body = (
            f"{sp.sponsor.name} rejected your {label} for {event.event_name}. "
            f"Reason: {reason}. Open the event page and re-enter the correct value; "
            "your registration stays pending until the sponsor approves it."
        )
    Notifications.objects.create(
        user=player, notification_type="sponsor_rejection",
        title=title, message=body, related_event=event,
    )
    try:
        inner = f"""
<tr><td style="padding:0 32px 8px;color:#e8efe9;font-size:18px;font-weight:bold;">{title}</td></tr>
<tr><td style="padding:0 32px 16px;color:#9fb3a6;font-size:14px;line-height:1.6;">{body}</td></tr>
"""
        send_email(player.email, title, _email_shell(inner, accent="gold"))
    except Exception:
        pass  # email is best-effort; the in-app notification is the guaranteed channel


@api_view(["POST"])
def decide_submission(request, submission_id):
    """POST sponsors/submissions/<submission_id>/decide/
    body: {action: approve|reject|reject_final|undo, reason?}

    The sponsor's decision surface (spec section 9):
      approve       submission approved; when ALL of the player's required submissions are
                    approved the registration activates (_sync_registration_state).
      reject        REQUIRES a reason. Player notified (email + in-app) with the reason + a
                    re-input prompt; resubmission returns the row to pending.
      reject_final  REQUIRES a reason. Rejects AND releases the registration slot (solo: the
                    competitor row leaves the active count; team: the member row is marked
                    rejected and the team drops back to pending review).
      undo          one-step revert of the last decision (prev_status snapshot), audit-logged
                    like every admin action via the global AuditLogMiddleware.
    Auth: an ACTIVE member of the sponsorship's sponsor, or sponsor-admin."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sub = SponsorEngagementSubmission.objects.select_related(
            "sponsorship__sponsor", "event", "user",
        ).get(id=submission_id)
    except SponsorEngagementSubmission.DoesNotExist:
        return Response({"message": "Submission not found."}, status=404)
    if not (_is_sponsor_admin(user) or _membership(user, sub.sponsorship.sponsor)):
        return Response({"message": "You do not have access to this sponsor."}, status=403)

    action = request.data.get("action")
    reason = (request.data.get("reason") or "").strip()

    if action == "approve":
        sub.prev_status, sub.prev_reason = sub.approval_status, sub.reason
        sub.approval_status, sub.reason = "approved", ""
        sub.decided_by, sub.decided_at = user, timezone.now()
        sub.save()
        _sync_registration_state(sub)

    elif action in ("reject", "reject_final"):
        if not reason:
            return Response({"message": "A rejection reason is required."}, status=400)
        sub.prev_status, sub.prev_reason = sub.approval_status, sub.reason
        sub.approval_status, sub.reason = "rejected", reason
        sub.decided_by, sub.decided_at = user, timezone.now()
        sub.save()
        if action == "reject_final":
            # Final = no resubmission expected: withdraw the registration so the slot frees
            # (owner decision: rejection auto-frees; solo capacity counts only
            # registered/approved, so status "rejected" releases the seat immediately).
            from afc_tournament_and_scrims.models import (
                RegisteredCompetitors, TournamentTeamMember,
            )
            from afc_tournament_and_scrims.views import check_and_activate_team
            member = TournamentTeamMember.objects.filter(
                tournament_team__event=sub.event, user_id=sub.user_id,
            ).select_related("tournament_team").first()
            if member:
                member.status = "rejected"
                member.save(update_fields=["status"])
                check_and_activate_team(member.tournament_team)
            else:
                RegisteredCompetitors.objects.filter(
                    event=sub.event, user_id=sub.user_id,
                ).update(status="rejected")
            _notify_rejection(sub, reason, final=True)
        else:
            _notify_rejection(sub, reason, final=False)
            _sync_registration_state(sub)

    elif action == "undo":
        if not sub.prev_status:
            return Response({"message": "Nothing to undo."}, status=400)
        sub.approval_status, sub.reason = sub.prev_status, sub.prev_reason
        sub.prev_status, sub.prev_reason = "", ""
        sub.decided_by, sub.decided_at = user, timezone.now()
        sub.save()
        _sync_registration_state(sub)

    else:
        return Response({"message": "Unknown action."}, status=400)

    return Response({"message": "Done.", "submission": {
        "id": sub.id, "approval_status": sub.approval_status,
        "reason": sub.reason, "can_undo": bool(sub.prev_status),
    }})


@api_view(["POST"])
def resubmit_submission(request, submission_id):
    """POST sponsors/submissions/<submission_id>/resubmit/  body: {payload}

    The PLAYER's side of the rejection loop: re-enter the corrected value; the row returns to
    the sponsor's pending queue (status history kept via prev_*). Auth: the submitting user
    only; only a rejected row can be resubmitted."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        sub = SponsorEngagementSubmission.objects.select_related("sponsorship").get(id=submission_id)
    except SponsorEngagementSubmission.DoesNotExist:
        return Response({"message": "Submission not found."}, status=404)
    if sub.user_id != user.user_id:
        return Response({"message": "You can only resubmit your own submission."}, status=403)
    if sub.approval_status != "rejected":
        return Response({"message": "Only a rejected submission can be resubmitted."}, status=400)

    engagements = sub.sponsorship.engagements or []
    engagement = engagements[sub.engagement_index] if sub.engagement_index < len(engagements) else {}
    payload = request.data.get("payload") or {}
    error = validate_payload(engagement, payload)
    if error:
        return Response({"message": error}, status=400)

    sub.prev_status, sub.prev_reason = sub.approval_status, sub.reason
    sub.payload = payload
    sub.approval_status, sub.reason = "pending", ""
    sub.save()
    return Response({"message": "Resubmitted. The sponsor will review it again.", "submission": {
        "id": sub.id, "approval_status": sub.approval_status,
    }})


@api_view(["GET"])
def my_event_submissions(request, event_id):
    """GET sponsors/my-submissions/<event_id>/  — the CALLER's own submissions for one event
    (status + rejection reasons), so the event page can show 'waiting for <sponsor> approval'
    badges and the re-input prompt for rejected entries. Auth: any logged-in user."""
    user, err = _auth_user(request)
    if err:
        return err
    rows = []
    for s in (
        SponsorEngagementSubmission.objects.filter(event_id=event_id, user=user)
        .select_related("sponsorship__sponsor").order_by("sponsorship_id", "engagement_index")
    ):
        engagements = s.sponsorship.engagements or []
        engagement = engagements[s.engagement_index] if s.engagement_index < len(engagements) else {}
        rows.append({
            "id": s.id,
            "sponsorship_id": s.sponsorship_id,
            "sponsor_name": s.sponsorship.sponsor.name,
            "engagement_index": s.engagement_index,
            "engagement_label": engagement.get("label") or engagement.get("platform") or engagement.get("type"),
            "engagement_type": engagement.get("type"),
            "payload": s.payload,
            "approval_status": s.approval_status,
            "reason": s.reason,
        })
    return Response({"results": rows, "total_count": len(rows)})
