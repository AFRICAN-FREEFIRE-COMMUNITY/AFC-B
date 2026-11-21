"""What an organization's badge decides: the ONE module every gate asks (open organizations, plan 1;
AFC inbox #582 to #601, approved mockup mockups/organizer-dashboard/, owner decisions in HANDOVER-2026-11-16.md).

Badges are trust and reach only (owner, 16 Nov 2026: "any organization can use the full features free"): Community
(anyone who starts one), Verified (given by an AFC admin when the six rules below are met), AFC Partner (Verified
first, then by agreement with AFC, with perks). No tool is ever gated here. What IS decided here:

- counts_for_rankings(org): only Verified and Partner results count (#588). Read by
  afc_rankings/aggregation.py (_unverified_org_event_ids) and create_event (a new event of such an organization is
  created with rankings_verified True, the per-event review lever that the reports flow can still switch off).
- event_may_email(event): event email (accepted, rejected, team activated, invitations, draws) goes out only for
  AFC's own events and AFC Partner events with the `promo` perk on (#599 to #601). Read by afc_auth.views.send_email
  when a caller passes event=, and by the event contract's `sends_email` key (the registration notice on the site).
- verified_rules(org): the six rules the admin Organizations page shows and set_badge("verified") requires.
- set_badge / set_perk / set_rule_tick: the only writers of badge, perks and the two admin ticks, each leaving an
  OrganizationBadgeHistory row. Called by views_admin.py (the admin page) and views_start.py (records "started").
"""
import datetime

from django.db import transaction
from django.db.models import Avg
from django.utils import timezone

BADGES = ("community", "verified", "partner")
# The approved mockup's six perks plus faster screenshot reading (owner, #597). Order = the admin page's order.
PERKS = ("featured", "promo", "stream", "qualifiers", "support", "early", "fast_reader")
# The Verified rules, in the order the pages list them (#587: 20 events; #592: the AFC official in the community).
RULES = ("events", "trophies", "rating", "reports", "profile", "community")
FINISHED_EVENTS_NEEDED = 20
RATING_NEEDED = 4.0
REPORT_WINDOW_DAYS = 90
TICK_RULES = {"trophies": ("trophies_confirmed_at", "trophies_confirmed_by"),
              "community": ("community_access_at", "community_access_by")}


def counts_for_rankings(org):
    """True when this organization's results may count for the AFC rankings (Verified or AFC Partner)."""
    return getattr(org, "badge", "") in ("verified", "partner")


def event_may_email(event):
    """True when AFC may email players about this event: AFC's own events, and AFC Partner events with `promo` on."""
    if getattr(event, "organization_id", None) is None:
        return True
    org = event.organization
    return org.badge == "partner" and bool((org.perks or {}).get("promo"))


def verified_rules(org):
    """[{"key", "met", "value"}] for the six Verified rules, in RULES order."""
    from afc_tournament_and_scrims.models import Event

    from .models import EventRating, OrganizationReport

    finished = Event.objects.filter(organization=org, event_status="completed").count()
    avg = EventRating.objects.filter(event__organization=org).aggregate(a=Avg("score"))["a"]
    since = timezone.now() - datetime.timedelta(days=REPORT_WINDOW_DAYS)
    upheld = OrganizationReport.objects.filter(organization=org, status="resolved", updated_at__gte=since).count()
    socials = org.socials if isinstance(org.socials, dict) else {}
    profile = bool(org.logo) and bool((org.description or "").strip()) and any(
        str(v or "").strip() for v in socials.values())
    return [
        {"key": "events", "met": finished >= FINISHED_EVENTS_NEEDED, "value": finished},
        {"key": "trophies", "met": org.trophies_confirmed_at is not None,
         "value": org.trophies_confirmed_at.isoformat() if org.trophies_confirmed_at else None},
        {"key": "rating", "met": avg is not None and avg >= RATING_NEEDED,
         "value": round(float(avg), 1) if avg is not None else None},
        {"key": "reports", "met": upheld == 0, "value": upheld},
        {"key": "profile", "met": profile, "value": profile},
        {"key": "community", "met": org.community_access_at is not None,
         "value": org.community_access_at.isoformat() if org.community_access_at else None},
    ]


def all_rules_met(org):
    return all(r["met"] for r in verified_rules(org))


def _locked(org):
    from .models import Organization
    return Organization.objects.select_for_update().get(pk=org.pk)


def set_badge(org, to, by, note=""):
    """Move an organization to badge `to`, logging it. Partner switches every perk on; leaving Partner clears them.
    The caller decides whether the move is allowed (views_admin checks the rules); this only refuses unknown badges."""
    from .models import OrganizationBadgeHistory

    if to not in BADGES:
        raise ValueError("unknown badge %r" % (to,))
    with transaction.atomic():
        row = _locked(org)
        before = row.badge
        row.badge = to
        if to == "partner":
            row.perks = {k: True for k in PERKS}
        elif before == "partner":
            row.perks = {}
        row.save(update_fields=["badge", "perks", "updated_at"])
        hist = OrganizationBadgeHistory.objects.create(organization=row, action="badge", from_badge=before,
                                                       to_badge=to, note=note or "", by=by)
    org.badge, org.perks = row.badge, row.perks
    return hist


def set_perk(org, key, on, by):
    from .models import OrganizationBadgeHistory

    if key not in PERKS:
        raise ValueError("unknown perk %r" % (key,))
    with transaction.atomic():
        row = _locked(org)
        perks = dict(row.perks or {})
        perks[key] = bool(on)
        row.perks = perks
        row.save(update_fields=["perks", "updated_at"])
        OrganizationBadgeHistory.objects.create(organization=row, action="perk_on" if on else "perk_off",
                                                from_badge=row.badge, to_badge=row.badge, note=key, by=by)
    org.perks = row.perks


def set_rule_tick(org, rule, on, by, note=""):
    """The two admin ticks: "trophies" (trophies handed over and confirmed) and "community" (an AFC official joined
    the organization's community)."""
    from .models import OrganizationBadgeHistory

    if rule not in TICK_RULES:
        raise ValueError("unknown rule tick %r" % (rule,))
    at_field, by_field = TICK_RULES[rule]
    with transaction.atomic():
        row = _locked(org)
        setattr(row, at_field, timezone.now() if on else None)
        setattr(row, by_field, by if on else None)
        row.save(update_fields=[at_field, by_field, "updated_at"])
        OrganizationBadgeHistory.objects.create(organization=row, action="tick_on" if on else "tick_off",
                                                from_badge=row.badge, to_badge=row.badge,
                                                note=("%s: %s" % (rule, note)) if note else rule, by=by)
    setattr(org, at_field, getattr(row, at_field))
    setattr(org, by_field + "_id", getattr(row, by_field + "_id"))
