"""
Tests for afc_team views.

search_teams (GET /team/search-teams/) — the team typeahead powering <TeamSearchSelect/> in the
Standalone Leaderboards wizard. Mirrors afc_auth.views.search_users: Bearer auth, q>=2, icontains,
{results:[{team_id,team_name,team_tag,country}], total_count} shape.
"""
from django.test import TestCase, Client

from afc_auth.models import User, SessionToken
from afc_team.models import Team


def _make_user(username):
    u = User.objects.create(username=username, email=f"{username}@x.com", full_name=username, role="player", password="x")
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


class SearchTeamsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user, self.tok = _make_user("searcher")
        self.dynasty = Team.objects.create(team_name="Dynasty Esports", team_tag="DYN", country="NG",
                                            join_settings="open", team_owner=self.user, team_creator=self.user)
        self.dynamo = Team.objects.create(team_name="Dynamo FC", team_tag="DMO", country="GH",
                                          join_settings="open", team_owner=self.user, team_creator=self.user)
        self.other = Team.objects.create(team_name="Falcons", team_tag="FAL", country="KE",
                                         join_settings="open", team_owner=self.user, team_creator=self.user)

    def _get(self, q=None, limit=None, tok=None):
        params = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        headers = {"HTTP_AUTHORIZATION": f"Bearer {tok}"} if tok else {}
        return self.client.get("/team/search-teams/", params, **headers)

    def test_requires_auth(self):
        self.assertEqual(self._get(q="dyn").status_code, 400)  # no token

    def test_q_under_two_chars_returns_empty(self):
        body = self._get(q="d", tok=self.tok).json()
        self.assertEqual(body, {"results": [], "total_count": 0})

    def test_matches_team_name_icontains(self):
        body = self._get(q="dyn", tok=self.tok).json()
        names = {r["team_name"] for r in body["results"]}
        self.assertEqual(names, {"Dynasty Esports", "Dynamo FC"})
        self.assertEqual(body["total_count"], 2)

    def test_result_shape(self):
        body = self._get(q="Dynasty", tok=self.tok).json()
        row = body["results"][0]
        self.assertEqual(set(row.keys()), {"team_id", "team_name", "team_tag", "country"})
        self.assertEqual(row["team_tag"], "DYN")
        self.assertEqual(row["country"], "NG")

    def test_matches_team_tag(self):
        body = self._get(q="FAL", tok=self.tok).json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["team_name"], "Falcons")

    def test_limit_capped(self):
        body = self._get(q="Dyn", limit=1, tok=self.tok).json()
        self.assertEqual(len(body["results"]), 1)   # page size honored
        self.assertEqual(body["total_count"], 2)    # but total reflects all matches

    def test_punctuation_insensitive_match(self):
        # The reported bug: a team literally named "V-E" must surface for the query "ve" (and "v-e",
        # "v e"). Powered by utils.search_utils.normalized_column / separator_stripped. This is a pure
        # WIDENING — the plain-icontains matches above still pass unchanged.
        ve = Team.objects.create(team_name="V-E", team_tag="VEX", country="NG",
                                 join_settings="open", team_owner=self.user, team_creator=self.user)
        for q in ("ve", "v-e", "v e"):
            ids = {r["team_id"] for r in self._get(q=q, tok=self.tok).json()["results"]}
            self.assertIn(ve.team_id, ids, f"query {q!r} should find the team named 'V-E'")


# ──────────────────────────────────────────────────────────────────────────
# Roster-lock rules (afc_team.views)
#
# Rule B  — membership is FROZEN while the team has an ACTIVE tournament registration
#           (a non-removed TournamentTeam row for an upcoming/ongoing event). Guards both
#           exit_team (POST /team/exit-team/) and kick_team_member (POST /team/kick-team-member/),
#           in addition to the pre-existing transfer-window guard.
# Rule C  — in-game POSITIONS (in_game_role) can be edited only while the active ranking
#           season's transfer window is OPEN (manage_team_roster, POST /team/manage-team-roster/).
#           Management-role changes keep their own rule and are not newly restricted by the window.
#
# Fixtures mirror the existing SearchTeamsTests style: _make_user() builds a User + SessionToken
# ("tok_<username>"), auth is sent as "Bearer <tok>". Event/TournamentTeam construction mirrors
# afc_tournament_and_scrims/tests_round_robin.py. Tests never touch the network.
# ──────────────────────────────────────────────────────────────────────────
import datetime

from afc_team.models import TeamMembers
from afc_rankings.models import Season
from afc_tournament_and_scrims.models import Event, TournamentTeam, TournamentTeamMember


# A fixed date used for all the Event/Season date fields in these fixtures.
_D = datetime.date(2026, 1, 1)


def _make_event(creator, event_status="upcoming", name="Roster Lock Cup"):
    """Minimal valid Event row (all required, non-null fields populated), mirroring the
    afc_tournament_and_scrims test fixtures. event_status drives Rule B (upcoming/ongoing
    = active; completed = finished).

    Dates span a window ENDING IN THE FUTURE so an upcoming/ongoing event is genuinely LIVE.
    The roster lock now derives liveness via effective_event_status() (owner 2026-07-14), which
    reads a PAST-end event as "completed" no matter what event_status says - so a fixed past date
    (the old _D = 2026-01-01) would make even an "upcoming" event effectively finished and stop it
    locking. A future end keeps these fixtures representing a real live tournament."""
    _today = datetime.date.today()
    _start = _today - datetime.timedelta(days=1)   # already started
    _end = _today + datetime.timedelta(days=7)     # ends in the future -> effectively live
    return Event.objects.create(
        event_name=name, competition_type="tournament", participant_type="squad",
        event_type="internal", max_teams_or_players=16, event_mode="virtual",
        start_date=_start, end_date=_end, registration_open_date=_D, registration_end_date=_D,
        prizepool="$1000", event_rules="rules", event_status=event_status,
        registration_link="https://afc.test/reg", number_of_stages=1,
        creator=creator, is_draft=False,
    )


def _make_season(*, window_open):
    """Active ranking Season whose transfer window is OPEN or CLOSED on today's date.
    Open: window spans a wide range that includes today. Closed: window sits entirely in
    the past, so is_transfer_window_open() is False today."""
    today = datetime.date.today()
    if window_open:
        w_open, w_close = today - datetime.timedelta(days=5), today + datetime.timedelta(days=5)
    else:
        w_open, w_close = today - datetime.timedelta(days=30), today - datetime.timedelta(days=20)
    return Season.objects.create(
        name="Lock Season", quarter=1, year=today.year,
        start_date=today - datetime.timedelta(days=60),
        end_date=today + datetime.timedelta(days=60),
        transfer_window_open=w_open, transfer_window_close=w_close,
        is_active=True,
    )


class RuleB_TournamentMembershipLockTests(TestCase):
    """Rule B: cannot leave or kick while the team is registered for an active tournament."""

    def setUp(self):
        self.client = Client()
        # Owner cannot leave their own team (a separate, earlier guard), so the LEAVING player
        # is a plain member; the OWNER is the one who kicks.
        self.owner, self.owner_tok = _make_user("rb_owner")
        self.member, self.member_tok = _make_user("rb_member")
        self.team = Team.objects.create(
            team_name="Lock FC", team_tag="LCK", country="NG", join_settings="open",
            team_owner=self.owner, team_creator=self.owner,
        )
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")
        self.membership = TeamMembers.objects.create(team=self.team, member=self.member, management_role="member")
        # Transfer window is OPEN, so ONLY Rule B (not the window guard) is under test here.
        _make_season(window_open=True)

    def _exit(self, tok):
        return self.client.post("/team/exit-team/", {}, content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {tok}")

    def _kick(self, tok, member_id):
        return self.client.post("/team/kick-team-member/",
                                {"team_id": self.team.team_id, "member_id": member_id},
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {tok}")

    def _roster(self, tt, user):
        # Put `user` on the event roster for TournamentTeam `tt` (the per-event roster the
        # lock keys off, owner 2026-06-21 per-player rule).
        return TournamentTeamMember.objects.create(
            tournament_team=tt, user=user, event=tt.event, status="active",
        )

    def test_exit_blocked_while_on_active_event_roster(self):
        # On the roster of a live event -> exit must 403 + NOT delete.
        ev = _make_event(self.owner, event_status="upcoming")
        tt = TournamentTeam.objects.create(event=ev, team=self.team)
        self.assertEqual(tt.status, "active")
        self._roster(tt, self.member)
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 403)
        self.assertIn("active tournament", res.json()["message"])
        self.assertTrue(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_kick_blocked_while_on_active_event_roster(self):
        # Same freeze applies to a captain/owner kick of a rostered player.
        tt = TournamentTeam.objects.create(event=_make_event(self.owner), team=self.team)
        self._roster(tt, self.member)
        res = self._kick(self.owner_tok, self.member.user_id)
        self.assertEqual(res.status_code, 403)
        self.assertIn("active tournament", res.json()["error"])
        self.assertTrue(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_kick_allowed_when_off_active_roster(self):
        # Per-player rule (owner 2026-06-21): the team IS registered for a live event, but this
        # member is NOT on that event's roster (e.g. an organizer removed them, or they are a
        # coach who never plays). The team must be able to remove them.
        TournamentTeam.objects.create(event=_make_event(self.owner), team=self.team)
        res = self._kick(self.owner_tok, self.member.user_id)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_exit_allowed_when_off_active_roster(self):
        # Same per-player rule for self-leave: not on the live event roster -> may leave.
        TournamentTeam.objects.create(event=_make_event(self.owner), team=self.team)
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_exit_allowed_with_no_active_registration(self):
        # No tournament registration at all -> leave still works (window is open).
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_kick_allowed_when_only_completed_or_withdrawn_registration(self):
        # A completed event and a withdrawn registration are both "not active" -> kick works.
        TournamentTeam.objects.create(event=_make_event(self.owner, event_status="completed"), team=self.team)
        TournamentTeam.objects.create(event=_make_event(self.owner, name="E_withdrawn"),
                                      team=self.team, status="withdrawn")
        res = self._kick(self.owner_tok, self.member.user_id)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_exit_and_kick_allowed_when_event_status_stale_but_end_passed(self):
        # Regression (owner 2026-07-14, VENTRIX GAMING / LEGACY SCRIMS 198+200): a scrim whose
        # end date/time has PASSED keeps event_status="upcoming" forever because the auto-complete
        # sweep is not scheduled in the live celery beat. effective_event_status() reads it as
        # "completed", so the roster lock must RELEASE even though the raw field still says upcoming.
        # Before the fix the member was frozen ("no current event but I can't leave / can't remove").
        _today = datetime.date.today()
        stale = Event.objects.create(
            event_name="Stale Scrim", competition_type="scrim", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_mode="virtual",
            start_date=_today - datetime.timedelta(days=8),
            end_date=_today - datetime.timedelta(days=7),   # ended a week ago
            registration_open_date=_D, registration_end_date=_D,
            prizepool="$0", event_rules="rules", event_status="upcoming",  # stale raw status
            registration_link="https://afc.test/reg", number_of_stages=1,
            creator=self.owner, is_draft=False,
        )
        tt = TournamentTeam.objects.create(event=stale, team=self.team)
        self._roster(tt, self.member)                       # member IS on the (stale) event roster
        # Owner kick is allowed...
        res = self._kick(self.owner_tok, self.member.user_id)
        self.assertEqual(res.status_code, 200, res.json())
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())


class RuleC_PositionTransferWindowTests(TestCase):
    """Rule C: in_game_role (position) editable only while the transfer window is open."""

    def setUp(self):
        self.client = Client()
        self.owner, self.owner_tok = _make_user("rc_owner")
        self.player, _ = _make_user("rc_player")
        self.team = Team.objects.create(
            team_name="Pos FC", team_tag="POS", country="NG", join_settings="open",
            team_owner=self.owner, team_creator=self.owner,
        )
        # Owner manages the roster (see _can_manage_roster). The player is a plain member who
        # starts with the "rusher" position.
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")
        self.tm = TeamMembers.objects.create(
            team=self.team, member=self.player, management_role="member", in_game_role="rusher",
        )

    def _manage(self, updates):
        return self.client.post("/team/manage-team-roster/",
                                {"team_id": self.team.team_id, "updates": updates},
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.owner_tok}")

    def test_position_change_blocked_when_window_closed(self):
        _make_season(window_open=False)
        res = self._manage([{"member_id": self.player.user_id, "in_game_role": "sniper"}])
        self.assertEqual(res.status_code, 403)
        self.assertIn("Positions are locked", res.json()["error"])
        self.tm.refresh_from_db()
        self.assertEqual(self.tm.in_game_role, "rusher")  # unchanged

    def test_position_change_allowed_when_window_open(self):
        _make_season(window_open=True)
        res = self._manage([{"member_id": self.player.user_id, "in_game_role": "sniper"}])
        self.assertEqual(res.status_code, 200)
        self.tm.refresh_from_db()
        self.assertEqual(self.tm.in_game_role, "sniper")  # updated

    def test_position_change_allowed_with_no_active_season(self):
        # No active season -> no window lock (matches the existing pattern): change applies.
        res = self._manage([{"member_id": self.player.user_id, "in_game_role": "support"}])
        self.assertEqual(res.status_code, 200)
        self.tm.refresh_from_db()
        self.assertEqual(self.tm.in_game_role, "support")

    def test_management_role_change_not_blocked_by_closed_window(self):
        # Rule C must NOT add a window restriction to management-role-only changes. Promoting the
        # member to "manager" (a STAFF role) is a player<->staff CROSSING, which the pre-existing
        # crossing rule already gates on the window — so it is expected to be blocked when closed,
        # but via the EXISTING per-member rule (200 + per-member failure), NOT via Rule C's
        # top-level 403. The key assertion: this is not a Rule C 403.
        _make_season(window_open=False)
        res = self._manage([{"member_id": self.player.user_id, "management_role": "manager"}])
        self.assertEqual(res.status_code, 200)  # batch contract preserved, not a Rule C 403


# ──────────────────────────────────────────────────────────────────────────
# Ban enforcement (afc_team.views)
#
# Ban rule (afc_auth.BannedPlayer): a player is currently banned when an is_active=True row
# exists AND ban_end_date is still in the FUTURE. An is_active row with a past ban_end_date is
# expired and must NOT block. The team-level sibling is Team.is_banned (driven by TeamBan).
#
# These tests cover the two afc_team guards added for the ban-enforcement feature:
#   exit_team (POST /team/exit-team/)  : a banned player, or a member of a banned team, cannot
#                                        leave; membership must survive the 403.
#   edit_team (POST /team/edit-team/)  : a banned team (or banned acting user) cannot be edited;
#                                        this is also the team-PROFILE update surface (name/logo/
#                                        join settings/social links), so it covers profile edits.
#
# Fixtures mirror the existing classes above: _make_user() builds User + SessionToken,
# auth is "Bearer <tok>". No network is touched.
# ──────────────────────────────────────────────────────────────────────────
from django.utils import timezone

from afc_auth.models import BannedPlayer


def _ban_player(user, *, active=True, days_remaining=30):
    """Create a BannedPlayer row for `user`. By default it is an ACTIVE, non-expired ban
    (blocks). Pass active=False or a negative days_remaining to build a non-blocking row
    (lifted, or expired) for the negative tests."""
    return BannedPlayer.objects.create(
        banned_player=user,
        ban_duration=days_remaining,
        ban_end_date=timezone.now() + datetime.timedelta(days=days_remaining),
        is_active=active,
        reason="test ban",
    )


class ExitTeamBanTests(TestCase):
    """exit_team must block a banned player or a member of a banned team (without deleting
    the membership), while leaving an unbanned player on an unbanned team unaffected."""

    def setUp(self):
        self.client = Client()
        self.owner, self.owner_tok = _make_user("eb_owner")
        self.member, self.member_tok = _make_user("eb_member")
        self.team = Team.objects.create(
            team_name="Ban Exit FC", team_tag="BEX", country="NG", join_settings="open",
            team_owner=self.owner, team_creator=self.owner,
        )
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")
        self.membership = TeamMembers.objects.create(team=self.team, member=self.member, management_role="member")
        # Transfer window OPEN + no tournament -> the other exit_team guards all pass, so only
        # the NEW ban guard is under test in these cases.
        _make_season(window_open=True)

    def _exit(self, tok):
        return self.client.post("/team/exit-team/", {}, content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {tok}")

    def test_banned_player_cannot_exit(self):
        # An active, non-expired BannedPlayer on the leaving member -> 403, membership kept.
        _ban_player(self.member)
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 403)
        self.assertIn("banned", res.json()["message"].lower())
        self.assertTrue(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_member_of_banned_team_cannot_exit(self):
        # Team-level ban (Team.is_banned, normally set by TeamBan) -> 403, membership kept.
        self.team.is_banned = True
        self.team.save(update_fields=["is_banned"])
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 403)
        self.assertIn("banned", res.json()["message"].lower())
        self.assertTrue(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_unbanned_player_on_unbanned_team_can_exit(self):
        # Clean player + clean team (window open, no tournament) -> leave still works.
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())

    def test_expired_ban_does_not_block_exit(self):
        # A still-is_active row whose ban_end_date is in the PAST is expired -> must NOT block.
        _ban_player(self.member, days_remaining=-1)
        res = self._exit(self.member_tok)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TeamMembers.objects.filter(pk=self.membership.pk).exists())


class EditTeamBanTests(TestCase):
    """edit_team must block edits to a banned team (or by a banned owner) and leave an
    unbanned team editable. edit_team is also the team-profile update endpoint."""

    def setUp(self):
        self.client = Client()
        self.owner, self.owner_tok = _make_user("etb_owner")
        self.team = Team.objects.create(
            team_name="Ban Edit FC", team_tag="BED", country="NG", join_settings="by_request",
            team_owner=self.owner, team_creator=self.owner,
        )
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")

    def _edit(self, tok, **data):
        payload = {"team_id": self.team.team_id, **data}
        return self.client.post("/team/edit-team/", payload,
                                HTTP_AUTHORIZATION=f"Bearer {tok}")

    def test_banned_team_cannot_be_edited(self):
        self.team.is_banned = True
        self.team.save(update_fields=["is_banned"])
        res = self._edit(self.owner_tok, join_settings="open")
        self.assertEqual(res.status_code, 403)
        self.assertIn("banned", res.json()["message"].lower())
        self.team.refresh_from_db()
        self.assertEqual(self.team.join_settings, "by_request")  # unchanged

    def test_banned_owner_cannot_edit_team(self):
        _ban_player(self.owner)
        res = self._edit(self.owner_tok, join_settings="open")
        self.assertEqual(res.status_code, 403)
        self.assertIn("banned", res.json()["message"].lower())
        self.team.refresh_from_db()
        self.assertEqual(self.team.join_settings, "by_request")  # unchanged

    def test_unbanned_team_can_be_edited(self):
        res = self._edit(self.owner_tok, join_settings="open")
        self.assertEqual(res.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.join_settings, "open")  # updated


class EditTeamTagTests(TestCase):
    """edit_team must let the team owner set / change / clear team_tag, normalising it
    (strip + uppercase, max 5 chars, letters+digits only). The tag also feeds team search
    (search_teams) and OCR name matching, so the normalisation matters beyond this endpoint."""

    def setUp(self):
        self.client = Client()
        self.owner, self.owner_tok = _make_user("ett_owner")
        self.team = Team.objects.create(
            team_name="Tag Edit FC", team_tag="OLD", country="NG", join_settings="open",
            team_owner=self.owner, team_creator=self.owner,
        )
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")

    def _edit(self, **data):
        payload = {"team_id": self.team.team_id, **data}
        return self.client.post("/team/edit-team/", payload,
                                HTTP_AUTHORIZATION=f"Bearer {self.owner_tok}")

    def test_set_tag_is_uppercased_and_trimmed(self):
        res = self._edit(team_tag=" afc ")
        self.assertEqual(res.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_tag, "AFC")  # stripped + uppercased

    def test_empty_tag_clears_it(self):
        res = self._edit(team_tag="")
        self.assertEqual(res.status_code, 200)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.team_tag)  # "" -> NULL

    def test_omitting_tag_leaves_it_unchanged(self):
        # team_tag key absent -> existing tag must survive an otherwise-valid edit.
        res = self._edit(join_settings="by_request")
        self.assertEqual(res.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_tag, "OLD")  # untouched

    def test_too_long_tag_is_rejected(self):
        res = self._edit(team_tag="TOOLONG")
        self.assertEqual(res.status_code, 400)
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_tag, "OLD")  # unchanged on 400

    def test_symbol_tag_is_rejected(self):
        res = self._edit(team_tag="A B")
        self.assertEqual(res.status_code, 400)
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_tag, "OLD")  # unchanged on 400
