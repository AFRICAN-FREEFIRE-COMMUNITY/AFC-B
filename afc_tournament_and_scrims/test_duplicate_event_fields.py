"""duplicate_event must carry every CONFIGURATION field of the event it copies.

WHY THIS FILE EXISTS
    duplicate_event built the copy with `Event.objects.create(...)` and 51 keyword arguments typed
    out by hand. Its own docstring says the list "mirrors create_event's Event.objects.create(...)
    so the two stay in lockstep". It had drifted, and drift in that direction is SILENT: a field
    nobody remembered to add simply vanishes from every duplicated event, with no error anywhere,
    and nothing in the suite noticed.

WHAT THIS FOUND, 2026-08-26
    Seven fields that create_event sets, so they are real organizer-set configuration, were not
    carried by duplicate_event:

        require_discord, discord_server_id, discord_invite_link
            The Discord registration GATE. Duplicating an event that required Discord produced a
            copy that did not require it. This is the serious one: a gate silently switching off.
        timezone
            Every displayed date and time on the copy fell back to the default.
        waitlist_mode
            Which waitlisted competitor gets promoted when a slot frees up.
        auto_seed_on_start, auto_seed_trigger
            Whether the entry stage seeds itself when the event starts, and on what.

    A SECOND ROUND on 2026-08-27 carried five more, left out of the first fix to keep it to one
    thing: checkin_enabled, count_flagged_kills, allow_team_result_submissions, mvp_config and
    tie_breakers. count_flagged_kills is the sharpest, because it defaults to TRUE: an organizer
    who switched it off got a copy with it back on, silently changing how the copy scores.

    Written to FAIL first, against the hand-written list, then to pass once the copy is built from
    the event contract, where inheriting is the default and dropping a field has to be deliberate.

WHAT IS DELIBERATELY *NOT* CARRIED
    A duplicate inherits the event's SHAPE, never its identity, its results, or its history. Those
    exclusions are asserted too, so "copy everything" cannot quietly become the new bug.

Run: AFC_TEST_DB_NAME=test_afc_contract python manage.py test afc_tournament_and_scrims.test_duplicate_event_fields
"""
import json
from datetime import date, time, timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

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


# Configuration an organizer sets, which a copy of the event has to inherit. Every one of these is
# also set by create_event, which is the test for "is this configuration or is it derived".
CONFIG_FIELDS = {
    # The Discord registration gate.
    "require_discord": True,
    "discord_server_id": "123456789012345678",
    "discord_invite_link": "https://discord.gg/afctest",
    # Display.
    "timezone": "Africa/Lagos",
    # Waitlist behaviour.
    "is_waitlist_enabled": True,
    "waitlist_capacity": 8,
    "waitlist_mode": "manual_admin",   # NOT the default, or a drop would be invisible
    # Seeding behaviour.
    "auto_seed_on_start": True,
    "auto_seed_trigger": "registration_close",   # NOT the default ("event_start")
    # Registration requirements, which were already carried; kept here so a future edit cannot drop
    # them without this test noticing.
    "require_player_uid": True,
    "require_whatsapp": True,
    "require_team_logo": True,
    "require_esport_images": True,
    "require_player_profile_image": True,
    "required_connections": ["google"],
    "min_letter_avatars": 3,
    # SCORING and CHECK-IN config (added 2026-08-27). The first fix carried the seven fields a test
    # proved were dropped and deliberately left these alone to keep that change to one thing. They
    # are the same class: configuration an organizer set on the source, which a copy is meant to
    # reuse. count_flagged_kills is the sharpest of them because it defaults to TRUE, so an
    # organizer who switched it OFF got a copy with it silently back ON, changing how the copy
    # scores.
    "checkin_enabled": True,
    "count_flagged_kills": False,          # NOT the default, or a drop would be invisible
    "allow_team_result_submissions": True,
    "mvp_config": {"metric": "kills"},
    "tie_breakers": {"first": "placement"},
}


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class DuplicateEventCarriesConfigTests(TestCase):
    def setUp(self):
        self.staff, self.token = _user("dupcfgstaff")
        self.event = Event.objects.create(
            event_name="Duplicate Config Cup",
            slug="duplicate-config-cup",
            competition_type="tournament",
            participant_type="solo",
            event_type="online",
            event_mode="single",
            max_teams_or_players=24,
            number_of_stages=1,
            # In the future, so _cloned_dates carries them across untouched rather than shifting
            # them, which keeps this test about FIELDS rather than about the date-shift rule.
            start_date=date.today() + timedelta(days=30),
            end_date=date.today() + timedelta(days=32),
            registration_open_date=date.today() + timedelta(days=1),
            registration_end_date=date.today() + timedelta(days=20),
            event_start_time=time(18, 0),
            event_end_time=time(21, 0),
            prizepool="1000 USD",
            prizepool_cash_value=1000,
            prize_currency="USD",
            event_rules="Rule one.",
            event_description="Config-carrying source event.",
            creator=self.staff,
            **CONFIG_FIELDS,
        )

    def _duplicate(self):
        resp = Client().post(
            f"/events/{self.event.event_id}/duplicate-event/",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertIn(resp.status_code, (200, 201), resp.content)
        copy = Event.objects.exclude(event_id=self.event.event_id).order_by("-event_id").first()
        self.assertIsNotNone(copy, "duplicate_event did not create a second event")
        return copy

    def test_every_configuration_field_is_carried_to_the_copy(self):
        copy = self._duplicate()
        dropped = {
            name: (getattr(self.event, name), getattr(copy, name))
            for name in CONFIG_FIELDS
            if getattr(copy, name) != getattr(self.event, name)
        }
        self.assertEqual(
            dropped, {},
            "duplicate_event dropped configuration fields (source value, copy value): "
            f"{dropped}",
        )

    def test_the_discord_gate_survives_duplication(self):
        """Called out on its own because it is the one that changes who may REGISTER.

        An organizer duplicating an event that requires Discord, and getting a copy that does not,
        is a registration gate switching itself off silently.
        """
        copy = self._duplicate()
        self.assertTrue(copy.require_discord, "the copy no longer requires Discord")
        self.assertEqual(copy.discord_server_id, self.event.discord_server_id)
        self.assertEqual(copy.discord_invite_link, self.event.discord_invite_link)

    def test_the_copy_does_not_inherit_identity_results_or_history(self):
        """The other half of the rule: inheriting everything would be its own bug."""
        self.event.rankings_verified = True
        self.event.partner_published = True
        self.event.results_imported_at = timezone.now()
        self.event.save()

        copy = self._duplicate()
        self.assertNotEqual(copy.event_id, self.event.event_id)
        self.assertNotEqual(copy.slug, self.event.slug)
        # NOT asserted: results_published. It defaults to True on the model, so every new event
        # starts with results visible and a copy showing True has inherited nothing. An earlier
        # version of this test asserted False and failed for that reason, which was the test being
        # wrong rather than a bug.
        self.assertFalse(copy.rankings_verified, "a clone must not inherit rankings verification")
        self.assertFalse(copy.partner_published, "a clone must not inherit partner publication")
        self.assertIsNone(copy.results_imported_at, "a clone must not inherit import provenance")
        # Lifecycle reset: a clone is always a fresh upcoming event, never a finished one.
        self.assertEqual(copy.event_status, "upcoming")
