# afc_partner_api/tests.py
# ──────────────────────────────────────────────────────────────────────────────
# Task 1 — model-level tests for the Partner Data API scaffold.
#
# These lock in the least-privilege contract: every resource/field toggle on a new
# Partner defaults OFF, the PARTNER_TOGGLE_FIELDS whitelist only ever names real
# BooleanFields (so admin-edit + serialization can trust it), and an issued key is
# bound to its partner with the expected active/rate-limit defaults.
# Full spec: WEBSITE/tasks/partner-api-design.md (§5 data model).
# ──────────────────────────────────────────────────────────────────────────────
from django.db import models
from django.test import TestCase
from django.test.client import RequestFactory

from afc_partner_api import auth
from afc_partner_api.models import FIELD_TOGGLES, Partner, PartnerApiKey, PARTNER_TOGGLE_FIELDS


class PartnerModelTests(TestCase):
    def test_partner_defaults_off(self):
        # Least privilege: a freshly provisioned partner can read nothing and see no
        # stat fields until an AFC admin flips toggles on.
        p = Partner.objects.create(name="ESL", slug="esl")
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertFalse(getattr(p, f), f"{f} should default False (least privilege)")
        self.assertTrue(p.status == "active")

    def test_toggle_whitelist_matches_fields(self):
        # every name in the whitelist must be a real BooleanField on Partner.
        # hasattr() alone is too weak: it passes for an attribute of ANY type, so a
        # CharField (or anything non-boolean) slipping into PARTNER_TOGGLE_FIELDS would
        # pass silently and break the least-privilege guarantee admin-edit/serialization
        # rely on. Assert the concrete field type via the model meta API instead.
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertIsInstance(Partner._meta.get_field(f), models.BooleanField)

    def test_key_belongs_to_partner(self):
        p = Partner.objects.create(name="ESL", slug="esl2")
        k = PartnerApiKey.objects.create(partner=p, key_prefix="afcp_aaaa", key_hash="x" * 64)
        self.assertEqual(k.partner_id, p.partner_id)
        self.assertEqual(k.status, "active")
        self.assertEqual(k.rate_limit_per_min, 60)


# ──────────────────────────────────────────────────────────────────────────────
# Task 2 — X-API-Key auth tests.
#
# These lock in the credential contract the read endpoints depend on: a valid key
# resolves to its Partner and stamps last_used_at; the secret is only ever stored
# as a sha256 hash (never plaintext); and EVERY failure mode an attacker could try
# — bad/unknown key, missing header, revoked key, suspended partner, expired key —
# raises PartnerAuthError (which the views translate to a 401). RequestFactory lets
# us inject the X-API-Key header directly (HTTP_X_API_KEY) without a live request.
# Full spec: WEBSITE/tasks/partner-api-design.md (§6 auth).
# ──────────────────────────────────────────────────────────────────────────────
class PartnerAuthTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.partner = Partner.objects.create(name="ESL", slug="esl")

    def _issue(self):
        # Issue a key the way the admin endpoint will: generate, store ONLY prefix +
        # hash, hand back the plaintext to authenticate with.
        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=self.partner, key_prefix=prefix, key_hash=h)
        return full

    def _req(self, key):
        # RequestFactory maps HTTP_X_API_KEY -> the "X-API-Key" request header.
        return self.rf.get("/api/v1/partner/events/", HTTP_X_API_KEY=key or "")

    def test_valid_key_authenticates(self):
        full = self._issue()
        partner, key = auth.authenticate_partner(self._req(full))
        self.assertEqual(partner.partner_id, self.partner.partner_id)
        self.assertIsNotNone(key.last_used_at)  # stamped on successful auth

    def test_hash_is_stored_not_plaintext(self):
        full = self._issue()
        row = PartnerApiKey.objects.get()
        self.assertNotIn(full.split("_")[-1], row.key_hash)  # secret never stored raw
        self.assertEqual(row.key_hash, auth.hash_key(full))

    def test_forged_secret_with_known_prefix_rejected(self):
        # The critical branch: an attacker who knows a key's PREFIX but not its secret.
        # The prefix matches a stored active row (so the lookup succeeds), but the secret
        # tail is wrong — the constant-time compare_digest must reject it. Without this
        # test the actual credential check is never exercised on a mismatch.
        full = self._issue()
        ns, prefix, _secret = full.split("_")            # afcp_<prefix>_<secret>
        forged = f"{ns}_{prefix}_{'0' * 48}"             # right prefix, wrong secret
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(forged))

    def test_bad_key_rejected(self):
        self._issue()
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req("afcp_zzzz_deadbeef"))

    def test_missing_header_rejected(self):
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(None))

    def test_revoked_key_rejected(self):
        full = self._issue()
        PartnerApiKey.objects.update(status="revoked")
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))

    def test_suspended_partner_rejected(self):
        full = self._issue()
        Partner.objects.update(status="suspended")
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))

    def test_expired_key_rejected(self):
        from datetime import timedelta

        from django.utils import timezone

        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=self.partner, key_prefix=prefix, key_hash=h,
                                     expires_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(self._req(full))


# ──────────────────────────────────────────────────────────────────────────────
# Task 3 — scope predicate tests.
#
# partner_visible_events(partner) is the ONE place that decides which Events a
# partner may read, so these lock in its three independent grant paths AND the
# publish gate that overrides all of them:
#   • explicit event grant     (Partner.allowed_events / Event.partner_grants)
#   • whole-organization grant  (Partner.allowed_organizations / Org.partner_grants)
#   • all native AFC events      (allow_all_native_afc -> organization IS NULL)
# The last test is the security guard: even a directly-granted event stays invisible
# while partner_published=False — the publish flag wins over any grant.
# We seed one native event (organization=None) and one org-owned event, then assert
# each grant path surfaces EXACTLY the expected event and nothing else.
# Full spec: WEBSITE/tasks/partner-api-design.md (§6 scope).
# ──────────────────────────────────────────────────────────────────────────────
class ScopeTests(TestCase):
    def setUp(self):
        from afc_organizers.models import Organization
        from afc_tournament_and_scrims.models import Event

        self.org = Organization.objects.create(name="Nova", slug="nova")
        # Native AFC event: organization=None. Reachable only via allow_all_native_afc.
        self.native = Event.objects.create(event_name="AFC Open", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=12,
            event_mode="virtual", start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="100", event_rules="-", event_status="completed", registration_link="https://x",
            number_of_stages=1, partner_published=True, organization=None)
        # Org-owned event: reachable via either an explicit event grant or an org grant.
        self.orgev = Event.objects.create(event_name="Nova Cup", competition_type="tournament",
            participant_type="squad", event_type="external", max_teams_or_players=12,
            event_mode="virtual", start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="100", event_rules="-", event_status="completed", registration_link="https://x",
            number_of_stages=1, partner_published=True, organization=self.org)
        self.partner = Partner.objects.create(name="ESL", slug="esl")

    def test_event_grant(self):
        # Explicit event grant surfaces only that event (not the unrelated native one).
        from afc_partner_api.scope import partner_visible_events

        self.partner.allowed_events.add(self.orgev)
        self.assertEqual(set(partner_visible_events(self.partner)), {self.orgev})

    def test_org_grant(self):
        # Granting the whole org surfaces every published event the org owns.
        from afc_partner_api.scope import partner_visible_events

        self.partner.allowed_organizations.add(self.org)
        self.assertEqual(set(partner_visible_events(self.partner)), {self.orgev})

    def test_native_toggle(self):
        # allow_all_native_afc surfaces organization-less events only (not org events).
        from afc_partner_api.scope import partner_visible_events

        self.partner.allow_all_native_afc = True
        self.partner.save()
        self.assertEqual(set(partner_visible_events(self.partner)), {self.native})

    def test_unpublished_excluded_even_when_granted(self):
        # Publish gate wins: an explicitly granted event still stays invisible while
        # partner_published=False. This is the security-critical branch.
        from afc_partner_api.scope import partner_visible_events

        self.orgev.partner_published = False
        self.orgev.save()
        self.partner.allowed_events.add(self.orgev)
        self.assertEqual(set(partner_visible_events(self.partner)), set())


# ──────────────────────────────────────────────────────────────────────────────
# Task 4 — serialization firewall tests (the PII/PK/room-cred guard).
#
# serialize.py is the ONE boundary that turns internal ORM rows into partner-facing
# JSON, so it is the single most security-critical module in this app. These tests
# lock in two contracts the spec (§8) makes non-negotiable:
#
#   1. DENYLIST (the firewall): json.dumps() of EVERY serializer's output must contain
#      none of FORBIDDEN_KEYS — raw PKs (event_id/match_id/.../organization_id), room
#      credentials (room_id/password/name), PII (contact_email/email/discord_role_id),
#      scoring internals (scoring_settings), and internal flags (creator/is_draft/
#      rankings_verified). If a field is not explicitly public + toggle-gated, it must
#      not appear. This guard runs over all seven resources so the firewall is proven
#      end-to-end, not just on the event row.
#   2. FIELD TOGGLES: a stat/detail field appears ONLY when its include_* toggle is on.
#      include_kills off -> no "kills" key; include_placements on -> "placement" present.
#      Toggles default OFF (least privilege), so the partner sees nothing extra unless
#      an AFC admin opted in.
#
# We seed one complete, completed squad event (event -> stage -> group -> leaderboard
# -> match -> two TournamentTeams, each with a finalized TournamentTeamMatchStats row
# and per-player TournamentPlayerMatchStats) using the existing model field set, so the
# aggregating serializers (team/standings/match/player) have real finalized rows to fold.
# Full spec: WEBSITE/tasks/partner-api-design.md (§8 serialization rules).
# ──────────────────────────────────────────────────────────────────────────────
class SerializeTests(TestCase):
    # The hard denylist. NONE of these keys may appear anywhere in any serializer's
    # JSON output. Mirrors spec §8 "NEVER emit" plus the plan's FORBIDDEN_KEYS set.
    FORBIDDEN_KEYS = {
        "event_id", "match_id", "stage_id", "group_id", "tournament_team_id", "player_id",
        "competitor_id", "leaderboard_id", "room_id", "room_password", "room_name",
        "contact_email", "email", "discord_role_id", "scoring_settings", "creator",
        "is_draft", "rankings_verified", "organization_id",
    }

    def setUp(self):
        from afc_auth.models import User
        from afc_team.models import Team
        from afc_tournament_and_scrims.models import (
            Event, Stages, StageGroups, Leaderboard, Match,
            TournamentTeam, TournamentTeamMatchStats, TournamentPlayerMatchStats,
        )

        # ── partner under test: all toggles default OFF (least privilege) ──
        self.partner = Partner.objects.create(name="ESL", slug="esl")

        # ── a completed, partner-published native AFC event ──
        self.event = Event.objects.create(
            event_name="AFC Open", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="$1000", event_rules="-", event_status="completed",
            registration_link="https://x", tournament_tier="tier_1", number_of_stages=1,
            partner_published=True, organization=None)

        # ── stage -> group ──
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Grand Final", start_date="2026-01-02",
            end_date="2026-01-02", number_of_groups=1, stage_format="br - normal",
            teams_qualifying_from_stage=2, stage_status="completed",
            stage_discord_role_id="999999999")  # PII-ish id that must NEVER leak
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date="2026-01-02",
            playing_time="18:00", teams_qualifying=2, match_count=1, match_maps=["bermuda"],
            group_discord_role_id="888888888")

        # ── creator user for the leaderboard (internal — never serialized) ──
        self.admin = User.objects.create_user(
            username="afcstaff", email="afc@x.com", password="x", full_name="AFC Staff",
            role="admin")
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="GF LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")

        # ── match carries room credentials that must be firewalled out ──
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda",
            room_id="SECRET123", room_password="hunter2", room_name="AFC-GF-ROOM",
            result_inputted=True, scoring_settings={"per_kill": 1})

        # ── two teams, each with a finalized team-match-stats row + player stats ──
        self.player1 = User.objects.create_user(
            username="ProGamer", email="p1@x.com", password="x", full_name="Real Name One",
            uid="UID0001", role="player", discord_id="discord-1")
        self.player2 = User.objects.create_user(
            username="Sniper", email="p2@x.com", password="x", full_name="Real Name Two",
            uid="UID0002", role="player", discord_id="discord-2")

        self.team1 = Team.objects.create(
            team_name="Team Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.player1, team_owner=self.player1, country="Nigeria")
        self.team2 = Team.objects.create(
            team_name="Team Bravo", team_tag="BRV", join_settings="open",
            team_creator=self.player2, team_owner=self.player2, country="Ghana")

        self.tteam = TournamentTeam.objects.create(
            event=self.event, team=self.team1, status="active", result_finalized=True,
            is_tournament_winner=True, reached_finals=True)
        self.tteam2 = TournamentTeam.objects.create(
            event=self.event, team=self.team2, status="active", result_finalized=True)

        # team1 won (placement 1), team2 second.
        self.tts1 = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tteam, placement=1, kills=10, damage=2500,
            assists=4, placement_points=12, kill_points=10, total_points=22)
        self.tts2 = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tteam2, placement=2, kills=6, damage=1800,
            assists=2, placement_points=9, kill_points=6, total_points=15)

        # per-player rows so serialize_player / rosters have real data to fold.
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.tts1, player=self.player1, kills=6, damage=1500, assists=2)
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.tts2, player=self.player2, kills=6, damage=1800, assists=2)

    def _assert_no_forbidden(self, obj):
        # json.dumps then substring-search for each forbidden key as a JSON object key
        # (quoted). This catches a leaked key anywhere in the (possibly nested) payload.
        import json

        text = json.dumps(obj, default=str)
        for k in self.FORBIDDEN_KEYS:
            self.assertNotIn(f'"{k}"', text, f"forbidden key {k} leaked: {text}")

    # ── event ──
    def test_event_serialization_has_slug_no_pk(self):
        from afc_partner_api.serialize import serialize_event

        out = serialize_event(self.event, self.partner)
        self.assertEqual(out["slug"], self.event.slug)
        self.assertEqual(out["name"], "AFC Open")
        self.assertTrue(out["is_native_afc"])          # organization is None
        self._assert_no_forbidden(out)

    def test_event_prize_gated_on_toggle(self):
        from afc_partner_api.serialize import serialize_event

        # toggle OFF (default) -> no prize_pool field.
        self.assertNotIn("prize_pool", serialize_event(self.event, self.partner))
        # toggle ON -> prize_pool present.
        self.partner.include_prize = True
        self.partner.save()
        self.assertIn("prize_pool", serialize_event(self.event, self.partner))

    # ── stage / group ──
    def test_stage_group_serialization(self):
        from afc_partner_api.serialize import serialize_group, serialize_stage

        st = serialize_stage(self.stage, self.partner)
        self.assertEqual(st["stage_name"], "Grand Final")
        self.assertIn("order", st)                     # stage_name + order (spec §8)
        self._assert_no_forbidden(st)

        gr = serialize_group(self.group, self.partner)
        self.assertEqual(gr["group_name"], "Group A")
        self._assert_no_forbidden(gr)

    # ── match ──
    def test_match_serialization_strips_room_creds(self):
        from afc_partner_api.serialize import serialize_match

        out = serialize_match(self.match, self.partner)
        self.assertEqual(out["match_number"], 1)
        self._assert_no_forbidden(out)                 # room_id/password/name must be gone

    # ── team: the toggle-gating contract ──
    def test_field_toggles_gate_stats(self):
        from afc_partner_api.serialize import serialize_team

        self.partner.include_kills = False
        self.partner.include_placements = True
        self.partner.save()
        out = serialize_team(self.tteam, self.partner)
        self.assertNotIn("kills", out)                 # toggle off -> absent
        self.assertIn("placement", out)                # toggle on -> present
        self._assert_no_forbidden(out)

    def test_team_all_stat_toggles(self):
        from afc_partner_api.serialize import serialize_team

        # turn on every stat/detail toggle -> all fields present, still no leak.
        for f in FIELD_TOGGLES:
            setattr(self.partner, f, True)
        self.partner.save()
        out = serialize_team(self.tteam, self.partner)
        self.assertEqual(out["team"], "Team Alpha")
        self.assertEqual(out["placement"], 1)          # best (lowest) placement in event
        self.assertEqual(out["kills"], 10)
        self.assertEqual(out["damage"], 2500)
        self.assertEqual(out["assists"], 4)
        self.assertIn("roster", out)                   # include_rosters -> player list
        self._assert_no_forbidden(out)

    def test_team_rosters_gated(self):
        from afc_partner_api.serialize import serialize_team

        # rosters off -> no roster key; on -> present with public handles only.
        self.assertNotIn("roster", serialize_team(self.tteam, self.partner))
        self.partner.include_rosters = True
        self.partner.save()
        out = serialize_team(self.tteam, self.partner)
        self.assertIn("roster", out)
        self._assert_no_forbidden(out)

    # ── standings ──
    def test_standings_serialization(self):
        from afc_partner_api.serialize import serialize_standings

        self.partner.include_placements = True
        self.partner.include_kills = True
        self.partner.save()
        rows = serialize_standings(self.event, self.partner)
        self.assertEqual(len(rows), 2)                 # two teams
        # winner first (most points): Team Alpha at rank 1.
        self.assertEqual(rows[0]["team"], "Team Alpha")
        self.assertEqual(rows[0]["rank"], 1)
        for r in rows:
            self._assert_no_forbidden(r)

    def test_standings_rank_by_effective_total_not_stale_total_points(self):
        # Regression guard for the ranking-metric mismatch: standings MUST rank by the
        # recomputed effective_total (placement+kill+bonus-penalty), the SAME metric the
        # admin standings view uses, NOT the stored total_points column (which can be
        # stale). We make the two diverge: give the team with the LOWER stored
        # total_points a bonus that flips the true (effective) ranking. If standings still
        # ranked by total_points, Team Alpha would lead; with effective_total, Team Bravo
        # must lead.
        #
        #   Team Alpha (tts1): total_points=22, no bonus/penalty -> effective=12+10=22
        #   Team Bravo (tts2): stored total_points=15 (STALE) but a +20 bonus banked later
        #                      -> effective = 9 + 6 + 20 = 35  (beats Alpha's 22)
        self.tts2.bonus_points = 20
        self.tts2.save(update_fields=["bonus_points"])

        from afc_partner_api.serialize import serialize_standings

        rows = serialize_standings(self.event, self.partner)
        # Bravo's higher effective_total wins despite its lower stored total_points.
        self.assertEqual(rows[0]["team"], "Team Bravo")
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["team"], "Team Alpha")

    # ── player ──
    def test_player_serialization_public_handle_only(self):
        import json

        from afc_partner_api.serialize import serialize_player

        self.partner.include_kills = True
        self.partner.save()
        # Scope the fold to player1's team in THIS event (the per-event/roster contract).
        out = serialize_player(self.player1, self.partner, tournament_team=self.tteam)
        # public in-game handle + id only — NEVER full_name/email/discord.
        self.assertEqual(out["username"], "ProGamer")
        self.assertEqual(out["in_game_id"], "UID0001")
        self.assertNotIn("Real Name One", json.dumps(out, default=str))  # PII name never leaks
        self.assertNotIn("discord-1", json.dumps(out, default=str))      # discord id never leaks
        self._assert_no_forbidden(out)

    def test_player_stats_scoped_to_event_not_lifetime(self):
        # Regression guard for the cross-event leak: serialize_player MUST fold only the
        # stats the player recorded for THIS team in THIS event, never their lifetime
        # totals across every event they ever played. We give player1 a SECOND event with
        # a big stat row; the per-event fold (scoped via tournament_team) must ignore it,
        # while an UNSCOPED fold would wrongly sum both. The single-event seed in setUp
        # can't catch this (lifetime == per-event there), so we add the second event here.
        from afc_team.models import Team
        from afc_tournament_and_scrims.models import (
            Event, Stages, StageGroups, Leaderboard, Match,
            TournamentTeam, TournamentTeamMatchStats, TournamentPlayerMatchStats,
        )

        # A second, unrelated published event where player1 also competes.
        other_event = Event.objects.create(
            event_name="AFC Spring", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-03-01", end_date="2026-03-02",
            registration_open_date="2026-02-01", registration_end_date="2026-02-20",
            prizepool="$500", event_rules="-", event_status="completed",
            registration_link="https://x", tournament_tier="tier_1", number_of_stages=1,
            partner_published=True, organization=None)
        other_stage = Stages.objects.create(
            event=other_event, stage_name="Final", start_date="2026-03-02",
            end_date="2026-03-02", number_of_groups=1, stage_format="br - normal",
            teams_qualifying_from_stage=2, stage_status="completed")
        other_group = StageGroups.objects.create(
            stage=other_stage, group_name="Group A", playing_date="2026-03-02",
            playing_time="18:00", teams_qualifying=2, match_count=1, match_maps=["bermuda"])
        other_lb = Leaderboard.objects.create(
            leaderboard_name="Spring LB", event=other_event, stage=other_stage,
            group=other_group, creator=self.admin, placement_points={"1": 12}, kill_point=1.0,
            leaderboard_method="manual")
        other_match = Match.objects.create(
            leaderboard=other_lb, group=other_group, match_number=1, match_map="bermuda",
            result_inputted=True)
        # player1's team1 also entered the other event; give it a large stat row.
        other_tteam = TournamentTeam.objects.create(
            event=other_event, team=self.team1, status="active", result_finalized=True)
        other_tts = TournamentTeamMatchStats.objects.create(
            match=other_match, tournament_team=other_tteam, placement=1, kills=99, damage=9999,
            assists=99, placement_points=12, kill_points=99, total_points=111)
        TournamentPlayerMatchStats.objects.create(
            team_stats=other_tts, player=self.player1, kills=50, damage=5000, assists=50)

        from afc_partner_api.serialize import serialize_player

        self.partner.include_kills = True
        self.partner.include_damage = True
        self.partner.include_assists = True
        self.partner.save()

        # Scoped to THIS event's team -> only the setUp row (6/1500/2), NOT the spring row.
        scoped = serialize_player(self.player1, self.partner, tournament_team=self.tteam)
        self.assertEqual(scoped["kills"], 6)
        self.assertEqual(scoped["damage"], 1500)
        self.assertEqual(scoped["assists"], 2)

        # And the roster path (serialize_team -> serialize_player) must be scoped too:
        # Team Alpha's roster in THIS event reports the per-event numbers, not lifetime.
        from afc_partner_api.serialize import serialize_team

        self.partner.include_rosters = True
        self.partner.save()
        team_out = serialize_team(self.tteam, self.partner)
        alpha_roster = {p["username"]: p for p in team_out["roster"]}
        self.assertEqual(alpha_roster["ProGamer"]["kills"], 6)     # per-event, not 6+50
        self.assertEqual(alpha_roster["ProGamer"]["damage"], 1500)  # per-event, not 1500+5000

        # Sanity: an UNSCOPED fold (the old bug) WOULD have summed both events.
        lifetime = serialize_player(self.player1, self.partner)
        self.assertEqual(lifetime["kills"], 6 + 50)  # proves the seed actually spans 2 events
