"""
Shared fixtures for the wager suites: a player with a profile old enough to wager, an event with
a stage, a group, a match and two real teams, the seeded templates, and the settings row.

Every test here runs under OUTBOUND_DELIVERY=outbox (afc/settings.py forces it under
`manage.py test`), so afc_wager.payments never calls Paystack: initialize_stake returns a local
URL, verify_stake answers success, transfers answer success at once.
"""
from datetime import date, time, timedelta

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserProfile, UserRoles
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, StageGroups, Stages, TournamentPlayerMatchStats, TournamentTeam,
    TournamentTeamMatchStats,
)
from afc_wager.management.commands.seed_wager_templates import Command as SeedTemplates
from afc_wager.models import Market, MarketOption, MarketTemplate, WagerSettings


def make_user(username, *, dob=None, roles=(), discord=None, whatsapp=""):
    user = User.objects.create(username=username, email=f"{username}@x.com", full_name=username.title(),
                               password="x", discord_id=discord)
    UserProfile.objects.create(user=user, date_of_birth=dob or date(2000, 1, 1), whatsapp_number=whatsapp)
    for name in roles:
        role, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
        UserRoles.objects.create(user=user, role=role)
    token = SessionToken.objects.create(user=user, token=f"tok_{username}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class WagerTestCase(TestCase):
    """One event, one match, two teams (Alpha, Bravo), the templates seeded, a player, an admin
    holding both wager roles, and a plain admin holding neither."""

    def setUp(self):
        SeedTemplates().handle()
        self.cfg = WagerSettings.get()
        self.client = Client()
        self.creator, _ = make_user("wcreator")
        self.player, self.player_auth = make_user("wplayer", whatsapp="+2348012345678", discord="1111")
        self.other, self.other_auth = make_user("wother", whatsapp="+2348012345679", discord="2222")
        self.admin, self.admin_auth = make_user("wadmin", roles=("wager_admin", "finance_admin"))
        self.head, self.head_auth = make_user("whead", roles=("head_admin",))
        self.plain_admin, self.plain_auth = make_user("wplain", roles=("news_admin",))

        today = date.today()
        self.event = Event.objects.create(
            event_name="Wager Cup", competition_type="tournament", participant_type="squad",
            event_type="online", max_teams_or_players=12, event_mode="single",
            start_date=today, end_date=today + timedelta(days=3),
            registration_open_date=today - timedelta(days=10), registration_end_date=today - timedelta(days=1),
            number_of_stages=1, creator=self.creator, event_status="ongoing",
            event_start_time=time(20, 0), event_end_time=time(22, 0),
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Finals", stage_order=1, start_date=today,
            end_date=today + timedelta(days=3), number_of_groups=1, stage_format="br - normal",
            teams_qualifying_from_stage=4, stage_status="ongoing",
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today, playing_time=time(19, 0),
            teams_qualifying=4, match_count=2, match_maps=["bermuda", "purgatory"],
        )
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Finals", event=self.event, stage=self.stage, group=self.group,
            creator=self.creator, leaderboard_method="manual", placement_points={}, kill_point=1.0,
        )
        self.match = Match.objects.create(leaderboard=self.leaderboard, group=self.group, match_map="bermuda", match_number=1)
        alpha = Team.objects.create(team_name="Alpha", team_tag="ALP", country="NG", join_settings="open",
                                    team_owner=self.creator, team_creator=self.creator)
        bravo = Team.objects.create(team_name="Bravo", team_tag="BRV", country="NG", join_settings="open",
                                    team_owner=self.creator, team_creator=self.creator)
        self.tt_alpha = TournamentTeam.objects.create(event=self.event, team=alpha)
        self.tt_bravo = TournamentTeam.objects.create(event=self.event, team=bravo)

    # ── helpers ──
    def make_market(self, *, template="match_winner", status=Market.OPEN, lock_in_minutes=60, **overrides):
        tpl = MarketTemplate.objects.get(code=template)
        m = Market(event=self.event, stage=self.stage, match=self.match, template=tpl, title=overrides.pop("title", "Match 1 winner"),
                   lock_at=timezone.now() + timedelta(minutes=lock_in_minutes), status=status,
                   rake_bps=self.cfg.rake_bps, cancel_fee_bps=self.cfg.cancel_fee_bps,
                   min_stake_kobo=self.cfg.min_stake_kobo, max_stake_per_user_kobo=self.cfg.max_stake_per_user_kobo,
                   created_by=self.admin)
        for k, v in overrides.items():
            setattr(m, k, v)
        m.save()
        if tpl.option_source == MarketTemplate.OPTIONS_OVER_UNDER:
            m.over_under_line = m.over_under_line or 60
            m.save(update_fields=["over_under_line"])
            self.opt_a = MarketOption.objects.create(market=m, label=f"Over {m.over_under_line}", side="over", sort_order=0)
            self.opt_b = MarketOption.objects.create(market=m, label=f"Under {m.over_under_line}", side="under", sort_order=1)
        else:
            self.opt_a = MarketOption.objects.create(market=m, label="Alpha", team=self.tt_alpha, sort_order=0)
            self.opt_b = MarketOption.objects.create(market=m, label="Bravo", team=self.tt_bravo, sort_order=1)
        return m

    def record_result(self, *, alpha_placement=1, bravo_placement=2, alpha_kills=10, bravo_kills=5):
        TournamentTeamMatchStats.objects.create(match=self.match, tournament_team=self.tt_alpha,
                                                placement=alpha_placement, kills=alpha_kills)
        TournamentTeamMatchStats.objects.create(match=self.match, tournament_team=self.tt_bravo,
                                                placement=bravo_placement, kills=bravo_kills)
        self.match.result_inputted = True
        self.match.save(update_fields=["result_inputted"])

    def place(self, auth, market, lines):
        return self.client.post(f"/wagers/markets/{market.slug}/place/", {"lines": lines},
                                content_type="application/json", **auth)

    def pay(self, auth, wager_token_or_reference):
        """Simulate the return from Paystack: the verify call activates the wager (outbox mode)."""
        from afc_wager.models import Wager
        w = Wager.objects.get(public_token=wager_token_or_reference) if wager_token_or_reference.startswith("w_") \
            else Wager.objects.get(paystack_reference=wager_token_or_reference)
        return self.client.get(f"/wagers/payments/verify/?reference={w.paystack_reference}", **auth)

    def place_and_pay(self, auth, market, lines):
        resp = self.place(auth, market, lines)
        assert resp.status_code == 201, resp.content
        token = resp.json()["wager"]["token"]
        paid = self.pay(auth, token)
        assert paid.status_code == 200, paid.content
        return token
