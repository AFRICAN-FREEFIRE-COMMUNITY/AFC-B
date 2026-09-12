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
    - a reminder reaches only the unpicked, in-app and by email, once per
      10 minutes; a team registered in ANOTHER event cannot pick           (RemindAndAccessTests)

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
    def test_race_on_one_card_has_one_winner(self):
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
        # 9 competitors fill 3 x 3 exactly; the tenth has nowhere to go at close
        for i in range(7, 10):
            cap, _ = _user(f"dr_late{i}")
            team = Team.objects.create(team_name=f"Late {i}", team_owner=cap, team_creator=cap)
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
            StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
        draw = services.deal(self.stage, self.admin)
        services.open_draw(draw, timezone.now() + timedelta(hours=1))
        cap, _ = _user("dr_tenth")
        team = Team.objects.create(team_name="Tenth", team_owner=cap, team_creator=cap)
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

    def test_remind_reaches_only_the_unpicked_once_per_ten_minutes(self):
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
        # a second one right away is refused
        resp = _post(self.admin_tok, f"/draws/{self.draw.draw_id}/remind/")
        self.assertEqual(resp.status_code, 429, resp.content)
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

