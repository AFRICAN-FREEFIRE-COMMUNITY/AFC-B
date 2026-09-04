# afc_auth/test_history_text.py
# ----------------------------------------------------------------------------------------------
# PLAIN ENGLISH FOR THE ADMIN HISTORY.
#
# Owner 2026-09-03, quoting his own screen: "cant the action be put into plainer english? edited
# roles for user ARDENT from what to what? ... { "event_id": 333, "changes": [ "event_name:
# ... what does that mean, we need plainer english please".
#
# EVERY INPUT IN THIS FILE IS A REAL STORED SHAPE, taken from the production clone rather than
# invented, because a formatter tested only against strings its author imagined is one that meets
# the real table and prints nonsense. The four shapes measured across 1,075 rows:
#
#     "event_name: 'A' ARROW 'B'"                a plain field edit
#     "Stage 326 name: 'GROUP STAGE' ARROW 'X'"  a stage or group field edit
#     "Group 652 maps changed"                   a statement with no old/new pair
#     "Stages added: [328, 327]"                 a list of ids
#
# and the single most common row of all: an event edit whose change list is EMPTY. 119 of the 221
# JSON rows say exactly that, and they were rendering on screen as `"changes": []`.
#
# ARROW above stands for the character the writer puts between old and new (u2192), spelled out
# so this file stays readable in a terminal that cannot draw it.
# ----------------------------------------------------------------------------------------------
import json

from django.test import TestCase

from .history_text import describe_history, humanize_action, humanize_change


def line(text):
    """One stored change line, with ARROW standing in for the arrow character."""
    return text.replace("ARROW", "→")


def blob(event_id, changes):
    """The exact writer shape: afc_tournament_and_scrims.views.edit_event does json.dumps with
    indent=2 and the default ensure_ascii, which is why an emoji arrives escaped."""
    return json.dumps({"event_id": event_id, "changes": [line(c) for c in changes]}, indent=2)


class ChangeLineTests(TestCase):
    def test_a_rename_reads_as_a_rename(self):
        self.assertEqual(
            humanize_change(line("event_name: 'DYNASTY CUP (Copy)' ARROW 'DYNASTY CUP SSA'")),
            'renamed it to "DYNASTY CUP SSA"',
        )

    def test_a_yes_no_column_reads_as_an_action_not_as_True_and_False(self):
        self.assertEqual(humanize_change(line("is_draft: 'True' ARROW 'False'")), "published it")
        self.assertEqual(humanize_change(line("is_draft: 'False' ARROW 'True'")),
                         "moved it back to draft")
        self.assertEqual(humanize_change(line("is_public: 'False' ARROW 'True'")), "made it public")

    def test_a_plain_field_names_both_sides(self):
        self.assertEqual(
            humanize_change(line("max_teams_or_players: '250' ARROW '18'")),
            "changed capacity from 250 to 18",
        )
        self.assertEqual(
            humanize_change(line("registration_end_date: '2026-06-05' ARROW '2026-07-03'")),
            "changed registration closing date from 2026-06-05 to 2026-07-03",
        )

    def test_an_underscored_value_loses_its_underscores(self):
        self.assertEqual(
            humanize_change(line("tournament_tier: 'tier_3' ARROW 'tier_1'")),
            "changed tier from tier 3 to tier 1",
        )

    def test_an_empty_side_reads_as_set_or_cleared(self):
        self.assertEqual(humanize_change(line("prize_pool: '' ARROW '500'")),
                         "set prize pool to 500")
        self.assertEqual(humanize_change(line("prize_pool: '500' ARROW ''")), "cleared prize pool")

    def test_a_stage_or_group_keeps_the_thing_it_is_about(self):
        self.assertEqual(
            humanize_change(line("Stage 326 name: 'GROUP STAGE' ARROW 'SEMI FINALS'")),
            "changed Stage 326 name from GROUP STAGE to SEMI FINALS",
        )
        self.assertEqual(
            humanize_change(line("Group 652 match_count: 3 ARROW 5")),
            "changed Group 652 match count from 3 to 5",
        )

    def test_a_list_line_counts_what_was_added(self):
        self.assertEqual(humanize_change("Stages added: [328, 327]"), "added 2 stages")
        self.assertEqual(
            humanize_change("Stream channels added: ['https://youtube.com/live/m15uYD7Ye4I']"),
            "added 1 stream channel",
        )

    def test_a_line_with_no_pair_survives_as_itself(self):
        self.assertEqual(humanize_change("Group 652 maps changed"), "group 652 maps changed")

    def test_an_unrecognised_line_is_returned_rather_than_guessed_at(self):
        # The rule the module follows: never invent. A shape nobody anticipated degrades to
        # today's behaviour, which is showing it.
        self.assertIn("something nobody has written yet",
                      humanize_change("something nobody has written yet"))


class DescribeHistoryTests(TestCase):
    def test_the_empty_change_list_says_so_in_words(self):
        # 119 of the 221 JSON rows in the clone. It was printing `"changes": []` at a person.
        told = describe_history("edit_event", blob(206, []), {206: "DYNASTY CUP"})
        self.assertEqual(told["summary"], "Saved DYNASTY CUP (event 206) without changing anything")
        self.assertEqual(told["details"], [])

    def test_the_owners_own_row_reads_as_a_sentence(self):
        # The row he pasted, escaped emoji and all.
        name = "\U0001F525 ARE ESPORTS x AFC QUALIFIERS"
        told = describe_history("edit_event", blob(333, [
            "event_name: '" + name + "' ARROW '" + name + " S2'",
            "is_draft: 'True' ARROW 'False'",
        ]), {333: name + " S2"})
        self.assertNotIn("\\u", told["summary"])
        self.assertNotIn("changes", told["summary"])
        self.assertNotIn("{", told["summary"])
        self.assertIn("renamed it to", told["summary"])
        self.assertIn("published it", told["summary"])

    def test_a_long_change_list_is_summarised_and_kept_in_full(self):
        told = describe_history("edit_event", blob(205, [
            "event_status: 'upcoming' ARROW 'completed'",
            "tournament_tier: 'tier_3' ARROW 'tier_1'",
            "start_date: '2026-06-05' ARROW '2026-07-03'",
            "end_date: '2026-07-31' ARROW '2026-07-11'",
        ]), {})
        self.assertIn("and 2 more changes", told["summary"])
        # Nothing is thrown away: the expander gets every one of them.
        self.assertEqual(len(told["details"]), 4)

    def test_exactly_one_extra_change_is_singular(self):
        told = describe_history("edit_event", blob(205, [
            "is_draft: 'True' ARROW 'False'",
            "is_public: 'False' ARROW 'True'",
            "start_date: '2026-06-05' ARROW '2026-07-03'",
        ]), {})
        self.assertIn("and 1 more change", told["summary"])
        self.assertNotIn("1 more changes", told["summary"])

    def test_the_event_id_is_used_when_the_name_is_unknown(self):
        # A deleted event still has rows in the log. Saying "event 999" is honest; inventing a
        # name is not.
        told = describe_history("edit_event", blob(999, []), {})
        self.assertEqual(told["summary"], "Saved event 999 without changing anything")

    def test_a_row_that_is_already_english_is_left_alone(self):
        text = "Created event LEGACY QUALIFIERS (ID: 344)"
        self.assertEqual(describe_history("create_event", text)["summary"], text)

    def test_a_broken_json_row_is_shown_not_swallowed(self):
        broken = '{"event_id": 3, "changes": ['
        self.assertEqual(describe_history("edit_event", broken)["summary"], broken)

    def test_a_row_with_no_description_falls_back_to_the_action(self):
        self.assertEqual(describe_history("banned_team", "")["summary"], "Banned a team")


class ActionLabelTests(TestCase):
    def test_known_slugs_read_as_english(self):
        self.assertEqual(humanize_action("edit_event"), "Edited an event")
        self.assertEqual(humanize_action("edited_user_roles"), "Changed a user's roles")

    def test_an_unknown_slug_is_prettified_rather_than_dropped(self):
        # A slug added tomorrow must still read acceptably with no edit to the dictionary.
        self.assertEqual(humanize_action("archived_something_new"), "Archived something new")

    def test_an_empty_slug_does_not_crash(self):
        self.assertEqual(humanize_action(""), "Did something")

class ScopedListTests(TestCase):
    """A stage or group gaining or losing children. Written by diff_stages, so it has its own
    shape: "Stage 374: groups added [846]". Found during the Chrome walk on real rows, where it
    was falling through to the raw-text fallback and reading as "stage 374: groups added [846]"."""

    def test_it_says_what_moved_and_where(self):
        self.assertEqual(humanize_change("Stage 374: groups added [846]"),
                         "added 1 group to Stage 374")
        self.assertEqual(humanize_change("Stage 374: groups removed [843, 844]"),
                         "removed 2 groups from Stage 374")


class DetailsAreTheRemainderTests(TestCase):
    """`details` exists so a truncated sentence does not hide anything. When the sentence carried
    every clause there is nothing left to reveal, and printing the same two clauses twice, once as
    a sentence and once as a list under it, is what the walk showed on screen."""

    def test_an_untruncated_row_carries_no_details(self):
        told = describe_history("edit_event", blob(222, [
            "Stage 374: groups added [846]",
            "Stage 374: groups removed [843]",
        ]), {})
        self.assertEqual(told["details"], [])
        self.assertIn("added 1 group to Stage 374", told["summary"])

    def test_a_truncated_row_carries_the_whole_list(self):
        told = describe_history("edit_event", blob(222, [
            "is_draft: 'True' ARROW 'False'",
            "is_public: 'False' ARROW 'True'",
            "start_date: '2026-06-05' ARROW '2026-07-03'",
        ]), {})
        self.assertEqual(len(told["details"]), 3)

