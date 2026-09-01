"""
afc_tournament_and_scrims.event_invite_delivery - WHERE AN EVENT INVITATION IS DELIVERED.

THE ITEM IN THE OWNER'S WORDS
    "Admins can send invitations to teams and teams accept it in their mails or notifications. Team
    captains or managers or coaches can accept. The admins can pick where they receive the
    invitations, the normal places."

WHAT WAS THERE BEFORE
    Item 34 raised an in-app Notifications row and nothing else. If a captain did not open the site,
    they never learned they had been invited, which for an invitation with a deadline is the same as
    not having sent it.

WHAT THIS ADDS
    One place that takes an invitation (or a bulk campaign) and delivers it over the channels the
    ADMIN CHOSE. Three exist:

      push      an in-app Notifications row, deep-linked to the team page where the Accept card is.
      email     the branded transactional email below, in the RECIPIENT'S OWN LANGUAGE, taken from
                the hand-authored afc_auth.email_i18n catalog (never machine translation).
      whatsapp  the already-approved `broadcast` template, through afc_auth.broadcast_whatsapp.

WHO IS TOLD, AND WHY IT IS THAT EXACT SET
    Everyone who may ANSWER: the team owner plus every member holding one of
    views.TEAM_EVENT_REGISTER_ROLES (captain, vice-captain, manager, coach). That is not a list
    chosen here; it is read off views._user_can_register_team, the same predicate the accept
    endpoint enforces. The two must not drift: telling somebody they are invited and then refusing
    their answer is the worst of both, and quietly telling only the captain means an invitation sits
    unanswered while the manager who reads the email never hears about it.

A NOTE ON WHATSAPP REACH (measured 2026-08-08, so nobody picks it expecting more)
    Of the 813 people across AFC who can answer an event invitation, 32 are WhatsApp-reachable
    (opted in AND hold a number) and all 813 are email-reachable. WhatsApp is offered because the
    plumbing is already built and approved, not because it is a substitute for the other two: the
    composer shows the admin the reachable count before they send.

HOW IT CONNECTS
    - Called by event_invites.create_team_invitations (one call per invited team) and by
      event_invites._deliver_bulk_campaign (one call per audience team of a bulk campaign).
    - Channels are named by EventInvitationCampaign.delivery, parsed by afc_auth.audience
      .parse_delivery, the SAME vocabulary the broadcast composer uses ("push"/"email"/"both"/
      "whatsapp", comma-joined).
    - Email copy lives in afc_auth.email_i18n under the "event_team_invitation" key and is sent
      through the single chokepoint afc_auth.views.send_email(..., prelocalized=True).
    - Notifications rows carry target_type="team"/target_id=<team_id>, so the recipient's "Take me
      there" opens the team page where EventInvitationsCard.tsx renders Accept / Decline.
"""
import threading
from urllib.parse import quote

from django.conf import settings

from afc_auth.models import Notifications


# Every delivery in this module is best-effort per channel and per recipient: an invitation that
# reached somebody in-app must not be undone because one mailbox bounced. Callers get counts back
# and never an exception.
def _decision_makers(team):
    """The people on `team` who may answer an invitation: the owner plus captain / vice-captain /
    manager / coach.

    Read straight off views.TEAM_EVENT_REGISTER_ROLES so this set is BY CONSTRUCTION the same one
    views._user_can_register_team accepts. If a role is ever added to that list, everybody who
    gains the power to answer starts being told in the same commit, with no change here.
    """
    from afc_team.models import TeamMembers
    from .views import TEAM_EVENT_REGISTER_ROLES

    users = {
        m.member for m in TeamMembers.objects.filter(
            team=team, management_role__in=TEAM_EVENT_REGISTER_ROLES,
        ).select_related("member")
    }
    if team.team_owner_id:
        users.add(team.team_owner)
    # Drop anybody with no usable account row. bulk_create on Notifications would happily write a
    # row pointing at a deactivated user; nobody reads it, and it inflates the "we told N people"
    # count the composer shows.
    return [u for u in users if u is not None]


def reach_for_teams(teams):
    """How many people a send to `teams` would actually reach, per channel.

    Returns {"recipients": n, "email": n, "whatsapp": n}, where `recipients` is everyone who may
    ANSWER for those teams (owner / captain / vice-captain / manager / coach, deduplicated across
    teams, because one person can run two of them).

    WHY THIS IS SHOWN BEFORE THE SEND, NOT AFTER
        The channels are the admin's choice now, and the three are not comparable: every AFC
        account has an email address, while WhatsApp only reaches somebody who both saved a number
        and left the opt-in on. Measured across the whole site on 2026-08-08 that was 32 of 813
        people, i.e. under 4%. An admin who ticks WhatsApp and assumes the teams were told is the
        failure this prevents, so the composer asks for these numbers as the selection changes and
        prints them next to the tick box.

    Opt-in is resolved through afc_auth.canonical_profile rather than profile_of because duplicate
    UserProfile rows exist in production, and canonical_profile (lowest profile_id) is the resolver
    every other reader agrees on. afc_whatsapp.tasks._opted_out uses it for exactly this reason, so
    a number counted here is a number that sender would also accept.
    """
    recipients = set()
    for team in teams:
        recipients.update(_decision_makers(team))

    email = sum(1 for u in recipients if (getattr(u, "email", "") or "").strip())

    whatsapp = 0
    try:
        from afc_auth.models import canonical_profile
    except Exception:
        canonical_profile = None
    if canonical_profile is not None:
        for user in recipients:
            try:
                profile = canonical_profile(user)
            except Exception:
                # A profile lookup must never break the composer; an uncountable person is simply
                # not counted as reachable, which errs toward the honest (lower) number.
                continue
            if profile is None:
                continue
            number = (getattr(profile, "whatsapp_number", "") or "").strip()
            if number and getattr(profile, "whatsapp_opt_in", True):
                whatsapp += 1

    return {"recipients": len(recipients), "email": email, "whatsapp": whatsapp}


# ── the email ────────────────────────────────────────────────────────────────────────────────
def _frontend_origin():
    """Where the captain's own pages live.

    The team page is a Next.js route, not a Django one, so the link in this email points at the
    FRONTEND origin, never at the API. Same helper shape as afc_partner_apply.emails._frontend_origin,
    with the production value as the default because an email is far more likely to be read
    somewhere other than a developer's machine."""
    return (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")


def _invitation_email_html(invitee_name, link_path, event, organizer_name, note, kind, lang):
    """The branded invitation email body, already in `lang`.

    Built from the HAND-AUTHORED copy in afc_auth.email_i18n ("event_team_invitation"), so a French
    or Portuguese captain gets natural sentences even when the DeepL engine is missing or over
    quota. The caller pairs this with send_email(..., prelocalized=True), which then SKIPS machine
    translation entirely. Composed exactly the way every other fixed transactional email in the
    project is: build the inner <tr> rows, hand them to afc_auth.views._email_shell.

    Dynamic values (team name, event name, the organizer's note) are injected AS-IS and never
    translated, exactly as a proper i18n system does: a team called "Les Loups" stays "Les Loups".
    """
    from django.utils.html import escape

    from afc_auth.email_i18n import copy_for
    from afc_auth.views import _email_shell

    copy = copy_for("event_team_invitation", lang)

    def sentence(key, **fmt):
        """One catalog sentence with its placeholders filled, or "" when the key is absent. A stray
        brace must never cost the captain the email, so a bad format falls back to the raw text."""
        text = copy.get(key) or ""
        if not text:
            return ""
        try:
            return text.format(**fmt) if fmt else text
        except Exception:
            return text

    # The paragraphs, in reading order: what happened, who asked, what it means, what to do.
    # The KIND decides exactly one of them: a first-come offer has to say that speed matters and a
    # general offer has to say the place is not being held, which is the difference between a
    # captain answering today and answering next week when the slot is gone.
    paragraphs = [sentence("intro", team=invitee_name, event=event.event_name)]
    if organizer_name:
        paragraphs.append(sentence("from_organizer", name=organizer_name))
    paragraphs.append(sentence(f"urgency_{kind}") or sentence("urgency_per_team"))
    paragraphs.append(sentence("how_to_answer"))

    body_html = "".join(
        f'<tr><td style="padding:0 44px 14px;font-size:15px;line-height:1.6;color:#aab5ae;">'
        f"{p}</td></tr>"
        for p in paragraphs
        if p
    )

    # The organizer's own words, set apart so they read as THEIRS rather than as AFC's. Escaped:
    # this is user-supplied free text landing in an HTML document.
    note_html = ""
    if note:
        note_html = (
            f'<tr><td style="padding:0 44px 18px;">'
            # white-space:pre-line keeps the line breaks the organizer typed. It matters since the
            # note grew to 2000 characters (2026-09-01): a schedule written on four lines arrived
            # as one paragraph without it, because HTML collapses newlines.
            f'<div style="border-left:3px solid #2c7a4d;padding:10px 16px;background:#0a120d;'
            f'font-size:15px;line-height:1.6;color:#cdd6cf;white-space:pre-line;">'
            f'{escape(note)}</div></td></tr>'
        )

    cta = sentence("cta")
    cta_html = ""
    if cta:
        # `link_path` is resolved by the caller and already percent-encoded. For a TEAM invitation
        # it is /teams/<name>: /teams/[id] resolves that segment as the team NAME (see the long note
        # in deliver_invitation), and unlike the in-app link this one is a raw href in an HTML
        # document, so a name with a space or an ampersand has to be quoted or the button lands
        # somewhere else entirely. For a SOLO invitation it is /tournaments/<slug>, because a player
        # answers on the event page and has no team page to answer on.
        cta_html = (
            f'<tr><td style="padding:6px 44px 34px;">'
            f'<a href="{_frontend_origin()}{link_path}" '
            f'style="display:inline-block;background:#2c7a4d;color:#ffffff;text-decoration:none;'
            f'font-size:15px;font-weight:600;padding:12px 22px;border-radius:10px;">{cta}</a>'
            f"</td></tr>"
        )

    inner = (
        f'<tr><td style="padding:38px 44px 14px;">'
        f'<div style="font-size:21px;font-weight:700;color:#ffffff;">'
        f'{sentence("heading", event=event.event_name)}</div></td></tr>'
        f"{body_html}{note_html}{cta_html}"
    )
    return _email_shell(inner, "green")


def _sync():
    """Send invitation email inline instead of on a background thread.

    Defaults to DEBUG, matching the WHATSAPP_SYNC / RANKINGS_RECALC_SYNC / OCR_ML_SYNC convention
    already used across this project, so local development and the test suite both get deterministic
    behaviour with no setting to remember. Production leaves it False and keeps the thread."""
    return getattr(settings, "EVENT_INVITE_EMAIL_SYNC", getattr(settings, "DEBUG", False))


def _send_invitation_emails(
    recipients, invitee_name, link_path, event, organizer_name, note, kind
):
    """Mail every recipient (a list of Users), each in their own language. Returns how many.

    ON A DAEMON THREAD in production, for the same reason deliver_broadcast's email channel is: SMTP
    is slow and a bulk campaign to twenty teams is sixty-odd messages, which would hold the
    organizer's HTTP request open for minutes. The in-app notification has already been written by
    then, so the sure channel never waits on the slow one. Under _sync() it runs inline instead, so
    a test can assert what was sent without racing a thread.

    Per-recipient failures are swallowed: one dead mailbox must not stop the other fifty-nine.
    """
    from afc_auth.email_i18n import subject_for
    from afc_auth.views import send_email

    # Resolve the (address, language) triples on THIS thread, while the ORM connection is still the
    # request's. Doing the queryset work inside the thread would open a second connection per send.
    targets = [
        (u.email, (getattr(u, "language", "") or "en"), u)
        for u in recipients
        if getattr(u, "email", None)
    ]
    if not targets:
        return 0

    def _run():
        for address, lang, _user in targets:
            try:
                send_email(
                    address,
                    subject_for("event_team_invitation", lang, event=event.event_name),
                    _invitation_email_html(
                        invitee_name, link_path, event, organizer_name, note, kind, lang
                    ),
                    language=lang,
                    # The copy arrived already localized from the hand-authored catalog, so the
                    # machine-translation block inside send_email is skipped.
                    prelocalized=True,
                )
            except Exception:
                continue

    if _sync():
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()
    return len(targets)


# ── the one entry point ──────────────────────────────────────────────────────────────────────
def deliver_invitation(*, team=None, player=None, event=None, delivery=None,
                       organizer_name="", note="", kind="per_team", target_team_id=None):
    """Tell `team` they have been invited to `event`, over the channels named in `delivery`.

    delivery   an afc_auth.audience delivery string: "push", "email", "both", "whatsapp", or a
               comma-joined combination such as "both,whatsapp". Parsed by parse_delivery, so this
               function and the broadcast composer share one vocabulary and one set of aliases.
    kind       the campaign's kind, which only changes ONE sentence of the email and one line of the
               in-app message (a first-come offer says speed matters).
    returns    {"recipients": n, "pushed": n, "emailed": n, "whatsapp": n} so the create endpoint can
               tell the organizer who was actually reached, per channel.

    Never raises. A delivery failure must not roll back an invitation that was legitimately created:
    the row is the invitation, and the team can still find it on their team page.
    """
    from afc_auth.audience import EMAIL, PUSH, WHATSAPP, parse_delivery

    channels = parse_delivery(delivery)

    # WHO IS TOLD, WHAT THEY ARE CALLED, AND WHERE THE LINK GOES (owner 2026-08-26).
    # A TEAM invitation reaches everyone who may answer FOR the team. A SOLO invitation reaches
    # exactly one person, the invitee, because a solo entrant answers only for themself.
    is_solo = player is not None
    recipients = [player] if is_solo else _decision_makers(team)
    invitee_name = player.username if is_solo else team.team_name
    result = {"recipients": len(recipients), "pushed": 0, "emailed": 0, "whatsapp": 0}
    if not recipients:
        return result

    # Deep link: the TEAM page, because that is where EventInvitationsCard renders Accept/Decline.
    # target_team_id is the same team for an addressed invitation; it is passed separately only so a
    # bulk campaign can point every audience team at its OWN page rather than a shared one.
    #
    # THE NAME, NOT THE ID (fixed 2026-08-08, verified in the browser).
    #   The frontend route is app/(user)/teams/[id]/page.tsx and it resolves that segment as the TEAM
    #   NAME (it does decodeURIComponent(id) and looks the team up by name). afc_auth
    #   .notification_links.build_notification_link turns target_type="team" into "/teams/<target_id>"
    #   verbatim, so storing the numeric team_id produced /teams/817, which is a hard 404. Every one
    #   of the 24 event_team_invitation rows written before this fix carried a dead link, which for
    #   an invitation whose whole purpose is "open your team page and answer" is the difference
    #   between a captain answering and a captain giving up. Resolved to the name here so BOTH the
    #   in-app link and the email button below point at the page that exists.
    # A SOLO invitation links to the EVENT page: that is where a player answers, and they have no
    # team page to answer on. Everything above about names-not-ids applies to the team case only.
    if is_solo:
        link_target_type = "event"
        link_target_id = event.slug
        link_path = f"/tournaments/{quote(event.slug, safe='')}"
        answer_hint = "Open the event page to accept or decline."
    else:
        link_target_type = "team"
        link_target_id = team.team_name
        link_path = f"/teams/{quote(team.team_name, safe='')}"
        answer_hint = "Open your team page to accept or decline."

    if PUSH in channels:
        # Written in English and translated ON READ by afc_auth.translation via LocaleMiddleware,
        # which is how every other Notifications row in the project is localized. Authoring three
        # copies here would bypass that layer and drift from it.
        headline = f"Invitation to {event.event_name}"
        body = (
            f"{invitee_name} has been invited to {event.event_name}. "
            f"{answer_hint}"
        )
        if kind == "fcfs":
            body += " Places are first come, first served."
        elif kind == "bulk":
            body += " This is an open invitation while places last."
        Notifications.objects.bulk_create([
            Notifications(
                user=user,
                notification_type="event_team_invitation",
                title=headline,
                message=body,
                related_event=event,
                target_type=link_target_type,
                target_id=link_target_id,
            )
            for user in recipients
        ])
        result["pushed"] = len(recipients)

    if EMAIL in channels:
        result["emailed"] = _send_invitation_emails(
            recipients, invitee_name, link_path, event, organizer_name, note, kind,
        )

    if WHATSAPP in channels:
        # Reuses the approved `broadcast` template through the existing sender, which already
        # respects opt-out, caps the audience, and records every message on WhatsAppMessage. Wiring
        # a second WhatsApp path here would need its own Meta-approved template and would duplicate
        # all of that.
        try:
            from afc_auth.broadcast_whatsapp import send_broadcast_whatsapp
            queued, _skipped = send_broadcast_whatsapp(
                recipients,
                f"Invitation to {event.event_name}",
                f"{invitee_name} has been invited to {event.event_name}. "
                f"{answer_hint}",
            )
            result["whatsapp"] = queued
        except Exception:
            # WhatsApp is the optional third channel; if it is unconfigured on this server the
            # invitation still went out in-app and by email.
            result["whatsapp"] = 0

    return result
