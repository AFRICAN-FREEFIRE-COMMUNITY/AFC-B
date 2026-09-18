"""
afc_tournament_and_scrims/still_in.py - is a competitor still IN an event, or has the event moved
on without them? One answer, read by every lock that depends on it.

WHY ONE PLACE (owner rules R24 / R27). Until 2026-09-18 the rule "still in = an active
StageCompetitor row in a stage that is not completed" was written four times: the team roster lock
(afc_team.views._active_event_roster_blockers), the identity lock
(afc_auth.views._competitor_in_active_stage), the event page's Edit Roster flag
(viewer_team_stage_over) and edit_roster's own gate (team_stage_over). Four copies is how a rule
drifts, and it is why the owner's correction below had to land once, here.

THE RULE (owner 2026-06-30, refined 2026-09-18). A competitor is still in the event while it holds
an ACTIVE StageCompetitor row in a stage that is NOT completed (upcoming, ongoing or paused). That
alone left one hole the owner hit: the organizer seeds the NEXT stage with the teams that
qualified and never marks the previous stage completed (its end date is still ahead, or nobody
pressed the button), so the teams that did NOT qualify keep an active row in an "ongoing" stage
and stay locked in their clubs. Owner, 2026-09-18: "when teams/players did not qualify to the next
stage of an event, they should be allowed to do roster moves like leaving team or kicking players
when transfer window is open. Only teams that qualified should be limited."

So the second question is asked as well: how far has the EVENT got? The furthest stage (by
stage_order, then start_date) that holds any competitor row is where the event is. A competitor
whose rows all sit in earlier stages was left behind when that stage was seeded: out, whatever
the earlier stage's status says. A competitor with a row in the
current stage is in while that stage is not completed. Branching routes (advancement_routing) put
a losers' bracket at its own order, so a team routed there has a row at the furthest order and
stays in, which is right: it is still playing.

SAFE DEFAULT, unchanged: a competitor with NO stage rows at all (stages not seeded yet, or a data
gap) counts as still in. The lock exists to protect result attribution; erring towards "locked"
costs a player a day, erring towards "free" costs a result its player.

Inputs: `event`, and one of `tournament_team` (a TournamentTeam, squad events) or `player`
(a RegisteredCompetitors row, solo events).
"""
from .models import StageCompetitor


def competitor_still_in(event, tournament_team=None, player=None) -> bool:
    """True while the competitor can still be fielded in `event`. See the module docstring."""
    rows = StageCompetitor.objects.filter(stage__event=event)
    if tournament_team is not None:
        rows = rows.filter(tournament_team=tournament_team)
    elif player is not None:
        rows = rows.filter(player=player)
    else:
        return True  # unknown competitor: the safe default
    if not rows.exists():
        return True  # no stage data for them yet: the safe default

    # Where the event is: the furthest stage anybody has been seeded into. "Furthest" is the
    # stage's (stage_order, start_date), never its id: two stages that share both are parallel
    # branches (a finals and a consolation seeded together), and a team in either is at the
    # front. A list index would put one branch "behind" the other and free its teams by accident.
    rank = {sid: (order, start) for sid, order, start in
            event.stages.values_list("stage_id", "stage_order", "start_date")}
    seeded = set(StageCompetitor.objects.filter(stage__event=event).values_list("stage_id", flat=True))
    event_at = max((rank[s] for s in seeded if s in rank), default=None)
    theirs_at = max((rank[s] for s in rows.values_list("stage_id", flat=True) if s in rank), default=None)
    if event_at is not None and theirs_at is not None and theirs_at < event_at:
        return False  # the event moved on to a later stage without them: eliminated

    # In the current stage: in while an active row of theirs sits in a stage that is not over.
    return rows.filter(status="active").exclude(stage__stage_status="completed").exists()
