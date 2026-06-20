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
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import TeamMembers


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
