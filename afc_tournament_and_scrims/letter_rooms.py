"""
afc_tournament_and_scrims.letter_rooms - in-game letters checked per ROOM (inbox #602, owner 2026-11-16).

Owner: "i need to be able to do this only teams in certains groups or per stage and there should also
be a part that suggests what each team should use". Then, on keeping a letter across stages: "The
admin or organizer decides that, also decides what stages it applies to."

THE RULE
  In Free Fire a letter only has to be distinct inside one ROOM (a lobby). So two teams may hold the
  same letter as long as they never share a lobby in a stage the letter applies to. Only 26 letters
  exist, which is why the old one-letter-per-event rule could never letter a 48-team event.

WHERE A LETTER LIVES
  - TournamentTeam.assigned_letter  = the ALL STAGES letter (kept across stages).
  - StageTeamLetter(stage, team)    = a letter for ONE stage; it overrides the all-stages letter there.
  effective_letters(event, stage) resolves the two.

WHAT IS A ROOM (stage_rooms)
  - every StageGroups lobby of the stage: its StageGroupCompetitor teams, PLUS, for a round robin
    day lobby ("Day 1: A+B"), the teams of the base groups it merges (source_groups). The base group
    teams are added even when the lobby's competitor rows were never seeded, because the group draw
    places teams in base groups (lessons_round_robin_groups_live_elsewhere).
  - a stage with no lobbies yet is ONE room: its StageCompetitor teams plus any round robin base
    group teams.

HOW IT CONNECTS
  - views.assign_team_letter (one team) and views.assign_team_letters_bulk (the suggester's "Apply
    all") call apply_letters; a LetterClash becomes a 409 {code: letter_taken}.
  - views.get_event_team_letters sends stage_rooms for every stage so the site can filter by stage /
    group and run the suggester (frontend lib/letterSuggest.ts) on the same rooms.
  - broadcast_kit uses effective_letters so a stage's client files carry that stage's letters.
"""
from django.db import transaction

from .models import Stages, StageCompetitor, StageTeamLetter, TournamentTeam


class LetterClash(Exception):
    """Raised by apply_letters when the final state puts one letter on two teams in one room.

    `info` = {tournament_team_id, other_tournament_team_id, letter, stage_id, stage_name, group_name}.
    Raised INSIDE the atomic block, so nothing from the refused apply is kept.
    """

    def __init__(self, info):
        super().__init__(f"letter {info['letter']} clashes in stage {info['stage_id']}")
        self.info = info


def stage_rooms(stage):
    """Rooms of one stage: [{"group_id": int|None, "group_name": str, "team_ids": set[int]}].

    team_ids are TournamentTeam primary keys. Lobbies come first in their stored order; a stage with
    no lobbies returns a single room (group_id None) so a final with one lobby-less pool still checks.
    """
    lobbies = list(
        stage.groups.all()
        .order_by("group_id")
        .prefetch_related("competitors", "source_groups__teams")
    )
    rooms = []
    for lobby in lobbies:
        ids = {c.tournament_team_id for c in lobby.competitors.all() if c.tournament_team_id}
        for base in lobby.source_groups.all():
            ids |= {t.pk for t in base.teams.all()}
        rooms.append({"group_id": lobby.group_id, "group_name": lobby.group_name, "team_ids": ids})
    if not lobbies:
        ids = set(
            StageCompetitor.objects.filter(stage=stage, tournament_team__isnull=False)
            .values_list("tournament_team_id", flat=True)
        )
        for base in stage.round_robin_groups.all().prefetch_related("teams"):
            ids |= {t.pk for t in base.teams.all()}
        rooms.append({"group_id": None, "group_name": "", "team_ids": ids})
    return rooms


def effective_letters(event, stage):
    """{tournament_team_id: letter or None} for every registered team of `event` in `stage`.

    The stage's own StageTeamLetter wins; otherwise the all-stages TournamentTeam.assigned_letter.
    """
    letters = dict(TournamentTeam.objects.filter(event=event).values_list("pk", "assigned_letter"))
    for tt_id, letter in StageTeamLetter.objects.filter(stage=stage).values_list("tournament_team_id", "letter"):
        letters[tt_id] = letter
    return letters


def _first_clash(event, stage, changed_ids):
    """The first room of `stage` where a CHANGED team shares its letter with another team, or None.

    Only clashes that involve a team this apply touched are reported, so an old clash between two
    other teams (made before this rule, or written by hand) never blocks an unrelated assignment.
    """
    letters = effective_letters(event, stage)
    for room in stage_rooms(stage):
        holder = {}
        # Changed teams last, so `holder` names the team that ALREADY had the letter.
        for tt_id in sorted(room["team_ids"], key=lambda i: (i in changed_ids, i)):
            letter = letters.get(tt_id)
            if not letter:
                continue
            other = holder.get(letter)
            if other is not None and (tt_id in changed_ids or other in changed_ids):
                return {
                    "tournament_team_id": tt_id,
                    "other_tournament_team_id": other,
                    "letter": letter,
                    "stage_id": stage.stage_id,
                    "stage_name": stage.stage_name,
                    "group_name": room["group_name"],
                }
            holder.setdefault(letter, tt_id)
    return None


def apply_letters(event, changes, stage_ids):
    """Write letters for several teams at once, refusing any room clash in the final state.

    changes   {tournament_team_id: "A".."Z" or None (None clears)}
    stage_ids None or [] -> ALL STAGES: sets TournamentTeam.assigned_letter and deletes the team's
              StageTeamLetter rows (the organizer chose "keep it across stages", so no stage may
              still override it). A list -> one StageTeamLetter per stage (None deletes that row);
              the all-stages letter is left alone.

    Returns the changed TournamentTeam rows (team selected). Raises ValueError when a stage id is not
    a stage of `event`, LetterClash when the final state clashes. Both leave the database untouched.
    """
    with transaction.atomic():
        # Lock the event's registrations so two staff applying at once cannot both pass the check.
        locked = {
            tt.pk: tt
            for tt in TournamentTeam.objects.select_for_update().filter(event=event).select_related("team")
        }
        targets = [locked[tt_id] for tt_id in changes if tt_id in locked]

        if stage_ids:
            stages = list(Stages.objects.filter(event=event, stage_id__in=stage_ids))
            if len(stages) != len(set(stage_ids)):
                raise ValueError("stage_ids must be stages of this event")
            for tt in targets:
                letter = changes[tt.pk]
                for stage in stages:
                    if letter:
                        StageTeamLetter.objects.update_or_create(
                            stage=stage, tournament_team=tt, defaults={"letter": letter},
                        )
                    else:
                        StageTeamLetter.objects.filter(stage=stage, tournament_team=tt).delete()
        else:
            stages = list(Stages.objects.filter(event=event))
            for tt in targets:
                tt.assigned_letter = changes[tt.pk] or None
                tt.save(update_fields=["assigned_letter"])
                StageTeamLetter.objects.filter(tournament_team=tt).delete()

        changed_ids = {tt.pk for tt in targets}
        for stage in stages:
            clash = _first_clash(event, stage, changed_ids)
            if clash:
                raise LetterClash(clash)
        if not stage_ids and not stages:
            # No stages yet: there are no lobbies to compare, so keep the old rule and treat the
            # whole event as one room (frontend lib/letterScope.ts roomsFor does the same).
            holder = {}
            for tt in sorted(TournamentTeam.objects.filter(event=event), key=lambda t: (t.pk in changed_ids, t.pk)):
                letter = tt.assigned_letter
                if not letter:
                    continue
                other = holder.get(letter)
                if other is not None and tt.pk in changed_ids:
                    raise LetterClash({
                        "tournament_team_id": tt.pk, "other_tournament_team_id": other, "letter": letter,
                        "stage_id": None, "stage_name": event.event_name, "group_name": "",
                    })
                holder.setdefault(letter, tt.pk)
    return targets
