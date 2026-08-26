"""Every Event column must be accounted for by the contract, one way or another.

This is tools/check_event_contract.py wired into the suite, so drift fails in CI without anybody
remembering to run the script. Both read the same helpers in event_contract, so the two cannot
disagree about what "accounted for" means.

THE RULE IS NOT "every field must be exposed". 23 of Event's 87 columns are internal machinery
(broadcast targeting, the overlay token, check-in windows, draft state, the currency working
values) and are correctly invisible. The rule is that every column is either declared in
EVENT_FIELDS, exposed through a derived key, or named as internal, so a NEW column cannot be
half-added and forgotten.

WHY IT EXISTS: duplicate_event dropped seven configuration fields, the Discord registration gate
among them, for an unknown length of time. Nothing failed, because a missing keyword argument just
takes the column default. Prose in a rule did not stop that happening twice. A check that fails
will.

Run: AFC_TEST_DB_NAME=test_afc_contract python manage.py test afc_tournament_and_scrims.test_event_contract_completeness
"""
from django.test import SimpleTestCase

from afc_tournament_and_scrims import event_contract as ec
from afc_tournament_and_scrims.models import Event


class ContractCompletenessTests(SimpleTestCase):
    """No database needed: these read the model definition and the contract, not any rows."""

    def test_every_event_column_is_accounted_for(self):
        unaccounted = ec.unaccounted_fields()
        self.assertEqual(
            unaccounted, [],
            "These Event columns are in none of EVENT_FIELDS, DERIVED_FROM or INTERNAL_FIELDS. "
            "Add each to whichever it belongs in: " + ", ".join(unaccounted),
        )

    def test_no_output_key_is_declared_twice(self):
        names = [f.name for f in ec.EVENT_FIELDS]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(duplicates, [], f"declared more than once, so one silently wins: {duplicates}")

    def test_internal_fields_are_not_also_declared(self):
        both = sorted(ec.INTERNAL_FIELDS & {f.attr for f in ec.EVENT_FIELDS})
        self.assertEqual(both, [], f"listed as internal AND declared: {both}")

    def test_every_declared_field_resolves_on_the_model(self):
        """A `source=` typo would otherwise only surface as an AttributeError at request time.

        Resolving means a column name, a foreign key's attname (organization_id), or a property on
        the class (roster_edit_open). All three are legitimate; checking only column names reports
        those two as dangling.
        """
        resolvable = (
            {f.name for f in Event._meta.concrete_fields}
            | {f.attname for f in Event._meta.concrete_fields}
            | {name for name in dir(Event) if not name.startswith("_")}
        )
        dangling = sorted(
            f"{f.name} -> Event.{f.attr}"
            for f in ec.EVENT_FIELDS
            if f.get is None and f.attr not in resolvable
        )
        self.assertEqual(dangling, [], f"declared fields pointing at nothing: {dangling}")

    def test_the_admin_subsets_only_name_declared_fields(self):
        declared = {f.name for f in ec.EVENT_FIELDS}
        for label, subset in (("ADMIN_OVERVIEW_FIELDS", ec.ADMIN_OVERVIEW_FIELDS),
                              ("ADMIN_TIMELINE_FIELDS", ec.ADMIN_TIMELINE_FIELDS)):
            unknown = sorted(set(subset) - declared)
            self.assertEqual(unknown, [], f"{label} names undeclared fields: {unknown}")

    def test_duplicate_excluded_only_names_real_columns(self):
        """A stale name in the exclusion list excludes nothing, and reads as if it does."""
        model_fields = {f.name for f in Event._meta.concrete_fields}
        stale = sorted(ec.DUPLICATE_EXCLUDED - model_fields)
        self.assertEqual(stale, [], f"DUPLICATE_EXCLUDED names non-columns: {stale}")

    def test_the_discord_gate_is_carried_by_duplication(self):
        """Guards the specific fields that were silently dropped, by name.

        The general rule is enforced by test_duplicate_event_fields against a real duplication.
        This is the cheap static half: if somebody adds these to DUPLICATE_EXCLUDED, they have to
        delete this test to do it, which is a conversation rather than an accident.
        """
        carried = set(ec.duplicate_field_names())
        for name in ("require_discord", "discord_server_id", "discord_invite_link", "timezone",
                     "waitlist_mode", "auto_seed_on_start", "auto_seed_trigger"):
            self.assertIn(name, carried, f"a duplicate would no longer inherit {name}")
