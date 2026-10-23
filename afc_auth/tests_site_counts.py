"""The landing page's numbers are real counts (Search Console work, 2026-10-16).

The public landing page (frontend app/(root)/page.tsx) printed hard-coded "1,200+ tournaments
held" and "$500K+ prize pool distributed" while production held 175 events, and downloaded every
team (get-all-teams, 426 KB) only to count them. GET /auth/get-total-number-of-users/ now answers
the counts it needs; these tests hold what each one means.

Run: .venv\\Scripts\\python.exe manage.py test afc_auth.tests_site_counts
"""
from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event

URL = "/auth/get-total-number-of-users/"


class SiteCountsTests(TestCase):
    def setUp(self):
        self.a = User.objects.create(username="countA", email="a@x.com", full_name="A", role="player",
                                     password="x", status="active")
        User.objects.create(username="countB", email="b@x.com", full_name="B", role="player",
                            password="x", status="pending")
        Team.objects.create(team_name="Count Team", team_tag="CT", join_settings="open",
                            team_creator=self.a, team_owner=self.a)

    def _event(self, name, status, draft=False):
        today = timezone.localdate()
        return Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name=name, event_mode="virtual", start_date=today,
            end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="r", event_status=status, is_draft=draft,
            registration_link="https://x.com/r", number_of_stages=1, creator=self.a)

    def test_counts_are_real_and_public(self):
        self._event("Done Cup", "completed")
        self._event("Draft Done Cup", "completed", draft=True)
        self._event("Live Cup", "ongoing")

        res = self.client.get(URL)

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total_users"], 2)
        self.assertEqual(body["verified_users"], 1)
        self.assertEqual(body["total_teams"], 1)
        # Only a published, finished event has been HELD.
        self.assertEqual(body["events_held"], 1)

    def test_a_real_zero_is_reported_as_zero(self):
        body = self.client.get(URL).json()
        self.assertEqual(body["events_held"], 0)
