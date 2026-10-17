"""
afc_mail/recipients.py - who a campaign may reach, and the consent and suppression rules.

ONE AUDIENCE ENGINE (owner rule R24): the filters are afc_auth.audience's, the same spec the
broadcast composer and polls use (everyone / players / teams / tiers / countries / roles /
languages / events / ranks). Marketing mail then keeps only people who:
  - said yes (EmailSubscription.marketing_opt_in), which is OFF for everyone until they turn it on;
  - have an email address on file;
  - are not on the suppression list (a hard bounce or a complaint, reported by SES);
  - are not deleted (afc_auth.audience.eligible_users already drops deleted and suspended accounts).

CONNECTS TO: afc_mail.views (audience count, send), afc_mail.tasks, afc_mail.sns (suppress()).
"""
from django.db.models import Q
from django.utils import timezone

from afc_auth.audience import parse_audience_spec, resolve_audience, spec_is_empty

from .models import CampaignRecipient, EmailSubscription, EmailSuppression


def normalise_email(email):
    return (email or "").strip().lower()


def is_suppressed(email):
    return EmailSuppression.objects.filter(email=normalise_email(email)).exists()


def suppress(email, reason, detail=""):
    """Never mail this address again. Idempotent; the first reason recorded is kept."""
    email = normalise_email(email)
    if not email:
        return
    EmailSuppression.objects.get_or_create(email=email, defaults={"reason": reason, "detail": (detail or "")[:255]})


def set_consent(user, opt_in, source):
    """Record a yes or a no, with when and where. Returns the subscription row."""
    sub, _ = EmailSubscription.objects.get_or_create(user=user)
    now = timezone.now()
    if opt_in and not sub.marketing_opt_in:
        sub.marketing_opt_in = True
        sub.opted_in_at = now
    elif not opt_in and sub.marketing_opt_in:
        sub.marketing_opt_in = False
        sub.opted_out_at = now
    elif not opt_in and sub.opted_out_at is None and sub.opted_in_at is None:
        sub.opted_out_at = now  # an explicit "no" from someone never asked before
    sub.source = (source or "")[:40]
    sub.save()
    return sub


def campaign_spec(campaign):
    """The stored spec, re-parsed so a hand-edited row can never widen the audience."""
    return parse_audience_spec(campaign.audience or {})


def audience_queryset(spec):
    """Users a campaign with this spec may reach (a queryset, never materialised)."""
    if spec_is_empty(spec):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.none()
    suppressed = EmailSuppression.objects.values("email")
    return (
        resolve_audience(spec)
        .filter(email_subscription__marketing_opt_in=True)
        .exclude(Q(email="") | Q(email__isnull=True))
        .exclude(email__in=suppressed)
    )


def audience_count(spec):
    """{"recipients": n, "selected": m}: how many the filters select at all, and how many of them
    can be mailed (said yes, have an address, not suppressed). The admin sees both, so a small
    number is explained rather than mysterious."""
    if spec_is_empty(spec):
        return {"selected": 0, "recipients": 0}
    return {"selected": resolve_audience(spec).count(), "recipients": audience_queryset(spec).count()}


LEFT_OUT_REASONS = ("inactive", "no_address", "bounced", "unsubscribed", "no_consent")


def reason_left_out(user, sub, suppressed, eligible_ids):
    """Why a picked person would not get the email, or None when they would. In the order the
    composer explains them: an account that is deleted or suspended, no address, an address SES
    reported bouncing or complaining, someone who said yes and later unsubscribed, someone who has
    not said yes (never asked, or said no)."""
    if user.pk not in eligible_ids:
        return "inactive"
    email = normalise_email(user.email)
    if not email:
        return "no_address"
    if email in suppressed:
        return "bounced"
    if sub is None or not sub.marketing_opt_in:
        return "unsubscribed" if (sub and sub.opted_in_at and sub.opted_out_at) else "no_consent"
    return None


def left_out(spec):
    """The picked players and teams that the email will miss, and why (#286: "select those accounts
    they want to send to specifically like in broadcast"; the email rule broadcasts do not have is
    consent, so a pick that cannot be mailed is named rather than silently dropped).

    [{"kind": "player", "id", "name", "reason"}, {"kind": "team", "id", "name", "left", "total"}].
    Nothing for "everyone" or the category filters: those are counted, not picked by name."""
    from django.contrib.auth import get_user_model

    from afc_auth.audience import eligible_users
    from afc_team.models import Team

    if spec.get("everyone"):
        return []
    out = []
    if spec.get("user_ids"):
        users = list(get_user_model().objects.filter(pk__in=spec["user_ids"]).only("pk", "username", "email"))
        ids = [u.pk for u in users]
        subs = {s.user_id: s for s in EmailSubscription.objects.filter(user_id__in=ids)}
        suppressed = set(EmailSuppression.objects.filter(email__in=[normalise_email(u.email) for u in users]).values_list("email", flat=True))
        eligible_ids = set(eligible_users(include_suspended=spec.get("include_suspended", False)).filter(pk__in=ids).values_list("pk", flat=True))
        for u in users:
            reason = reason_left_out(u, subs.get(u.pk), suppressed, eligible_ids)
            if reason:
                out.append({"kind": "player", "id": u.pk, "name": u.username, "reason": reason})
    for team in Team.objects.filter(team_id__in=spec.get("team_ids") or []).only("team_id", "team_name"):
        team_spec = parse_audience_spec({"team_ids": [team.team_id], "include_suspended": spec.get("include_suspended", False)})
        total = resolve_audience(team_spec).count()
        mailable = audience_queryset(team_spec).count()
        if total > mailable:
            out.append({"kind": "team", "id": team.team_id, "name": team.team_name, "left": total - mailable, "total": total})
    return out


def picked(spec):
    """The players and teams picked by name, with their names, so the To line can show them again
    when a draft is opened (a campaign stores ids only)."""
    from django.contrib.auth import get_user_model

    from afc_team.models import Team

    out = [{"kind": "team", "id": t.team_id, "name": t.team_name}
           for t in Team.objects.filter(team_id__in=spec.get("team_ids") or []).only("team_id", "team_name")]
    out += [{"kind": "player", "id": u.pk, "name": u.username}
            for u in get_user_model().objects.filter(pk__in=spec.get("user_ids") or []).only("pk", "username")]
    return out


def freeze_recipients(campaign):
    """Write one CampaignRecipient per person, at the moment the admin presses Send. The audience
    is decided once; later opt-ins do not join a campaign that is already going out, and later
    opt-outs are honoured at send time (afc_mail.tasks re-checks)."""
    rows = []
    seen = set()
    for user in audience_queryset(campaign_spec(campaign)).only("user_id", "email", "language").iterator():
        email = normalise_email(user.email)
        if not email or email in seen or len(email) > 180:
            continue
        seen.add(email)
        rows.append(CampaignRecipient(campaign=campaign, user=user, email=email,
                                      language=(getattr(user, "language", "") or "en")[:8]))
    CampaignRecipient.objects.bulk_create(rows, batch_size=500)
    return len(rows)
