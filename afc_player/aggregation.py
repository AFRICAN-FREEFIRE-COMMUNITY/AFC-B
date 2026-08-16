"""
afc_player/aggregation.py
─────────────────────────
Shared player-stats aggregation used by BOTH the admin player-profile endpoint
(afc_player.views.get_player_details, authenticated, keyed by player_id) and the
new PUBLIC player-profile endpoint (afc_player.views.get_public_player_stats,
no auth, keyed by username/IGN).

WHY a shared helper:
The admin endpoint already computes the canonical aggregate numbers (total_kills,
total_wins, total_mvps, kdr, avg_damage, win_rate, scrim/tournament splits).
The public Team Stats + Player Profile pages need the SAME numbers plus
a per-event and per-match breakdown. Rather than duplicate (and risk drift), the
heavy lifting lives here once and both views call it.

DATA SOURCES (all real tables - nothing is fabricated here):
  • TournamentPlayerMatchStats  → per-player per-match kills / damage  (the SQUAD player line)
  • SoloPlayerMatchStats         → per-player per-match kills / placement (the SOLO player line)
  • TournamentTeamMatchStats     → per-team   per-match placement / points (the team line
                                    the player's booyah / win is read from)
  • Match.mvp                    → MVP awards
  • Event (via match.leaderboard.event) → competition_type (tournament vs scrims),
                                    name, date, tier

BOTH ENTRY PATHS ARE READ (owner bug 2026-08-08). This module used to walk ONLY
TournamentPlayerMatchStats, i.e. only squad play. afc_auth.views.get_user_profile has always
added the solo table too, so the same human read one career on their OWN profile and a different
one on the PUBLIC player page and the admin player detail: 109 players disagreed with themselves
on total kills, and for 78 of them - people who have only ever entered solo events - the public
page reported 0 kills and 0 matches while their own profile showed real numbers. Both surfaces
now read solo + squad here, so there is one population and one answer. See §1 below for the one
number that deliberately keeps a squad-only denominator (avg_damage) and why.

If a player has no recorded stats every number is simply 0 / every list empty - 
that is the truthful empty state, not a stub.

NOTE on competition_type: an Event row's competition_type is "tournament" or
"scrims" (see Event.COMPETITION_TYPE_CHOICES). We split kills/wins on that value,
mirroring the admin endpoint's existing `if event_type == "scrims"` branch.
"""
from collections import OrderedDict

from afc_auth.models import User
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import (
    Match,
    RegisteredCompetitors,
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)
# The ONE rule for "did this player really play in this event" (a scored match line), shared
# with afc_auth.views.get_user_profile so the public player page and the owner's own profile
# cannot disagree, plus the separate roster-shaped rule the TEAM record needs. Both live in
# afc_tournament_and_scrims/participation.py, which explains why they are different questions.
from afc_tournament_and_scrims.participation import (
    counted_tournament_team_ids,
    played_event_counts,
)


# Map the Event's stored tournament_tier code to a human label for display.
# (Event.TOURNAMENT_TIER_CHOICES = tier_1/tier_2/tier_3.)
_EVENT_TIER_LABELS = {
    "tier_1": "Tier 1",
    "tier_2": "Tier 2",
    "tier_3": "Tier 3",
}


def _event_of(team_stats_row):
    """
    Safely walk TournamentTeamMatchStats → Match → Leaderboard → Event.

    Match.leaderboard is nullable, so the admin endpoint's direct
    `row.match.leaderboard.event` access can raise on matches that were entered
    without a leaderboard. This helper returns None instead of crashing, so the
    public surface degrades gracefully on partial data.
    """
    match = getattr(team_stats_row, "match", None)
    if match is None:
        return None
    leaderboard = getattr(match, "leaderboard", None)
    if leaderboard is None:
        return None
    return getattr(leaderboard, "event", None)


def compute_player_stats(player, *, include_breakdown=True):
    """
    Compute the full aggregate stat block for a single player (a User instance).

    Returns a dict with:
      • the same scalar aggregates the admin endpoint returns
        (total_kills, total_wins, total_mvps, kdr, avg_damage, win_rate,
         scrims_kills, tournaments_kills, scrims_wins, tournaments_wins,
         total_matches) plus the separately-named TEAM record
        (team_matches, team_wins, team_win_rate) plus the events-PLAYED pair
        (tournaments_played, scrims_played) shared with the owner's own profile

    TWO DIFFERENT WIN STATISTICS, deliberately kept apart (owner bug 2026-08-07):
      • total_wins / win_rate  -> the PLAYER'S OWN record. Only matches this player was on
        the sheet for; a win is that match's team line placing 1st. Numerator and denominator
        are the same rows, so win_rate is bounded 0-100%.
      • team_wins / team_win_rate -> the record of the TEAMS the player was rostered on, every
        match, played or not. Useful, but it is the team's rate and is labelled as such.
    They were previously fused into one number (team wins over the player's own match count),
    which reported 400% win rates for four real players on the public site.
      • when include_breakdown=True, two extra real lists:
         - per_event[]  → one row per Event the player competed in
         - recent_matches[] → the player's last 25 individual match lines

    `include_breakdown=False` is available for callers (e.g. a future list view)
    that only need the scalars and want to skip the per-row work.
    """
    # ── 1. Per-player match lines (the individual kill/damage record) ──
    # select_related the full Event path so each row read is one query, not N.
    player_stat_rows = (
        TournamentPlayerMatchStats.objects.filter(player=player)
        .select_related(
            "team_stats",
            "team_stats__match",
            # match -> group -> stage: so the per-match breakdown can be split by stage/group (owner
            # 2026-07-02). One query, not N, thanks to select_related.
            "team_stats__match__group",
            "team_stats__match__group__stage",
            "team_stats__match__leaderboard",
            "team_stats__match__leaderboard__event",
        )
    )

    # SOLO match lines, the OTHER half of the same career (owner bug 2026-08-08). A solo
    # competitor has no team and no roster, so their line hangs off their RegisteredCompetitors
    # row, which is what carries the event - the same shape participation.scored_solo_match_lines
    # reads. Without this half, a solo-only player's public page showed 0 kills and 0 matches
    # while their own profile showed the real numbers (see the module header).
    solo_stat_rows = (
        SoloPlayerMatchStats.objects.filter(competitor__user=player)
        .select_related("competitor", "competitor__event", "match")
    )

    total_kills = 0
    total_damage = 0
    # Counted as the two row sets are walked rather than with .count(), so the squad and solo
    # halves land in ONE denominator and every ratio below divides by the population it was
    # actually counted from.
    total_matches = 0

    # avg_damage keeps its own SQUAD-ONLY denominator. SoloPlayerMatchStats has no `damage`
    # column at all (kills / placement / points only), so folding solo matches into the divisor
    # while no solo row can add to the dividend would quietly drag every solo player's average
    # damage towards zero - a new wrong number in the act of fixing an old one. Damage is
    # therefore averaged over the matches damage was actually recorded for, and stays 0 for a
    # solo-only player because there is genuinely no damage data for them, not because of a
    # divide-by-zero guard.
    damage_matches = 0

    scrim_kills = 0
    tournament_kills = 0

    # ── the player's OWN win record (owner bug 2026-08-07) ────────────────────────────────────
    # Counted HERE, in the same loop as total_matches, because numerator and denominator MUST
    # come from the same population. See the win_rate note in §5: these used to be counted from
    # every rostered team's match rows while total_matches counted only the player's own rows,
    # which is what produced a 400% win rate on the live site.
    # A match is a win for the player when the team line of the match THEY PLAYED placed 1st -
    # s.team_stats is exactly that row, so no extra query is needed.
    total_wins = 0
    scrim_wins = 0
    tournament_wins = 0

    # per-event accumulator (kills / mvps / placement context), keyed by event_id.
    # OrderedDict keeps insertion order stable for a deterministic response.
    events_acc = OrderedDict()

    # per-match breakdown rows (individual player lines)
    match_breakdown = []

    for s in player_stat_rows:
        total_kills += s.kills
        total_damage += s.damage
        total_matches += 1
        damage_matches += 1

        team_stats = s.team_stats
        event = _event_of(team_stats)
        event_type = event.competition_type if event else None

        if event_type == "scrims":
            scrim_kills += s.kills
        elif event_type is not None:
            # any non-scrims competition_type counts as tournament (mirrors admin's else-branch)
            tournament_kills += s.kills

        # This match's team line placed 1st, and this player was on the sheet for it, so it is
        # THEIR win. Same row set as total_matches, so win_rate can never exceed 100%.
        if team_stats is not None and team_stats.placement == 1:
            total_wins += 1
            if event_type == "scrims":
                scrim_wins += 1
            elif event_type is not None:
                tournament_wins += 1

        if include_breakdown and event is not None:
            # ── per-event roll-up ──
            acc = events_acc.get(event.event_id)
            if acc is None:
                acc = {
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "competition_type": event.competition_type,
                    "event_date": event.start_date.isoformat() if event.start_date else None,
                    "tournament_tier": event.tournament_tier,
                    "tournament_tier_label": _EVENT_TIER_LABELS.get(event.tournament_tier),
                    "kills": 0,
                    "damage": 0,
                    "matches_played": 0,
                    "mvps": 0,
                    "best_placement": None,   # filled from the team line below
                    "total_points": 0,        # filled from the team line below
                }
                events_acc[event.event_id] = acc
            acc["kills"] += s.kills
            acc["damage"] += s.damage
            acc["matches_played"] += 1

            # ── per-match line ──
            match = getattr(team_stats, "match", None)
            _grp = getattr(match, "group", None) if match else None
            _stg = getattr(_grp, "stage", None) if _grp else None
            match_breakdown.append({
                "event_id": event.event_id,
                "event_name": event.event_name,
                "competition_type": event.competition_type,
                # Stage + group of THIS match so the profile splits a multi-stage event's per-match list
                # by its separate leaderboards instead of one flat list (owner 2026-07-02).
                "stage_name": getattr(_stg, "stage_name", None),
                "group_name": getattr(_grp, "group_name", None),
                "match_number": getattr(match, "match_number", None),
                "match_map": getattr(match, "match_map", None),
                "match_date": match.match_date.isoformat() if match and match.match_date else None,
                # team line context for this match (placement / team points)
                "placement": team_stats.placement,
                "team_points": team_stats.total_points,
                # the player's own line
                "kills": s.kills,
                "damage": s.damage,
                "assists": s.assists,
                # is this player the MVP of this match?
                "is_mvp": bool(match and match.mvp_id == player.user_id),
            })

    # ── 1b. The SOLO half of the same career (owner bug 2026-08-08) ─────────────────────────────
    # Identical accounting to the squad loop above, against the solo table: one row is one match
    # the player was on the sheet for, a win is that row placing 1st, and the event is taken from
    # competitor.event (a non-null FK) rather than match.leaderboard.event, for the same reason
    # participation.py takes it there - Match.leaderboard is nullable and silently loses rows.
    #
    # NOTE there is no `damage` here and none is invented: SoloPlayerMatchStats has no such
    # column, which is exactly why damage_matches is tracked separately above.
    for s in solo_stat_rows:
        event = s.competitor.event
        # Draft events are an organizer's unpublished sketch, never a played event. The squad loop
        # gets this filter for free (a draft event has no leaderboard, so _event_of returns None);
        # the solo path resolves its event directly, so it must say so.
        if event is None or event.is_draft:
            continue
        event_type = event.competition_type

        total_kills += s.kills
        total_matches += 1

        if event_type == "scrims":
            scrim_kills += s.kills
        else:
            tournament_kills += s.kills

        # A solo win is this player's own row placing 1st - numerator and denominator are the same
        # rows here by construction, since a solo competitor IS the whole competitor.
        if s.placement == 1:
            total_wins += 1
            if event_type == "scrims":
                scrim_wins += 1
            else:
                tournament_wins += 1

        if include_breakdown:
            # ── per-event roll-up ──
            # Folded into the SAME accumulator as squad play, so a player who entered one event
            # solo and another as a squad gets one combined history rather than two. Before this,
            # a solo-only player's Stats tab was completely empty while the Overview tab claimed
            # they had played tournaments.
            acc = events_acc.get(event.event_id)
            if acc is None:
                acc = {
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "competition_type": event.competition_type,
                    "event_date": event.start_date.isoformat() if event.start_date else None,
                    "tournament_tier": event.tournament_tier,
                    "tournament_tier_label": _EVENT_TIER_LABELS.get(event.tournament_tier),
                    "kills": 0,
                    "damage": 0,
                    "matches_played": 0,
                    "mvps": 0,
                    "best_placement": None,
                    "total_points": 0,
                }
                events_acc[event.event_id] = acc
            acc["kills"] += s.kills
            acc["matches_played"] += 1
            # In a solo event the competitor's own line IS the standings line, so placement and
            # points come straight off this row (in the squad path they come off the team line).
            acc["total_points"] += s.total_points
            if acc["best_placement"] is None or s.placement < acc["best_placement"]:
                acc["best_placement"] = s.placement

            # ── per-match line ──
            match = getattr(s, "match", None)
            match_breakdown.append({
                "event_id": event.event_id,
                "event_name": event.event_name,
                "competition_type": event.competition_type,
                # A solo event has no team/roster grouping to split by.
                "stage_name": None,
                "group_name": None,
                "match_number": getattr(match, "match_number", None),
                "match_map": getattr(match, "match_map", None),
                "match_date": match.match_date.isoformat() if match and match.match_date else None,
                "placement": s.placement,
                "team_points": s.total_points,
                "kills": s.kills,
                # No damage/assists columns on the solo table - reported as 0, the honest
                # "not recorded" value for this format, never a fabricated figure.
                "damage": 0,
                "assists": 0,
                "is_mvp": bool(match and match.mvp_id == player.user_id),
            })

    # ── 2. MVPs (Match.mvp points at the User) ──
    total_mvps = Match.objects.filter(mvp=player).count()

    # ── 3. The player's TEAM record (a DIFFERENT statistic, now named as one) ──────────────────
    # Every match played by every tournament-team this player was legitimately rostered on,
    # whether or not the player was on the sheet for that particular match.
    #
    # This is what the old `total_wins` was secretly measuring while being labelled as the
    # player's own. It is a genuinely useful number - "how did the teams I was part of do" - so it
    # is kept, but it is now reported under its own team_* names and rendered under its own
    # "Team Wins" / "Team Win Rate" labels, never mixed into the player's personal rate.
    #
    # Roster membership is resolved through participation.counted_tournament_team_ids so a
    # REJECTED or pending roster slot, or a slot on a disqualified/withdrawn team, does not lend
    # this player a record they were never part of (same rule the profile's tournaments-played
    # count uses - see afc_tournament_and_scrims/participation.py).
    team_ids = counted_tournament_team_ids(player)
    team_stat_rows = (
        TournamentTeamMatchStats.objects.filter(tournament_team_id__in=team_ids)
        .select_related(
            "match",
            "match__leaderboard",
            "match__leaderboard__event",
            "tournament_team__event",
        )
    )

    team_matches = 0
    team_wins = 0

    for t in team_stat_rows:
        event = _event_of(t)

        team_matches += 1
        if t.placement == 1:
            team_wins += 1

        # fold the team line into the per-event roll-up (best placement + team points)
        if include_breakdown and event is not None and event.event_id in events_acc:
            acc = events_acc[event.event_id]
            acc["total_points"] += t.total_points
            if acc["best_placement"] is None or t.placement < acc["best_placement"]:
                acc["best_placement"] = t.placement

    # ── 4. Fold MVP counts into the per-event roll-up ──
    if include_breakdown:
        mvp_event_rows = (
            Match.objects.filter(mvp=player)
            .select_related("leaderboard", "leaderboard__event")
        )
        for m in mvp_event_rows:
            lb = getattr(m, "leaderboard", None)
            ev = getattr(lb, "event", None) if lb else None
            if ev is not None and ev.event_id in events_acc:
                events_acc[ev.event_id]["mvps"] += 1

    # ── 5. Derived ratios (guard divide-by-zero exactly like the admin endpoint) ──
    # EVERY ratio below divides a number by the population it was counted from:
    #   kdr / win_rate  -> the player's own match rows, SOLO + SQUAD   (total_matches)
    #   avg_damage      -> only the rows that carry a damage column     (damage_matches)
    #   team_win_rate   -> the rostered teams' match rows              (team_matches)
    # Mixing the first and the last is the bug this block was rewritten to make impossible;
    # win_rate is bounded to 0-100% by construction because total_wins is a subset of
    # total_matches. Splitting avg_damage onto its own divisor is the same discipline applied to
    # the solo path, which records kills but no damage (owner bug 2026-08-08).
    kdr = total_kills / total_matches if total_matches > 0 else 0
    avg_damage = total_damage / damage_matches if damage_matches > 0 else 0
    win_rate = (total_wins / total_matches * 100) if total_matches > 0 else 0
    team_win_rate = (team_wins / team_matches * 100) if team_matches > 0 else 0

    # ── 6. Events PLAYED, split tournaments / scrims (owner ruling 2026-08-08) ──────────────────
    # The same two numbers the owner's own profile shows, from the same shared rule, so the public
    # player page and the profile can never report different careers for one person.
    #
    # Deliberately NOT derived by counting per_event above, even though per_event now covers both
    # entry paths too: the SQUAD half of per_event resolves its event through Match.leaderboard,
    # which is nullable, and 230 scored lines in the live database (one whole event) hang off
    # matches with no leaderboard. Those rows are invisible to per_event and would be silently
    # missing from the count. played_event_counts reads the non-null tournament_team.event
    # instead, so it sees them. See participation.py, which explains the choice at length.
    tournaments_played, scrims_played = played_event_counts(player)

    result = {
        "total_matches": total_matches,
        "total_kills": total_kills,
        "total_wins": total_wins,
        "total_mvps": total_mvps,
        "kdr": round(kdr, 2),
        "avg_damage": round(avg_damage, 2),
        "win_rate": round(win_rate, 2),
        "scrims_kills": scrim_kills,
        "tournaments_kills": tournament_kills,
        # Personal wins split by the event's competition_type. These used to be shadowed by an
        # identical scrim_booyah/tournament_booyah pair that was incremented in the same branch and
        # so could never differ; a booyah IS a match win in Free Fire, so the duplicate pair was
        # removed rather than kept as a second name for one number (owner bug 2026-08-07).
        "scrims_wins": scrim_wins,
        "tournaments_wins": tournament_wins,
        # ── events PLAYED (not events registered for) - see §6 above ──
        "tournaments_played": tournaments_played,
        "scrims_played": scrims_played,
        # ── the TEAM record, explicitly named so it can never be read as the player's own ──
        "team_matches": team_matches,
        "team_wins": team_wins,
        "team_win_rate": round(team_win_rate, 2),
    }

    if include_breakdown:
        # newest events first by date (None dates sort last)
        per_event = list(events_acc.values())
        per_event.sort(key=lambda e: (e["event_date"] is not None, e["event_date"]), reverse=True)
        result["per_event"] = per_event

        # newest 25 match lines first
        match_breakdown.sort(
            key=lambda m: (m["match_date"] is not None, m["match_date"]), reverse=True
        )
        result["recent_matches"] = match_breakdown[:25]

    return result


def compute_registered_events(player):
    """
    The events a player is CURRENTLY registered for (event_status upcoming/ongoing),
    across BOTH the ways a player can enter an event:
      • SOLO events  → afc_tournament_and_scrims.RegisteredCompetitors (keyed straight to
                       the User as the solo competitor)            → participant_type "solo"
      • SQUAD events → afc_tournament_and_scrims.TournamentTeamMember (the player's roster
                       slot) → TournamentTeam → Event              → participant_type "squad"

    This is deliberately NOT part of the stats block in compute_player_stats: a player's
    registration schedule is PUBLIC information, not sensitive performance data. Its caller
    (afc_player.views.get_public_player_stats) therefore merges it into the response payload
    OUTSIDE the stats_visible privacy gate, so every viewer - anonymous, other players,
    teammates, admins - gets it. This mirrors the team side, where
    afc_team.views.get_team_details returns `registered_events` outside its own stats gate.

    Filtering (matches the team query): drop draft events and cancelled registrations
    (RegisteredCompetitors status rejected/withdrawn/left/disqualified; TournamentTeamMember
    rejected, or whose TournamentTeam is disqualified/withdrawn/left) and keep only
    upcoming/ongoing events (completed events show in per_event[] instead).

    Returns a list of dicts, deduped by event_id (squad WINS when a player somehow appears
    both ways for one event - squad rows are written last so they overwrite), sorted by
    event start_date ascending (soonest first; null dates last):
      {event_id, event_slug, event_name, event_status, event_date, participant_type}

    Consumed by: get_public_player_stats -> payload["registered_events"] -> frontend
    PlayerClient.tsx "Registered Events" section on the public player profile.
    """
    # Deferred: afc_tournament_and_scrims.views reaches back into this app, so importing it at
    # module level is a cycle. Same pattern the rest of this codebase uses for cross-app helpers.
    from afc_tournament_and_scrims.views import effective_event_status

    # Keyed by event_id so a player registered in the same event both ways is listed once.
    events_by_id = OrderedDict()

    # ── SOLO registrations (write first; squad overwrites on the dedupe below) ──
    solo_qs = (
        RegisteredCompetitors.objects
        .filter(user=player)
        .exclude(status__in=["rejected", "withdrawn", "left", "disqualified"])
        # WIDENED, then filtered in Python below (owner backlog item 27). This used to filter
        # event_status in SQL, which reads the RAW stored word. A duplicated event carries a
        # stored "completed" until the next sweep re-stamps it, so a player who registered for
        # one simply did not appear to be registered for anything: the row was dropped by the
        # database before any code could ask what the status EFFECTIVELY is. The same event
        # showed "upcoming" on its own page at the same moment.
        .filter(event__is_draft=False)
        .exclude(event__event_status__in=["cancelled"])
        .select_related("event")
    )
    for rc in solo_qs:
        ev = rc.event
        status = effective_event_status(ev)
        if status not in ("upcoming", "ongoing"):
            continue
        events_by_id[ev.event_id] = {
            "event_id": ev.event_id,
            "event_slug": ev.slug,
            "event_name": ev.event_name,
            # The EFFECTIVE status, so the badge here cannot disagree with the event's own page.
            "event_status": status,
            "event_date": ev.start_date.isoformat() if ev.start_date else None,
            "participant_type": "solo",
        }

    # ── SQUAD registrations (the player on a tournament-team roster) ──
    squad_qs = (
        TournamentTeamMember.objects
        .filter(user=player)
        .exclude(status="rejected")
        .exclude(tournament_team__status__in=["disqualified", "withdrawn", "left"])
        # Same widening as the solo queryset above, for the same reason.
        .filter(tournament_team__event__is_draft=False)
        .exclude(tournament_team__event__event_status__in=["cancelled"])
        .select_related("tournament_team__event")
    )
    for ttm in squad_qs:
        ev = ttm.tournament_team.event
        status = effective_event_status(ev)
        if status not in ("upcoming", "ongoing"):
            continue
        events_by_id[ev.event_id] = {
            "event_id": ev.event_id,
            "event_slug": ev.slug,
            "event_name": ev.event_name,
            "event_status": status,
            "event_date": ev.start_date.isoformat() if ev.start_date else None,
            "participant_type": "squad",
        }

    rows = list(events_by_id.values())
    # Soonest first; events with no start_date sort last (None -> True sorts after False).
    rows.sort(key=lambda e: (e["event_date"] is None, e["event_date"] or ""))
    return rows


def basic_player_profile(player, request=None):
    """
    The public, NON-sensitive identity block for a player.

    Deliberately omits email and any other PII. Mirrors the field names the
    existing public /team/get-player-details/ endpoint already exposes
    (username, country, profile_picture, uid, team, roles, join_date) so the
    frontend can reuse its existing types.

    `request` (optional) is used only to build absolute media URLs.
    """
    from afc_auth.models import UserProfile

    profile = UserProfile.objects.filter(user=player).first()

    def _abs(media_field):
        if not media_field:
            return None
        url = media_field.url
        return request.build_absolute_uri(url) if request is not None else url

    # current team (if any) - a player may be on no team; handle gracefully
    membership = (
        TeamMembers.objects.select_related("team").filter(member=player).first()
    )
    team_block = None
    in_game_role = None
    management_role = None
    join_date = None
    if membership is not None:
        team = membership.team
        in_game_role = membership.in_game_role
        management_role = membership.management_role
        join_date = membership.join_date
        team_block = {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_tag": team.team_tag,
            "team_logo": _abs(getattr(team, "team_logo", None)),
        }

    return {
        "username": player.username,
        # Player flag = IP-derived country (owner 2026-06-29), profile country as fallback. See
        # afc_auth.views.set_ip_country / User.ip_country. Consumed by the public player profile.
        "country": (player.ip_country or player.country),
        "uid": player.uid,
        "discord_username": player.discord_username,
        "profile_picture": _abs(getattr(profile, "profile_pic", None)) if profile else None,
        "esports_picture": _abs(getattr(profile, "esports_pic", None)) if profile else None,
        "in_game_role": in_game_role,
        "management_role": management_role,
        "join_date": join_date.isoformat() if join_date else None,
        "team": team_block,
    }


def player_tier_history(player):
    """
    Per-season tier + rank history for a player, sourced from the real
    afc_rankings.PlayerQuarterlyScore table - ONLY for seasons whose tiers have
    been published (Season.tiers_published). Rank is shown only when the season's
    rankings are published (Season.rankings_published). This mirrors the public
    rankings read API's two independent publish gates exactly.

    Returns a list (possibly empty) of:
      {season_id, season_name, year, quarter, tier, tier_label, rank}

    If afc_rankings is unavailable for any reason, returns [] (the frontend then
    shows the truthful "no tier history" empty state).
    """
    try:
        from afc_rankings.models import PlayerQuarterlyScore
        from afc_rankings.serializers import TIER_LABELS
    except Exception:
        return []

    rows = (
        PlayerQuarterlyScore.objects.filter(player=player)
        .select_related("season")
        .order_by("season__year", "season__quarter")
    )

    history = []
    for r in rows:
        season = r.season
        # tier is gated behind tiers_published; rank behind rankings_published
        tier = r.tier_assigned if season.tiers_published else None
        rank = r.rank if season.rankings_published else None
        # skip seasons that expose nothing publicly yet
        if tier is None and rank is None:
            continue
        history.append({
            "season_id": season.season_id,
            "season_name": season.name,
            "year": season.year,
            "quarter": season.quarter,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier) if tier is not None else None,
            "rank": rank,
        })
    return history
