"""
afc_draws/tests.py - the group draw, Phase 1 (owner 2026-09-12).

What must stay true, and the test that holds it:
    - the deal is even across groups and the seal verifies at close       (DealTests)
    - only whoever may seed the stage may create/open/close/reset          (LifecycleTests)
    - a captain may pick, a plain member may not, another club may not     (PickTests)
    - one card per team, a taken card is refused, a race has one winner    (PickTests)
    - a pick writes the ordinary StageGroupCompetitor row                  (PickTests)
    - close deals every straggler exactly once; a late competitor gets an
      extra card in the smallest group                                     (CloseTests)
    - a draw past its close time closes itself on the next read            (CloseTests)
    - the random seeders refuse while a draw is open                       (GuardTests)
    - opening notifies every captain, closing notifies the auto-placed     (NotifyTests)
    - the stage's group size caps the deal and the extra card               (CapacityTests)
    - the close time and the straggler choice can change while open; a
      close that leaves the rest unplaced lists and notifies them          (WindowTests)
    - a reminder reaches only the unpicked, in-app and by email, and only
      when the organizer presses the button (a double press is refused);
      a team registered in ANOTHER event cannot pick                       (RemindAndAccessTests)
    - a board limited to participants hides its cards from guests and
      outsiders, shows them to any member of a registered club and to
      the organizer; the choice can change in any state                    (VisibilityTests)
    - on a round robin stage the cards hide base groups A, B, C (not the day
      lobbies); a pick places the team in its base group and the two lobbies
      it plays; close, the extra card and reset follow; saving the event
      form during the draw keeps the groups, the cards and the picks (#570) (RoundRobinDrawTests)

Run: python manage.py test afc_draws
"""
import datetime
import json
import threading
from datetime import date, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User, UserProfile
from afc_draws import tasks as draw_tasks
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event, Match, Leaderboard, StageCompetitor, StageGroupCompetitor, StageGroups, Stages,
    TournamentTeam,
)

from . import services
from .models import DrawCard, StageDraw


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(
        user=u, token=f"tok_{username}"[:32],
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )
    return u, tok.token


def _event(creator, **overrides):
    fields = dict(
        event_name="Draw Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=16, event_mode="single",
        start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=4),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=2),
        number_of_stages=1, creator=creator, is_public=True, is_draft=False,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


def _stage(event, groups=3):
    today = date.today()
    st = Stages.objects.create(
        event=event, stage_name="Group Stage", start_date=today, end_date=today,
        number_of_groups=groups, stage_format="br - normal", teams_qualifying_from_stage=1,
    )
    for i in range(groups):
        StageGroups.objects.create(
            stage=st, group_name=f"Group {chr(65 + i)}", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=1,
        )
    return st


def _post(token, path, body=None):
    return Client().post(path, json.dumps(body or {}), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _get(path, token=None):
    kw = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return Client().get(path, **kw)


class DrawFixture(TestCase):
    """An event with one 3-group stage and 7 registered clubs, each with a captain (owner) and a
    plain member. Team i's captain token is self.tokens[i]."""

    n_teams = 7

    def setUp(self):
        # open_draw and remind queue afc_draws.tasks.send_draw_emails; patched here (a patcher
        # in setUp reaches every subclass, a class decorator would not) so no test publishes to
        # the broker: on the VPS rig that Redis feeds the PRODUCTION worker. Tests that care
        # about the emails read self.mail.
        patcher = patch("afc_draws.tasks.send_draw_emails.delay")
        self.mail = patcher.start()
        self.addCleanup(patcher.stop)
        self.admin, self.admin_tok = _user("dr_admin", role="admin")
        self.event = _event(self.admin)
        self.stage = _stage(self.event, groups=3)
        self.teams, self.tts, self.tokens, self.members = [], [], [], []
        for i in range(self.n_teams):
            cap, tok = _user(f"dr_cap{i}")
            member, _ = _user(f"dr_mem{i}")
            team = Team.objects.create(team_name=f"Club {i}", team_owner=cap, team_creator=cap)
            TeamMembers.objects.create(team=team, member=cap, management_role="team_captain")
            TeamMembers.objects.create(team=team, member=member, management_role="member")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
            StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
            self.teams.append(team)
            self.tts.append(tt)
            self.tokens.append(tok)
            self.members.append(member)
        self.member_tok = SessionToken.objects.get(user=self.members[0]).token


class DealTests(DrawFixture):
    def test_even_deal_and_sealed_mapping(self):
        draw = services.deal(self.stage, self.admin)
        cards = list(draw.cards.all())
        self.assertEqual(len(cards), 7)
        per_group = {}
        for c in cards:
            per_group[c.stage_group_id] = per_group.get(c.stage_group_id, 0) + 1
        # 7 cards over 3 groups: 3, 2, 2
        self.assertEqual(sorted(per_group.values()), [2, 2, 3])
        self.assertEqual(sorted(c.number for c in cards), list(range(1, 8)))
        self.assertEqual(draw.commitment, services.commitment_for(draw.salt, services.mapping_of(cards)))
        self.assertEqual(draw.status, "draft")

    def test_refuses_without_groups_or_competitors_or_over_seeded_groups(self):
        empty_event = _event(self.admin, event_name="Empty")
        bare = Stages.objects.create(
            event=empty_event, stage_name="S", start_date=date.today(), end_date=date.today(),
            number_of_groups=0, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        with self.assertRaises(services.DrawError):
            services.deal(bare, self.admin)
        StageGroupCompetitor.objects.create(stage_group=self.stage.groups.first(), tournament_team=self.tts[0])
        with self.assertRaises(services.DrawError) as ctx:
            services.deal(self.stage, self.admin)
        self.assertEqual(ctx.exception.status, 409)

    def test_board_hides_groups_while_open_and_publishes_salt_when_closed(self):
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        board = services.serialize_board(draw)
        self.assertIsNone(board["salt"])
        self.assertIsNone(board["mapping"])
        self.assertTrue(all(c["group_name"] is None for c in board["cards"]))
        services.close_draw(draw)
        board = services.serialize_board(StageDraw.objects.get(pk=draw.pk))
        self.assertEqual(board["status"], "closed")
        self.assertEqual(board["commitment"], services.commitment_for(board["salt"], board["mapping"]))


class LifecycleTests(DrawFixture):
    def test_only_a_manager_may_create(self):
        resp = _post(self.tokens[0], f"/draws/stages/{self.stage.stage_id}/create/")
        self.assertEqual(resp.status_code, 403, resp.content)
        resp = _post(self.admin_tok, f"/draws/stages/{self.stage.stage_id}/create/")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["cards_total"], 7)

    def test_open_needs_a_future_close_time(self):
        draw = services.deal(self.stage, self.admin)
        resp = _post(self.admin_tok, f"/draws/{draw.draw_id}/open/", {"closes_at": "nonsense"})
        self.assertEqual(resp.status_code, 400)
        past = (timezone.now() - timedelta(minutes=1)).isoformat()
        resp = _post(self.admin_tok, f"/draws/{draw.draw_id}/open/", {"closes_at": past})
        self.assertEqual(resp.status_code, 400)
        future = (timezone.now() + timedelta(hours=1)).isoformat()
        resp = _post(self.admin_tok, f"/draws/{draw.draw_id}/open/", {"closes_at": future})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "open")

    def test_reset_deletes_the_draw_and_its_group_rows_unless_results_exist(self):
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        cap0 = User.objects.get(username="dr_cap0")
        services.pick(draw, cap0, 1)
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group__stage=self.stage).count(), 1)
        resp = _post(self.admin_tok, f"/draws/{draw.draw_id}/reset/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(StageDraw.objects.filter(pk=draw.pk).exists())
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group__stage=self.stage).count(), 0)

        # Now with a result on the stage: refused.
        draw = services.deal(self.stage, self.admin)
        group = self.stage.groups.first()
        lb = Leaderboard.objects.create(
            leaderboard_name="LB", event=self.event, stage=self.stage, group=group, creator=self.admin,
            placement_points={"1": 12}, kill_point=1.0, leaderboard_method="manual",
        )
        Match.objects.create(leaderboard=lb, group=group, match_number=1, match_map="bermuda",
                             scoring_settings={"placement_points": {"1": 12}, "kill_point": 1},
                             result_inputted=True)
        resp = _post(self.admin_tok, f"/draws/{draw.draw_id}/reset/")
        self.assertEqual(resp.status_code, 409)


class PickTests(DrawFixture):
    def setUp(self):
        super().setUp()
        self.draw = services.deal(self.stage, self.admin)
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))

    def _pick(self, token, number, **extra):
        return _post(token, f"/draws/{self.draw.draw_id}/pick/", {"card_number": number, **extra})

    def test_captain_picks_and_the_group_row_is_written(self):
        resp = self._pick(self.tokens[0], 3)
        self.assertEqual(resp.status_code, 200, resp.content)
        card = DrawCard.objects.get(draw=self.draw, number=3)
        self.assertEqual(card.tournament_team_id, self.tts[0].tournament_team_id)
        self.assertEqual(card.via, "pick")
        self.assertTrue(StageGroupCompetitor.objects.filter(
            stage_group=card.stage_group, tournament_team=self.tts[0]).exists())
        body = resp.json()
        mine = body["viewer"]["competitors"][0]
        self.assertEqual((mine["card_number"], mine["group_name"]), (3, card.stage_group.group_name))
        self.assertFalse(body["viewer"]["can_pick"])
        # everyone sees the taken card with the revealed group
        taken = [c for c in body["cards"] if c["number"] == 3][0]
        self.assertEqual((taken["taken"], taken["competitor"]), (True, "Club 0"))

    def test_plain_member_and_outsider_cannot_pick(self):
        resp = self._pick(self.member_tok, 1)
        self.assertEqual(resp.status_code, 403, resp.content)
        stranger, stranger_tok = _user("dr_stranger")
        resp = self._pick(stranger_tok, 1)
        self.assertEqual(resp.status_code, 403)

    def test_one_card_per_team_and_taken_card_refused(self):
        self.assertEqual(self._pick(self.tokens[0], 1).status_code, 200)
        self.assertEqual(self._pick(self.tokens[0], 2).status_code, 409)     # second pick
        self.assertEqual(self._pick(self.tokens[1], 1).status_code, 409)     # taken card
        self.assertEqual(self._pick(self.tokens[1], 99).status_code, 404)    # no such card
        self.assertEqual(self._pick(self.tokens[1], "x").status_code, 400)   # not a number

    def test_pick_refused_once_closed(self):
        services.close_draw(self.draw)
        self.assertEqual(self._pick(self.tokens[0], 1).status_code, 409)

    def test_board_is_public_and_viewer_specific(self):
        resp = _get(f"/draws/{self.draw.draw_id}/board/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["viewer"])
        resp = _get(f"/draws/{self.draw.draw_id}/board/", self.tokens[2])
        self.assertTrue(resp.json()["viewer"]["can_pick"])
        self.assertEqual(resp.json()["viewer"]["competitors"][0]["name"], "Club 2")
        resp = _get(f"/draws/events/{self.event.event_id}/", self.tokens[2])
        self.assertEqual(len(resp.json()["draws"]), 1)


class ConcurrentPickTests(TransactionTestCase):
    """Two captains tap the same card in the same instant: exactly one holds it afterwards.
    TransactionTestCase because the two picks must run in real, separate transactions."""

    @patch("afc_draws.tasks.send_draw_emails.delay")
    def test_race_on_one_card_has_one_winner(self, _mail):
        admin, _ = _user("rc_admin", role="admin")
        event = _event(admin)
        stage = _stage(event, groups=2)
        caps = []
        for i in range(2):
            cap, _ = _user(f"rc_cap{i}")
            team = Team.objects.create(team_name=f"Racer {i}", team_owner=cap, team_creator=cap)
            TeamMembers.objects.create(team=team, member=cap, management_role="team_captain")
            tt = TournamentTeam.objects.create(event=event, team=team, registered_by=cap)
            StageCompetitor.objects.create(stage=stage, tournament_team=tt)
            caps.append(cap)
        draw = services.deal(stage, admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))

        results = {}

        def go(idx):
            try:
                services.pick(draw, caps[idx], 1)
                results[idx] = "ok"
            except services.DrawError as exc:
                results[idx] = exc.status
            finally:
                connection.close()

        threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(str(v) for v in results.values()), ["409", "ok"])
        card = DrawCard.objects.get(draw=draw, number=1)
        self.assertTrue(card.is_taken)
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group__stage=stage).count(), 1)


class CloseTests(DrawFixture):
    def setUp(self):
        super().setUp()
        self.draw = services.deal(self.stage, self.admin)
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))

    def test_close_places_every_straggler_exactly_once(self):
        cap0 = User.objects.get(username="dr_cap0")
        cap1 = User.objects.get(username="dr_cap1")
        services.pick(self.draw, cap0, 2)
        services.pick(self.draw, cap1, 5)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/close/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["status"], "closed")
        self.assertEqual(body["cards_taken"], 7)
        rows = StageGroupCompetitor.objects.filter(stage_group__stage=self.stage)
        self.assertEqual(rows.count(), 7)
        self.assertEqual(rows.values("tournament_team").distinct().count(), 7)
        # the two real picks kept their cards, the rest are auto
        cards = {c.number: c for c in self.draw.cards.all()}
        self.assertEqual(cards[2].via, "pick")
        self.assertEqual(cards[5].via, "pick")
        self.assertEqual(sum(1 for c in cards.values() if c.via == "auto"), 5)
        # the seal verifies against the published mapping
        self.assertEqual(body["commitment"], services.commitment_for(body["salt"], body["mapping"]))

    def test_a_late_competitor_gets_an_extra_card_in_the_smallest_group(self):
        late_cap, _ = _user("dr_late")
        team = Team.objects.create(team_name="Late FC", team_owner=late_cap, team_creator=late_cap)
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=late_cap)
        StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
        services.close_draw(self.draw)
        self.assertEqual(self.draw.cards.count(), 8)
        extra = self.draw.cards.get(number=8)
        self.assertEqual(extra.tournament_team_id, tt.tournament_team_id)
        # 7 cards were 3/2/2; the extra lands on one of the two-card groups
        counts = {}
        for c in self.draw.cards.all():
            counts[c.stage_group_id] = counts.get(c.stage_group_id, 0) + 1
        self.assertEqual(sorted(counts.values()), [2, 3, 3])

    def test_lazy_close_on_read_after_the_deadline(self):
        StageDraw.objects.filter(pk=self.draw.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
        resp = _get(f"/draws/{self.draw.draw_id}/board/")
        self.assertEqual(resp.json()["status"], "closed")
        self.assertEqual(resp.json()["cards_taken"], 7)


class GuardTests(DrawFixture):
    def test_seeder_refuses_while_a_draw_is_open(self):
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        resp = _post(self.admin_tok, "/events/seed-stage-competitors-to-groups-team/",
                     {"stage_id": self.stage.stage_id, "clear_existing": True})
        self.assertEqual(resp.status_code, 409, resp.content)
        services.close_draw(draw)
        self.assertFalse(services.draw_is_open(self.stage))


class NotifyTests(DrawFixture):
    def test_open_notifies_captains_not_plain_members(self):
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        opened = Notifications.objects.filter(notification_type="group_draw_open")
        self.assertEqual(opened.count(), 7)
        self.assertEqual(set(opened.values_list("user__username", flat=True)),
                         {f"dr_cap{i}" for i in range(7)})
        self.assertEqual(opened.first().target_type, "event")

    def test_close_notifies_only_the_auto_placed(self):
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        services.pick(draw, User.objects.get(username="dr_cap0"), 1)
        services.close_draw(draw)
        auto = Notifications.objects.filter(notification_type="group_draw_auto")
        self.assertEqual(auto.count(), 6)
        self.assertNotIn("dr_cap0", set(auto.values_list("user__username", flat=True)))

class CapacityTests(DrawFixture):
    """The stage's competitors_per_group (group_capacity.py) decides the deal."""

    def test_deal_refuses_when_the_pool_does_not_fit(self):
        Stages.objects.filter(pk=self.stage.pk).update(competitors_per_group=2)   # 3 x 2 = 6 < 7
        self.stage.refresh_from_db()
        resp = _post(self.admin_tok, f"/draws/stages/{self.stage.stage_id}/create/")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("7 teams for 3 groups of 2: room for 6", resp.json()["message"])
        self.assertFalse(StageDraw.objects.filter(stage=self.stage).exists())

    def test_deal_within_the_size_is_even_and_the_board_says_the_size(self):
        Stages.objects.filter(pk=self.stage.pk).update(competitors_per_group=3)
        self.stage.refresh_from_db()
        draw = services.deal(self.stage, self.admin)
        counts = {}
        for c in draw.cards.all():
            counts[c.stage_group_id] = counts.get(c.stage_group_id, 0) + 1
        self.assertEqual(sorted(counts.values()), [2, 2, 3])
        self.assertEqual(services.serialize_board(draw)["per_group"], 3)

    def test_a_late_competitor_is_left_unplaced_when_every_group_is_full(self):
        Stages.objects.filter(pk=self.stage.pk).update(competitors_per_group=3)
        self.stage.refresh_from_db()
        # 7 + 2 = 9 competitors fill 3 x 3 exactly; a tenth has nowhere to go at close
        for i in range(7, 9):
            cap, _ = _user(f"dr_late{i}")
            team = Team.objects.create(team_name=f"Late {i}", team_owner=cap, team_creator=cap)
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
            StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        cap, _ = _user("dr_tenth")
        team = Team.objects.create(team_name="Tenth", team_owner=cap, team_creator=cap)
        TeamMembers.objects.create(team=team, member=cap, management_role="team_captain")
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
        StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
        services.close_draw(draw)
        self.assertEqual(draw.cards.count(), 9)
        board = services.serialize_board(draw)
        self.assertEqual(board["unpicked"], ["Tenth"])
        self.assertEqual(Notifications.objects.filter(notification_type="group_draw_unplaced", user=cap).count(), 1)


class WindowTests(DrawFixture):
    def setUp(self):
        super().setUp()
        self.draw = services.deal(self.stage, self.admin)

    def test_open_records_the_choice_and_the_window_can_change(self):
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/open/",
                     {"closes_at": (timezone.now() + timedelta(hours=1)).isoformat(), "auto_place_at_close": False})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["auto_place_at_close"])
        later = timezone.now() + timedelta(hours=5)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/",
                     {"closes_at": later.isoformat(), "auto_place_at_close": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["auto_place_at_close"])
        self.draw.refresh_from_db()
        self.assertEqual(int(self.draw.closes_at.timestamp()), int(later.timestamp()))
        # the past is refused, and so is an empty change
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/",
                     {"closes_at": (timezone.now() - timedelta(minutes=1)).isoformat()})
        self.assertEqual(resp.status_code, 400)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/", {})
        self.assertEqual(resp.status_code, 400)
        # a plain member may not touch the window
        resp = _post(self.member_tok, f"/draws/{self.draw.draw_id}/window/", {"auto_place_at_close": False})
        self.assertEqual(resp.status_code, 403)

    def test_close_can_leave_the_rest_unplaced(self):
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))
        services.pick(self.draw, User.objects.get(username="dr_cap0"), 1)
        services.pick(self.draw, User.objects.get(username="dr_cap1"), 2)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/close/", {"place_rest": False})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["status"], "closed")
        self.assertEqual(body["cards_taken"], 2)
        self.assertEqual(len(body["unpicked"]), 5)
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group__stage=self.stage).count(), 2)
        self.assertEqual(Notifications.objects.filter(notification_type="group_draw_unplaced").count(), 5)
        self.assertEqual(Notifications.objects.filter(notification_type="group_draw_auto").count(), 0)
        # the seal still verifies
        self.assertEqual(body["commitment"], services.commitment_for(body["salt"], body["mapping"]))

    def test_the_deadline_honours_the_choice(self):
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1), auto_place_at_close=False)
        StageDraw.objects.filter(pk=self.draw.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
        board = _get(f"/draws/{self.draw.draw_id}/board/").json()
        self.assertEqual(board["status"], "closed")
        self.assertEqual(board["cards_taken"], 0)
        self.assertEqual(len(board["unpicked"]), 7)


class RemindAndAccessTests(DrawFixture):
    def setUp(self):
        super().setUp()
        self.draw = services.deal(self.stage, self.admin)

    def test_open_emails_every_captain(self):
        mail = self.mail
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))
        self.assertEqual(mail.call_count, 1)
        ids, subject = mail.call_args.args[0], mail.call_args.args[1]
        self.assertEqual(len(ids), 7)
        self.assertIn("Group draw open", subject)

    def test_remind_reaches_only_the_unpicked_and_refuses_a_double_press(self):
        mail = self.mail
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))
        mail.reset_mock()
        services.pick(self.draw, User.objects.get(username="dr_cap0"), 3)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/remind/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["reminded"], 6)
        reminded = Notifications.objects.filter(notification_type="group_draw_reminder")
        self.assertEqual(set(reminded.values_list("user__username", flat=True)),
                         {f"dr_cap{i}" for i in range(1, 7)})
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(len(mail.call_args.args[0]), 6)
        # a second press within the minute is refused (a double-send guard, not a schedule)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/remind/")
        self.assertEqual(resp.status_code, 429, resp.content)
        # a minute later the organizer may send again
        StageDraw.objects.filter(pk=self.draw.pk).update(last_reminder_at=timezone.now() - timedelta(seconds=61))
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/remind/")
        self.assertEqual(resp.status_code, 200, resp.content)
        # a plain member may not send one
        resp = _post(self.member_tok, f"/draws/{self.draw.draw_id}/remind/")
        self.assertEqual(resp.status_code, 403)

    def test_the_email_task_sends_one_per_recipient(self):
        with patch("afc_auth.views.send_email", return_value=True) as send:
            ids = [User.objects.get(username=f"dr_cap{i}").user_id for i in range(3)]
            sent = draw_tasks.send_draw_emails(ids, "Subject", "<b>lead</b>", "tail", "https://x/e")
        self.assertEqual(sent, 3)
        self.assertEqual({c.args[0] for c in send.call_args_list}, {f"dr_cap{i}@x.com" for i in range(3)})
        self.assertEqual(send.call_args_list[0].kwargs["language"], "en")

    def test_a_team_from_another_event_cannot_pick(self):
        services.open_draw(self.draw, timezone.now() + timedelta(hours=1))
        other_cap, other_tok = _user("dr_other")
        other_event = _event(self.admin, event_name="Other Cup")
        team = Team.objects.create(team_name="Elsewhere", team_owner=other_cap, team_creator=other_cap)
        TeamMembers.objects.create(team=team, member=other_cap, management_role="team_captain")
        tt = TournamentTeam.objects.create(event=other_event, team=team, registered_by=other_cap)
        resp = _post(other_tok, f"/draws/{self.draw.draw_id}/pick/",
                     {"card_number": 1, "tournament_team_id": tt.tournament_team_id})
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(self.draw.cards.get(number=1).is_taken)


class VisibilityTests(DrawFixture):
    def setUp(self):
        super().setUp()
        self.draw = services.deal(self.stage, self.admin)

    def test_everyone_by_default(self):
        board = _get(f"/draws/{self.draw.draw_id}/board/").json()
        self.assertEqual(board["visibility"], "everyone")
        self.assertEqual(len(board["cards"]), 7)

    def test_participants_only_hides_the_board_from_guests_and_outsiders(self):
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/", {"visibility": "participants"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["visibility"], "participants")
        # a guest: the stub, no cards, no seal
        guest = _get(f"/draws/{self.draw.draw_id}/board/").json()
        self.assertTrue(guest["hidden"])
        self.assertNotIn("cards", guest)
        self.assertNotIn("commitment", guest)
        # the event list carries the same stub
        listed = _get(f"/draws/events/{self.event.event_id}/").json()["draws"][0]
        self.assertTrue(listed["hidden"])
        # a signed-in user with no club in this event: hidden too
        _, other_tok = _user("dr_outsider")
        self.assertTrue(_get(f"/draws/{self.draw.draw_id}/board/", other_tok).json()["hidden"])
        # a plain MEMBER of a registered club (not the captain) sees it
        member_board = _get(f"/draws/{self.draw.draw_id}/board/", self.member_tok).json()
        self.assertNotIn("hidden", member_board)
        self.assertEqual(len(member_board["cards"]), 7)
        # the captain and the organizer see it
        self.assertEqual(len(_get(f"/draws/{self.draw.draw_id}/board/", self.tokens[0]).json()["cards"]), 7)
        self.assertEqual(len(_get(f"/draws/{self.draw.draw_id}/board/", self.admin_tok).json()["cards"]), 7)
        # back to everyone, in draft state (visibility is not tied to the window)
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/", {"visibility": "everyone"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(_get(f"/draws/{self.draw.draw_id}/board/").json()["cards"]), 7)
        # nonsense is refused, and so is a plain member changing it
        self.assertEqual(_post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/", {"visibility": "nobody"}).status_code, 400)
        self.assertEqual(_post(self.member_tok, f"/draws/{self.draw.draw_id}/window/", {"visibility": "participants"}).status_code, 403)

    def test_close_time_still_needs_an_open_draw(self):
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/window/",
                     {"closes_at": (timezone.now() + timedelta(hours=1)).isoformat()})
        self.assertEqual(resp.status_code, 409, resp.content)



# ── round robin stages (owner 13 Nov 2026, AFC inbox #570) ──────────────────────────────────────────
# "It's supposed to put teams into a single group like A or B or C": on the NFPL Semi Finals (a
# br - round robin stage, base groups A B C, day lobbies Day 1: A+B, Day 2: A+C, Day 3: B+C) the draw
# dealt its cards over the LOBBIES, so every team landed in one day. These hold the fix: the cards
# hide base groups, and a pick places the team in its base group and in both lobbies it plays.

def _round_robin_stage(event, user):
    """A br - round robin stage the way create_event builds one: base groups A, B, C and one lobby
    per pairing, each lobby's source_groups set (views._materialise_round_robin_lobby)."""
    from afc_tournament_and_scrims import round_robin
    from afc_tournament_and_scrims.models import RoundRobinGroup
    from afc_tournament_and_scrims.views import _materialise_round_robin_lobby

    today = date.today()
    st = Stages.objects.create(
        event=event, stage_name="Semi Finals", start_date=today, end_date=today,
        number_of_groups=3, stage_format="br - round robin", teams_qualifying_from_stage=4,
    )
    bases = [RoundRobinGroup.objects.create(stage=st, label=label, order=i) for i, label in enumerate("ABC")]
    by_id = {g.group_id: g for g in bases}
    for spec in round_robin.round_robin_schedule([g.group_id for g in bases], games_per_day=1):
        _materialise_round_robin_lobby(
            st, event, user, spec, [by_id[i] for i in spec["source_group_ids"]], today, datetime.time(19, 0))
    return st, bases


class RoundRobinDrawTests(DrawFixture):
    n_teams = 6

    def setUp(self):
        super().setUp()
        self.rr, self.bases = _round_robin_stage(self.event, self.admin)
        for tt in self.tts:
            StageCompetitor.objects.create(stage=self.rr, tournament_team=tt)
        self.lobbies = list(StageGroups.objects.filter(stage=self.rr).order_by("game_day"))

    def _lobbies_of(self, tt):
        return sorted(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.rr, tournament_team=tt)
            .values_list("stage_group__group_name", flat=True)
        )

    def _open(self):
        draw = services.deal(self.rr, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        return draw

    def test_deal_is_over_the_base_groups_not_the_lobbies(self):
        self.assertEqual([lb.group_name for lb in self.lobbies], ["Day 1: A+B", "Day 2: A+C", "Day 3: B+C"])
        draw = services.deal(self.rr, self.admin)
        cards = list(draw.cards.all())
        self.assertEqual(len(cards), 6)
        self.assertTrue(all(c.stage_group_id is None and c.round_robin_group_id for c in cards))
        per_base = {}
        for c in cards:
            per_base[c.round_robin_group.label] = per_base.get(c.round_robin_group.label, 0) + 1
        self.assertEqual(per_base, {"A": 2, "B": 2, "C": 2})
        self.assertEqual(draw.commitment, services.commitment_for(draw.salt, services.mapping_of(cards)))

    def test_pick_places_in_base_group_and_its_lobbies(self):
        draw = self._open()
        cap0 = User.objects.get(username="dr_cap0")
        card = services.pick(draw, cap0, 1)
        base = card.round_robin_group
        self.assertEqual(
            list(base.teams.values_list("tournament_team_id", flat=True)), [self.tts[0].tournament_team_id])
        # in both lobbies that base group plays, never in the third
        expected = sorted(lb.group_name for lb in self.lobbies if base.label in lb.group_name.split(": ")[1])
        self.assertEqual(len(expected), 2)
        self.assertEqual(self._lobbies_of(self.tts[0]), expected)
        # the board talks in single groups
        board = services.serialize_board(StageDraw.objects.get(pk=draw.pk), viewer=cap0)
        self.assertEqual([g["group_name"] for g in board["groups"]], ["Group A", "Group B", "Group C"])
        self.assertEqual(board["cards"][0]["group_name"], f"Group {base.label}")
        self.assertEqual(board["cards"][0]["group_id"], base.group_id)
        self.assertEqual(board["viewer"]["competitors"][0]["group_name"], f"Group {base.label}")
        self.assertEqual(len(board["unpicked"]), 5)

    def test_a_team_already_in_a_base_group_cannot_pick_and_is_no_straggler(self):
        draw = self._open()
        self.bases[0].teams.add(self.tts[1])
        with self.assertRaises(services.DrawError) as ctx:
            services.pick(draw, User.objects.get(username="dr_cap1"), 2)
        self.assertEqual(ctx.exception.status, 409)
        board = services.serialize_board(StageDraw.objects.get(pk=draw.pk))
        self.assertEqual(len(board["unpicked"]), 5)

    def test_close_places_everyone_in_one_base_group_and_the_seal_verifies(self):
        draw = self._open()
        services.pick(draw, User.objects.get(username="dr_cap0"), 3)
        services.close_draw(draw)
        for tt in self.tts:
            homes = [g.label for g in self.bases if g.teams.filter(pk=tt.pk).exists()]
            self.assertEqual(len(homes), 1, homes)
            self.assertEqual(len(self._lobbies_of(tt)), 2)
        self.assertEqual(sorted(g.teams.count() for g in self.bases), [2, 2, 2])
        # each lobby holds its two base groups: 4 teams
        for lb in self.lobbies:
            self.assertEqual(StageGroupCompetitor.objects.filter(stage_group=lb).count(), 4)
        board = services.serialize_board(StageDraw.objects.get(pk=draw.pk))
        self.assertEqual(board["commitment"], services.commitment_for(board["salt"], board["mapping"]))
        self.assertTrue(all(c["group_name"].startswith("Group ") for c in board["cards"]))
        auto = Notifications.objects.filter(notification_type="group_draw_auto").first()
        self.assertTrue(auto.title.startswith("You were placed in Group "), auto.title)

    def test_late_team_gets_an_extra_card_in_a_base_group(self):
        draw = self._open()
        cap, _ = _user("dr_cap_late")
        team = Team.objects.create(team_name="Club Late", team_owner=cap, team_creator=cap)
        TeamMembers.objects.create(team=team, member=cap, management_role="team_captain")
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
        StageCompetitor.objects.create(stage=self.rr, tournament_team=tt)
        services.close_draw(draw)
        card = DrawCard.objects.get(draw=draw, tournament_team=tt)
        self.assertEqual(card.number, 7)
        self.assertIsNotNone(card.round_robin_group_id)
        self.assertTrue(card.round_robin_group.teams.filter(pk=tt.pk).exists())

    def test_reset_takes_the_team_out_of_its_base_group_and_lobbies(self):
        draw = self._open()
        services.pick(draw, User.objects.get(username="dr_cap0"), 1)
        services.reset(draw)
        self.assertFalse(StageDraw.objects.filter(pk=draw.pk).exists())
        self.assertEqual(sum(g.teams.count() for g in self.bases), 0)
        self.assertEqual(self._lobbies_of(self.tts[0]), [])
        # and a fresh deal is allowed again
        self.assertEqual(services.deal(self.rr, self.admin).cards.count(), 6)

    def test_refuses_to_deal_over_base_groups_that_hold_teams(self):
        self.bases[2].teams.add(self.tts[0])
        with self.assertRaises(services.DrawError) as ctx:
            services.deal(self.rr, self.admin)
        self.assertEqual(ctx.exception.status, 409)

    def test_saving_the_event_form_during_the_draw_keeps_groups_cards_and_picks(self):
        # The edit form rebuilt the base groups on every save (new ids), which would have cascaded
        # the draw's cards away; and its team lists may predate the latest pick.
        from afc_tournament_and_scrims.models import RoundRobinGroup
        from afc_tournament_and_scrims.views import _edit_round_robin_stage

        draw = self._open()
        card = services.pick(draw, User.objects.get(username="dr_cap0"), 1)
        ids_before = [g.group_id for g in self.bases]
        payload = {"stage_format": "br - round robin", "round_robin": {
            "round_robin_groups": [{"label": lab, "order": i, "team_ids": []} for i, lab in enumerate("ABC")],
            "generate_schedule": True, "games_per_day": 1}}
        _edit_round_robin_stage(self.rr, self.event, self.admin, payload)
        self.assertEqual(
            list(RoundRobinGroup.objects.filter(stage=self.rr).values_list("group_id", flat=True)), ids_before)
        self.assertEqual(DrawCard.objects.filter(draw=draw).count(), 6)
        base = RoundRobinGroup.objects.get(pk=card.round_robin_group_id)
        self.assertTrue(base.teams.filter(pk=self.tts[0].pk).exists())
        # the regenerated lobbies carry the pick again
        self.assertEqual(len(self._lobbies_of(self.tts[0])), 2)
