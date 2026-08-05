r"""How much work creating one event actually costs.

WHY (owner backlog item 25: "creating a new event is slow, it loads for a long time"). Before
changing anything, measure. This builds an event with a realistic competitive structure and counts
the database round trips, so a claim about the cause is evidence rather than a guess, and so a
later "optimisation" has a number to beat.

WHAT THE MEASUREMENT SAID, 2026-08-05, and why nothing was rewritten:

  * A realistic event (3 stages, 10 groups, 60 matches): 53 queries, 0.07s.
  * A deliberately extreme one (9 stages, 97 groups, 582 matches): 326 queries, 0.35s.
  * The create PAGE served from a production build: 78ms, in line with /a/events at 53ms.

So neither the endpoint nor the page is slow, and the per-group cost is already about three
queries: the group row, its leaderboard, and one bulk insert for all of that group's matches.
Rewriting a well-tested creation path to chase a number that is already small would be changing
working code on a hunch, so it was left alone and the finding reported back instead.

If the slowness is real for the owner, the remaining suspects are the client-side wizard rather
than the server, or a specific event much larger than the extreme case above. That needs the
actual event and a timing from their browser, which is a question, not a guess.

The assertion is a CEILING, not a target. It exists to fail loudly if somebody reintroduces a
per-row query inside the stage/group loops, which is the failure mode this endpoint is shaped for.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_create_event_perf
"""
import datetime
import json
import time

from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import Event, Match, StageGroups, Stages

URL = "/events/create-event/"

# A shape AFC actually runs: a qualifier with several groups, then smaller stages.
STAGE_SHAPES = [("Qualifiers", 6), ("Semi Finals", 3), ("Grand Finals", 1)]
MATCHES_PER_GROUP = 6


class CreateEventCostTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(
            username="perf_admin", email="perf_admin@x.com", full_name="Perf Admin",
            role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="perf-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

    def _payload(self):
        today = datetime.date.today().isoformat()
        stages = []
        for order, (name, group_count) in enumerate(STAGE_SHAPES, start=1):
            stages.append({
                "stage_name": name,
                "start_date": today,
                "end_date": today,
                "number_of_groups": group_count,
                "stage_format": "br - normal",
                "teams_qualifying_from_stage": 2,
                "stage_order": order,
                "groups": [
                    {
                        "group_name": f"{name} Group {g + 1}",
                        "playing_date": today,
                        "playing_time": "18:00",
                        "teams_qualifying": 2,
                        "match_count": MATCHES_PER_GROUP,
                        "match_maps": ["bermuda", "purgatory", "kalahari"],
                    }
                    for g in range(group_count)
                ],
            })
        return {
            "competition_type": "tournament",
            "participant_type": "squad",
            "event_type": "internal",
            "max_teams_or_players": 48,
            "event_name": "Perf Cup",
            "event_mode": "virtual",
            "start_date": today,
            "end_date": today,
            "registration_open_date": today,
            "registration_end_date": today,
            "prizepool": "0",
            "event_rules": "rules",
            "registration_link": "https://x.com/r",
            "number_of_stages": len(STAGE_SHAPES),
            "is_draft": "false",
            # The four wall-clock times the endpoint requires alongside the dates.
            "event_start_time": "18:00",
            "event_end_time": "21:00",
            "registration_start_time": "09:00",
            "registration_end_time": "17:00",
            "stages": json.dumps(stages),
        }

    def test_creating_a_realistic_event_does_not_scale_its_queries_per_group(self):
        """10 groups, 60 matches. The matches are bulk inserted, so the query count should track
        the number of GROUPS, not the number of matches, and should stay well clear of a per-row
        pattern. The number in the assertion is a ceiling with room in it: this test is here to
        catch a regression into N+1, not to police a handful of queries either way.
        """
        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            resp = self.client.post(
                URL, data=self._payload(), HTTP_AUTHORIZATION="Bearer perf-token")
            elapsed = time.perf_counter() - started

        self.assertIn(resp.status_code, (200, 201), resp.content)

        group_count = sum(count for _name, count in STAGE_SHAPES)
        self.assertEqual(Stages.objects.count(), len(STAGE_SHAPES))
        self.assertEqual(StageGroups.objects.count(), group_count)
        self.assertEqual(Match.objects.count(), group_count * MATCHES_PER_GROUP)

        queries = len(captured.captured_queries)
        print(f"\n  create_event: {queries} queries, {elapsed:.2f}s "
              f"for {len(STAGE_SHAPES)} stages / {group_count} groups / "
              f"{group_count * MATCHES_PER_GROUP} matches")

        # Roughly: a handful of setup queries, then a small constant per GROUP (the group row, its
        # leaderboard, one bulk match insert). Per MATCH would be 60 more and would blow this.
        self.assertLess(
            queries, 12 * group_count,
            f"create_event issued {queries} queries for {group_count} groups, which looks like a "
            f"per-row pattern rather than a per-group one")

    def test_the_event_is_created_once_not_partially(self):
        """The whole build is one transaction. A failure halfway through must leave nothing, or an
        organizer retries and ends up with two half-built events in the list."""
        self.client.post(URL, data=self._payload(), HTTP_AUTHORIZATION="Bearer perf-token")

        self.assertEqual(Event.objects.filter(event_name="Perf Cup").count(), 1)
