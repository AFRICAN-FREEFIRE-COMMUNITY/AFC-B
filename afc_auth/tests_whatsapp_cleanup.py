"""
afc_auth/tests_whatsapp_cleanup.py
================================================================================
Clearing the numbers that are already shared, and a guard so the rule cannot vanish again.

Owner, 2026-09-22, told the count of 52 numbers on 110 accounts: "remove all the numbers from those
accounts so they have to reenter them."

The rule itself (one number, one account) is inbox #21 and lives in identifiers.py §4 with its own
tests in tests_whatsapp_unique.py. This file covers the two things that were missing:

  1. `manage.py clear_shared_whatsapp_numbers`, which empties every account on a shared number and
     tells each person why, so the real owner of the line can type it back in,
  2. a GUARD that the rule is actually wired at all three doors.

Why the guard exists, and it is not theoretical: the rule shipped on 2026-09-17 at 19:48 (AFC-B
#68) and was gone from main by 21:03 the same evening. The Discord-reminders branch had been cut
before it, its copy of identifiers.py won the merge, and the §4 block plus the tests file went with
it. Nothing failed, because the tests that would have failed were deleted in the same commit. The
frontend kept checking for a `whatsapp_taken` code the backend no longer sent, and production ran
for five days with no rule at all. A test that reads the DOORS survives the deletion of the rule,
because the doors live in files a merge is unlikely to take wholesale.

Run: python manage.py test afc_auth.tests_whatsapp_cleanup
"""
import datetime
import io as _io
import os
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from afc_auth.identifiers import whatsapp_number_holder
from afc_auth.models import Notifications, User, UserProfile

NUMBER = "+2348051234567"
OTHER = "+2348051239999"
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def make_user(username, number=None):
    user = User.objects.create(username=username, email="%s@gmail.com" % username,
                               full_name=username, role="player", password="x", is_active=True)
    profile = UserProfile.objects.create(user=user, whatsapp_number=number or "")
    return user, profile


class ClearSharedNumbersTests(TestCase):
    def setUp(self):
        self.a, self.pa = make_user("shared_a", NUMBER)
        self.b, self.pb = make_user("shared_b", NUMBER)
        self.c, self.pc = make_user("alone", OTHER)
        for profile in (self.pa, self.pb):
            profile.whatsapp_opt_in = True
            profile.whatsapp_number_updated_at = datetime.datetime.now(datetime.timezone.utc)
            profile.save()

    def test_both_sides_are_cleared_told_why_and_the_unshared_is_left_alone(self):
        out = StringIO()
        call_command("clear_shared_whatsapp_numbers", stdout=out)
        for profile in (self.pa, self.pb):
            profile.refresh_from_db()
            self.assertEqual(profile.whatsapp_number, "")
            self.assertIsNone(profile.whatsapp_number_updated_at)
            self.assertFalse(profile.whatsapp_opt_in)
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.whatsapp_number, OTHER)
        notes = Notifications.objects.filter(notification_type="whatsapp_number_cleared")
        self.assertEqual(notes.count(), 2)
        self.assertEqual(notes.first().target_type, "profile_settings")
        self.assertIn("cleared 2 accounts", out.getvalue())

    def test_a_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("clear_shared_whatsapp_numbers", "--dry-run", stdout=out)
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.whatsapp_number, NUMBER)
        self.assertEqual(Notifications.objects.count(), 0)
        self.assertIn("dry run", out.getvalue())

    def test_running_it_again_finds_nothing(self):
        call_command("clear_shared_whatsapp_numbers", stdout=StringIO())
        out = StringIO()
        call_command("clear_shared_whatsapp_numbers", stdout=out)
        self.assertIn("nothing to clear", out.getvalue())

    def test_after_clearing_either_person_can_claim_the_number_again(self):
        call_command("clear_shared_whatsapp_numbers", stdout=StringIO())
        # Nobody holds it now, so the first of them to re-enter it gets it...
        self.assertIsNone(whatsapp_number_holder(NUMBER))
        self.pa.whatsapp_number = NUMBER
        self.pa.save(update_fields=["whatsapp_number"])
        # ...and the second is refused, which is the whole point of clearing rather than choosing.
        holder = whatsapp_number_holder(NUMBER, exclude_pk=self.b.pk)
        self.assertIsNotNone(holder)
        self.assertEqual(holder.pk, self.a.pk)


class TheRuleIsStillWiredTests(TestCase):
    """Read the doors, not the helper: a merge that deletes the rule deletes its tests with it.

    See the module docstring: that is exactly what happened on 2026-09-17."""

    def _source(self, filename):
        with _io.open(os.path.join(APP_DIR, filename), encoding="utf-8") as fh:
            return fh.read()

    def test_signup_and_profile_edit_both_call_the_holder_check(self):
        views = self._source("views.py")
        self.assertGreaterEqual(
            views.count("whatsapp_number_holder("), 2,
            "signup and edit_profile must each ask whatsapp_number_holder before saving the number "
            "(inbox #21). If this fails, the rule was deleted again: restore it from "
            "identifiers.py section 4 and see tests_whatsapp_cleanup's docstring.")

    def test_the_support_screen_calls_it_too(self):
        admin = self._source("views_admin_identity.py")
        self.assertIn("whatsapp_number_holder(", admin)

    def test_the_helper_and_its_code_still_exist(self):
        ident = self._source("identifiers.py")
        self.assertIn("def whatsapp_number_holder(", ident)
        self.assertIn('WHATSAPP_TAKEN_CODE = "whatsapp_taken"', ident)
