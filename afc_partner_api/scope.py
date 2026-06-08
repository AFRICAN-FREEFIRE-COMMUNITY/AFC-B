# afc_partner_api/scope.py
# ──────────────────────────────────────────────────────────────────────────────
# The ONE place that decides which Events a partner may read. Every read endpoint
# resolves its queryset through partner_visible_events(partner), so the scope rules
# live here once instead of being re-derived (and drifting) per view.
#
# Two invariants, in order:
#   1. PUBLISH GATE (always first): partner_published=True. An AFC admin must have
#      explicitly published an event before ANY partner — however broadly scoped —
#      can see it. This filter is applied before the grant union, so an unpublished
#      event is unreachable even when directly granted.
#   2. GRANT UNION (any one path is enough):
#        • explicit event grants  — Event in partner.allowed_events
#        • whole-org grants        — event owned by an org in partner.allowed_organizations
#        • native AFC events       — organization IS NULL, only if allow_all_native_afc
#
# `partner_grants` is the related_name shared by Partner.allowed_events (on Event)
# and Partner.allowed_organizations (on Organization) — the same accessor name on two
# different models — so Q(partner_grants=partner) reads the event-level grant while
# Q(organization__partner_grants=partner) hops to the org-level grant.
# Full spec: WEBSITE/tasks/partner-api-design.md (§6 scope).
# ──────────────────────────────────────────────────────────────────────────────
from django.db.models import Q

from afc_tournament_and_scrims.models import Event


def partner_visible_events(partner):
    """Return the distinct, published Events this partner is scoped to read."""
    # Union of the grant paths. Event-level + org-level grants always apply; the
    # native-AFC path is opt-in per partner (least privilege).
    grants = Q(partner_grants=partner) | Q(organization__partner_grants=partner)
    if partner.allow_all_native_afc:
        grants |= Q(organization__isnull=True)
    # Publish gate first, THEN the grants. .distinct() collapses duplicates from the
    # M2M joins (an event reachable via more than one grant path).
    return Event.objects.filter(partner_published=True).filter(grants).distinct()
