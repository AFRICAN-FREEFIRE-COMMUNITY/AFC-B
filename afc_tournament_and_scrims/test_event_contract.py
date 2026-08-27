"""Tests for the single event contract (owner 2026-08-26, "one contract per domain object").

WHY THIS MODULE EXISTS
    `Event` carries 87 fields, and six functions used to list those fields BY HAND: create_event,
    edit_event, duplicate_event (79 keyword arguments typed out), plus the three readers. Adding one
    field therefore meant about a dozen edits, and a field forgotten in duplicate_event vanished
    from every duplicated event with no error at all.

WHAT IS COVERED HERE
    The machinery only: the role ladder, role resolution against a real event, serialisation, and
    the writer. The tests pass their own small field table rather than the real 64-row one, so a
    failure points at the mechanism instead of at a typo in the table. The real table is checked by
    test_event_contract_golden.py (payloads must not change) and by
    test_event_contract_completeness.py (no field may go unaccounted for).

Run: AFC_TEST_DB_NAME=test_afc_contract python manage.py test afc_tournament_and_scrims.test_event_contract
"""
import json
from datetime import date, timedelta

from django.test import TestCase

from afc_auth.models import Roles, SessionToken, User, UserProfile, UserRoles
from afc_tournament_and_scrims import event_contract as ec
from afc_tournament_and_scrims.models import Event


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u


def _event(creator):
    # Field values copied from test_required_connections._event on purpose: several of these
    # columns are max_length=10, so "battle_royale" is a DataError rather than a choice mismatch.
    return Event.objects.create(
        event_name="Ladder Cup", competition_type="tournament", participant_type="solo",
        event_type="online", event_mode="single", max_teams_or_players=12,
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today(), registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )


def _demo_clean_list(raw):
    """Stand-in for views._clean_required_connections, reduced to the part under test.

    Note what it does NOT do: it never asks "is this list non-empty". It asks "did this parse as a
    list", which is the whole difference between the fixed version and the one that took event
    creation down on 2026-08-26.
    """
    if isinstance(raw, list):
        return raw
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError("required_connections must be a list")
    if not isinstance(parsed, list):
        raise ValueError("required_connections must be a list")
    return parsed


# ── the ladder itself ─────────────────────────────────────────────────────────────────────────
class RoleLadderTests(TestCase):
    def test_ladder_is_ordered_least_to_most(self):
        # A viewer at a given rung satisfies every rung at or below it.
        self.assertTrue(ec.satisfies(ec.ADMIN, ec.PUBLIC))
        self.assertTrue(ec.satisfies(ec.PLAYER, ec.PUBLIC))
        self.assertTrue(ec.satisfies(ec.PUBLIC, ec.PUBLIC))
        self.assertTrue(ec.satisfies(ec.ORGANIZER, ec.PLAYER))

    def test_ladder_does_not_run_upwards(self):
        self.assertFalse(ec.satisfies(ec.PUBLIC, ec.PLAYER))
        self.assertFalse(ec.satisfies(ec.PLAYER, ec.ADMIN))
        self.assertFalse(ec.satisfies(ec.ORGANIZER, ec.ADMIN))

    def test_nobody_is_satisfied_by_no_one(self):
        # NOBODY marks a field that is never exposed, or never writable through the API. It is not
        # a rung, which is why it is kept out of the rank table.
        self.assertFalse(ec.satisfies(ec.ADMIN, ec.NOBODY))
        self.assertFalse(ec.satisfies(ec.NOBODY, ec.NOBODY))
        self.assertFalse(ec.satisfies(ec.NOBODY, ec.PUBLIC))


# ── who a viewer is, for THIS event ───────────────────────────────────────────────────────────
class RoleResolutionTests(TestCase):
    def setUp(self):
        self.owner = _user("ladderowner")
        self.event = _event(self.owner)

    def test_anonymous_viewer_is_public(self):
        self.assertEqual(ec.role_of(None, self.event), ec.PUBLIC)

    def test_signed_in_stranger_is_player(self):
        stranger = _user("ladderstranger")
        self.assertEqual(ec.role_of(stranger, self.event), ec.PLAYER)

    def test_the_events_creator_is_an_organizer(self):
        # Mirrors views._is_event_creator: the person who made the event can always manage it,
        # including a native event with no owning organization, where org_can_event is admin-only.
        self.assertEqual(ec.role_of(self.owner, self.event), ec.ORGANIZER)

    def test_afc_staff_is_admin(self):
        staff = _user("ladderstaff", role="admin")
        self.assertEqual(ec.role_of(staff, self.event), ec.ADMIN)

    def test_granular_event_admin_is_admin(self):
        # UserRoles points at a Roles row and the role NAME lives on the related row, so the lookup
        # is role__role_name, not role_name. views._is_event_admin carries a comment about the
        # older inline gates in that file getting this wrong; reusing the helper avoids repeating
        # the mistake here.
        u = _user("laddergranular")
        role, _ = Roles.objects.get_or_create(role_name="event_admin")
        UserRoles.objects.create(user=u, role=role)
        self.assertEqual(ec.role_of(u, self.event), ec.ADMIN)


# ── reading ───────────────────────────────────────────────────────────────────────────────────
class SerializeTests(TestCase):
    def setUp(self):
        self.owner = _user("serowner")
        self.event = _event(self.owner)

    def test_public_viewer_gets_only_public_fields(self):
        table = [
            ec.Field("event_name", read=ec.PUBLIC),
            ec.Field("timezone", read=ec.PLAYER),
        ]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table)
        self.assertEqual(list(out.keys()), ["event_name"])
        self.assertEqual(out["event_name"], "Ladder Cup")

    def test_player_viewer_gets_public_and_player_fields(self):
        table = [
            ec.Field("event_name", read=ec.PUBLIC),
            ec.Field("timezone", read=ec.PLAYER),
        ]
        out = ec.serialize_event(self.event, role=ec.PLAYER, table=table)
        self.assertEqual(list(out.keys()), ["event_name", "timezone"])

    def test_a_renamed_key_reads_from_its_source_attribute(self):
        # The real case this exists for: get_event_details_for_admin emits the
        # registration_open_date COLUMN under the key "registration_start_date" (views.py:11247).
        table = [ec.Field("registration_start_date", read=ec.ADMIN, source="registration_open_date")]
        out = ec.serialize_event(self.event, role=ec.ADMIN, table=table)
        self.assertEqual(out["registration_start_date"], self.event.registration_open_date)

    def test_a_computed_key_runs_its_getter(self):
        table = [ec.Field("organization_name", read=ec.PUBLIC,
                          get=lambda e, ctx: e.organization.name if e.organization_id else None)]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table)
        self.assertIsNone(out["organization_name"])

    def test_a_getter_can_read_a_precomputed_value_from_extra(self):
        # Endpoints already compute things like the published sponsors and the capacity snapshot
        # with their own queries. `extra` hands those over so the contract never recomputes them.
        table = [ec.Field("event_status", read=ec.PUBLIC,
                          get=lambda e, ctx: ctx["extra"]["event_status"])]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table,
                                 extra={"event_status": "ongoing"})
        self.assertEqual(out["event_status"], "ongoing")

    def test_fields_argument_restricts_the_output(self):
        table = [ec.Field("event_name", read=ec.PUBLIC), ec.Field("event_mode", read=ec.PUBLIC)]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table, fields=["event_mode"])
        self.assertEqual(list(out.keys()), ["event_mode"])

    def test_declaration_order_is_the_output_order(self):
        # Key ORDER matters here only because the golden files compare exactly, which is what makes
        # a conversion provable rather than hopeful.
        table = [ec.Field("event_mode", read=ec.PUBLIC), ec.Field("event_name", read=ec.PUBLIC)]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table)
        self.assertEqual(list(out.keys()), ["event_mode", "event_name"])

    def test_fields_argument_does_not_change_the_order(self):
        table = [ec.Field("event_mode", read=ec.PUBLIC), ec.Field("event_name", read=ec.PUBLIC)]
        out = ec.serialize_event(self.event, role=ec.PUBLIC, table=table,
                                 fields=["event_name", "event_mode"])
        self.assertEqual(list(out.keys()), ["event_mode", "event_name"])

    def test_a_nobody_field_is_never_emitted_even_to_admin(self):
        table = [ec.Field("overlay_token", read=ec.NOBODY)]
        out = ec.serialize_event(self.event, role=ec.ADMIN, table=table)
        self.assertEqual(out, {})

    def test_role_is_resolved_from_the_viewer_when_not_given(self):
        stranger = _user("serstranger")
        table = [ec.Field("event_name", read=ec.PUBLIC), ec.Field("timezone", read=ec.PLAYER)]
        out = ec.serialize_event(self.event, viewer=stranger, table=table)
        self.assertEqual(sorted(out.keys()), ["event_name", "timezone"])


# ── writing ───────────────────────────────────────────────────────────────────────────────────
class WriteTests(TestCase):
    def setUp(self):
        self.owner = _user("writeowner")
        self.event = _event(self.owner)
        self.table = [
            ec.Field("event_name", read=ec.PUBLIC, write=ec.ORGANIZER),
            ec.Field("tournament_tier", read=ec.PUBLIC, write=ec.ADMIN),
            ec.Field("event_id", read=ec.PUBLIC, write=ec.NOBODY),
            ec.Field("required_connections", read=ec.PUBLIC, write=ec.ORGANIZER,
                     clean=_demo_clean_list),
        ]

    def test_a_key_that_is_absent_is_left_alone(self):
        # A partial edit must stay partial. edit_event guards 45 fields with `in request.data`
        # today, and that is the behaviour being reproduced.
        before = self.event.event_name
        changed = ec.apply_event_writes(self.event, {}, role=ec.ORGANIZER, table=self.table)
        self.assertEqual(changed, [])
        self.assertEqual(self.event.event_name, before)

    def test_a_writable_key_is_applied_and_reported(self):
        changed = ec.apply_event_writes(self.event, {"event_name": "Renamed"},
                                        role=ec.ORGANIZER, table=self.table)
        self.assertEqual(changed, ["event_name"])
        self.assertEqual(self.event.event_name, "Renamed")

    def test_an_unchanged_value_is_not_reported_as_changed(self):
        changed = ec.apply_event_writes(self.event, {"event_name": self.event.event_name},
                                        role=ec.ORGANIZER, table=self.table)
        self.assertEqual(changed, [])

    def test_a_key_above_the_actors_rung_is_refused(self):
        with self.assertRaises(ec.WriteRefused) as caught:
            ec.apply_event_writes(self.event, {"tournament_tier": "Tier 1"},
                                  role=ec.ORGANIZER, table=self.table)
        self.assertEqual(caught.exception.field, "tournament_tier")

    def test_a_refused_write_does_not_apply_anything(self):
        # The refusal must not leave the event half-written, or a 400 would still have mutated it.
        before = self.event.event_name
        with self.assertRaises(ec.WriteRefused):
            ec.apply_event_writes(self.event,
                                  {"event_name": "Should Not Stick", "tournament_tier": "Tier 1"},
                                  role=ec.ORGANIZER, table=self.table)
        self.assertEqual(self.event.event_name, before)

    def test_a_never_writable_key_is_IGNORED_rather_than_refused(self):
        """write=NOBODY is structural, not a permission boundary, so it must not 400.

        Ordinary traffic carries these keys: edit_event is looked up BY event_id, so every edit
        request contains it. Refusing it rejected every edit the first time this was wired up.
        A field above the actor's RUNG is different and is still refused loudly, because that one
        is a real permission mistake worth surfacing (see the test below).
        """
        before = self.event.event_id
        changed = ec.apply_event_writes(self.event, {"event_id": 999}, role=ec.ADMIN,
                                        table=self.table)
        self.assertEqual(changed, [])
        self.assertEqual(self.event.event_id, before, "a NOBODY field was written anyway")

    def test_an_empty_list_CLEARS_rather_than_raising(self):
        # THE 2026-08-26 OUTAGE, pinned at the contract level so it cannot come back through a new
        # field. Clearing the picker posts an empty list, which arrives from multipart FormData as
        # the string "[]". The old guard read `if not raw: raise` and refused it, so the
        # requirement could be switched on and never off, and an untouched picker blocked event
        # creation outright.
        self.event.required_connections = ["google"]
        changed = ec.apply_event_writes(self.event, {"required_connections": "[]"},
                                        role=ec.ORGANIZER, table=self.table)
        self.assertEqual(changed, ["required_connections"])
        self.assertEqual(self.event.required_connections, [])

    def test_a_real_empty_list_also_clears(self):
        self.event.required_connections = ["google"]
        changed = ec.apply_event_writes(self.event, {"required_connections": []},
                                        role=ec.ORGANIZER, table=self.table)
        self.assertEqual(changed, ["required_connections"])
        self.assertEqual(self.event.required_connections, [])

    def test_clean_rejects_junk_and_names_the_field(self):
        with self.assertRaises(ec.WriteRefused) as caught:
            ec.apply_event_writes(self.event, {"required_connections": "not json"},
                                  role=ec.ORGANIZER, table=self.table)
        self.assertEqual(caught.exception.field, "required_connections")

    def test_role_is_resolved_from_the_actor_when_not_given(self):
        # The creator resolves to ORGANIZER, so this write is allowed without passing role=.
        changed = ec.apply_event_writes(self.event, {"event_name": "By The Creator"},
                                        actor=self.owner, table=self.table)
        self.assertEqual(changed, ["event_name"])
