"""
Clash-Squad BRACKET notifications (owner 2026-08-12).

WHY THIS EXISTS: nothing in the Clash Squad path told a player anything. The Battle Royale
room-details notice is written per StageGroups lobby (room id / name / password on the group),
and a Clash Squad stage has no groups, no lobbies and no StageGroupCompetitor rows - so a CS
competitor was never notified that a bracket had been drawn, who they were playing, when, what
the room ID was, or whether they had won. Everything they knew came from Discord.

WHAT IT SENDS (four moments, all best-effort - see the note on failure below)
  1. bracket drawn          -> every team in the bracket, with their first opponent
  2. room settings published -> every team the settings apply to, with the room ID + password
  3. match scheduled / live  -> the two teams in that match
  4. result recorded         -> the two teams, with who won and who they play next

EVERY notice DEEP-LINKS to the event (target_type="event", target_id=slug), per the platform rule
that a notification pointing somewhere must carry a "Take me there" link.

ENGLISH ON PURPOSE, like every other notification row: they are stored in English and localized
at READ time by afc_auth.get_notifications (translate-on-read), so writing correct English here
carries into French and Portuguese with no catalogue change. Event KIND ("tournament" / "scrims")
comes from event_wording.event_noun rather than being hardcoded.

BEST-EFFORT BY DESIGN: every public function swallows its own exceptions and logs. A notification
must never be the reason a bracket fails to generate or a result fails to save - the same rule the
Discord side-effects follow.

HOW IT CONNECTS
  - called by head_to_head_views.generate_h2h_bracket / report_h2h_match_result /
    update_h2h_match, and by cs_room_views.room_settings when a config is published;
  - reads TournamentTeamMember for who to tell (the frozen per-event roster), the same identity
    every other result surface uses;
  - writes afc_auth.Notifications, read by the FE notifications dropdown.
"""
import logging

from afc_auth.models import Notifications

from .event_wording import event_noun
from .models import HeadToHeadMatch, TournamentTeamMember

logger = logging.getLogger(__name__)


# ── who to tell ──────────────────────────────────────────────────────────────────────────────
def _members_of(tournament_team_ids):
    """Every user on the given teams' per-event rosters, de-duplicated.

    Uses TournamentTeamMember (the roster frozen for THIS event) rather than the live club roster,
    so somebody who left the team last week is not told about a match they are not in.
    """
    if not tournament_team_ids:
        return []
    rows = (
        TournamentTeamMember.objects
        .filter(tournament_team_id__in=list(tournament_team_ids),
                status__in=("active", "approved"))
        .select_related("user")
    )
    seen = {}
    for row in rows:
        if row.user_id and row.user_id not in seen:
            seen[row.user_id] = row.user
    return list(seen.values())


def _notify(users, event, title, message):
    """Create one notification per user, all deep-linked to the event page."""
    if not users:
        return 0
    Notifications.objects.bulk_create([
        Notifications(
            user=user,
            title=title,
            message=message,
            notification_type="clash_squad_bracket",
            related_event=event,
            # "Take me there" opens the event page, where the bracket lives.
            target_type="event",
            target_id=str(event.slug or event.event_id),
        )
        for user in users
    ])
    return len(users)


def _team_name(tt):
    # display_name, not .team.team_name: a bracket slot can hold a GHOST competitor, whose
    # team_id is NULL, and naming it "TBD" in a notification would tell a real opponent their
    # match has no opponent yet when it does (owner 2026-08-20).
    return tt.display_name if tt else "TBD"


# ── 1. the bracket was drawn ─────────────────────────────────────────────────────────────────
def notify_bracket_generated(stage):
    """Tell every team in a freshly generated bracket that it exists, and who they open against.

    Each team gets its OWN first opponent named, which is the only part of a bracket anybody
    reads first. A team with a bye is told it has one instead of being told nothing.
    Called by head_to_head_views.generate_h2h_bracket after the tree is built.
    """
    try:
        event = stage.event
        noun = event_noun(event, capitalized=False)
        matches = list(
            HeadToHeadMatch.objects
            .filter(stage=stage)
            .select_related("team_a__team", "team_b__team")
            .order_by("round_number", "position")
        )
        # First appearance of each team decides which match we quote to them.
        first_match = {}
        for m in matches:
            for tt in (m.team_a, m.team_b):
                if tt and tt.tournament_team_id not in first_match:
                    first_match[tt.tournament_team_id] = m

        sent = 0
        for team_id, match in first_match.items():
            opponent = match.team_b if match.team_a_id == team_id else match.team_a
            if opponent is None:
                body = (f"The bracket for '{stage.stage_name}' in the {noun} "
                        f"'{event.event_name}' is out. You have a bye in the first round.")
            else:
                body = (f"The bracket for '{stage.stage_name}' in the {noun} "
                        f"'{event.event_name}' is out. Your first match is against "
                        f"{_team_name(opponent)}.")
            sent += _notify(_members_of([team_id]), event, "Your bracket is out", body)
        return sent
    except Exception as exc:
        logger.warning("notify_bracket_generated failed for stage %s: %s",
                       getattr(stage, "stage_id", None), exc)
        return 0


# ── 2. the room settings were published ──────────────────────────────────────────────────────
def notify_room_published(config, scope, scope_object):
    """Tell the affected teams that the room is open, with the ID and password.

    Only fires for a PUBLISHED config that actually carries a room ID: publishing a ruleset with
    no room yet is a normal thing to do, and sending "the room is open" without a room would be a
    lie. Audience follows the scope - one match tells two teams, a stage tells everybody in it.
    Called by cs_room_views.room_settings after a save that turned is_published on.
    """
    try:
        if not config.is_published or not config.room_id:
            return 0

        if scope == "match":
            event = scope_object.stage.event
            team_ids = [t for t in (scope_object.team_a_id, scope_object.team_b_id) if t]
            where = f"your match in '{scope_object.stage.stage_name}'"
        elif scope == "stage":
            event = scope_object.event
            team_ids = _stage_team_ids(scope_object)
            where = f"'{scope_object.stage_name}'"
        elif scope == "event":
            event = scope_object
            team_ids = _event_team_ids(scope_object)
            where = f"'{event.event_name}'"
        else:
            return 0  # group scope is Battle Royale, which has its own room notice already

        password = config.room_password or "none"
        body = (f"The room for {where} is open. Room ID: {config.room_id}. "
                f"Password: {password}. "
                f"The full room settings are on the event page.")
        if config.notes:
            body += f" Note from the organizer: {config.notes}"
        return _notify(_members_of(team_ids), event, "Room details are up", body)
    except Exception as exc:
        logger.warning("notify_room_published failed for config %s: %s",
                       getattr(config, "cs_room_config_id", None), exc)
        return 0


def _stage_team_ids(stage):
    """Every team that appears anywhere in the stage's bracket, else its competitor pool."""
    ids = set()
    for m in HeadToHeadMatch.objects.filter(stage=stage).values_list("team_a_id", "team_b_id"):
        ids.update(t for t in m if t)
    if not ids:
        from .models import StageCompetitor
        ids = set(
            StageCompetitor.objects
            .filter(stage=stage, tournament_team__isnull=False)
            .values_list("tournament_team_id", flat=True))
    return list(ids)


def _event_team_ids(event):
    from .models import TournamentTeam
    return list(
        TournamentTeam.objects
        .filter(event=event, status="active")
        .exclude(is_waitlisted=True)
        .values_list("tournament_team_id", flat=True))


# ── 3. the match was scheduled, or went live ─────────────────────────────────────────────────
def notify_match_scheduled(match, *, went_live=False):
    """Tell the two teams their kick-off time, or that the match has started.

    The time is written as the organizer entered it (the event's own clock) and the FE renders
    every stored time in the VIEWER's timezone, so this deliberately names the date and time
    plainly rather than trying to localize here.
    Called by head_to_head_views.update_h2h_match.
    """
    try:
        event = match.stage.event
        team_ids = [t for t in (match.team_a_id, match.team_b_id) if t]
        opponents = f"{_team_name(match.team_a)} vs {_team_name(match.team_b)}"
        if went_live:
            return _notify(
                _members_of(team_ids), event, "Your match is live",
                f"{opponents} in '{match.stage.stage_name}' has started. Join the room now.")
        if not match.scheduled_date:
            return 0  # a cleared schedule is not news
        when = str(match.scheduled_date)
        if match.scheduled_time:
            when += f" at {match.scheduled_time.strftime('%H:%M')}"
        return _notify(
            _members_of(team_ids), event, "Your match has a time",
            f"{opponents} in '{match.stage.stage_name}' is scheduled for {when}.")
    except Exception as exc:
        logger.warning("notify_match_scheduled failed for match %s: %s",
                       getattr(match, "h2h_match_id", None), exc)
        return 0


# ── 4. the result was recorded ───────────────────────────────────────────────────────────────
def notify_match_result(match):
    """Tell both teams the result, and tell the winner who they play next.

    Each side gets its own sentence - "You beat X" reads very differently from "You lost to X" -
    because a single shared message that names both teams makes a player work out which one they
    are. A drawn league set says so rather than picking a winner.
    Called by head_to_head_views.report_h2h_match_result after the engine has advanced the tree.
    """
    try:
        event = match.stage.event
        if not (match.team_a_id and match.team_b_id):
            return 0

        # Reload the next match so a freshly-advanced opponent is visible.
        next_opponent = None
        if match.next_match_id and match.winner_id:
            nxt = (HeadToHeadMatch.objects
                   .select_related("team_a__team", "team_b__team")
                   .filter(h2h_match_id=match.next_match_id).first())
            if nxt:
                other = nxt.team_b if nxt.team_a_id == match.winner_id else nxt.team_a
                next_opponent = _team_name(other) if other else None

        how = ""
        if match.result_type != "normal":
            label = dict(HeadToHeadMatch.RESULT_TYPE_CHOICES).get(match.result_type,
                                                                  match.result_type)
            how = f" ({label.lower()}{': ' + match.result_note if match.result_note else ''})"

        sent = 0
        for team_id in (match.team_a_id, match.team_b_id):
            is_a = team_id == match.team_a_id
            mine, theirs = (match.score_a, match.score_b) if is_a else (match.score_b, match.score_a)
            opponent = _team_name(match.team_b if is_a else match.team_a)

            if not match.winner_id:
                title = "Your match ended in a draw"
                body = (f"Your match against {opponent} in '{match.stage.stage_name}' "
                        f"finished {mine}-{theirs}.{how}")
            elif match.winner_id == team_id:
                title = "You won your match"
                body = (f"You beat {opponent} {mine}-{theirs} in "
                        f"'{match.stage.stage_name}'.{how}")
                if next_opponent:
                    body += f" You play {next_opponent} next."
                elif next_opponent is None and match.next_match_id:
                    body += " Your next opponent is not decided yet."
            else:
                title = "Your match result is in"
                # Always the reader's OWN score first, so "you lost 1-4" reads the way a player
                # would say it rather than quoting the opponent's number at them.
                body = (f"You lost to {opponent} {mine}-{theirs} in "
                        f"'{match.stage.stage_name}'.{how}")
            sent += _notify(_members_of([team_id]), event, title, body)
        return sent
    except Exception as exc:
        logger.warning("notify_match_result failed for match %s: %s",
                       getattr(match, "h2h_match_id", None), exc)
        return 0
