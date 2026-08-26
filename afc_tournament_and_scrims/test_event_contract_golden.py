"""CHARACTERIZATION tests for the event contract conversion.

WHY THIS EXISTS
    Converting a reader means deleting a hand-typed list of dozens of keys and generating it from
    the contract instead. The risk is not that it crashes. The risk is that one key quietly changes
    name, disappears, or changes type, and the frontend breaks somewhere nobody looked. There are
    no serializers and no schema, so nothing else in this repo would notice.

HOW IT WORKS
    A golden file per endpoint, captured from the code BEFORE the conversion and committed. The
    test then asserts the endpoint still returns exactly that, comparing the key SET first so a
    missing or added key is reported by NAME rather than as a wall of diff.

    The golden is the truth. When one of these fails during a conversion, fix the contract, never
    the golden.

REGENERATING a golden is deliberate and must be justified in the commit message:
    AFC_GOLDEN_WRITE=1 AFC_TEST_DB_NAME=test_afc_contract python manage.py test \\
        afc_tournament_and_scrims.test_event_contract_golden

WHAT IS AND IS NOT UNDER CONTRACT
    Both readers return {"event_details": event_data}, and event_data gains three COLLECTION keys
    after the field literal: registered_competitors, tournament_teams and stages. Those describe
    other objects, not the event, so they are excluded here and left exactly where they are in
    views.py. Only the event's own fields move to the contract.

Run: AFC_TEST_DB_NAME=test_afc_contract python manage.py test afc_tournament_and_scrims.test_event_contract_golden
"""
import json
import os
from datetime import date, time
from pathlib import Path

from django.test import Client, TestCase
from django.test import override_settings

from afc_auth.models import SessionToken, User, UserProfile
from afc_tournament_and_scrims.models import Event

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"

# Keys that live in the same dict but describe OTHER objects. Not part of the event contract.
NOT_EVENT_FIELDS = {"registered_competitors", "tournament_teams", "stages"}

# Keys whose value cannot be the same twice: the autoincrement primary key, and an auto_now_add
# timestamp. Pinning their VALUES would make the golden churn on every run and teach whoever hits
# it to regenerate the file, which is exactly the habit that makes a golden worthless. Their
# presence and non-nullness are still asserted; only the value is masked.
VOLATILE_FIELDS = {"event_id", "created_at"}
_MASK = "<volatile>"


def _mask_volatile(payload):
    return {k: (_MASK if k in VOLATILE_FIELDS else v) for k, v in payload.items()}


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _fully_populated_event(creator, **overrides):
    """An event with as many contract fields as possible set to a NON-default value.

    A golden captured from an event with half its fields left null proves nothing about the other
    half, so this deliberately fills them. Dates are FIXED rather than relative to today, so the
    golden does not churn from one day to the next.

    Several of these columns are short (event_type and competition_type are max_length=10,
    prize_currency is 3), so values are copied from the shapes the existing tests already use
    rather than invented.
    """
    fields = dict(
        event_name="Contract Golden Cup",
        slug="contract-golden-cup",
        competition_type="tournament",
        participant_type="solo",
        event_type="online",
        event_mode="single",
        max_teams_or_players=48,
        number_of_stages=1,
        start_date=date(2027, 3, 1),
        end_date=date(2027, 3, 3),
        event_start_time=time(18, 0),
        event_end_time=time(21, 0),
        registration_open_date=date(2027, 2, 1),
        registration_end_date=date(2027, 2, 20),
        timezone="Africa/Lagos",
        prizepool="1000 USD",
        prizepool_cash_value=1000,
        prize_currency="USD",
        prize_distribution={"1": 500, "2": 300, "3": 200},
        registration_type="paid",
        registration_fee=10,
        registration_fee_currency="USD",
        event_rules="Rule one. Rule two.",
        event_description="A fully populated event used to capture the contract golden files.",
        registration_link="https://example.invalid/register",
        results_published=True,
        is_public=True,
        is_waitlist_enabled=True,
        waitlist_capacity=8,
        require_player_uid=True,
        require_whatsapp=True,
        required_connections=["google"],
        creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


class GoldenMixin:
    """Compare a payload against its committed golden, or write it when explicitly asked."""

    def assert_matches_golden(self, name, payload):
        path = GOLDEN_DIR / f"{name}.json"
        # default=str so dates and Decimals render deterministically; the point of the comparison
        # is the SHAPE and the VALUES as the frontend sees them after DRF renders the response.
        for key in VOLATILE_FIELDS & set(payload):
            self.assertIsNotNone(payload[key], f"{key} should still carry a value")
        rendered = json.dumps(_mask_volatile(payload), indent=2, sort_keys=False, default=str)
        if os.environ.get("AFC_GOLDEN_WRITE") == "1":
            GOLDEN_DIR.mkdir(exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            self.skipTest(f"golden {name} written, rerun without AFC_GOLDEN_WRITE to assert")
        self.assertTrue(path.exists(), f"golden {name} missing, capture it with AFC_GOLDEN_WRITE=1")
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual = json.loads(rendered)
        missing = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        self.assertEqual(missing, [], f"keys the endpoint no longer returns: {missing}")
        self.assertEqual(added, [], f"keys the endpoint did not return before: {added}")
        self.assertEqual(expected, actual)

    @staticmethod
    def event_portion(body):
        """The event's own fields, with the collection keys stripped out."""
        return {k: v for k, v in body["event_details"].items() if k not in NOT_EVENT_FIELDS}


# GOOGLE_OAUTH_CLIENT_ID is set so the "google" provider is enabled, matching production: without
# it, required_connections would validate against an empty registry.
@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class PublicReaderGoldenTests(GoldenMixin, TestCase):
    """get_event_details_not_logged_in: the PUBLIC rung, 53 event fields as of 2026-08-26."""

    def setUp(self):
        self.creator, _ = _user("goldencreator")
        self.event = _fully_populated_event(self.creator)

    def test_public_event_detail_payload_is_unchanged(self):
        resp = Client().post(
            "/events/get-event-details-not-logged-in/",
            data=json.dumps({"slug": self.event.slug}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_matches_golden("public_event_data", self.event_portion(resp.json()))

    def test_contract_reproduces_the_public_payload(self):
        """The CONTRACT agrees with the reader, while the reader is still the old code.

        This is the check that makes the conversion safe: the declaration is verified against the
        real endpoint BEFORE anything is allowed to depend on it. Values are compared as JSON, the
        way the frontend sees them, so a date rendering differently counts as a difference.
        """
        from django.test import RequestFactory

        from afc_tournament_and_scrims import event_contract as ec
        from afc_tournament_and_scrims.views import (
            effective_event_status,
            serialize_public_sponsors,
        )
        from afc_tournament_and_scrims.models import SponsorEvent

        request = RequestFactory().post("/events/get-event-details-not-logged-in/")
        produced = ec.serialize_event(
            self.event, role=ec.PUBLIC, request=request,
            extra={
                "public_sponsors": serialize_public_sponsors(self.event, request),
                "event_status": effective_event_status(self.event),
                "sponsors": SponsorEvent.objects.filter(event=self.event).select_related("sponsor"),
                "active_registered": 0,
                "your_registration_fee": None,
            },
        )
        golden = json.loads((GOLDEN_DIR / "public_event_data.json").read_text(encoding="utf-8"))
        missing = sorted(set(golden) - set(produced))
        added = sorted(set(produced) - set(golden))
        self.assertEqual(missing, [], f"contract is missing keys the reader emits: {missing}")
        self.assertEqual(added, [], f"contract emits keys the reader does not: {added}")

        # Values too, not just the key set: a field pointed at the wrong attribute would otherwise
        # sail straight through.
        rendered = json.loads(json.dumps(_mask_volatile(produced), default=str))
        differing = sorted(k for k in golden if golden[k] != rendered[k])
        self.assertEqual(differing, [], f"contract produces different values for: {differing}")


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class PlayerReaderGoldenTests(GoldenMixin, TestCase):
    """get_event_details: the PLAYER rung, 64 event fields as of 2026-08-26."""

    def setUp(self):
        self.creator, _ = _user("goldenplayercreator")
        self.viewer, self.token = _user("goldenplayerviewer")
        self.event = _fully_populated_event(self.creator, slug="contract-golden-cup-player",
                                            event_name="Contract Golden Cup Player")

    def test_signed_in_event_detail_payload_is_unchanged(self):
        resp = Client().post(
            "/events/get-event-details/",
            data=json.dumps({"slug": self.event.slug}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assert_matches_golden("player_event_data", self.event_portion(resp.json()))
