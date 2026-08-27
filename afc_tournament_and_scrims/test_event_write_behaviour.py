"""Behaviour of the event WRITE paths, pinned before create_event and edit_event are converted.

WHY THIS FILE EXISTS
    The read side of the event contract was made safe by golden payloads: capture the old output,
    convert, prove the output did not move. The write side has no equivalent, because a write
    produces a row rather than a payload. This file is the substitute, and it is deliberately
    written and run BEFORE the conversion, against the hand-written code, so it records what the
    endpoints actually do rather than what the conversion makes them do.

    Every test here passed against the unconverted create_event and edit_event. A failure after
    the conversion is therefore a behaviour change, not a bad expectation.

WHAT IT PINS, and each is a rule the conversion could plausibly break
    1. PATCH semantics: a key the request omits is left alone. This is the single most important
       property of edit_event, and the reason it uses `if "x" in request.data` 45 times.
    2. Type coercion: booleans arriving as the strings "true"/"false", numbers as strings, lists
       as JSON text from multipart forms.
    3. Emptiness: a legitimately empty list CLEARS rather than raising (the 2026-08-26 outage), and
       clearing a nullable string stores None rather than "".
    4. Rejection: junk is refused with a 400 and the event is NOT half-written.
    5. The fields that must NOT be settable through the API at all.

Run: AFC_TEST_DB_NAME=test_afc_contract python manage.py test afc_tournament_and_scrims.test_event_write_behaviour
"""
import json
from datetime import date, time, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, User, UserProfile
from afc_tournament_and_scrims.models import Event


def _user(username, role="admin"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class EditEventWriteBehaviourTests(TestCase):
    def setUp(self):
        self.staff, self.token = _user("editbehaviour")
        self.event = Event.objects.create(
            event_name="Write Behaviour Cup",
            slug="write-behaviour-cup",
            competition_type="tournament",
            participant_type="solo",
            event_type="online",
            event_mode="single",
            max_teams_or_players=24,
            number_of_stages=1,
            start_date=date.today() + timedelta(days=30),
            end_date=date.today() + timedelta(days=32),
            registration_open_date=date.today() + timedelta(days=1),
            registration_end_date=date.today() + timedelta(days=20),
            event_start_time=time(18, 0),
            event_end_time=time(21, 0),
            timezone="Africa/Lagos",
            prizepool="1000 USD",
            prizepool_cash_value=1000,
            prize_currency="USD",
            event_rules="Rule one.",
            event_description="Original description.",
            require_player_uid=True,
            require_whatsapp=True,
            required_connections=["google"],
            min_letter_avatars=3,
            waitlist_capacity=8,
            creator=self.staff,
        )

    def _edit(self, payload):
        return Client().post(
            "/events/edit-event/",
            data=json.dumps({"event_id": self.event.event_id, **payload}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    # ── 1. PATCH semantics ────────────────────────────────────────────────────────────────────
    def test_an_omitted_key_is_left_alone(self):
        """The property that makes edit_event safe for an older client to call.

        A client that does not know about a field must not be able to blank it just by not sending
        it. This is why the endpoint checks `in request.data` rather than reading `.get()`.
        """
        watched = ("event_name", "event_description", "event_rules", "max_teams_or_players",
                   "prizepool", "prize_currency", "require_player_uid", "require_whatsapp",
                   "required_connections", "min_letter_avatars", "waitlist_capacity",
                   "timezone", "event_start_time")
        before = {f: getattr(self.event, f) for f in watched}

        resp = self._edit({"event_name": "Renamed By Test"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()

        self.assertEqual(self.event.event_name, "Renamed By Test")
        for field in watched:
            if field == "event_name":
                continue
            self.assertEqual(getattr(self.event, field), before[field],
                             f"{field} changed and nothing asked it to")

    # ── 2. type coercion ──────────────────────────────────────────────────────────────────────
    def test_a_boolean_sent_as_a_string_is_coerced(self):
        """Multipart forms can only send strings, so "false" has to mean False, not truthy."""
        resp = self._edit({"require_whatsapp": "false"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertFalse(self.event.require_whatsapp)

        resp = self._edit({"require_whatsapp": "true"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertTrue(self.event.require_whatsapp)

    def test_a_number_sent_as_a_string_is_coerced(self):
        resp = self._edit({"max_teams_or_players": "64", "waitlist_capacity": "12"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertEqual(int(self.event.max_teams_or_players), 64)
        self.assertEqual(self.event.waitlist_capacity, 12)

    def test_a_list_sent_as_json_text_is_parsed(self):
        """The exact shape multipart FormData produces for a list field."""
        resp = self._edit({"required_connections": '["google"]'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertEqual(self.event.required_connections, ["google"])

    # ── 3. emptiness is a real answer ─────────────────────────────────────────────────────────
    def test_an_empty_list_clears_the_requirement(self):
        """THE 2026-08-26 OUTAGE. An empty selection travels as the string "[]" from a form.

        The original guard read `if not raw: raise`, so the requirement could be switched on and
        never off, and an untouched picker blocked event creation outright.
        """
        for payload in ('[]', []):
            self.event.required_connections = ["google"]
            self.event.save()
            resp = self._edit({"required_connections": payload})
            self.assertEqual(resp.status_code, 200, resp.content)
            self.event.refresh_from_db()
            self.assertEqual(self.event.required_connections, [],
                             f"an empty list sent as {payload!r} did not clear the requirement")

    def test_clearing_a_nullable_string_stores_none(self):
        resp = self._edit({"timezone": ""})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.timezone)

    def test_zero_is_stored_rather_than_treated_as_absent(self):
        """A falsy-but-real value. min_letter_avatars 0 means the gate is OFF, not unset."""
        resp = self._edit({"min_letter_avatars": 0})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertEqual(self.event.min_letter_avatars, 0)

    # ── 4. rejection leaves nothing half-written ──────────────────────────────────────────────
    def test_an_unknown_connection_provider_is_refused(self):
        resp = self._edit({"required_connections": ["myspace"]})
        self.assertEqual(resp.status_code, 400)
        self.event.refresh_from_db()
        self.assertEqual(self.event.required_connections, ["google"], "the old value must survive")

    def test_discord_is_refused_as_a_required_connection(self):
        """require_discord is its own switch and means MORE, so accepting both would be two ways
        to say one thing, with different behaviour."""
        resp = self._edit({"required_connections": ["discord"]})
        self.assertEqual(resp.status_code, 400)

    def test_a_non_numeric_cash_value_is_refused(self):
        resp = self._edit({"prizepool_cash_value": "not a number"})
        self.assertEqual(resp.status_code, 400)
        self.event.refresh_from_db()
        self.assertEqual(self.event.prizepool_cash_value, 1000)

    def test_a_rejected_payload_does_not_apply_its_other_keys(self):
        """The one that matters most for the conversion: a 400 must not leave a partial write."""
        resp = self._edit({"event_name": "Should Not Stick", "required_connections": ["myspace"]})
        self.assertEqual(resp.status_code, 400)
        self.event.refresh_from_db()
        self.assertEqual(self.event.event_name, "Write Behaviour Cup",
                         "a refused payload half-applied its other keys")

    # ── 5. what must never be settable through the API ────────────────────────────────────────
    def test_tournament_tier_is_owned_by_apply_event_tier_not_by_the_field_writes(self):
        """tournament_tier does NOT ride the ordinary field writes, and must not start doing so.

        It is owned by apply_event_tier, which runs AFTER the save: a head or super admin's
        explicit pick overrides the auto classifier and PINS it via tier_overridden, and everybody
        else's tier is auto-classified from the Tournament Tiers rules (owner 2026-06-30, "both,
        but only head/super can override").

        So the observable rule is not "it cannot be set". It is "setting it goes through that gate
        and leaves the pin behind". An earlier version of this test asserted the tier could not
        change at all and failed against the UNCONVERTED code, which was the test being wrong: the
        actor here is a super admin, so the override is exactly what should happen.

        This is why tournament_tier carries write=NOBODY in the contract. Declaring it writable
        would let it be assigned without ever setting tier_overridden, silently unpinning it.
        """
        resp = self._edit({"tournament_tier": "tier_1"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.event.refresh_from_db()
        self.assertEqual(self.event.tournament_tier, "tier_1")
        self.assertTrue(self.event.tier_overridden,
                        "the tier changed without being pinned, so the classifier will undo it")

    def test_the_primary_key_cannot_be_reassigned(self):
        original = self.event.event_id
        self._edit({"event_id": original})   # the lookup key itself, never a writable field
        self.assertTrue(Event.objects.filter(event_id=original).exists())
