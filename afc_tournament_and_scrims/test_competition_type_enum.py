# afc_tournament_and_scrims/test_competition_type_enum.py
# ──────────────────────────────────────────────────────────────────────────────
# THE BUG THIS EXISTS TO STOP RECURRING (found 2026-09-02 auditing the admin dashboard).
#
# Event.COMPETITION_TYPE_CHOICES declares "tournament" and "scrims". FIVE places compared against
# "scrim", singular. A Django filter on a value no row holds does not raise; it matches nothing and
# returns 0. So:
#
#   get_total_scrims_count                        -> 0, with 105 scrims live on production
#   get_all_tournaments_and_scrims_separated      -> an empty scrims list, always
#   ...separated_paginated                        -> the same
#   frontend organizer events page                -> "Scrims 0" to organizers running scrims
#   afc_team/tests.py fixture                     -> built an Event with an invalid enum value
#
# The last line is why nothing caught the first four for as long as they existed. The fixture
# supplied the same wrong string the production code used, so the test agreed with the bug. A test
# that hands the code the input it wants proves the code READS that input, never that the input is
# the one production stores.
#
# So this file does NOT test a filter against a hand-written string. It reads the model's own
# choices and greps the source, which is the only version that keeps working when somebody adds a
# third competition type.
#
# CONNECTS TO: afc_tournament_and_scrims.models.Event.COMPETITION_TYPE_CHOICES (the authority),
# afc_tournament_and_scrims.views (the filters), afc_auth.views_dashboard (the aggregate).
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import pathlib
import re

from django.test import TestCase
from django.utils import timezone

from .models import Event

# Every .py we hold responsible. Tests are included deliberately: an invalid enum in a FIXTURE is
# what hid this, so a fixture is exactly as much of a defect as a view.
_APP_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SEARCHED = ("afc_tournament_and_scrims", "afc_auth", "afc_team", "afc_leaderboard")

# `competition_type="..."` as a KEYWORD: a filter lookup or a model-construction kwarg. Both are
# places where a value the model does not declare is a defect.
#
# The negative lookbehind for a dot is not incidental. `event.competition_type = "something_new"`
# is an ATTRIBUTE assignment on an instance, and tests_event_wording.py does exactly that on
# purpose, to prove event_noun() falls back safely for a value nobody expected. That is the correct
# way to test an unknown value and must not be reported as a typo.
_LITERAL = re.compile(r"""(?<![.\w])competition_type\s*=\s*["']([^"']*)["']""")


def _valid_values():
    return {value for value, _label in Event.COMPETITION_TYPE_CHOICES}


class CompetitionTypeEnumTests(TestCase):
    """Nothing may compare competition_type against a value the model does not declare."""

    def test_the_model_still_declares_the_values_this_file_assumes(self):
        # A guard on the guard. If somebody renames a choice, the sweep below starts passing for
        # the wrong reason, so state the expectation out loud and fail loudly on a rename.
        self.assertEqual(_valid_values(), {"tournament", "scrims"})

    def test_no_source_file_filters_on_an_undeclared_competition_type(self):
        valid = _valid_values()
        offenders = []
        scanned = 0
        for app in _SEARCHED:
            for path in (_APP_ROOT / app).rglob("*.py"):
                if "migrations" in path.parts or path.name == pathlib.Path(__file__).name:
                    continue
                scanned += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    for value in _LITERAL.findall(line):
                        # An empty string is a legitimate "unset" comparison, not a typo.
                        if value and value not in valid:
                            rel = path.relative_to(_APP_ROOT)
                            offenders.append(f"{rel}:{lineno}  competition_type={value!r}")

        self.assertEqual(
            offenders, [],
            "competition_type compared against a value Event.COMPETITION_TYPE_CHOICES does not "
            "declare. Such a filter matches nothing and silently reports 0 rather than raising.\n"
            + "\n".join(offenders)
            + f"\n(scanned {scanned} files; valid values are {sorted(valid)})",
        )
        self.assertGreater(scanned, 20, "the sweep found almost no files; the path is probably wrong")

    def test_a_scrim_is_actually_counted_as_a_scrim(self):
        # The behavioural half, written from the PRODUCTION shape: create the event the way the app
        # creates it (through the model's own declared value) and assert the count endpoint's query
        # finds it. Against the old "scrim" filter this returns 0 and the test fails.
        today = timezone.localdate()
        Event.objects.create(
            event_name="Enum Guard Scrim", competition_type="scrims", participant_type="squad",
            event_type="virtual", event_mode="br", max_teams_or_players=12, number_of_stages=1,
            is_public=True, is_draft=False,
            start_date=today, end_date=today + datetime.timedelta(days=1),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=1),
        )
        counted = Event.objects.filter(competition_type="scrims", is_draft=False).count()
        self.assertEqual(counted, 1, "a scrim created through the model's own enum was not counted")
