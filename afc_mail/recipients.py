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
