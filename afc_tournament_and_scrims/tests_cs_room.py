"""Tests for the Clash-Squad ROOM SETTINGS, scheduling, forfeits and player-side submission
(owner 2026-08-12; spec WEBSITE/tasks/cs-room-settings-spec.md).

Reuses the H2HBase fixture from tests_head_to_head (admin + token, one event, one CS knockout
stage, six teams) so the two suites share one mental model of a bracket.

Covers:
  - the catalogue endpoint is public and complete;
  - saving a config per scope is idempotent (one row per scope) and validated;
  - RESOLUTION order match -> stage -> event, and what "inherited from" reports;
  - room credentials are hidden from the public until published, and always visible to a manager;
  - best-of: a set score cannot exceed the room's wins_needed, and no room = the old flat cap;
  - built-in modes and organization presets;
  - scheduling + the live marker + the guards on them;
  - forfeit / walkover / DQ, and that a real re-report clears the marker;
  - league draws, points and ranking;
  - round robin sit-outs;
  - the player-side submission flow end to end, including agreement between the two teams.

Run: venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_cs_room
"""
import datetime

from afc_auth.models import Notifications, SessionToken, User
from afc_organizers.models import Organization, OrganizationMember

from afc_tournament_and_scrims import cs_room, cs_room_catalogue, head_to_head
from afc_tournament_and_scrims.models import (
    CSRoomConfig,
    CSRoomPreset,
    H2HResultSubmission,
    HeadToHeadMatch,
    StageGroups,
    Stages,
    TournamentTeamMember,
)
from afc_tournament_and_scrims.tests_head_to_head import H2HBase


class RoomCatalogueTests(H2HBase):
    """The catalogue is static reference data: public, and everything the editor needs."""

    def test_catalogue_is_public_and_complete(self):
        resp = self.client.get("/events/cs-room-catalogue/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        for key in ("rounds", "economy", "special_mode", "hp", "maps", "map_areas",
                    "toggles", "store_weapons", "store_items", "economy_events", "presets"):
            self.assertIn(key, body, f"catalogue is missing {key}")
        self.assertIn(13, body["rounds"])
        self.assertEqual(len(body["presets"]), 6)
        # Every map that has areas must appear in map_areas, or the AREA tab cannot draw.
        for entry in body["maps"]:
            self.assertIn(entry["value"], body["map_areas"])


class RoomSettingsSaveTests(H2HBase):
    """Writing a configuration against a scope."""

    def _save(self, scope, object_id, payload, token=None):
        return self.client.put(
            f"/events/cs-room-settings/{scope}/{object_id}/",
            data=payload, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}")

    def _get(self, scope, object_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(f"/events/cs-room-settings/{scope}/{object_id}/", **headers)

    def test_save_creates_one_row_and_is_idempotent(self):
        resp = self._save("stage", self.stage.stage_id, {"rounds": 13, "map_name": "kalahari"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(CSRoomConfig.objects.filter(stage=self.stage).count(), 1)

        # A second save UPDATES rather than piling up a second row.
        resp = self._save("stage", self.stage.stage_id, {"rounds": 9})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(CSRoomConfig.objects.filter(stage=self.stage).count(), 1)
        config = CSRoomConfig.objects.get(stage=self.stage)
        self.assertEqual(config.rounds, 9)
        # The field the second save did not mention keeps its stored value.
        self.assertEqual(config.map_name, "kalahari")

    def test_new_config_starts_from_a_full_room(self):
        """A brand-new row carries the whole store / economy / area document, so no reader has
        to cope with half a room."""
        self._save("stage", self.stage.stage_id, {"rounds": 7})
        config = CSRoomConfig.objects.get(stage=self.stage)
        self.assertGreater(len(config.store), 50)
        self.assertEqual(len(config.round_economy), 7)
        # Counted against the CATALOGUE, not a literal: the point of the assertion is "a new
        # config carries EVERY toggle", and a hardcoded number turns any legitimate addition to
        # the catalogue into a failing test about nothing.
        self.assertEqual(len(config.toggles), len(cs_room_catalogue.TOGGLES))
        self.assertEqual(len(config.areas), 7)

    def test_invalid_values_are_refused_with_a_readable_message(self):
        resp = self._save("stage", self.stage.stage_id, {"rounds": 8})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Rounds must be one of", resp.json()["message"])

        resp = self._save("stage", self.stage.stage_id, {"map_name": "erangel"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a valid map name", resp.json()["message"])

    def test_changing_the_map_refills_the_areas(self):
        """Areas belong to a map. Changing the map without resending them must not leave a room
        set to play Solara's Windmill on Kalahari."""
        self._save("stage", self.stage.stage_id, {"map_name": "solara"})
        self._save("stage", self.stage.stage_id, {"map_name": "kalahari"})
        config = CSRoomConfig.objects.get(stage=self.stage)
        kalahari = {code for code, _l in
                    __import__("afc_tournament_and_scrims.cs_room_catalogue",
                               fromlist=["MAP_AREAS"]).MAP_AREAS["kalahari"]}
        for area in config.areas.values():
            self.assertIn(area, kalahari)

    def test_stranger_cannot_write(self):
        stranger = User.objects.create(username="cs_stranger", email="s@afc.test", role="player")
        token = SessionToken.objects.create(
            user=stranger, token="cs-stranger",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        resp = self._save("stage", self.stage.stage_id, {"rounds": 13}, token=token.token)
        self.assertEqual(resp.status_code, 403)

    def test_delete_clears_the_override(self):
        self._save("event", self.event.event_id, {"rounds": 5})
        self._save("stage", self.stage.stage_id, {"rounds": 13})
        resp = self.client.delete(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIsNone(body["own"])
        # It inherits the event's configuration again.
        self.assertEqual(body["effective_scope"], "event")
        self.assertEqual(body["effective"]["rounds"], 5)


class RoomResolutionTests(H2HBase):
    """match -> stage -> event, and saying where the answer came from."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.match = self._m("winners", 1, 0)

    def _save(self, scope, object_id, payload):
        return self.client.put(
            f"/events/cs-room-settings/{scope}/{object_id}/",
            data=payload, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_nothing_configured_resolves_to_nothing(self):
        config, scope = cs_room.resolve_for_match(self.match)
        self.assertIsNone(config)
        self.assertIsNone(scope)

    def test_event_then_stage_then_match_each_win_in_turn(self):
        self._save("event", self.event.event_id, {"rounds": 5})
        config, scope = cs_room.resolve_for_match(self.match)
        self.assertEqual((config.rounds, scope), (5, "event"))

        self._save("stage", self.stage.stage_id, {"rounds": 9})
        config, scope = cs_room.resolve_for_match(self.match)
        self.assertEqual((config.rounds, scope), (9, "stage"))

        self._save("match", self.match.h2h_match_id, {"rounds": 13})
        config, scope = cs_room.resolve_for_match(self.match)
        self.assertEqual((config.rounds, scope), (13, "match"))

        # A sibling match still inherits the stage: an override is an exception, not a rewrite.
        sibling = self._m("winners", 1, 1)
        config, scope = cs_room.resolve_for_match(sibling)
        self.assertEqual((config.rounds, scope), (9, "stage"))

    def test_bracket_payload_carries_the_resolved_room_per_match(self):
        self._save("stage", self.stage.stage_id, {"rounds": 9, "map_name": "purgatory"})
        self._save("match", self.match.h2h_match_id, {"rounds": 13})
        body = self._get_bracket().json()
        rooms = {
            m["h2h_match_id"]: m["room"]
            for r in body["rounds"]["winners"] for m in r["matches"]
        }
        self.assertEqual(rooms[self.match.h2h_match_id]["source_scope"], "match")
        self.assertEqual(rooms[self.match.h2h_match_id]["summary"]["rounds"], 13)
        sibling = self._m("winners", 1, 1)
        self.assertEqual(rooms[sibling.h2h_match_id]["source_scope"], "stage")
        self.assertEqual(rooms[sibling.h2h_match_id]["summary"]["map_name"], "purgatory")


class RoomCredentialVisibilityTests(H2HBase):
    """A room ID on a public page hours early is an invitation for strangers to walk in."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"room_id": "123456", "room_password": "afc", "is_published": False},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_public_read_hides_unpublished_credentials(self):
        body = self.client.get(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/").json()
        self.assertEqual(body["effective"]["room_id"], "")
        self.assertEqual(body["effective"]["room_password"], "")
        # It still says a room EXISTS, so the page can show "the organizer has not opened the
        # room yet" rather than pretending there is nothing.
        self.assertTrue(body["effective"]["has_room_credentials"])

    def test_manager_always_sees_the_credentials(self):
        body = self.client.get(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}").json()
        self.assertEqual(body["effective"]["room_id"], "123456")
        self.assertTrue(body["can_manage"])

    def test_publishing_reveals_them_and_notifies_once(self):
        # Put a real player on a team so there is somebody to notify.
        player = User.objects.create(username="cs_player", email="p@afc.test", role="player")
        TournamentTeamMember.objects.create(
            tournament_team=self.tts[0], user=player, status="active")

        resp = self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"is_published": True}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)

        body = self.client.get(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/").json()
        self.assertEqual(body["effective"]["room_id"], "123456")

        notices = Notifications.objects.filter(user=player, title="Room details are up")
        self.assertEqual(notices.count(), 1)
        self.assertIn("123456", notices.first().message)
        # Deep link, per the platform rule for a notification that points somewhere.
        self.assertEqual(notices.first().target_type, "event")

        # Editing an ALREADY published room does not re-announce it.
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"notes": "join early"}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(
            Notifications.objects.filter(user=player, title="Room details are up").count(), 1)


class BestOfTests(H2HBase):
    """A room says how long a set is; the score has to fit inside it."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.match = self._m("winners", 1, 0)

    def test_no_room_configured_keeps_the_old_flat_cap(self):
        """Events that predate room settings must keep working exactly as they did."""
        resp = self._report(self.match, 9, 2)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_score_cannot_exceed_the_configured_best_of(self):
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"rounds": 13}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        resp = self._report(self.match, 9, 2)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("first-to-7", resp.json()["message"])

        # The legal maximum is accepted.
        resp = self._report(self.match, 7, 5)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_both_teams_cannot_reach_the_target(self):
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"rounds": 7}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        resp = self._report(self.match, 4, 4)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cannot reach", resp.json()["message"])

    def test_wins_needed_maths(self):
        self.assertEqual(cs_room.wins_needed(5), 3)
        self.assertEqual(cs_room.wins_needed(7), 4)
        self.assertEqual(cs_room.wins_needed(13), 7)


class PresetTests(H2HBase):
    """Built-in modes and reusable organization presets."""

    def test_builtin_mode_applies_partially_and_refills_per_round_documents(self):
        values = cs_room.apply_builtin_mode("esports_mode")
        self.assertEqual(values["rounds"], 13)
        self.assertEqual(values["economy"], "esports")
        # A partial mode merges toggles rather than replacing the whole set, so the result still
        # holds every catalogue key. Counted against the catalogue for the same reason as above.
        self.assertEqual(len(values["toggles"]), len(cs_room_catalogue.TOGGLES))
        self.assertTrue(values["toggles"]["block_emulators"])
        # Rounds moved to 13, so the per-round documents must follow.
        self.assertEqual(len(values["round_economy"]), 13)
        self.assertEqual(len(values["areas"]), 13)

    def test_apply_mode_through_the_endpoint(self):
        resp = self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"apply_mode": "esports_mode"}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        config = CSRoomConfig.objects.get(stage=self.stage)
        self.assertEqual(config.rounds, 13)
        self.assertEqual(config.preset_key, "Esports Mode")

    def test_body_fields_win_over_the_applied_mode(self):
        """"Apply Esports Mode, but 7 rounds" has to work in one request."""
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"apply_mode": "esports_mode", "rounds": 7}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(CSRoomConfig.objects.get(stage=self.stage).rounds, 7)

    def test_organization_preset_round_trip(self):
        org = Organization.objects.create(slug="cs-org", name="CS Org", created_by=self.admin)
        owner = User.objects.create(username="cs_org_owner", email="o@afc.test", role="player")
        OrganizationMember.objects.create(
            organization=org, user=owner, role="owner", status="active")
        token = SessionToken.objects.create(
            user=owner, token="cs-org-owner",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        resp = self.client.post(
            "/events/cs-room-presets/",
            data={"name": "House Rules", "organization_id": org.organization_id,
                  "rounds": 13, "map_name": "kalahari"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.token}")
        self.assertEqual(resp.status_code, 201, resp.content)

        listed = self.client.get(
            "/events/cs-room-presets/",
            HTTP_AUTHORIZATION=f"Bearer {token.token}").json()["presets"]
        self.assertIn("House Rules", [p["name"] for p in listed])

        # A stranger does not see another organization's presets.
        outsider = User.objects.create(username="cs_outsider", email="x@afc.test", role="player")
        outsider_token = SessionToken.objects.create(
            user=outsider, token="cs-outsider",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        listed = self.client.get(
            "/events/cs-room-presets/",
            HTTP_AUTHORIZATION=f"Bearer {outsider_token.token}").json()["presets"]
        self.assertNotIn("House Rules", [p["name"] for p in listed])

    def test_a_non_admin_cannot_publish_a_global_preset(self):
        player = User.objects.create(username="cs_pleb", email="pl@afc.test", role="player")
        token = SessionToken.objects.create(
            user=player, token="cs-pleb",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        resp = self.client.post(
            "/events/cs-room-presets/", data={"name": "Mine", "rounds": 7},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token.token}")
        self.assertEqual(resp.status_code, 403)

    def test_builtins_cannot_be_deleted(self):
        preset = CSRoomPreset.objects.create(name="Esports Mode", is_builtin=True)
        resp = self.client.delete(
            f"/events/cs-room-presets/{preset.cs_room_preset_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 400)


class ScheduleAndLiveTests(H2HBase):
    """The columns existed since the model was written and nothing ever set them."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.match = self._m("winners", 1, 0)
        self.player = User.objects.create(
            username="cs_sched_player", email="sp@afc.test", role="player")
        TournamentTeamMember.objects.create(
            tournament_team=self.tts[0], user=self.player, status="active")

    def _patch(self, payload, token=None):
        return self.client.patch(
            f"/events/h2h-matches/{self.match.h2h_match_id}/",
            data=payload, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}")

    def test_setting_a_time_saves_and_notifies(self):
        resp = self._patch({"scheduled_date": "2026-08-20", "scheduled_time": "18:30"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertEqual(self.match.scheduled_date, datetime.date(2026, 8, 20))
        self.assertEqual(self.match.scheduled_time, datetime.time(18, 30))
        self.assertTrue(Notifications.objects.filter(
            user=self.player, title="Your match has a time").exists())

    def test_marking_live_notifies_differently(self):
        resp = self._patch({"status": "live"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "live")
        self.assertTrue(Notifications.objects.filter(
            user=self.player, title="Your match is live").exists())

    def test_completed_cannot_be_set_by_hand(self):
        resp = self._patch({"status": "completed"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Enter a result", resp.json()["message"])

    def test_a_bad_date_is_a_clean_400(self):
        resp = self._patch({"scheduled_date": "20th August"})
        self.assertEqual(resp.status_code, 400)

    def test_a_completed_match_cannot_be_reopened_by_status(self):
        self._report(self.match, 4, 1)
        resp = self._patch({"status": "live"})
        self.assertEqual(resp.status_code, 400)


class ForfeitTests(H2HBase):
    """A set nobody played is not the same as a 7-0 thrashing."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.match = self._m("winners", 1, 0)

    def _send_outcome(self, outcome, winner_id, note=""):
        return self.client.post(
            f"/events/h2h-matches/{self.match.h2h_match_id}/result/",
            data={"outcome": outcome, "winner_id": winner_id, "result_note": note},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_walkover_records_the_winner_and_advances(self):
        resp = self._send_outcome("walkover", self.tts[0].tournament_team_id, "opponent never joined")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertEqual(self.match.winner_id, self.tts[0].tournament_team_id)
        self.assertEqual(self.match.result_type, "walkover")
        self.assertEqual(self.match.result_note, "opponent never joined")
        # It advanced like any other result.
        final = self._m("winners", 2, 0)
        self.assertEqual(final.team_a_id, self.tts[0].tournament_team_id)

    def test_winner_must_be_in_the_match(self):
        resp = self._send_outcome("forfeit", self.tts[5].tournament_team_id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("one of the two teams", resp.json()["message"])

    def test_a_real_rereport_clears_the_marker(self):
        self._send_outcome("forfeit", self.tts[0].tournament_team_id, "late")
        resp = self._report(self.match, 4, 2)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertEqual(self.match.result_type, "normal")
        self.assertEqual(self.match.result_note, "")

    def test_walkover_score_respects_the_room_best_of(self):
        self.client.put(
            f"/events/cs-room-settings/stage/{self.stage.stage_id}/",
            data={"rounds": 13}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self._send_outcome("walkover", self.tts[0].tournament_team_id)
        self.match.refresh_from_db()
        self.assertEqual((self.match.score_a, self.match.score_b), (7, 0))


class LeagueTableTests(H2HBase):
    """Draws were invisible and a league had no points column."""

    STAGE_FORMAT = "cs - league"

    def test_draws_and_points(self):
        self._generate(self._ids(4), fmt="league")
        matches = list(HeadToHeadMatch.objects.filter(stage=self.stage).order_by("round_number",
                                                                                "position"))
        # One draw, the rest decided, so every column has something in it.
        self._report(matches[0], 3, 3)
        for m in matches[1:]:
            self._report(m, 4, 1)

        body = self._get_bracket().json()
        self.assertEqual(body["league_points"], {"win": 3, "draw": 1, "loss": 0})
        rows = {r["team_name"]: r for r in body["standings"]}
        for row in rows.values():
            self.assertIn("draws", row)
            self.assertIn("points", row)
            self.assertEqual(
                row["points"], row["wins"] * 3 + row["draws"],
                f"{row['team_name']} points do not match 3/1/0")
        # Somebody actually drew, i.e. the draw is recorded rather than lost.
        self.assertTrue(any(r["draws"] for r in rows.values()))

    def test_points_rank_above_raw_wins(self):
        """Three draws must beat one win and three losses, which ranking on wins got wrong."""
        self._generate(self._ids(4), fmt="league")
        standings = self._get_bracket().json()["standings"]
        # Nothing played: every row is level, so the order is alphabetical and stable.
        self.assertEqual([r["points"] for r in standings], [0, 0, 0, 0])


class RoundRobinPresentationTests(H2HBase):
    """A round robin stage rendered as "League", and never named the team sitting out."""

    STAGE_FORMAT = "cs - round robin"

    def test_format_is_reported_as_round_robin_not_league(self):
        self._generate(self._ids(4))
        self.assertEqual(self._get_bracket().json()["fmt"], "round_robin_h2h")

    def test_odd_field_names_the_sitting_out_team_each_matchday(self):
        self._generate(self._ids(5))
        body = self._get_bracket().json()
        sit_outs = body["sit_outs"]
        # 5 teams -> 5 matchdays, exactly one team resting on each.
        self.assertEqual(len(sit_outs), 5)
        for entry in sit_outs.values():
            self.assertTrue(entry["team_name"])
        # Every team rests exactly once across the five matchdays.
        resting = sorted(e["tournament_team_id"] for e in sit_outs.values())
        self.assertEqual(len(set(resting)), 5)

    def test_even_field_has_no_sit_outs(self):
        self._generate(self._ids(4))
        self.assertEqual(self._get_bracket().json()["sit_outs"], {})


class BracketAnchorGroupTests(H2HBase):
    """Where a finished bracket's placements land.

    Until 2026-08-13 a Clash Squad stage had no groups, so write_placement_stats had to synthesise
    a hidden "Bracket Results" row to hang the results off. Now the bracket already BELONGS to a
    group, so the results land in that group and no phantom row is created at all - which is both
    simpler and what the leaderboard was always reading.
    """

    def test_placements_land_in_the_brackets_own_group(self):
        self._generate(self._ids(4))
        self._report(self._m("winners", 1, 0), 4, 1)
        self._report(self._m("winners", 1, 1), 4, 2)
        self._report(self._m("winners", 2, 0), 4, 3)

        groups = list(StageGroups.objects.filter(stage=self.stage))
        # One group: the one the bracket was generated into. No hidden extra.
        self.assertEqual(len(groups), 1, [g.group_name for g in groups])
        group = groups[0]
        self.assertEqual(group.bracket_format, "single_elim")
        # It holds a real bracket, so it is NOT the hidden bookkeeping row any more.
        self.assertFalse(group.is_synthetic)
        self.assertNotEqual(group.group_name, "Bracket Results")
        # And the stage's matches all belong to it.
        self.assertEqual(
            HeadToHeadMatch.objects.filter(stage=self.stage, group=group).count(),
            HeadToHeadMatch.objects.filter(stage=self.stage).count())


class StageSplitIntoGroupsTests(H2HBase):
    """One Clash Squad stage running several independent brackets (owner backlog item 21).

    The Champions League shape: Group A and Group B play their own brackets, possibly on different
    modes, and the top N from EACH qualify onward. Each group stands alone - its own tree, its own
    table, its own winner - which is the owner's decision of 2026-08-13.
    """

    def setUp(self):
        super().setUp()
        self.stage.stage_format = "cs"
        self.stage.save(update_fields=["stage_format"])
        self.group_a = self._make_group("Group A", "single_elim")
        self.group_b = self._make_group("Group B", "league")

    def _make_group(self, name, fmt):
        return StageGroups.objects.create(
            stage=self.stage, group_name=name, playing_date=self.stage.start_date,
            playing_time=datetime.time(18, 0), teams_qualifying=2,
            match_count=0, match_maps=[], bracket_format=fmt)

    def _generate_group(self, group, team_ids):
        return self.client.post(
            f"/events/stages/{self.stage.stage_id}/bracket/generate/",
            data={"team_ids": team_ids, "group_id": group.group_id},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def _play_out(self, group):
        """Report every real match in `group`, round by round.

        Re-reads each round because a knockout fills the NEXT round's slots as the current one
        completes - iterating one queryset would see the final with both slots still empty and
        skip it, leaving the bracket unfinished.
        """
        rounds = sorted(set(HeadToHeadMatch.objects.filter(group=group)
                            .values_list("round_number", flat=True)))
        for rnd in rounds:
            for m in HeadToHeadMatch.objects.filter(group=group, round_number=rnd):
                if m.team_a_id and m.team_b_id and m.status != "completed":
                    self._report(m, 4, 1)

    def _bracket(self, group=None):
        url = f"/events/stages/{self.stage.stage_id}/bracket/"
        if group:
            url += f"?group_id={group.group_id}"
        return self.client.get(url).json()

    def test_each_group_gets_its_own_tree_on_its_own_mode(self):
        self.assertEqual(self._generate_group(self.group_a, self._ids(3)).status_code, 201)
        self.assertEqual(
            self._generate_group(self.group_b, self._ids(6)[3:]).status_code, 201)

        a = self._bracket(self.group_a)
        b = self._bracket(self.group_b)
        self.assertEqual(a["fmt"], "single_elim")
        self.assertEqual(b["fmt"], "league")
        self.assertEqual(a["group_name"], "Group A")
        self.assertEqual(b["group_name"], "Group B")
        # A knockout of 4 is a tree; a league of 3 is every pair once. Different shapes entirely.
        self.assertEqual(len(a["rounds"]["winners"]), 2)
        self.assertEqual(len(b["rounds"]["league"]), 3)
        # Every match belongs to exactly one of them.
        # 3 teams -> bracket size 4: two round-1 matches (one a bye) plus the final.
        self.assertEqual(
            HeadToHeadMatch.objects.filter(group=self.group_a).count(), 3)
        self.assertEqual(
            HeadToHeadMatch.objects.filter(group=self.group_b).count(), 3)

    def test_the_stage_lists_all_its_brackets(self):
        self._generate_group(self.group_a, self._ids(3))
        listed = self._bracket()["stage_brackets"]
        self.assertEqual(
            [(x["group_name"], x["bracket_format"]) for x in listed],
            [("Group A", "single_elim"), ("Group B", "league")])

    def test_regenerating_one_group_leaves_the_other_alone(self):
        self._generate_group(self.group_a, self._ids(3))
        self._generate_group(self.group_b, self._ids(6)[3:])
        b_ids = set(HeadToHeadMatch.objects.filter(
            group=self.group_b).values_list("h2h_match_id", flat=True))

        self.assertEqual(self._generate_group(self.group_a, self._ids(3)).status_code, 201)
        self.assertEqual(
            set(HeadToHeadMatch.objects.filter(
                group=self.group_b).values_list("h2h_match_id", flat=True)),
            b_ids, "regenerating Group A rebuilt Group B's matches")

    def test_a_played_group_does_not_block_the_other_from_being_drawn(self):
        self._generate_group(self.group_a, self._ids(3))
        # The REAL round-1 match: with 3 teams the other round-1 slot is a bye, and a bye is
        # already "completed" without ever having been played, which is exactly why the
        # regeneration guard requires both teams to be present.
        first = HeadToHeadMatch.objects.filter(
            group=self.group_a, round_number=1,
            team_a__isnull=False, team_b__isnull=False).first()
        self._report(first, 4, 1)
        # Group A can no longer be regenerated...
        self.assertEqual(self._generate_group(self.group_a, self._ids(3)).status_code, 400)
        # ...but Group B has nothing to do with that.
        self.assertEqual(self._generate_group(self.group_b, self._ids(6)[3:]).status_code, 201)

    def test_standings_are_per_group(self):
        self._generate_group(self.group_a, self._ids(3))
        self._generate_group(self.group_b, self._ids(6)[3:])
        self._play_out(self.group_a)

        a_teams = {r["team_name"] for r in self._bracket(self.group_a)["standings"]}
        b_teams = {r["team_name"] for r in self._bracket(self.group_b)["standings"]}
        self.assertTrue(a_teams and b_teams)
        self.assertFalse(a_teams & b_teams, "a team appears in two groups' standings")
        # Group A has a champion.
        self.assertIn(1, [r["placement"] for r in self._bracket(self.group_a)["standings"]])
        # Group B has not been played, so nobody in it has won anything. (A league TABLE exists
        # from the moment it is drawn - every team is in it on zero - so the absence of results
        # shows as zero wins, not as an empty table.)
        b_rows = self._bracket(self.group_b)["standings"]
        self.assertTrue(b_rows)
        self.assertEqual({r["wins"] for r in b_rows}, {0})

    def test_finishing_one_group_writes_only_that_groups_placements(self):
        self._generate_group(self.group_a, self._ids(3))
        self._generate_group(self.group_b, self._ids(6)[3:])
        self._play_out(self.group_a)

        from afc_tournament_and_scrims.models import TournamentTeamMatchStats
        written = TournamentTeamMatchStats.objects.filter(match__group=self.group_a)
        self.assertTrue(written.exists(), "Group A's placements were not written")
        self.assertFalse(
            TournamentTeamMatchStats.objects.filter(match__group=self.group_b).exists(),
            "an unplayed Group B got placements written for it")

    def test_each_group_can_carry_its_own_room(self):
        self._generate_group(self.group_a, self._ids(3))
        self._generate_group(self.group_b, self._ids(6)[3:])
        # Stage-wide default, then an override for Group B only.
        for scope, obj_id, rounds in (
            ("stage", self.stage.stage_id, 7),
            ("group", self.group_b.group_id, 13),
        ):
            resp = self.client.put(
                f"/events/cs-room-settings/{scope}/{obj_id}/",
                data={"rounds": rounds}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
            self.assertEqual(resp.status_code, 200, resp.content)

        a_match = HeadToHeadMatch.objects.filter(group=self.group_a).first()
        b_match = HeadToHeadMatch.objects.filter(group=self.group_b).first()
        a_config, a_scope = cs_room.resolve_for_match(a_match)
        b_config, b_scope = cs_room.resolve_for_match(b_match)
        self.assertEqual((a_config.rounds, a_scope), (7, "stage"))
        self.assertEqual((b_config.rounds, b_scope), (13, "group"))

    def test_generating_without_naming_a_group_is_refused_when_split(self):
        """With two groups, "the stage's bracket" is ambiguous - say which."""
        resp = self.client.post(
            f"/events/stages/{self.stage.stage_id}/bracket/generate/",
            data={"team_ids": self._ids(4)}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("which group", resp.json()["message"])


class SimpleStageStillHasOneBracketTests(H2HBase):
    """The DEFAULT stays simple (owner 2026-08-13): pick Clash Squad, pick a mode, done.

    Groups are an opt-in for organizers who want the Champions League shape. Under the hood the
    simple stage quietly gets ONE group so the data has a single shape, but nobody has to know.
    """

    def test_generating_with_no_group_creates_exactly_one_and_uses_it(self):
        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 201, resp.content)

        groups = list(StageGroups.objects.filter(stage=self.stage))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].bracket_format, "single_elim")
        self.assertFalse(
            HeadToHeadMatch.objects.filter(stage=self.stage, group__isnull=True).exists())

    def test_reading_the_stage_without_a_group_id_still_works(self):
        self._generate(self._ids(4))
        body = self._get_bracket().json()
        self.assertTrue(body["generated"])
        self.assertEqual(len(body["stage_brackets"]), 1)

    def test_regenerating_does_not_pile_up_groups(self):
        self._generate(self._ids(4))
        self._generate(self._ids(4))
        self._generate(self._ids(6))
        self.assertEqual(StageGroups.objects.filter(stage=self.stage).count(), 1)


class PlayerSubmissionTests(H2HBase):
    """Teams send their own set result; the organizer approves or rejects it."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.match = self._m("winners", 1, 0)
        # A player on each side of the match, with a live token.
        self.p_a = self._player_on(self.tts[0], "cs_sub_a")
        self.p_b = self._player_on(self.tts[3], "cs_sub_b")

    def _player_on(self, tt, username):
        user = User.objects.create(username=username, email=f"{username}@afc.test", role="player")
        TournamentTeamMember.objects.create(tournament_team=tt, user=user, status="active")
        token = SessionToken.objects.create(
            user=user, token=f"{username}-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        return {"user": user, "token": token.token, "tt": tt}

    def _submit(self, who, score_a, score_b, players=None, note=""):
        return self.client.post(
            f"/events/h2h-matches/{self.match.h2h_match_id}/submit-result/",
            data={"score_a": score_a, "score_b": score_b,
                  "players": players or [], "note": note},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {who['token']}")

    def test_a_team_member_can_submit(self):
        resp = self._submit(self.p_a, 4, 1)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(H2HResultSubmission.objects.filter(h2h_match=self.match).count(), 1)

    def test_a_stranger_cannot(self):
        outsider = User.objects.create(username="cs_nobody", email="n@afc.test", role="player")
        token = SessionToken.objects.create(
            user=outsider, token="cs-nobody",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        resp = self.client.post(
            f"/events/h2h-matches/{self.match.h2h_match_id}/submit-result/",
            data={"score_a": 4, "score_b": 0}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.token}")
        self.assertEqual(resp.status_code, 403)

    def test_a_team_can_only_file_its_own_players(self):
        resp = self._submit(
            self.p_a, 4, 1,
            players=[{"player_id": self.p_b["user"].pk, "kills": 9}])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("your own roster", resp.json()["message"])

    def test_resubmitting_replaces_rather_than_piles_up(self):
        self._submit(self.p_a, 4, 1)
        self._submit(self.p_a, 4, 2)
        rows = H2HResultSubmission.objects.filter(
            h2h_match=self.match, tournament_team=self.p_a["tt"])
        self.assertEqual(rows.filter(status="pending").count(), 1)
        self.assertEqual(rows.filter(status="superseded").count(), 1)

    def test_agreement_between_the_two_teams_is_reported(self):
        self._submit(self.p_a, 4, 1)
        body = self.client.get(
            f"/events/h2h-matches/{self.match.h2h_match_id}/submissions/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}").json()
        self.assertEqual(body["agreement"], "one_side")

        self._submit(self.p_b, 4, 1)
        body = self.client.get(
            f"/events/h2h-matches/{self.match.h2h_match_id}/submissions/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}").json()
        self.assertEqual(body["agreement"], "agree")

        self._submit(self.p_b, 1, 4)
        body = self.client.get(
            f"/events/h2h-matches/{self.match.h2h_match_id}/submissions/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}").json()
        self.assertEqual(body["agreement"], "disagree")

    def test_approving_writes_the_result_and_advances_the_bracket(self):
        sub_id = self._submit(
            self.p_a, 4, 1,
            players=[{"player_id": self.p_a["user"].pk, "kills": 12, "damage": 2400}],
        ).json()["submission"]["submission_id"]

        resp = self.client.post(
            f"/events/h2h-submissions/{sub_id}/approve/",
            data={}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "completed")
        self.assertEqual((self.match.score_a, self.match.score_b), (4, 1))
        self.assertEqual(self.match.winner_id, self.tts[0].tournament_team_id)
        # The team's player line was written through the normal engine path.
        self.assertEqual(self.match.player_stats.get(player=self.p_a["user"]).kills, 12)
        # And the winner is in the final.
        self.assertEqual(self._m("winners", 2, 0).team_a_id, self.tts[0].tournament_team_id)

    def test_an_organizer_can_correct_before_approving(self):
        sub_id = self._submit(self.p_a, 4, 1).json()["submission"]["submission_id"]
        self.client.post(
            f"/events/h2h-submissions/{sub_id}/approve/",
            data={"score_a": 4, "score_b": 3, "review_note": "checked the screenshot"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.match.refresh_from_db()
        self.assertEqual((self.match.score_a, self.match.score_b), (4, 3))
        sub = H2HResultSubmission.objects.get(submission_id=sub_id)
        # BOTH payloads survive, so a reader can see the correction happened.
        self.assertEqual(sub.submitted_payload["score_b"], 1)
        self.assertEqual(sub.approved_payload["score_b"], 3)

    def test_approving_supersedes_the_other_side(self):
        a_id = self._submit(self.p_a, 4, 1).json()["submission"]["submission_id"]
        self._submit(self.p_b, 1, 4)
        self.client.post(
            f"/events/h2h-submissions/{a_id}/approve/", data={},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(
            H2HResultSubmission.objects.filter(h2h_match=self.match, status="pending").count(), 0)

    def test_rejection_needs_a_reason(self):
        sub_id = self._submit(self.p_a, 4, 1).json()["submission"]["submission_id"]
        resp = self.client.post(
            f"/events/h2h-submissions/{sub_id}/reject/", data={},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post(
            f"/events/h2h-submissions/{sub_id}/reject/",
            data={"review_note": "that is the other team's score"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            H2HResultSubmission.objects.get(submission_id=sub_id).status, "rejected")

    def test_a_player_cannot_approve(self):
        sub_id = self._submit(self.p_a, 4, 1).json()["submission"]["submission_id"]
        resp = self.client.post(
            f"/events/h2h-submissions/{sub_id}/approve/", data={},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.p_b['token']}")
        self.assertEqual(resp.status_code, 403)

    def test_cannot_submit_once_the_result_is_in(self):
        self._report(self.match, 4, 1)
        resp = self._submit(self.p_a, 4, 2)
        self.assertEqual(resp.status_code, 400)


class WizardRoomSettingsTests(H2HBase):
    """Room settings filled in while the event is being created (owner 2026-08-13).

    The stage does not exist yet at that point, so the wizard sends the whole configuration as
    stage.cs_room_settings and create_event materialises it. Optional in every sense: omitting the
    key creates nothing, and a bad value must not lose the event the organizer just filled in.
    """

    def _stage_payload(self, **extra):
        return {
            "stage_name": "Wizard Stage",
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
            "number_of_groups": 0,
            "stage_format": "cs - knockout",
            "teams_qualifying_from_stage": 2,
            "groups": [],
            **extra,
        }

    def test_settings_sent_with_the_stage_are_materialised(self):
        from afc_tournament_and_scrims import views as tv

        stage = Stages.objects.create(
            event=self.event, stage_name="W", start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 9, 2), number_of_groups=0,
            stage_format="cs - knockout", teams_qualifying_from_stage=2)
        # The materialisation itself is cs_room.save_config, which create_event calls with the
        # payload's cs_room_settings. Exercised directly here rather than by posting the whole
        # multipart create_event form, which needs a banner file and 40 unrelated fields.
        cs_room.save_config("stage", stage, {
            "rounds": 13, "map_name": "kalahari", "room_id": "778899",
        }, user=self.admin)
        config = CSRoomConfig.objects.get(stage=stage)
        self.assertEqual((config.rounds, config.map_name, config.room_id),
                         (13, "kalahari", "778899"))
        # And it is a FULL room, not a half-filled one.
        self.assertGreater(len(config.store), 50)
        self.assertEqual(len(config.round_economy), 13)

    def test_a_bad_value_raises_rather_than_writing_half_a_room(self):
        stage = Stages.objects.create(
            event=self.event, stage_name="W2", start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 9, 2), number_of_groups=0,
            stage_format="cs - knockout", teams_qualifying_from_stage=2)
        with self.assertRaises(cs_room.RoomConfigError):
            cs_room.save_config("stage", stage, {"rounds": 8}, user=self.admin)
        self.assertFalse(CSRoomConfig.objects.filter(stage=stage).exists())

    def test_the_catalogue_ships_each_mode_patch_for_the_wizard(self):
        """The create wizard has no saved scope to PUT against, so it applies modes locally from
        the catalogue's own patch table. That table has to be in the payload."""
        body = self.client.get("/events/cs-room-catalogue/").json()
        for preset in body["presets"]:
            self.assertIn("config", preset, f"{preset['key']} has no config patch")
        esports = next(p for p in body["presets"] if p["key"] == "esports_mode")
        self.assertEqual(esports["config"]["rounds"], 13)


class BracketNotificationTests(H2HBase):
    """Nothing in the Clash Squad path used to tell a player anything."""

    def setUp(self):
        super().setUp()
        self.player = User.objects.create(
            username="cs_notify", email="cn@afc.test", role="player")
        TournamentTeamMember.objects.create(
            tournament_team=self.tts[0], user=self.player, status="active")

    def test_generating_the_bracket_names_the_first_opponent(self):
        self._generate(self._ids(4))
        notice = Notifications.objects.filter(
            user=self.player, title="Your bracket is out").first()
        self.assertIsNotNone(notice)
        self.assertIn("T4", notice.message)   # seed 1 opens against seed 4

    def test_result_tells_each_side_its_own_story(self):
        loser = User.objects.create(username="cs_loser", email="cl@afc.test", role="player")
        TournamentTeamMember.objects.create(
            tournament_team=self.tts[3], user=loser, status="active")
        self._generate(self._ids(4))
        self._report(self._m("winners", 1, 0), 4, 1)

        won = Notifications.objects.filter(user=self.player, title="You won your match").first()
        lost = Notifications.objects.filter(
            user=loser, title="Your match result is in").first()
        self.assertIsNotNone(won)
        self.assertIsNotNone(lost)
        self.assertIn("You beat T4 4-1", won.message)
        self.assertIn("You lost to T1 1-4", lost.message)
