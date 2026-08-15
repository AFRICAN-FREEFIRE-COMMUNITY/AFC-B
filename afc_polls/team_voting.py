"""
afc_polls.team_voting - how a team's members turn into one team answer.

WHAT THIS IS FOR
    Poll.subject = 'team'. Only somebody on a roster may answer, their PollResponse.team is stamped
    at submit, and the team as a whole is recorded as having said one thing per question. This
    module is the whole of that arithmetic: quorum, plurality, ties, and the captain override.

THE THREE DECISIONS IT ENCODES (polls spec 3, decisions 5 and 6)

  1. QUORUM IS NOT OPTIONAL, and its denominator is the PLAYING roles only.
     Without a quorum, one member speaks for a five-man roster and a team poll is decided by
     whoever opens the notification first, which is not a team poll. And counting staff would
     punish the better-organised team: five players plus a coach, a manager and an analyst would
     need five answers under a whole-roster count while a bare five-player roster needs three. A
     team should not fail quorum because its analyst is on holiday.

  2. A TIE FALLS TO THE CAPTAIN, and a tie with no captain answer is `no_consensus`.
     The second half is the part worth defending. A team that was SPLIT is not the same event as a
     team that was SILENT, and neither is the same as a team led by somebody who never opened the
     poll. Three different follow-ups, so three different buckets. Collapsing them into "no result"
     takes the distinction away from the only person who could act on it.

  3. THE CAPTAIN OVERRIDE IS OFF BY DEFAULT AND ALWAYS VISIBLE WHEN USED.
     An override the roster cannot see is a trust problem, not a feature. If a captain can quietly
     overturn what five people voted for, the next poll gets fewer answers and the one after that
     gets fewer still. Making it visible costs the captain nothing when the override was reasonable
     and costs them a conversation when it was not, which is the right distribution of that cost.

WHEN IT RUNS
    On EVERY member submit, for that team and that poll only: one grouped count over that team's
    responses, so it is cheap, and the roll-up panel has to be live or it is pointless. Frozen at
    close by `freeze_poll` against the roster AS IT STANDS THEN, and the frozen row is what results
    and exports read.

    One consequence to accept out loud: a team that adds a member during an open poll RAISES ITS
    OWN QUORUM and can drop below it. That is correct (the new member really is on the team) but it
    will surprise somebody, so the roll-up reports the fraction rather than a bare "quorum met".

HOW THIS CONNECTS
    Reads afc_team.TeamMembers / Team and afc_polls.PollResponse / PollAnswer; writes
    afc_polls.PollTeamResult. Called by afc_polls.views.submit_response (recompute), the captain
    override endpoint, and the close-time freeze. Rendered by
    frontend/app/(user)/polls/[slug]/_components/TeamRollup.tsx.
"""
from django.db.models import Count
from django.utils import timezone

from .models import Poll, PollAnswer, PollResponse, PollTeamResult

# The roles that can be FIELDED, and therefore the only ones a quorum counts. The canonical split
# lives in afc_team/views.py as PLAYER_ROLES / STAFF_ROLES; it is repeated rather than imported
# because importing afc_team.views here would drag a whole view module (and its own imports) into
# every poll submit. If that list ever changes, this is the second place to change.
PLAYING_ROLES = ("team_captain", "vice_captain", "member")


def user_team_for_poll(poll, user):
    """The team this user answers on behalf of, or None.

    A person on two rosters is a real thing at AFC. When a poll is scoped to an event, the team
    that is REGISTERED in that event wins, which is almost always what an organizer running a
    captains' vote means. Otherwise the earliest roster row wins, so at least the choice is stable
    between two loads of the same page rather than depending on row order.
    """
    from afc_team.models import Team, TeamMembers

    memberships = list(
        TeamMembers.objects.filter(member=user).order_by("id").values_list("team_id", flat=True)
    )
    owned = list(Team.objects.filter(team_owner=user).values_list("team_id", flat=True))
    candidates = memberships + [team_id for team_id in owned if team_id not in memberships]
    if not candidates:
        return None

    if poll.event_id and len(candidates) > 1:
        from afc_tournament_and_scrims.models import RegisteredCompetitors

        registered = set(
            RegisteredCompetitors.objects.filter(
                event_id=poll.event_id, team_id__in=candidates,
                status__in=["registered", "approved"], is_waitlisted=False,
            ).values_list("team_id", flat=True)
        )
        for team_id in candidates:
            if team_id in registered:
                return Team.objects.filter(team_id=team_id).first()

    return Team.objects.filter(team_id=candidates[0]).first()


def roster_counts(team):
    """(playing, full). Two numbers, because they answer two different questions and one field
    called `roster_size` would be read as the second and used as the first."""
    from afc_team.models import TeamMembers

    rows = list(TeamMembers.objects.filter(team=team).values_list("management_role", flat=True))
    playing = sum(1 for role in rows if role in PLAYING_ROLES)
    return playing, len(rows)


def quorum_target(poll, playing_size):
    """How many playing members must answer before the team casts a vote at all.

    `half` means MORE than half, so a roster of six needs four and a roster of five needs three. A
    plain half would let a 3-3 split be a decision, which is the opposite of what a quorum is for.
    """
    if poll.team_quorum == Poll.QUORUM_ANY:
        return 1
    if poll.team_quorum == Poll.QUORUM_ALL:
        return max(playing_size, 1)
    return max(playing_size // 2 + 1, 1)


def _captain_ids(team):
    """Both representations of "captain", because they can disagree and the roster data is old
    enough that both spellings exist. Team.team_captain is a direct FK; a TeamMembers row with
    management_role == 'team_captain' is a separate fact. A rule honouring only one of them would
    quietly exclude a real captain, which is indistinguishable from a bug."""
    from afc_team.models import TeamMembers

    ids = set(
        TeamMembers.objects.filter(team=team, management_role="team_captain")
        .values_list("member_id", flat=True)
    )
    if team.team_captain_id:
        ids.add(team.team_captain_id)
    return ids


def user_is_captain(team, user):
    return bool(user) and user.pk in _captain_ids(team)


def recompute_team_result(poll, team, question, actor=None):
    """Roll this team's member answers up into one PollTeamResult for one question.

    Returns the saved row. Never raises on a team with no answers: a team that has not started is
    a real state and gets a row saying so, which is what lets the roll-up panel render "0 of 6"
    instead of an empty card that reads as an error.

    An existing CAPTAIN_OVERRIDE row is left in place. The captain's decision is not something a
    later member submit should silently undo; the tally underneath it is refreshed so the roster
    keeps seeing what they actually voted for beside it, which is the visibility half of decision 6.
    """
    playing_size, full_size = roster_counts(team)
    target = quorum_target(poll, playing_size)

    rows = (
        PollAnswer.objects.filter(
            question=question,
            response__poll=poll,
            response__team=team,
            response__status=PollResponse.SUBMITTED,
            option__isnull=False,
        )
        .values("option_id")
        .annotate(votes=Count("answer_id"))
    )
    tally = {row["option_id"]: row["votes"] for row in rows}

    # Answered = distinct PLAYING members who submitted, not the number of picks: a multiple-choice
    # question would otherwise let one person satisfy a quorum of three on their own.
    answered = _answered_playing_members(poll, team, question)

    existing = PollTeamResult.objects.filter(poll=poll, question=question, team=team).first()
    if existing and existing.resolution == PollTeamResult.CAPTAIN_OVERRIDE:
        existing.tally = {str(k): v for k, v in tally.items()}
        existing.playing_roster_size = playing_size
        existing.full_roster_size = full_size
        existing.answered_count = answered
        existing.quorum_met = answered >= target
        existing.save()
        return existing

    winning_option_id, resolution = _resolve(poll, team, question, tally, answered, target)

    result, _ = PollTeamResult.objects.update_or_create(
        poll=poll, question=question, team=team,
        defaults={
            "winning_option_id": winning_option_id,
            "tally": {str(k): v for k, v in tally.items()},
            "playing_roster_size": playing_size,
            "full_roster_size": full_size,
            "answered_count": answered,
            "quorum_met": answered >= target,
            "resolution": resolution,
            "set_by": actor if resolution == PollTeamResult.CAPTAIN_OVERRIDE else None,
        },
    )
    return result


def _answered_playing_members(poll, team, question):
    """How many DISTINCT playing members of this team have answered this question."""
    from afc_team.models import TeamMembers

    playing_ids = set(
        TeamMembers.objects.filter(team=team, management_role__in=PLAYING_ROLES)
        .values_list("member_id", flat=True)
    )
    if not playing_ids:
        return 0
    responded = set(
        PollAnswer.objects.filter(
            question=question, response__poll=poll, response__team=team,
            response__status=PollResponse.SUBMITTED,
        ).values_list("response__respondent_id", flat=True)
    )
    return len(playing_ids & {rid for rid in responded if rid})


def _resolve(poll, team, question, tally, answered, target):
    """(winning_option_id, resolution) for one question, given the members' tally.

    The order of the tests IS the policy: quorum first (a team that did not turn out has not voted,
    whatever the three people who did think), then a clear leader, then the tie policy.
    """
    if answered < target:
        return None, PollTeamResult.BELOW_QUORUM
    if not tally:
        return None, PollTeamResult.BELOW_QUORUM

    top = max(tally.values())
    leaders = [option_id for option_id, votes in tally.items() if votes == top]
    if len(leaders) == 1:
        return leaders[0], PollTeamResult.PLURALITY

    if poll.team_tie_policy == Poll.TIE_EARLIEST:
        # The option that REACHED the winning count first. Resolved by the earliest answer among
        # the tied options, which is the only ordering the data actually carries.
        earliest = (
            PollAnswer.objects.filter(
                question=question, response__poll=poll, response__team=team,
                response__status=PollResponse.SUBMITTED, option_id__in=leaders,
            ).order_by("created_at", "answer_id").first()
        )
        if earliest:
            return earliest.option_id, PollTeamResult.PLURALITY

    if poll.team_tie_policy == Poll.TIE_CAPTAIN:
        captain_pick = (
            PollAnswer.objects.filter(
                question=question, response__poll=poll, response__team=team,
                response__status=PollResponse.SUBMITTED,
                response__respondent_id__in=_captain_ids(team),
                option_id__in=leaders,
            ).first()
        )
        if captain_pick:
            return captain_pick.option_id, PollTeamResult.TIE_BROKEN_BY_CAPTAIN

    # Tied, and nothing broke it. Its own bucket, never a missing row.
    return None, PollTeamResult.NO_CONSENSUS


def set_captain_override(poll, team, question, option, captain):
    """The captain sets the team answer directly. Recorded as an override, with who and when.

    The members' tally is NOT cleared. Decision 6 requires the roster to keep seeing what they
    voted for beside the override, which is the entire difference between an override and a quiet
    substitution.
    """
    playing_size, full_size = roster_counts(team)
    rows = (
        PollAnswer.objects.filter(
            question=question, response__poll=poll, response__team=team,
            response__status=PollResponse.SUBMITTED, option__isnull=False,
        ).values("option_id").annotate(votes=Count("answer_id"))
    )
    tally = {str(row["option_id"]): row["votes"] for row in rows}
    answered = _answered_playing_members(poll, team, question)

    result, _ = PollTeamResult.objects.update_or_create(
        poll=poll, question=question, team=team,
        defaults={
            "winning_option": option,
            "tally": tally,
            "playing_roster_size": playing_size,
            "full_roster_size": full_size,
            "answered_count": answered,
            "quorum_met": answered >= quorum_target(poll, playing_size),
            "resolution": PollTeamResult.CAPTAIN_OVERRIDE,
            "set_by": captain,
        },
    )
    return result


def rollup_for(poll, team, questions=None):
    """What the roll-up panel renders: this team's standing on every question, right now.

    Returns a list of dicts rather than model rows, because the panel needs the quorum fraction and
    the resolution WORDED, and working that out in the template would put the arithmetic in two
    places.
    """
    if not team:
        return []
    questions = list(questions if questions is not None else poll.questions.all())
    playing_size, full_size = roster_counts(team)
    target = quorum_target(poll, playing_size)
    results = {
        row.question_id: row
        for row in PollTeamResult.objects.filter(poll=poll, team=team)
    }

    out = []
    for question in questions:
        row = results.get(question.question_id)
        out.append({
            "question_id": question.question_id,
            "winning_option_id": row.winning_option_id if row else None,
            "tally": (row.tally if row else {}),
            "answered_count": row.answered_count if row else 0,
            # The fraction, not a bare flag. A team that adds a member mid-poll raises its own
            # quorum, and the only way somebody understands that is by seeing both numbers.
            "playing_roster_size": playing_size,
            "full_roster_size": full_size,
            "quorum_target": target,
            "quorum_met": row.quorum_met if row else False,
            "resolution": row.resolution if row else PollTeamResult.BELOW_QUORUM,
            "set_by_username": (
                row.set_by.username if row and row.set_by_id and row.set_by else ""
            ),
        })
    return out


def freeze_poll(poll):
    """Freeze every team result on this poll against the roster AS IT STANDS NOW.

    Run once when the poll closes. After this the frozen rows are what results and exports read, so
    a roster change the following week cannot rewrite what a team was recorded as saying.
    Idempotent: a row that already carries `frozen_at` is left exactly as it was, so re-running the
    sweep (or running it after a manual close) can never re-freeze against a newer roster.
    """
    if poll.subject != Poll.TEAM:
        return 0
    from afc_team.models import Team

    team_ids = set(
        PollResponse.objects.filter(poll=poll, status=PollResponse.SUBMITTED, team__isnull=False)
        .values_list("team_id", flat=True)
    )
    now = timezone.now()
    frozen = 0
    for team in Team.objects.filter(team_id__in=team_ids):
        for question in poll.questions.all():
            existing = PollTeamResult.objects.filter(
                poll=poll, question=question, team=team
            ).first()
            if existing and existing.frozen_at:
                continue
            result = recompute_team_result(poll, team, question)
            result.frozen_at = now
            result.save(update_fields=["frozen_at"])
            frozen += 1
    return frozen
