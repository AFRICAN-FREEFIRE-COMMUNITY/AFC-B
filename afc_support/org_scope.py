"""
afc_support/org_scope.py - who may work an ORGANIZER's support conversations (inbox #167 / #175).

Owner 2026-10-08: "We want to give organizers their own support feature ... people will be able to
ask them questions and they should be able to answer and view things sent to them, including
attachments." Then, on who else sees them: "Only head admin and super admins can see stuff of
organizer", and on who may ask: "Signed-in players only".

THE RULE, in one place (every desk endpoint asks it through desk_access below):
    A ticket with `organization` set is the organization's conversation. It may be read and
    answered by:
      - an ACTIVE member of that organization who is its owner or holds can_answer_support, and
      - on the AFC side, ONLY head admins and super admins (and Django superusers).
    Ordinary AFC support staff (support_admin, moderators, the coarse "admin" role) never see it,
    and the AFC desk never lists it. That is deliberately STRICTER than
    afc_organizers.permissions.org_can, whose oversight bypass also passes organizer_admin.

    A ticket with no organization is AFC's, worked by AFC support staff exactly as before.

Callers: afc_support/views_people.py (the desk by person), afc_support/views.py (the per-ticket
status / reply / detail endpoints), afc_support/views_org.py (Ask the organizer).
"""
from rest_framework import status
from rest_framework.response import Response

from django.db import models

from afc_organizers.models import Organization, OrganizationMember

# The AFC roles that may see organizer conversations (owner: head admins and super admins only).
ORG_OVERSIGHT_ROLES = ("super_admin", "head_admin")


def is_head_or_super(user) -> bool:
    if not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    try:
        return user.userroles.filter(role__role_name__in=ORG_OVERSIGHT_ROLES).exists()
    except Exception:
        return False


def can_answer_org_support(user, organization) -> bool:
    """May `user` read and answer the support conversations addressed to `organization`?"""
    if not user or organization is None:
        return False
    if is_head_or_super(user):
        return True
    # A suspended or deleted organization has no desk for its members ("no actions"); AFC
    # oversight still reads what it was sent.
    if organization.status != "active":
        return False
    member = OrganizationMember.objects.filter(organization=organization, user=user, status="active").first()
    return bool(member and (member.role == "owner" or member.can_answer_support))


def works_any_desk(user, is_support_staff) -> bool:
    """Does `user` work ANY support desk (AFC's, or at least one organization's)? Somebody who
    works none gets the plain "no access" refusal before any ticket is looked up, so the answer
    never depends on whether a ticket number exists."""
    if not user:
        return False
    if is_support_staff(user) or is_head_or_super(user):
        return True
    return (OrganizationMember.objects.filter(user=user, status="active", organization__status="active")
            .filter(models.Q(role="owner") | models.Q(can_answer_support=True)).exists())


def answerers(organization):
    """The users who work `organization`'s desk (owners + members with the permission), for the
    "new question" notification. AFC oversight is not notified: it reads, it does not answer."""
    rows = (OrganizationMember.objects.filter(organization=organization, status="active")
            .select_related("user"))
    return [m.user for m in rows if m.role == "owner" or m.can_answer_support]


def desk_access(request, actor, is_support_staff, slug=None):
    """Which desk the caller is working, as (organization | None, error Response | None).

    `slug` (the `organization` query or body value) selects an organizer desk; without it the
    caller is working the AFC desk and must be AFC support staff (`is_support_staff`, passed in so
    this module does not import views.py and make a cycle).
    """
    if not actor:
        return None, Response({"message": "Please sign in to continue.", "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    slug = (slug or "").strip()
    if slug:
        org = Organization.objects.filter(slug=slug).first()
        if not org or not can_answer_org_support(actor, org):
            # 403, and the same answer for an organization that does not exist, so the desk does
            # not confirm which slugs are real to somebody who cannot open them.
            return None, Response({"message": "You do not have access to this organization's support desk.",
                                   "code": "org_support_forbidden"}, status=status.HTTP_403_FORBIDDEN)
        return org, None
    if not is_support_staff(actor):
        return None, Response({"message": "You do not have access to the support desk.",
                               "code": "support_forbidden"}, status=status.HTTP_403_FORBIDDEN)
    return None, None


def ticket_access(actor, ticket, is_support_staff):
    """May `actor` work this one ticket? AFC tickets: support staff. Organizer tickets: the
    organization's answerers and head/super admins only. Returns an error Response or None."""
    if not actor:
        return Response({"message": "Please sign in to continue.", "code": "auth_required"},
                        status=status.HTTP_401_UNAUTHORIZED)
    if ticket.organization_id:
        if can_answer_org_support(actor, ticket.organization):
            return None
    elif is_support_staff(actor):
        return None
    # A ticket the caller may not see answers like one that does not exist.
    return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                    status=status.HTTP_404_NOT_FOUND)


def organizer_desks(user):
    """The organizer desks `user` may open, as [{name, slug, open_count}], for support/access/.

    Head / super admins: every organization that has been asked anything. Everybody else: the
    active organizations where they are owner or hold can_answer_support. `open_count` is how many
    of that organization's questions wait on an answer (status open)."""
    from django.db.models import Count, Q

    if not user:
        return []
    if is_head_or_super(user):
        orgs = Organization.objects.filter(support_tickets__isnull=False).distinct()
    else:
        mine = (OrganizationMember.objects.filter(user=user, status="active", organization__status="active")
                .filter(Q(role="owner") | Q(can_answer_support=True))
                .values_list("organization_id", flat=True))
        orgs = Organization.objects.filter(pk__in=list(mine))
    orgs = orgs.annotate(open_count=Count("support_tickets", filter=Q(support_tickets__status="open"),
                                          distinct=True)).order_by("name")
    return [{"name": o.name, "slug": o.slug, "open_count": o.open_count} for o in orgs]
