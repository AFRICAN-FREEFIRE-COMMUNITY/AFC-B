"""
Mark invitations as accepted where the invitee is ALREADY registered for the event.

WHY THIS EXISTS (owner 2026-09-03, looking at a live event): "it says no teams accepted but
obviously some have accepted." The Team invitations panel read "Accepted 0" with eight rows
pending, while three of those eight teams were sitting in Registered Teams on the same screen.

THE CAUSE, and it was ours. Until 2026-09-02 the only way to say yes was the Accept dialog on the
team page, which posts team-invitations/<id>/accept/ and flips the row. That dialog was replaced by
a link to the EVENT PAGE, because only the event page can resolve a refusal (sponsors, waivers,
connections, payment). The event page registers through views.register_for_event, which knew
nothing about invitations, so the answer was never written down. Teams got in and the organizer was
told nobody had replied.

register_for_event now settles the invitation itself (event_invites.settle_pending_invitation), so
this command exists for the rows written BEFORE that fix. It is a data repair, not a routine.

WHAT COUNTS AS ACCEPTED HERE. A pending invitation whose invitee holds a RegisteredCompetitors row
for the same event, in any live state (registered, approved, pending sponsor approval, waitlisted).
Withdrawn, left, rejected and disqualified are NOT counted: those are people who are not in the
event, so "accepted" would be a false record.

WHAT IT DELIBERATELY DOES NOT DO. It does not notify the inviter. Those notifications belong to the
moment the answer happened, days ago in most cases, and sending a burst of "X accepted your
invitation" now would be a lie about when. It also never touches an invitation that is already
answered (accepted, declined, cancelled, expired).

SAFE BY DEFAULT: dry-run unless --apply is passed.

USAGE (prod, inside the backend venv):
    python manage.py settle_accepted_invitations                  # dry-run, whole DB
    python manage.py settle_accepted_invitations --event 344      # dry-run, one event
    python manage.py settle_accepted_invitations --event 344 --apply

Touches afc_tournament_and_scrims.EventTeamInvitation only (status, responded_at). responded_by is
left NULL on purpose: nobody knows WHICH member of the team pressed register, and inventing one
would put a name against an action they may not have taken.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_tournament_and_scrims.models import EventTeamInvitation, RegisteredCompetitors

# The registration states that mean "this competitor is in the event". Mirrors the states the
# organizer's own Registered Teams panel treats as live.
LIVE_STATES = ("registered", "approved", "pending")


class Command(BaseCommand):
    help = "Mark pending invitations as accepted where the invitee already registered."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, default=None,
                            help="Limit to one event id.")
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without this the command only reports.")

    def handle(self, *args, **options):
        event_id = options["event"]
        apply_changes = options["apply"]

        rows = (EventTeamInvitation.objects
                .filter(status="pending")
                .select_related("event", "team", "user"))
        if event_id:
            rows = rows.filter(event_id=event_id)

        settled, skipped = [], 0
        for invitation in rows:
            regs = RegisteredCompetitors.objects.filter(
                event_id=invitation.event_id, status__in=LIVE_STATES)
            # A team invitation is answered by the TEAM being registered; a solo one by the PLAYER.
            if invitation.team_id:
                is_in = regs.filter(team_id=invitation.team_id).exists()
                who = invitation.team.team_name
            elif invitation.user_id:
                is_in = regs.filter(user_id=invitation.user_id).exists()
                who = invitation.user.username
            else:
                skipped += 1
                continue
            if is_in:
                settled.append((invitation, who))

        for invitation, who in settled:
            self.stdout.write(
                f"  #{invitation.id}  {who}  ->  accepted   "
                f"(event {invitation.event_id}: {invitation.event.event_name})"
            )
            if apply_changes:
                invitation.status = "accepted"
                invitation.responded_at = invitation.responded_at or timezone.now()
                invitation.save(update_fields=["status", "responded_at"])

        self.stdout.write("")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(
                f"Settled {len(settled)} invitation(s)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN. {len(settled)} invitation(s) would be settled. "
                f"Re-run with --apply to write them."))
        if skipped:
            self.stdout.write(f"Skipped {skipped} row(s) that address neither a team nor a player.")
