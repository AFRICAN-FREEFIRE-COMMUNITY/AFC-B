# ─────────────────────────────────────────────────────────────────────────────────────────────────
# backfill_player_roles - fill in the role-history columns for data that already exists
# (owner 2026-08-04: "role history is not stored, so a player who switched roles appears under their
# current one. fix the above so it records properly using data and is stored.")
#
# WHAT THE NEW COLUMNS ARE
#   TournamentTeamMember.in_game_role        the role FROZEN when a player is put on an event roster
#   TournamentPlayerMatchStats.role_at_match the role stamped when a match result is recorded
#   PlayerMonthly/QuarterlyScore.role        the period's primary role, derived from those stamps
#   PlayerMonthly/QuarterlyScore.role_breakdown  the per-role matches/kills split behind it
# Everything written from now on is stamped by the live code paths. This command exists only for the
# rows that were written before those paths existed.
#
# ══ WHAT THIS BACKFILL CAN AND CANNOT HONESTLY CLAIM - read this before trusting its output ══
#
# It CANNOT reconstruct a past role. Nothing in this database ever recorded one: afc_team.TeamMembers
# .in_game_role is a single mutable column with no history, no audit row and no updated_at, so there
# is no evidence anywhere of what a player was in July. Stamping today's role onto July's rows would
# LOOK like a fix and would be the exact bug the owner reported, dressed up as data. So this command
# refuses to do it, and a period it cannot speak for is left honestly EMPTY.
#
# It CAN do three things, each defensible on its own terms:
#
#   STEP 1  Freeze the current club role onto the rosters of events that have RECORDED NO RESULTS.
#           Defensible for exactly one reason, and it is worth stating precisely: an event with no
#           result rows has awarded nothing to anybody, so there is no past performance for the role
#           to mis-describe. Copying the club role now writes exactly the value registration would
#           have written had the column existed, and from that event's first match onward the value
#           is frozen like any other. Any event that HAS results is SKIPPED and reported, never
#           stamped: whatever its players earned there, they earned under a role nobody wrote down,
#           and today's club role is not evidence of what it was.
#           "No results recorded" is the test rather than "not started", because it is the condition
#           the argument above actually rests on, and it also catches an event whose dates have
#           passed but which never ran. KNOWN LIMIT: an event whose results were CLEARED by an admin
#           looks unplayed to this test, so re-entering them later would attach today's role to play
#           from back then. Rare, and preferable to leaving every genuinely unplayed event unstamped.
#
#   STEP 2  Fill role_at_match on match rows that have none, from the FROZEN roster role of their own
#           event. Defensible because the source is the frozen per-event value, not the live club
#           one: either it was written at registration (so it predates the match) or step 1 wrote it
#           for an event that had not started (so it also predates the match). Rows that already
#           carry a stamp are never overwritten, and an event whose roster step 1 correctly left
#           empty contributes nothing here.
#
#   STEP 3  Rebuild the derived period columns (role, role_breakdown) on every player score row from
#           whatever stamps exist. Purely derived, so it is safe to re-run at any time, and it is the
#           step that makes the ladders reflect steps 1 and 2. A period with no stamped match gets
#           role=NULL, role_breakdown=NULL, and the rankings page then SAYS the period has no role
#           data instead of showing four empty role tabs as if nobody played those roles.
#
# NET EFFECT ON HISTORY: periods played before this shipped will mostly report NO role data, and
# that is the correct and intended outcome. The role tables become trustworthy going forward.
#
# IDEMPOTENT. Safe to re-run; each step only writes what is still missing (step 3 recomputes, which
# is stable input-for-input). Reports by default, writes only with --apply.
#
# RUN (prod, after deploy):  python manage.py backfill_player_roles --apply
#
# CONNECTS TO: afc_tournament_and_scrims.roster_roles (the same frozen-role resolution the live write
# paths use), afc_rankings.aggregation._collect_player + primary_role (the same derivation recalc
# uses, so a rebuilt row is byte-identical to a recalculated one), and afc_rankings.player_roles,
# which serves the public role ladders off the columns this fills.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Q

from afc_tournament_and_scrims.models import (
    Event, SoloPlayerMatchStats, TournamentTeamMatchStats, TournamentPlayerMatchStats,
    TournamentTeamMember,
)
from afc_team.models import TeamMembers

from afc_rankings import aggregation
from afc_rankings.models import PlayerMonthlyScore, PlayerQuarterlyScore


def _event_has_results(event_id):
    """True when ANY match result has been recorded for this event, team or solo.

    The defensibility test for step 1. A match belongs to its event through group -> stage -> event
    or through the leaderboard, the two routes afc_rankings.aggregation._event_of_match walks, so
    both are checked here. Solo events are included because a solo competitor can be a rostered
    player too, and an event that awarded solo points is just as "played" as a team one.
    """
    in_event = (Q(match__group__stage__event_id=event_id)
                | Q(match__leaderboard__event_id=event_id))
    return (TournamentTeamMatchStats.objects.filter(in_event).exists()
            or SoloPlayerMatchStats.objects.filter(in_event).exists())


class Command(BaseCommand):
    help = ("Fill the stored in-game role columns for existing rows. Only stamps what can be "
            "defended: upcoming-event rosters, match rows whose event roster already carries a "
            "frozen role, and the derived period columns. Finished periods are left empty.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the values. Without this the command only reports (dry run).")

    # ── STEP 1 ──────────────────────────────────────────────────────────────────────────────────
    def _freeze_unplayed_rosters(self, apply):
        """Copy the live club role onto the rosters of events that have recorded NO results.

        An event with no result rows has awarded nothing, so freezing the current club role onto it
        cannot mis-describe anything anyone earned. An event that HAS results is skipped: its play
        happened under a role this database never recorded, and today's role is not evidence of it.
        """
        # Only events that actually have unstamped roster rows are worth looking at.
        event_ids = set(
            TournamentTeamMember.objects
            .filter(Q(in_game_role=None) | Q(in_game_role=""))
            .values_list("tournament_team__event_id", flat=True)
        )
        event_ids.discard(None)
        events = Event.objects.filter(event_id__in=event_ids)

        stamped = stamped_events = skipped_events = skipped_rows = 0
        for event in events:
            rows = list(
                TournamentTeamMember.objects
                .filter(tournament_team__event_id=event.event_id)
                .filter(Q(in_game_role=None) | Q(in_game_role=""))
                .select_related("tournament_team")   # row.tournament_team.team_id below, no N+1
            )
            if _event_has_results(event.event_id):
                skipped_events += 1
                skipped_rows += len(rows)
                continue

            # One query per event for the club roles of everyone on it. The club role is read
            # per (team, member) because in_game_role belongs to the club membership, not the user.
            member_ids = [r.user_id for r in rows]
            team_ids = set(
                TournamentTeamMember.objects
                .filter(tournament_team__event_id=event.event_id)
                .values_list("tournament_team__team_id", flat=True)
            )
            club_roles = {
                (team_id, member_id): role
                for team_id, member_id, role in TeamMembers.objects
                .filter(team_id__in=team_ids, member_id__in=member_ids)
                .values_list("team_id", "member_id", "in_game_role")
            }

            writes = []
            for row in rows:
                team_id = row.tournament_team.team_id
                role = club_roles.get((team_id, row.user_id))
                if not role:
                    continue          # staff, or a club row with no role: honestly left empty
                row.in_game_role = role
                writes.append(row)
            if writes and apply:
                TournamentTeamMember.objects.bulk_update(writes, ["in_game_role"], batch_size=500)
            stamped += len(writes)
            stamped_events += 1

        self.stdout.write(
            f"STEP 1  rosters frozen on events with no recorded results: {stamped} row(s) across "
            f"{stamped_events} event(s)")
        self.stdout.write(
            f"        skipped (event already has results, left empty ON PURPOSE): "
            f"{skipped_rows} row(s) across {skipped_events} event(s)")
        return stamped

    # ── STEP 2 ──────────────────────────────────────────────────────────────────────────────────
    def _stamp_match_rows(self, apply):
        """Fill role_at_match from the event's FROZEN roster, never from the live club roster.

        Rows that already carry a stamp are left alone: they were written by a live path from the
        same frozen source and re-deriving them could only introduce drift.
        """
        # Events that have at least one frozen roster role to give.
        event_ids = sorted(set(
            TournamentTeamMember.objects
            .exclude(in_game_role=None).exclude(in_game_role="")
            .values_list("tournament_team__event_id", flat=True)
        ) - {None})

        filled = 0
        for event_id in event_ids:
            roles = {
                user_id: role
                for user_id, role in TournamentTeamMember.objects
                .filter(tournament_team__event_id=event_id)
                .exclude(in_game_role=None).exclude(in_game_role="")
                .values_list("user_id", "in_game_role")
            }
            if not roles:
                continue
            # A match reaches its event either through group -> stage -> event or through the
            # leaderboard, the same two routes afc_rankings.aggregation._event_of_match walks.
            rows = list(
                TournamentPlayerMatchStats.objects
                .filter(Q(team_stats__match__group__stage__event_id=event_id)
                        | Q(team_stats__match__leaderboard__event_id=event_id))
                .filter(Q(role_at_match=None) | Q(role_at_match=""))
                .filter(player_id__in=roles.keys())
            )
            for row in rows:
                row.role_at_match = roles[row.player_id]
            if rows and apply:
                TournamentPlayerMatchStats.objects.bulk_update(
                    rows, ["role_at_match"], batch_size=500)
            filled += len(rows)

        self.stdout.write(f"STEP 2  match rows stamped from their event's frozen roster: {filled}")
        return filled

    # ── STEP 3 ──────────────────────────────────────────────────────────────────────────────────
    def _rebuild_period_roles(self, apply):
        """Recompute role + role_breakdown on every player score row from the stamps that exist.

        Uses aggregation._collect_player, the SAME collection recalc runs, so a rebuilt row is
        identical to what a full recalc would write, and the exclusions / counting controls are
        honoured without this command re-implementing them. Nothing else on the row is touched, so
        no score can move.

        Ghost rows are skipped: a ghost is an unclaimed historical name with no roster and therefore
        no role, and its row is already NULL.
        """
        monthly_written = monthly_cleared = 0
        for score in PlayerMonthlyScore.objects.exclude(player=None).select_related("player"):
            start, end = aggregation.month_bounds(score.month)
            tables = aggregation.resolve_tables(month=score.month)
            breakdown = aggregation._collect_player(score.player, start, end, tables)[5]
            role = aggregation.primary_role(breakdown)
            if score.role == role and (score.role_breakdown or None) == (breakdown or None):
                continue
            score.role = role
            score.role_breakdown = breakdown or None
            if apply:
                score.save(update_fields=["role", "role_breakdown"])
            if role:
                monthly_written += 1
            else:
                monthly_cleared += 1

        quarterly_written = quarterly_cleared = 0
        for score in (PlayerQuarterlyScore.objects.exclude(player=None)
                      .select_related("player", "season")):
            season = score.season
            start = season.start_date
            end = season.end_date + datetime.timedelta(days=1)
            tables = aggregation.resolve_tables(season=season)
            breakdown = aggregation._collect_player(score.player, start, end, tables)[5]
            role = aggregation.primary_role(breakdown)
            if score.role == role and (score.role_breakdown or None) == (breakdown or None):
                continue
            score.role = role
            score.role_breakdown = breakdown or None
            if apply:
                score.save(update_fields=["role", "role_breakdown"])
            if role:
                quarterly_written += 1
            else:
                quarterly_cleared += 1

        self.stdout.write(
            f"STEP 3  monthly rows given a role: {monthly_written}, "
            f"left/blanked with no role data: {monthly_cleared}")
        self.stdout.write(
            f"        quarterly rows given a role: {quarterly_written}, "
            f"left/blanked with no role data: {quarterly_cleared}")
        return monthly_written + quarterly_written

    def handle(self, *args, **opts):
        apply = opts["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN: reporting what would be written. Re-run with --apply to write."))

        self._freeze_unplayed_rosters(apply)
        self._stamp_match_rows(apply)
        self._rebuild_period_roles(apply)

        # Say plainly how much of the ladder now has a role, so nobody reads an empty role tab as
        # "nobody played this role" when it really means "this period predates the stamping".
        total = PlayerMonthlyScore.objects.count()
        with_role = PlayerMonthlyScore.objects.exclude(role=None).count()
        self.stdout.write(
            f"COVERAGE  monthly score rows with a stored role: {with_role} of {total}")
        self.stdout.write(
            "          rows without one are periods this database holds no evidence for. They are "
            "left empty on purpose; the rankings page reports that rather than showing empty role "
            "tabs as fact.")

        if apply:
            self.stdout.write(self.style.SUCCESS("Done."))
        else:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
