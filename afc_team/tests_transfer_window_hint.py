r"""The date on a refused roster move.

WHY: backlog item 10 asked that the transfer-window notice say WHEN the window opens or closes
rather than just "until it reopens". The banner on the public pages was done; a verification pass
reported the SERVER-side refusals as still date-less, which turned out to be wrong: all three
already append _transfer_window_reopen_hint. These tests pin that down so the next person to read
the message literal, see no date in it, and "fix" it does not undo the concatenation.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_transfer_window_hint
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_rankings.models import Season
from afc_team.views import _transfer_window_reopen_hint


class TransferWindowHintTests(TestCase):
    def _season(self, opens, closes):
        today = timezone.localdate()
        return Season.objects.create(
            year=2026, quarter=3, is_active=True,
            # Season requires its own range; it is irrelevant to the hint, which reads only the
            # two transfer-window columns, so it is set to something valid and ignored.
            start_date=today - datetime.timedelta(days=90),
            end_date=today + datetime.timedelta(days=90),
            transfer_window_open=opens, transfer_window_close=closes)

    def test_a_window_that_has_not_opened_yet_names_the_opening_date(self):
        today = timezone.localdate()
        season = self._season(today + datetime.timedelta(days=10),
                              today + datetime.timedelta(days=20))

        hint = _transfer_window_reopen_hint(season)

        self.assertIn("opens on", hint)
        self.assertIn((today + datetime.timedelta(days=10)).strftime("%d %B %Y"), hint)

    def test_a_window_that_has_already_closed_names_the_date_it_closed(self):
        today = timezone.localdate()
        season = self._season(today - datetime.timedelta(days=30),
                              today - datetime.timedelta(days=10))

        hint = _transfer_window_reopen_hint(season)

        self.assertIn("closed on", hint)
        self.assertIn((today - datetime.timedelta(days=10)).strftime("%d %B %Y"), hint)

    def test_an_open_window_adds_nothing(self):
        """The caller only builds this hint when it is refusing a move, and a refusal while the
        window is open is about something else entirely, so a date here would misdirect."""
        today = timezone.localdate()
        season = self._season(today - datetime.timedelta(days=1),
                              today + datetime.timedelta(days=1))

        self.assertEqual(_transfer_window_reopen_hint(season), "")

    def test_a_season_with_no_dates_adds_nothing_rather_than_guessing(self):
        """The columns are NOT NULL, so this state cannot be stored; it is reachable through any
        caller that hands the helper a season it built or annotated itself. The helper reads
        attributes and must not invent a date from a missing one either way."""
        today = timezone.localdate()
        season = self._season(today + datetime.timedelta(days=5), today + datetime.timedelta(days=9))
        season.transfer_window_open = None
        season.transfer_window_close = None

        self.assertEqual(_transfer_window_reopen_hint(season), "")

    def test_no_active_season_adds_nothing(self):
        self.assertEqual(_transfer_window_reopen_hint(None), "")
