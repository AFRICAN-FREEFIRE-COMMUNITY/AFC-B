"""
afc_results_import.tests_settings - the decisions an admin makes ABOUT an imported event.

Three gaps are covered here, all of the same family: a switch that existed but could not actually
be operated, or a default that pointed the unsafe way.

  GAP 1  Event.imported_results_visible_on_profiles and .imported_results_count_in_profile_stats
         shipped ENFORCED (afc_team/views.py reads both) but UNWRITABLE. No endpoint and no UI set
         them, so an imported event was permanently invisible on every team profile and an admin
         could not change that without a Django shell.

  GAP 2  An imported event fed the RANKINGS ladder by default. EventCountingControl's own rule is
         "no row for an event => everything counts", and commit_import created no row, so an import
         reached the official ladder unless somebody remembered to switch it off. Tier had the same
         shape: aggregation applies tier as the WEIGHT on an event's results and auto_classify_event
         derives it from the prize pool, which for an imported event is whatever was typed rather
         than the real one.

  GAP 3  ResultsImport.team_scores_only was settable while nothing in the import wrote a single
         per-player row, so passing false changed NOTHING and the API promised an option that did
         not exist.

Run: python manage.py test afc_results_import.tests_settings
"""
import datetime
import secrets

from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_rankings.models import EventCountingControl
from afc_tournament_and_scrims.models import Event

TODAY = datetime.date.today()


def _event(slug, **kw):
    return Event.objects.create(
        slug=slug, competition_type="tournament", participant_type="squad",
        event_type="internal", max_teams_or_players=16, event_name=slug,
        event_mode="virtual", start_date=TODAY, end_date=TODAY,
        registration_open_date=TODAY, registration_end_date=TODAY,
        prizepool="0", event_rules="r", event_status="completed",
        registration_link="https://example.com/r", number_of_stages=1, **kw)


class ImportSettingsEndpointTests(TestCase):
    """GAP 1: the four switches can be read and written by the right people."""

    def setUp(self):
        self.admin = User.objects.create(
            username="set_admin", email="sa@example.com", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token=secrets.token_hex(32)).token
        self.event = _event("settings-test")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_get_reports_the_current_state(self):
        r = self.client.get("/results-import/settings/?slug=settings-test", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        body = r.json()
        self.assertFalse(body["visible_on_profiles"])
        self.assertFalse(body["count_in_profile_stats"])

    def test_get_reports_the_EFFECTIVE_rankings_answer_when_no_control_row_exists(self):
        """"No row => everything counts" is EventCountingControl's rule, so the payload must say
        True rather than leaking the absence of a row as False."""
        self.assertFalse(EventCountingControl.objects.filter(event=self.event).exists())

        body = self.client.get(
            "/results-import/settings/?slug=settings-test", **self._auth()).json()

        self.assertTrue(body["counts_toward_rankings"])

    def test_the_two_profile_switches_can_be_turned_on(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "settings-test", "visible_on_profiles": True,
             "count_in_profile_stats": True},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.event.refresh_from_db()
        self.assertTrue(self.event.imported_results_visible_on_profiles)
        self.assertTrue(self.event.imported_results_count_in_profile_stats)

    def test_a_field_the_caller_did_not_mention_is_left_alone(self):
        """Tri-state, not a bool default. Sending only one switch must not silently clear another."""
        self.event.imported_results_visible_on_profiles = True
        self.event.save(update_fields=["imported_results_visible_on_profiles"])

        self.client.post(
            "/results-import/settings/",
            {"slug": "settings-test", "count_in_profile_stats": True},
            content_type="application/json", **self._auth())

        self.event.refresh_from_db()
        self.assertTrue(self.event.imported_results_visible_on_profiles)

    def test_turning_rankings_off_writes_the_control_row(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "settings-test", "counts_toward_rankings": False},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        control = EventCountingControl.objects.get(event=self.event)
        self.assertFalse(control.counts_toward_rankings)

    def test_setting_a_tier_LOCKS_it(self):
        """A hand-picked tier must not be re-derived by the automatic classifier from a prize pool
        nobody imported, because tier is the weight applied to every competitor's points."""
        self.client.post(
            "/results-import/settings/",
            {"slug": "settings-test", "tournament_tier": "tier_1"},
            content_type="application/json", **self._auth())

        self.event.refresh_from_db()
        self.assertEqual(self.event.tournament_tier, "tier_1")
        self.assertTrue(self.event.tier_overridden)

    def test_a_bad_tier_is_refused(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "settings-test", "tournament_tier": "tier_9"},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 400)
        self.event.refresh_from_db()
        self.assertEqual(self.event.tournament_tier, "tier_3")

    def test_an_unknown_event_is_404_not_500(self):
        r = self.client.get("/results-import/settings/?slug=nope", **self._auth())
        self.assertEqual(r.status_code, 404)

    def test_slug_is_required(self):
        r = self.client.get("/results-import/settings/", **self._auth())
        self.assertEqual(r.status_code, 400)

    def test_signed_out_is_refused(self):
        r = self.client.get("/results-import/settings/?slug=settings-test")
        self.assertEqual(r.status_code, 401)


class RankingsSwitchIsAdminOnlyTests(TestCase):
    """The rankings half needs an AFC event admin: it moves points on a PUBLIC ladder for teams
    with nothing to do with this event. The profile half follows the ordinary import gate."""

    def setUp(self):
        self.creator = User.objects.create(
            username="ev_creator", email="ec@example.com", role="player")
        self.token = SessionToken.objects.create(
            user=self.creator, token=secrets.token_hex(32)).token
        # The event's own creator passes the import gate but is NOT an AFC event admin.
        self.event = _event("creator-owned", creator=self.creator)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_the_creator_may_set_the_profile_switches(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "creator-owned", "visible_on_profiles": True},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.event.refresh_from_db()
        self.assertTrue(self.event.imported_results_visible_on_profiles)

    def test_the_creator_may_NOT_change_what_reaches_the_rankings(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "creator-owned", "counts_toward_rankings": True},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 403, r.content[:300])
        self.assertFalse(EventCountingControl.objects.filter(event=self.event).exists())

    def test_the_creator_may_NOT_change_the_tier(self):
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "creator-owned", "tournament_tier": "tier_1"},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 403, r.content[:300])
        self.event.refresh_from_db()
        self.assertEqual(self.event.tournament_tier, "tier_3")

    def test_a_refused_rankings_change_does_not_apply_the_profile_half_either(self):
        """One request, one outcome. A partly-applied update is the worst answer, because the admin
        is told it failed while half of it landed."""
        r = self.client.post(
            "/results-import/settings/",
            {"slug": "creator-owned", "visible_on_profiles": True,
             "counts_toward_rankings": True},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 403)
        self.event.refresh_from_db()
        self.assertFalse(self.event.imported_results_visible_on_profiles)
