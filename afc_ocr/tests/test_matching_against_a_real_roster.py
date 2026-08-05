r"""OCR name matching, measured against a roster shaped like a real one.

WHY THIS EXISTS (owner backlog item 36: "OCR is not matching players to teams, and not matching
teams to what exists on the platform").

The complaint was true when it was made. A stored review session from 2026-07-08 has 43 of its 44
rows bound to nobody, every row carrying an EMPTY candidate list, so a reviewer was given a screen
full of unmatched names and nothing to pick from. One row was worse than unmatched: the read
"NJ Walcott" was bound to the account "UND MITSUKE7" at confidence 0.0.

Two commits on 2026-08-03 fixed it. Re-running the CURRENT matcher over those same 44 reads and
that event's real 66-player roster now binds 35 of them and offers five candidates on every single
row, including the ones it declines to bind. The nine it still refuses share only a clan tag with
their nearest candidate ("LMG KOLA" against "LMG SALMAN", "ZN.Levix02" against "ZN.MALXO9"), which
is a refusal anybody would want: a wrong bind silently rewrites a tournament standing, while an
unmatched row costs the organizer one click.

What was missing was a test. The fixes went in without one, so nothing stops the same regression
arriving again, and "it matches now" was a claim rather than a measurement. This file is that
measurement, using the shapes that actually broke it: clan-tag prefixes, doubled spaces, casing
that disagrees with the roster, and near-miss teammates who must NOT be bound to each other.

Run: .venv\Scripts\python.exe manage.py test afc_ocr.tests.test_matching_against_a_real_roster
"""
import datetime

from django.test import TestCase

from afc_auth.models import User
from afc_ocr.services.matching import get_registered_players, match_name
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMember,
)

# Rosters in the shapes that broke it, taken from the real event behind the stored session.
# The doubled space in "NJ  ANJOznX7" and the lower-case "Nj solozin" are verbatim: both are how
# the account is actually stored, and both differ from how a screenshot reads them.
ROSTERS = {
    "UNDERGROUND": ["UND MITSUKE7", "UND SLYFER", "UND ZEUS", "UND KAYZ"],
    "NEM JOGOU": ["NJ Walcott", "Nj solozin", "NJ  ANJOznX7", "NJ Salgado7"],
    "ZENITH": ["ZN.MALXO9", "ZN.ATOMIC", "ZN.KAIRO", "ZN.DREW"],
}

# What the screenshot reads. Each pairs with the account it must resolve to, or None when the
# player is not in this event at all and the matcher must refuse rather than guess.
READS = [
    ("UND MITSUKE7", "UND MITSUKE7"),   # identical to the roster entry
    ("UND SLYFER", "UND SLYFER"),       # identical
    ("NJ Solozin", "Nj solozin"),       # casing differs from the stored account
    ("NJ ANJOznX7", "NJ  ANJOznX7"),    # the roster has a doubled space, the read does not
    ("NJ Walcott", "NJ Walcott"),       # the row that used to bind to UND MITSUKE7
    ("LMG KOLA", None),                 # shares only a clan tag with anybody here
    ("ZN.Levix02", None),               # ditto: ZN.MALXO9 is a different person
]


class OcrMatchesARealRosterTests(TestCase):
    def setUp(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="ocr_match_admin", email="ocr_match_admin@x.com",
            full_name="OCR Match Admin", role="admin", password="x")

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="OCR Match Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, kill_point=1.0, leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda")

        self.by_username = {}
        for team_name, usernames in ROSTERS.items():
            team = Team.objects.create(
                team_name=team_name, team_tag=team_name[:3], join_settings="open",
                team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(
                event=self.event, team=team, registered_by=self.admin, status="active")
            for username in usernames:
                user = User.objects.create(
                    username=username, email=f"{abs(hash(username))}@x.com",
                    full_name=username, role="player", password="x")
                TournamentTeamMember.objects.create(
                    tournament_team=tt, user=user, event=self.event, status="active")
                self.by_username[username] = (user, tt)

        self.registered = get_registered_players(self.match, self.event, "team")

    def test_the_roster_pool_is_actually_built(self):
        """If this is empty, every assertion below passes for the wrong reason. The pool is built
        by filtering on team and member STATUS, so a status value nobody expected silently empties
        it, which looks exactly like a broken matcher."""
        self.assertEqual(len(self.registered), sum(len(v) for v in ROSTERS.values()))

    def test_each_read_resolves_to_the_right_account_or_to_nobody(self):
        for read, expected in READS:
            with self.subTest(read=read):
                row = match_name(read, self.registered)
                if expected is None:
                    self.assertIsNone(
                        row["matched_user_id"],
                        f"{read!r} was bound to {row.get('matched_username')!r}; it belongs to "
                        f"nobody in this event and a wrong bind rewrites a standing")
                else:
                    self.assertEqual(
                        row["matched_user_id"], self.by_username[expected][0].user_id,
                        f"{read!r} should be {expected!r}, got {row.get('matched_username')!r}")

    def test_a_matched_player_carries_the_team_they_play_for(self):
        """The other half of the complaint. Binding the player but not their team leaves the commit
        with nothing to credit the kills to."""
        row = match_name("NJ Solozin", self.registered)

        self.assertEqual(
            row["matched_team_id"], self.by_username["Nj solozin"][1].tournament_team_id)

    def test_every_row_offers_candidates_even_when_it_refuses_to_bind(self):
        """THE ACTUAL 2026-07 FAILURE. Rows came back unmatched AND with an empty candidate list,
        so the reviewer had a screen of names and no way to resolve them except by hand. A refusal
        is fine; a refusal with nothing to pick from is the bug."""
        for read, _expected in READS:
            with self.subTest(read=read):
                row = match_name(read, self.registered)
                self.assertTrue(
                    row["top_candidates"],
                    f"{read!r} came back with no candidates, so a reviewer has nothing to pick")

    def test_a_refusal_says_why(self):
        """The review table prints this next to the picker. An empty reason renders as a blank
        explanation, which is how the July session looked to whoever opened it."""
        row = match_name("LMG KOLA", self.registered)

        self.assertIsNone(row["matched_user_id"])
        self.assertTrue(row["unmatched_reason"], "a refusal with no reason explains nothing")

    def test_two_teammates_behind_the_same_clan_tag_are_not_confused(self):
        """Every account in a squad shares a tag, so a matcher leaning on the tag binds the wrong
        team-mate. ZN.Levix02 is not ZN.MALXO9 however similar the prefix looks."""
        row = match_name("ZN.Levix02", self.registered)

        self.assertIsNone(row["matched_user_id"])
        candidate_names = [c.get("username") for c in row["top_candidates"]]
        self.assertIn(
            "ZN.MALXO9", candidate_names,
            "the near miss should still be OFFERED, it just must not be asserted")
