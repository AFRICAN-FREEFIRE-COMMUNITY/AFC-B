r"""Public sponsors and the event description: the parts an ordinary visitor sees.

WHY (owner backlog item 26). Two separate asks that landed together because they share one page:

  * "an event needs somewhere to say what it IS" - Event.event_description, deliberately NOT
    event_rules, which answers a different question and is capped at 200 characters.
  * "sponsor logos and links visible to EVERYONE" - EventPublicSponsor, deliberately NOT
    afc_sponsors.EventSponsorship, whose entire purpose is to GATE a registration.

The property that matters most, and the one easiest to break: a LOGGED-OUT visitor must see both.
That is the whole point of the feature, and there are two separate detail builders on this
endpoint pair (get_event_details for a session, get_event_details_not_logged_in for everybody
else). A change that only remembers one of them looks correct to whoever is testing while signed
in, and shows an empty page to the public. Several tests below exist only to hold that line.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_public_sponsors
"""
import datetime
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import Event, EventPublicSponsor

ADD = "/events/public-sponsors/add/"


def _png_bytes():
    """A real 1x1 PNG. The upload path sniffs the image rather than trusting the extension, so a
    file of arbitrary bytes named .png is rejected and would fail these tests for the wrong
    reason."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
        b"\x00\x00IEND\xaeB`\x82"
    )


class PublicSponsorTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="ps_admin", email="ps_admin@x.com", full_name="PS Admin",
            role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="ps-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.outsider = User.objects.create(
            username="ps_player", email="ps_player@x.com", full_name="PS Player",
            role="player", password="x")
        SessionToken.objects.create(
            user=self.outsider, token="ps-player-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=24, event_name="Sponsor Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r",
            event_status="upcoming", registration_link="https://x.com/r", number_of_stages=1,
            creator=self.admin, is_public=True,
            event_description="A monthly open bracket for African squads.")

    def _public_detail(self):
        """The logged-out event page, unwrapped.

        A POST carrying the slug in the BODY - there is no path parameter - and the event sits
        under "event_details", NOT "event". Both shapes are easy to guess wrong, and guessing wrong
        produces a passing 200 with a None field, which reads like a broken feature rather than a
        broken test. Returns (response, event_dict).
        """
        resp = self.client.post(
            "/events/get-event-details-not-logged-in/",
            data={"slug": self.event.slug}, content_type="application/json")
        body = resp.json() if resp.status_code == 200 else {}
        return resp, body.get("event_details", body)

    def _add(self, token="ps-admin-token", **over):
        payload = {"event_id": self.event.event_id, "name": "Acme Energy"}
        payload.update(over)
        return self.client.post(ADD, data=payload, HTTP_AUTHORIZATION=f"Bearer {token}")

    # ── writing ──
    def test_an_admin_can_add_a_public_sponsor(self):
        resp = self._add(link="https://acme.example.com")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(EventPublicSponsor.objects.filter(event=self.event).count(), 1)
        # The endpoint returns the FULL list, so the UI can render from the response without
        # a second round trip.
        self.assertEqual(len(resp.json()["public_sponsors"]), 1)

    def test_a_logo_is_accepted_and_served_back_as_a_url(self):
        resp = self._add(logo=SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"))

        self.assertEqual(resp.status_code, 201, resp.content)
        row = resp.json()["public_sponsors"][0]
        self.assertTrue(row["logo_url"], "a stored logo must come back as a URL the page can use")

    def test_a_player_cannot_add_one(self):
        """The gate is the same one edit_event uses. Anyone who can register for an event must not
        be able to put their own logo on its page."""
        resp = self._add(token="ps-player-token")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(EventPublicSponsor.objects.count(), 0)

    def test_a_link_that_is_not_an_https_url_is_refused(self):
        """The link is attacker-supplied and lands in an anchor on a PUBLIC page. javascript: is
        the one that turns a sponsor row into stored XSS."""
        for bad in ("javascript:alert(1)", "http://acme.example.com", "not-a-url"):
            with self.subTest(link=bad):
                resp = self._add(link=bad)
                self.assertEqual(resp.status_code, 400, f"{bad!r} was accepted: {resp.content}")
        self.assertEqual(EventPublicSponsor.objects.count(), 0)

    def test_a_nameless_sponsor_is_refused(self):
        """The name is the logo's alt text, so an empty one is an accessibility hole as well as a
        blank row in the admin list."""
        resp = self._add(name="   ")

        self.assertEqual(resp.status_code, 400, resp.content)

    # ── reading, which is the point of the feature ──
    def test_a_logged_out_visitor_sees_the_sponsors(self):
        """THE LINE THIS FEATURE EXISTS FOR. There are two detail builders and only one of them
        serves the public; a change that updates the signed-in one looks right to whoever is
        testing and shows nothing to everybody else."""
        self._add(link="https://acme.example.com")

        resp, event = self._public_detail()

        self.assertEqual(resp.status_code, 200, resp.content)
        sponsors = event.get("public_sponsors")
        self.assertTrue(sponsors, "a logged-out visitor got no public_sponsors")
        self.assertEqual(sponsors[0]["name"], "Acme Energy")

    def test_a_logged_out_visitor_sees_the_description(self):
        resp, event = self._public_detail()

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            event.get("event_description"),
            "A monthly open bracket for African squads.")

    def test_an_event_with_no_description_reports_an_empty_one_rather_than_omitting_it(self):
        """The page decides whether to render the About block from this value. A missing key and
        an empty string are different things to a frontend, and every event created before this
        field existed has the empty one."""
        self.event.event_description = ""
        self.event.save(update_fields=["event_description"])

        _resp, event = self._public_detail()

        self.assertIn("event_description", event)
        self.assertEqual(event["event_description"], "")

    # ── deleting ──
    def test_deleting_removes_it_from_the_public_page(self):
        self._add()
        sponsor = EventPublicSponsor.objects.get()

        resp = self.client.delete(
            f"/events/public-sponsors/{sponsor.id}/delete/",
            HTTP_AUTHORIZATION="Bearer ps-admin-token")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(EventPublicSponsor.objects.count(), 0)

    def test_a_player_cannot_delete_one(self):
        self._add()
        sponsor = EventPublicSponsor.objects.get()

        resp = self.client.delete(
            f"/events/public-sponsors/{sponsor.id}/delete/",
            HTTP_AUTHORIZATION="Bearer ps-player-token")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(EventPublicSponsor.objects.count(), 1)

    def test_sponsors_disappear_with_their_event(self):
        """CASCADE, so deleting an event cannot leave orphan rows pointing at nothing."""
        self._add()

        self.event.delete()

        self.assertEqual(EventPublicSponsor.objects.count(), 0)
