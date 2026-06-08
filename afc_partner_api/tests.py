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


# ──────────────────────────────────────────────────────────────────────────────
# Task 5 — per-key rate-limit tests.
#
# check_rate_limit(key) is the abuse guard every read endpoint runs right after
# auth: a fixed window of one wall-clock minute, counted per key in the shared
# Redis cache. The contract this locks in: the first rate_limit_per_min calls in a
# window pass, and the very next call (the (N+1)th) raises RateLimitExceeded.
#
# We drive it with a tiny stand-in object (not a real PartnerApiKey row) because the
# limiter only touches two attributes — .key_prefix (the bucket id) and
# .rate_limit_per_min (the ceiling) — so a DB row would be pure overhead and would
# couple this unit test to the model. cache.clear() first so a leftover bucket from a
# previous run (Redis persists across the test process) can't make the count start
# above zero and flip the pass/raise boundary.
# Full spec: WEBSITE/tasks/partner-api-design.md (§7 rate limiting).
# ──────────────────────────────────────────────────────────────────────────────
class RateLimitTests(TestCase):
    def test_blocks_over_limit(self):
        from django.core.cache import cache

        from afc_partner_api import ratelimit

        # Wipe any stale bucket so the window genuinely starts at zero.
        cache.clear()
        # Fake key: just the two attributes the limiter reads. Limit of 3 keeps the
        # boundary (3 ok, 4th blocked) explicit and fast.
        key = type("K", (), {"key_prefix": "afcp_test", "rate_limit_per_min": 3})()
        for _ in range(3):
            ratelimit.check_rate_limit(key)  # at-or-under the limit -> passes
        with self.assertRaises(ratelimit.RateLimitExceeded):
            ratelimit.check_rate_limit(key)  # one over -> blocked

    def test_returns_running_count(self):
        # check_rate_limit RETURNS the running count for the window so the caller can
        # compute X-RateLimit-Remaining (limit - count) without a second cache round-trip.
        from django.core.cache import cache

        from afc_partner_api import ratelimit

        cache.clear()
        key = type("K", (), {"key_prefix": "afcp_count", "rate_limit_per_min": 10})()
        self.assertEqual(ratelimit.check_rate_limit(key), 1)   # 1st call in window
        self.assertEqual(ratelimit.check_rate_limit(key), 2)   # 2nd call -> count grows
        self.assertEqual(ratelimit.check_rate_limit(key), 3)


# ──────────────────────────────────────────────────────────────────────────────
# Task 6 — read-endpoint tests (the whole request→response stack, end-to-end).
#
# These drive the live Django test Client against the mounted /api/v1/partner/
# routes, so they exercise EVERY layer of the partner_endpoint decorator together:
# authenticate_partner → check_rate_limit → resource toggle → scope filter →
# serializer firewall. The unit tests above proved each layer in isolation; THESE
# prove they are wired in the right order, that the HTTP status codes are correct,
# and that the pagination envelope is shaped as the spec promises. The contracts
# locked in (spec §6/§7/§9):
#   • valid key + resource toggle ON  -> 200 with a {results, has_more, next_offset,
#                                        total_count} pagination envelope
#   • resource toggle OFF             -> 403 resource_not_enabled (auth succeeded but
#                                        the partner isn't entitled to that resource)
#   • out-of-scope / unpublished slug -> 404 (NOT 403 — we never confirm an event a
#                                        partner can't see even exists; spec landmine)
#   • bad / missing key               -> 401 (PartnerAuthError -> 401)
#   • over the per-minute limit       -> 429 with a Retry-After header
#   • a field toggle (include_*)      -> reflected in the serialized body
#
# We issue a real key the way the admin endpoint will (store prefix+hash only), grant
# the partner one published event, and clear the cache in setUp so a leftover bucket
# from a prior run can't trip the rate-limit boundary early.
# Full spec: WEBSITE/tasks/partner-api-design.md (§9 endpoints).
# ──────────────────────────────────────────────────────────────────────────────
class PartnerEndpointTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from afc_auth.models import User
        from afc_team.models import Team
        from afc_tournament_and_scrims.models import (
            Event, Stages, StageGroups, Leaderboard, Match,
            TournamentTeam, TournamentTeamMatchStats, TournamentPlayerMatchStats,
        )

        # Fresh rate-limit window for every test (Redis persists across the process).
        cache.clear()

        # ── partner under test: all toggles default OFF (least privilege) ──
        self.partner = Partner.objects.create(name="ESL", slug="esl")

        # ── one published, completed native AFC event the partner is granted ──
        self.event = Event.objects.create(
            event_name="AFC Open", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="$1000", event_rules="-", event_status="completed",
            registration_link="https://x", tournament_tier="tier_1", number_of_stages=1,
            partner_published=True, organization=None)
        self.partner.allowed_events.add(self.event)

        # ── an event the partner is NOT scoped to (published, but never granted) ──
        # Used to prove out-of-scope reads 404, not 403 (don't confirm existence).
        self.other_event = Event.objects.create(
            event_name="Secret Cup", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-02-01", end_date="2026-02-02",
            registration_open_date="2026-01-01", registration_end_date="2026-01-20",
            prizepool="$500", event_rules="-", event_status="completed",
            registration_link="https://x", tournament_tier="tier_2", number_of_stages=1,
            partner_published=True, organization=None)

        # ── stage -> group -> leaderboard -> match -> teams + finalized stats ──
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Grand Final", start_date="2026-01-02",
            end_date="2026-01-02", number_of_groups=1, stage_format="br - normal",
            teams_qualifying_from_stage=2, stage_status="completed")
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date="2026-01-02",
            playing_time="18:00", teams_qualifying=2, match_count=1, match_maps=["bermuda"])
        self.admin = User.objects.create_user(
            username="afcstaff", email="afc@x.com", password="x", full_name="AFC Staff",
            role="admin")
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="GF LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda",
            room_id="SECRET123", room_password="hunter2", room_name="AFC-GF-ROOM",
            result_inputted=True, scoring_settings={"per_kill": 1})

        self.player1 = User.objects.create_user(
            username="ProGamer", email="p1@x.com", password="x", full_name="Real Name One",
            uid="UID0001", role="player", discord_id="discord-1")
        self.team1 = Team.objects.create(
            team_name="Team Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.player1, team_owner=self.player1, country="Nigeria")
        self.tteam = TournamentTeam.objects.create(
            event=self.event, team=self.team1, status="active", result_finalized=True,
            is_tournament_winner=True, reached_finals=True)
        self.tts1 = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.tteam, placement=1, kills=10, damage=2500,
            assists=4, placement_points=12, kill_points=10, total_points=22)
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.tts1, player=self.player1, kills=10, damage=2500, assists=4)

        # ── issue a real key (store prefix+hash only; keep the plaintext to send) ──
        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=self.partner, key_prefix=prefix, key_hash=h)
        self.api_key = full

    def _get(self, path, key="__default__"):
        # key="__default__" sentinel -> use the issued key; pass None/"" to test missing.
        api_key = self.api_key if key == "__default__" else (key or "")
        return self.client.get(path, HTTP_X_API_KEY=api_key)

    # ── 200 + pagination envelope ──
    def test_list_events_ok_with_pagination(self):
        # can_read_events ON -> 200 list, scoped to the one granted event, with the
        # full pagination envelope the spec promises.
        self.partner.can_read_events = True
        self.partner.save()
        resp = self._get("/api/v1/partner/events/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)           # only the granted event
        self.assertEqual(body["results"][0]["slug"], self.event.slug)
        # pagination metadata present and correctly shaped for a single-page result.
        self.assertFalse(body["has_more"])
        self.assertIsNone(body["next_offset"])
        self.assertIn("results", body)

    def test_pagination_limit_and_has_more(self):
        # A limit smaller than the result set -> has_more True + a next_offset cursor.
        from afc_tournament_and_scrims.models import Event

        # grant a second published event so the list has two rows.
        second = Event.objects.create(
            event_name="AFC Two", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-01-05", end_date="2026-01-06",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="$1", event_rules="-", event_status="completed",
            registration_link="https://x", tournament_tier="tier_1", number_of_stages=1,
            partner_published=True, organization=None)
        self.partner.allowed_events.add(second)
        self.partner.can_read_events = True
        self.partner.save()
        resp = self._get("/api/v1/partner/events/?limit=1&offset=0")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["results"]), 1)          # page size honored
        self.assertEqual(body["total_count"], 2)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 1)           # offset+limit, more remain

    # ── 403 when the resource toggle is OFF ──
    def test_resource_toggle_off_403(self):
        # auth succeeds but can_read_events defaults OFF -> 403 resource_not_enabled.
        resp = self._get("/api/v1/partner/events/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "resource_not_enabled")

    # ── 404 (NOT 403) for an out-of-scope / unpublished event slug ──
    def test_out_of_scope_event_404(self):
        # The other event exists + is published, but the partner was never granted it.
        # We must answer 404 (not 403) so we never confirm an unseen event exists.
        self.partner.can_read_events = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.other_event.slug}/")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_slug_404(self):
        self.partner.can_read_events = True
        self.partner.save()
        resp = self._get("/api/v1/partner/events/does-not-exist/")
        self.assertEqual(resp.status_code, 404)

    # ── 401 for bad / missing credentials ──
    def test_bad_key_401(self):
        resp = self._get("/api/v1/partner/events/", key="afcp_zzzz_deadbeef")
        self.assertEqual(resp.status_code, 401)

    def test_missing_key_401(self):
        resp = self._get("/api/v1/partner/events/", key=None)
        self.assertEqual(resp.status_code, 401)

    # ── 429 when over the per-minute limit (+ Retry-After header) ──
    def test_over_rate_limit_429(self):
        # Drop the limit to 1 so the SECOND call in the window trips the guard. The
        # response must carry Retry-After so a well-behaved client backs off.
        self.partner.can_read_events = True
        self.partner.save()
        PartnerApiKey.objects.update(rate_limit_per_min=1)
        first = self._get("/api/v1/partner/events/")
        self.assertEqual(first.status_code, 200)           # 1st call: under the limit
        second = self._get("/api/v1/partner/events/")
        self.assertEqual(second.status_code, 429)          # 2nd call: over the limit
        self.assertEqual(second["Retry-After"], "60")

    # ── rate-limit headers on a successful (2xx) response ──
    def test_rate_limit_headers_on_success_and_decrease(self):
        # A successful read must advertise the partner's budget so a well-behaved client
        # can self-throttle WITHOUT having to be 429'd first: X-RateLimit-Limit is the
        # per-minute ceiling, X-RateLimit-Remaining is how many calls are left in THIS
        # window. Remaining must strictly decrease as calls are spent in the same window.
        self.partner.can_read_events = True
        self.partner.save()
        # Pin a known ceiling so the assertions are exact (default is 60).
        PartnerApiKey.objects.update(rate_limit_per_min=5)

        first = self._get("/api/v1/partner/events/")
        self.assertEqual(first.status_code, 200)
        # Both headers are present on the 200.
        self.assertIn("X-RateLimit-Limit", first)
        self.assertIn("X-RateLimit-Remaining", first)
        # Limit echoes the key's ceiling; one call spent -> 4 remain (5 - 1).
        self.assertEqual(first["X-RateLimit-Limit"], "5")
        self.assertEqual(first["X-RateLimit-Remaining"], "4")

        # A second call in the same window spends one more -> Remaining drops to 3.
        second = self._get("/api/v1/partner/events/")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second["X-RateLimit-Limit"], "5")
        self.assertEqual(second["X-RateLimit-Remaining"], "3")
        # Strictly decreasing across calls (defends the "spend a slot per call" contract).
        self.assertLess(int(second["X-RateLimit-Remaining"]),
                        int(first["X-RateLimit-Remaining"]))

    # ── field toggle reflected in the body ──
    def test_field_toggle_reflected_in_body(self):
        # include_prize OFF -> no prize_pool in the event payload; ON -> it appears.
        self.partner.can_read_events = True
        self.partner.save()
        off = self._get(f"/api/v1/partner/events/{self.event.slug}/").json()
        self.assertNotIn("prize_pool", off)
        self.partner.include_prize = True
        self.partner.save()
        on = self._get(f"/api/v1/partner/events/{self.event.slug}/").json()
        self.assertEqual(on["prize_pool"], "$1000")

    # ── the other six resources mount + gate on their own toggle ──
    def test_stages_endpoint_toggle_and_scope(self):
        # toggle off -> 403; on -> 200 list of the event's stages (with groups nested).
        resp_off = self._get(f"/api/v1/partner/events/{self.event.slug}/stages/")
        self.assertEqual(resp_off.status_code, 403)
        self.partner.can_read_stages = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.event.slug}/stages/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["stage_name"], "Grand Final")

    def test_matches_endpoint_toggle_and_scope(self):
        self.partner.can_read_matches = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.event.slug}/matches/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["match_number"], 1)
        # room creds must NEVER appear in a match payload.
        import json
        self.assertNotIn("room_password", json.dumps(body))

    def test_standings_endpoint_toggle_and_scope(self):
        self.partner.can_read_standings = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.event.slug}/standings/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)           # one team competed
        self.assertEqual(body["results"][0]["team"], "Team Alpha")
        self.assertEqual(body["results"][0]["rank"], 1)

    def test_teams_endpoint_toggle_and_scope(self):
        self.partner.can_read_teams = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.event.slug}/teams/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["team"], "Team Alpha")

    def test_players_endpoint_toggle_and_scope(self):
        self.partner.can_read_players = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.event.slug}/players/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreaterEqual(body["total_count"], 1)
        usernames = {r["username"] for r in body["results"]}
        self.assertIn("ProGamer", usernames)
        # PII (real name / discord / email) must never leak through the players list.
        import json
        text = json.dumps(body)
        self.assertNotIn("Real Name One", text)
        self.assertNotIn("discord-1", text)

    # ── nested resources also 404 for out-of-scope events ──
    def test_nested_resource_out_of_scope_404(self):
        # stages of an event the partner can't see -> 404, even with the toggle on.
        self.partner.can_read_stages = True
        self.partner.save()
        resp = self._get(f"/api/v1/partner/events/{self.other_event.slug}/stages/")
        self.assertEqual(resp.status_code, 404)


# ──────────────────────────────────────────────────────────────────────────────
# Task 7 — AFC admin (provisioning) endpoint tests.
#
# views_admin.py is the AFC-staff surface that stands a Partner up and configures
# everything the read API later enforces: scope, the 14 toggles, the API keys, and
# the per-event publish flag. Unlike the partner read API (X-API-Key), these views
# are USER-SESSION authenticated (Bearer token -> validate_token) and gated to
# head_admin / partner_admin via _is_partner_admin. These tests lock in the
# security-critical contracts (spec §9 admin surface):
#   • GATE: a logged-in user WITHOUT head_admin/partner_admin gets 403 on every
#     endpoint (and a missing/garbage token is 400/401) — least privilege.
#   • CREATE: a head_admin provisions a Partner (defaults: active, all toggles off).
#   • KEY ISSUE: issue_key returns the FULL plaintext exactly ONCE, and the stored
#     row holds only prefix+hash — the plaintext secret is never persisted and never
#     returned again (the get_partner detail exposes key metadata, never plaintext).
#   • REVOKE: a revoked key fails authenticate_partner (-> the read API 401s).
#   • EDIT: PATCH accepts ONLY whitelisted keys (PARTNER_TOGGLE_FIELDS + the scope
#     id-lists + allow_all_native_afc) and REJECTS an unknown field (400) so a typo
#     or a malicious key can never silently set an attribute it shouldn't.
#   • PUBLISH: publish_event flips Event.partner_published (the read API's gate).
#
# We forge the Bearer session token directly (SessionToken.objects.create) exactly
# like afc_tournament_and_scrims/tests_scoring.py — no login round-trip needed.
# Full spec: WEBSITE/tasks/partner-api-design.md (§9 admin surface).
# ──────────────────────────────────────────────────────────────────────────────
class PartnerAdminEndpointTests(TestCase):
    def setUp(self):
        import datetime

        from afc_auth.models import Roles, SessionToken, User, UserRoles

        # ── an AFC head_admin (role="admin" + the head_admin granular role) ──
        # _is_partner_admin gates on the granular UserRoles row, so we attach one.
        self.head_admin_role, _ = Roles.objects.get_or_create(role_name="head_admin")
        self.admin = User.objects.create_user(
            username="afcstaff", email="afc@x.com", password="x", full_name="AFC Staff",
            role="admin")
        UserRoles.objects.create(user=self.admin, role=self.head_admin_role)
        self.admin_token = SessionToken.objects.create(
            user=self.admin, token="partner-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1)).token

        # ── a plain (non-admin) logged-in user: holds NO partner-admin role ──
        # Proves the 403 gate: a valid session is not enough; the role is required.
        self.outsider = User.objects.create_user(
            username="nobody", email="nobody@x.com", password="x", full_name="No Body",
            role="player")
        self.outsider_token = SessionToken.objects.create(
            user=self.outsider, token="partner-outsider-token-123456",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1)).token

    def _auth(self, token):
        # Bearer header the way validate_token expects it (afc_team/views.py idiom).
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # ── 403 gate: a valid session that lacks the role is refused everywhere ──
    def test_non_admin_create_403(self):
        resp = self.client.post(
            "/partners/admin/create/", {"name": "ESL"},
            content_type="application/json", **self._auth(self.outsider_token))
        self.assertEqual(resp.status_code, 403)

    def test_missing_token_400(self):
        # No Authorization header at all -> 400 (malformed request, not an auth failure yet).
        resp = self.client.get("/partners/admin/list/")
        self.assertEqual(resp.status_code, 400)

    def test_bad_token_401(self):
        # Well-formed Bearer header but the token resolves to no session -> 401.
        resp = self.client.get("/partners/admin/list/", **self._auth("not-a-real-token"))
        self.assertEqual(resp.status_code, 401)

    # ── create + list + detail ──
    def test_head_admin_creates_partner(self):
        resp = self.client.post(
            "/partners/admin/create/", {"name": "ESL Gaming"},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["partner"]["name"], "ESL Gaming")
        self.assertEqual(body["partner"]["status"], "active")
        # Provisioned in the DB with the slug the view derived.
        p = Partner.objects.get(slug=body["partner"]["slug"])
        # Least privilege: every toggle defaults OFF on a brand-new partner.
        for f in PARTNER_TOGGLE_FIELDS:
            self.assertFalse(getattr(p, f))

    def test_create_requires_name(self):
        resp = self.client.post(
            "/partners/admin/create/", {},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 400)

    def test_list_partners_includes_key_counts(self):
        p = Partner.objects.create(name="ESL", slug="esl")
        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=p, key_prefix=prefix, key_hash=h)
        resp = self.client.get("/partners/admin/list/", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        row = next(r for r in body["results"] if r["slug"] == "esl")
        self.assertEqual(row["active_key_count"], 1)

    def test_get_partner_returns_scope_toggles_keys_no_plaintext(self):
        p = Partner.objects.create(name="ESL", slug="esl", can_read_events=True)
        full, prefix, h = auth.generate_key()
        PartnerApiKey.objects.create(partner=p, key_prefix=prefix, key_hash=h, label="prod")
        resp = self.client.get("/partners/admin/esl/", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # toggles surfaced (the FE renders switches off these), scope id-lists present.
        self.assertTrue(body["partner"]["can_read_events"])
        self.assertIn("allowed_events", body["partner"])
        self.assertIn("allowed_organizations", body["partner"])
        # key metadata present, but the plaintext secret is NEVER returned in detail.
        self.assertEqual(len(body["keys"]), 1)
        self.assertEqual(body["keys"][0]["key_prefix"], prefix)
        import json
        text = json.dumps(body)
        self.assertNotIn(full, text)                 # full key never echoed
        self.assertNotIn(h, text)                    # hash is internal too
        self.assertNotIn("key_hash", text)           # never expose the hash field

    def test_get_partner_404(self):
        resp = self.client.get("/partners/admin/ghost/", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 404)

    # ── edit: whitelist-validated PATCH ──
    def test_edit_sets_toggles_and_scope(self):
        from afc_organizers.models import Organization
        from afc_tournament_and_scrims.models import Event

        p = Partner.objects.create(name="ESL", slug="esl")
        org = Organization.objects.create(name="Nova", slug="nova")
        ev = Event.objects.create(
            event_name="AFC Open", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="100", event_rules="-", event_status="completed",
            registration_link="https://x", number_of_stages=1, organization=None)
        resp = self.client.patch(
            "/partners/admin/esl/",
            {"can_read_events": True, "include_kills": True, "allow_all_native_afc": True,
             "allowed_events": [ev.pk], "allowed_organizations": [org.pk]},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertTrue(p.can_read_events)
        self.assertTrue(p.include_kills)
        self.assertTrue(p.allow_all_native_afc)
        self.assertEqual(set(p.allowed_events.values_list("pk", flat=True)), {ev.pk})
        self.assertEqual(set(p.allowed_organizations.values_list("pk", flat=True)), {org.pk})

    def test_edit_rejects_unknown_field(self):
        # The whitelist is the guard: a key that is NOT a toggle/scope field is a 400,
        # so a typo or a malicious body can never set an arbitrary Partner attribute
        # (e.g. status="active" bypassing suspend, or contact_email).
        p = Partner.objects.create(name="ESL", slug="esl")
        resp = self.client.patch(
            "/partners/admin/esl/", {"contact_email": "leak@x.com"},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 400)
        p.refresh_from_db()
        self.assertEqual(p.contact_email, "")        # untouched

    # ── suspend toggle ──
    def test_suspend_toggles_status(self):
        p = Partner.objects.create(name="ESL", slug="esl")
        resp = self.client.post(
            "/partners/admin/esl/suspend/", {"suspend": True},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, "suspended")
        # un-suspend flips it back.
        self.client.post(
            "/partners/admin/esl/suspend/", {"suspend": False},
            content_type="application/json", **self._auth(self.admin_token))
        p.refresh_from_db()
        self.assertEqual(p.status, "active")

    # ── issue key: plaintext shown ONCE, only the hash stored ──
    def test_issue_key_returns_plaintext_once_stores_only_hash(self):
        p = Partner.objects.create(name="ESL", slug="esl")
        resp = self.client.post(
            "/partners/admin/esl/keys/", {"label": "prod"},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        full = body["api_key"]                        # the plaintext, shown once
        self.assertTrue(full.startswith("afcp_"))
        # The stored row carries ONLY prefix + hash — never the plaintext secret.
        row = PartnerApiKey.objects.get(partner=p)
        self.assertEqual(row.key_hash, auth.hash_key(full))
        self.assertNotIn(full.split("_")[-1], row.key_hash)  # secret never stored raw
        # The issued plaintext actually authenticates at the read API boundary.
        from django.test.client import RequestFactory
        req = RequestFactory().get("/api/v1/partner/events/", HTTP_X_API_KEY=full)
        partner, _key = auth.authenticate_partner(req)
        self.assertEqual(partner.partner_id, p.partner_id)

    # ── revoke key: the read API then 401s with it ──
    def test_revoke_key_makes_it_fail_auth(self):
        from django.test.client import RequestFactory

        p = Partner.objects.create(name="ESL", slug="esl")
        # issue via the endpoint so we hold the real plaintext to revoke + test.
        full = self.client.post(
            "/partners/admin/esl/keys/", {},
            content_type="application/json", **self._auth(self.admin_token)).json()["api_key"]
        key_id = PartnerApiKey.objects.get(partner=p).key_id
        resp = self.client.post(
            f"/partners/admin/keys/{key_id}/revoke/", {},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(PartnerApiKey.objects.get(key_id=key_id).status, "revoked")
        # A revoked key no longer authenticates (the read API would 401).
        req = RequestFactory().get("/api/v1/partner/events/", HTTP_X_API_KEY=full)
        with self.assertRaises(auth.PartnerAuthError):
            auth.authenticate_partner(req)

    # ── publish: flips Event.partner_published (the read API's gate) ──
    def test_publish_event_flips_flag(self):
        from afc_tournament_and_scrims.models import Event

        ev = Event.objects.create(
            event_name="AFC Open", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date="2026-01-01", end_date="2026-01-02",
            registration_open_date="2025-12-01", registration_end_date="2025-12-20",
            prizepool="100", event_rules="-", event_status="completed",
            registration_link="https://x", number_of_stages=1, organization=None)
        self.assertFalse(ev.partner_published)
        resp = self.client.post(
            f"/partners/admin/events/{ev.slug}/publish/", {"published": True},
            content_type="application/json", **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        ev.refresh_from_db()
        self.assertTrue(ev.partner_published)
        # un-publish flips it back off.
        self.client.post(
            f"/partners/admin/events/{ev.slug}/publish/", {"published": False},
            content_type="application/json", **self._auth(self.admin_token))
        ev.refresh_from_db()
        self.assertFalse(ev.partner_published)
