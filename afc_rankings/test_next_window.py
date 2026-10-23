"""
test_next_window.py
───────────────────
Covers Season.is_transfer_window_open and Season.next_window_opens (inbox #150, owner 2026-10-05:
"next open date should also show, and the next open date is the day after the last day of the
season").

Before: once a season's window closed, the only "next opening" anyone could name was a window on a
LATER season record; with none on record the banner showed nothing, and the season, which stays
is_active after its end_date until a successor's dates begin (auto_rollover_seasons), kept every
team locked for good. Now an ended season no longer locks moves, and the next opening is the day
after its last day unless a successor already on record takes over the lock first.

Run: ..\\backend\\.venv\\Scripts\\python.exe manage.py test afc_rankings.test_next_window --noinput
"""
import datetime

from django.test import TestCase

from afc_rankings.models import Season
from afc_rankings.serializers import season as season_json

D = datetime.date


def make(quarter, year, start, end, w_open, w_close):
    return Season.objects.create(name=f"SEASON {quarter} {year}", quarter=quarter, year=year,
                                 start_date=start, end_date=end,
                                 transfer_window_open=w_open, transfer_window_close=w_close)


class NextWindowTests(TestCase):
    def setUp(self):
        # The shape of production on 5 Oct 2026: SEASON 4, window 11 to 25 Sep, season to 31 Dec.
        self.s4 = make(4, 2026, D(2026, 9, 1), D(2026, 12, 31), D(2026, 9, 11), D(2026, 9, 25))

    def test_inside_the_window_moves_are_allowed_and_nothing_is_next(self):
        self.assertTrue(self.s4.is_transfer_window_open(D(2026, 9, 20)))
        self.assertIsNone(self.s4.next_window_opens(D(2026, 9, 20)))

    def test_before_the_window_the_next_opening_is_this_seasons_window(self):
        self.assertFalse(self.s4.is_transfer_window_open(D(2026, 9, 5)))
        self.assertEqual(self.s4.next_window_opens(D(2026, 9, 5)), D(2026, 9, 11))

    def test_window_spent_and_no_successor_means_the_day_after_the_season(self):
        self.assertFalse(self.s4.is_transfer_window_open(D(2026, 10, 5)))
        self.assertEqual(self.s4.next_window_opens(D(2026, 10, 5)), D(2027, 1, 1))

    def test_after_the_last_day_an_ended_season_no_longer_locks(self):
        self.assertFalse(self.s4.is_transfer_window_open(D(2026, 12, 31)))   # last day: still locked
        self.assertTrue(self.s4.is_transfer_window_open(D(2027, 1, 1)))     # the day after: free
        self.assertIsNone(self.s4.next_window_opens(D(2027, 1, 1)))

    def test_a_successor_starting_next_day_with_a_later_window_takes_over(self):
        make(1, 2027, D(2027, 1, 1), D(2027, 3, 31), D(2027, 1, 10), D(2027, 1, 24))
        self.assertEqual(self.s4.next_window_opens(D(2026, 10, 5)), D(2027, 1, 10))

    def test_a_successor_whose_window_opens_on_its_first_day(self):
        make(1, 2027, D(2027, 1, 1), D(2027, 3, 31), D(2027, 1, 1), D(2027, 1, 14))
        self.assertEqual(self.s4.next_window_opens(D(2026, 10, 5)), D(2027, 1, 1))

    def test_a_gap_before_the_successor_means_the_day_after_the_season(self):
        # Nothing locks between the seasons, so moves reopen the day after SEASON 4 ends.
        make(1, 2027, D(2027, 2, 1), D(2027, 4, 30), D(2027, 2, 10), D(2027, 2, 24))
        self.assertEqual(self.s4.next_window_opens(D(2026, 10, 5)), D(2027, 1, 1))

    def test_the_api_shape_carries_the_next_opening(self):
        body = season_json(self.s4)
        self.assertIn("next_window_opens", body)
        # Relative to the real today, so only the type is fixed here; the dated cases are above.
        self.assertTrue(body["next_window_opens"] is None or len(body["next_window_opens"]) == 10)


class AutoSeasonTests(TestCase):
    """ensure_next_season (inbox #157): one season always on record after the latest that has begun."""

    def test_after_the_latest_starts_the_next_is_created_chained_with_a_two_week_window(self):
        from afc_rankings.models import ensure_next_season
        make(4, 2026, D(2026, 9, 11), D(2026, 12, 31), D(2026, 9, 11), D(2026, 9, 25))
        created = ensure_next_season(D(2026, 10, 5))
        self.assertEqual((created.name, created.quarter, created.year), ("SEASON 1 2027", 1, 2027))
        self.assertEqual((created.start_date, created.end_date), (D(2027, 1, 1), D(2027, 3, 31)))
        self.assertEqual((created.transfer_window_open, created.transfer_window_close), (D(2027, 1, 1), D(2027, 1, 14)))
        self.assertFalse(created.is_active)

    def test_nothing_while_a_future_season_is_on_record_and_never_twice(self):
        from afc_rankings.models import ensure_next_season
        make(4, 2026, D(2026, 9, 11), D(2026, 12, 31), D(2026, 9, 11), D(2026, 9, 25))
        self.assertIsNotNone(ensure_next_season(D(2026, 10, 5)))
        self.assertIsNone(ensure_next_season(D(2026, 10, 5)))
        self.assertEqual(Season.objects.count(), 2)

    def test_an_edited_future_season_is_left_alone(self):
        from afc_rankings.models import ensure_next_season
        make(4, 2026, D(2026, 9, 11), D(2026, 12, 31), D(2026, 9, 11), D(2026, 9, 25))
        edited = make(1, 2027, D(2027, 1, 5), D(2027, 4, 2), D(2027, 1, 5), D(2027, 1, 30))
        self.assertIsNone(ensure_next_season(D(2026, 10, 5)))
        edited.refresh_from_db()
        self.assertEqual((edited.start_date, edited.transfer_window_close), (D(2027, 1, 5), D(2027, 1, 30)))

    def test_the_following_quarter_and_month_ends(self):
        from afc_rankings.models import ensure_next_season
        make(1, 2027, D(2027, 1, 1), D(2027, 3, 31), D(2027, 1, 1), D(2027, 1, 14))
        created = ensure_next_season(D(2027, 1, 1))
        self.assertEqual((created.name, created.start_date, created.end_date), ("SEASON 2 2027", D(2027, 4, 1), D(2027, 6, 30)))

    def test_no_seasons_at_all_creates_nothing(self):
        from afc_rankings.models import ensure_next_season
        self.assertIsNone(ensure_next_season(D(2026, 10, 5)))

    def test_with_the_auto_season_on_record_the_next_opening_is_its_first_day(self):
        from afc_rankings.models import ensure_next_season
        s4 = make(4, 2026, D(2026, 9, 11), D(2026, 12, 31), D(2026, 9, 11), D(2026, 9, 25))
        ensure_next_season(D(2026, 10, 5))
        self.assertEqual(s4.next_window_opens(D(2026, 10, 5)), D(2027, 1, 1))


class OverlappingSuccessorTests(TestCase):
    def test_a_successor_that_starts_before_this_season_ends_takes_over_on_its_first_day(self):
        # The lock passes to it when auto_rollover_seasons activates it, so its window is the answer.
        s = make(4, 2026, D(2026, 9, 1), D(2026, 12, 31), D(2026, 9, 1), D(2026, 9, 14))
        make(1, 2027, D(2026, 10, 6), D(2027, 1, 31), D(2026, 10, 11), D(2026, 10, 25))
        self.assertEqual(s.next_window_opens(D(2026, 10, 5)), D(2026, 10, 11))
