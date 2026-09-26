"""afc_referrals.signals - where a referral gets counted: the real write paths, not the views.

Each qualifying action has many writers (a team membership alone is created by five views: create, join,
accept a request, accept an invite, an admin add). Listening to the model saves means every one of them,
and any written later, counts without having to remember to. All four hand the user to engine.on_action,
which does nothing unless that user has a pending referral whose program counts that rule, and which
never raises.

  RULE_SIGNUP    afc_auth.User saved with is_active True (email confirmed; Google / Discord accounts
                 are active from creation, and engine.claim counts those at claim time)
  RULE_TEAM      afc_team.TeamMembers created (joining, or creating a team, which makes you its captain)
  RULE_EVENT     afc_tournament_and_scrims.RegisteredCompetitors saved as registered / approved: the
                 player themself, or every member of the registering team
  RULE_PURCHASE  afc_shop.Order saved as paid / fulfilled
Connected in AfcReferralsConfig.ready().
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from afc_auth.models import User
from afc_shop.models import Order
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import RegisteredCompetitors

from . import engine
from .models import ReferralProgram

COUNTED_REGISTRATION = ("registered", "approved")
COUNTED_ORDER = ("paid", "fulfilled")


def _has_pending(user_id):
    # Cheap guard so the ordinary save (no referral) costs one indexed lookup and nothing more
    from .models import Referral
    return Referral.objects.filter(referred_id=user_id, status=Referral.PENDING).exists()


@receiver(post_save, sender=User, dispatch_uid="referrals_user_verified")
def user_verified(sender, instance, created, **kwargs):
    if instance.is_active and _has_pending(instance.pk):
        engine.on_action(instance, ReferralProgram.RULE_SIGNUP)


@receiver(post_save, sender=TeamMembers, dispatch_uid="referrals_team_member")
def team_member(sender, instance, created, **kwargs):
    if created and _has_pending(instance.member_id):
        engine.on_action(instance.member, ReferralProgram.RULE_TEAM)


@receiver(post_save, sender=RegisteredCompetitors, dispatch_uid="referrals_registration")
def registration(sender, instance, created, **kwargs):
    if instance.status not in COUNTED_REGISTRATION:
        return
    if instance.user_id:
        people = [instance.user_id]
    elif instance.team_id:
        people = list(TeamMembers.objects.filter(team_id=instance.team_id).values_list("member_id", flat=True))
    else:
        return
    for user_id in people:
        if _has_pending(user_id):
            engine.on_action(User.objects.get(pk=user_id), ReferralProgram.RULE_EVENT, event=instance.event)


@receiver(post_save, sender=Order, dispatch_uid="referrals_order_paid")
def order_paid(sender, instance, created, **kwargs):
    if instance.status in COUNTED_ORDER and _has_pending(instance.user_id):
        engine.on_action(instance.user, ReferralProgram.RULE_PURCHASE)
