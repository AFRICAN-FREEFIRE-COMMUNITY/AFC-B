# afc_team/signals.py
#
# Keep Team.country auto-derived from the roster (owner 2026-06-20). Team.country reflects the LOCATION of
# the team's PLAYING members (see recompute_team_country / _derive_team_country in afc_team.views). Instead
# of calling the recompute from every roster-mutation endpoint (respond_invite, review_join_request,
# kick_team_member, exit_team, manage_team_roster), we hook the TeamMembers post_save + post_delete signals
# so EVERY current and future roster change recomputes the country in ONE place. The two triggers that do
# NOT touch a TeamMembers row are wired explicitly instead: transfer_ownership (the owner tiebreak changes)
# and afc_auth.edit_profile (a member edits their own User.country). Best-effort: a failure here must never
# break the underlying mutation (recompute_team_country swallows its own errors too).
#
# Wired in afc_team/apps.py -> AfcTeamConfig.ready(). Follows the existing repo signal pattern
# (see afc_rankings/signals.py).
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver

from .models import Team, TeamMembers


@receiver(post_save, sender=TeamMembers)
@receiver(post_delete, sender=TeamMembers)
def recompute_team_country_on_roster_change(sender, instance, **kwargs):
    """Any add / remove / role change on a team's roster re-derives that team's country."""
    # Lazy imports: views.py is heavy, and on a team DISBAND the cascade deletes members while the Team row
    # itself is going away - so look the team up defensively and no-op if it is already gone.
    try:
        from .models import Team
        from .views import recompute_team_country
        team = Team.objects.filter(pk=instance.team_id).first()
        if team is not None:
            recompute_team_country(team)
    except Exception:
        pass


# ── Automatic transfer feed (backlog item 21, owner 2026-08-08) ────────────────────────────────
# Same two triggers as the country recompute above, and for the same reason: a TeamMembers row
# existing IS membership, so hooking the row's own lifecycle covers every endpoint that moves a
# player - respond_invite, review_join_request, join_team, exit_team, kick_team_member,
# admin_add_member, admin_remove_member, the admin force-move branch, disband_team, and the Team
# cascade - as well as any path written after today. See the header of afc_team/models.py
# TeamTransfer for why this is a signal and not ten calls inside views.py.
#
# Writes to: afc_team.models.TeamTransfer, via afc_team.transfers.record_transfer (which also
# captures whether the transfer window was open at that moment). Read back by
# afc_team.views_transfers.get_transfer_feed.
@receiver(post_save, sender=TeamMembers)
def record_team_join(sender, instance, created, **kwargs):
    """A NEW membership row is somebody joining a team."""
    # created=False means an existing membership was edited - a management_role or in_game_role
    # change from manage_team_roster. That is not a transfer, and logging it would fill the public
    # feed with "X joined Y" every time a captain adjusts a position.
    if not created:
        return
    from .transfers import record_transfer
    record_transfer(instance, "joined")


@receiver(post_delete, sender=TeamMembers)
def record_team_leave(sender, instance, **kwargs):
    """A deleted membership row is somebody leaving a team (by choice, kick, or admin move)."""
    from .transfers import record_transfer
    record_transfer(instance, "left")


# ── Team.delete() / User.delete() guards ───────────────────────────────────────────────────────
# Deleting a Team cascades into its TeamMembers rows, so the receiver above fires for each one and
# would insert a TeamTransfer pointing at a team that is one statement away from being deleted -
# which makes the delete itself fail on the foreign key (MySQL 1451). Deleting a USER does exactly
# the same thing through TeamMembers.member (also a CASCADE), and that one is the likelier of the
# two to be hit, because deleting an account from the Django admin is an ordinary moderation act.
# These receivers bracket both deletes so record_transfer can see one coming and store the entry
# with that side left NULL instead. Full reasoning in afc_team/transfers.py §0.
@receiver(pre_delete, sender=Team)
def mark_team_being_deleted(sender, instance, **kwargs):
    from .transfers import TEAM_SCOPE, mark_deleting
    mark_deleting(TEAM_SCOPE, instance.pk)


@receiver(post_delete, sender=Team)
def clear_team_being_deleted(sender, instance, **kwargs):
    from .transfers import TEAM_SCOPE, unmark_deleting
    unmark_deleting(TEAM_SCOPE, instance.pk)


# sender is resolved through get_user_model() rather than importing afc_auth.models directly:
# afc_auth imports afc_team (afc_auth/views.py, audience.py), so the reverse import at module load
# would be circular. This module is imported from AfcTeamConfig.ready(), by which point the app
# registry is populated and get_user_model() is safe.
@receiver(pre_delete, sender=get_user_model())
def mark_player_being_deleted(sender, instance, **kwargs):
    from .transfers import PLAYER_SCOPE, mark_deleting
    mark_deleting(PLAYER_SCOPE, instance.pk)


@receiver(post_delete, sender=get_user_model())
def clear_player_being_deleted(sender, instance, **kwargs):
    from .transfers import PLAYER_SCOPE, unmark_deleting
    unmark_deleting(PLAYER_SCOPE, instance.pk)
