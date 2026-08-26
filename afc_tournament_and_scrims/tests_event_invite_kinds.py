# afc_tournament_and_scrims/tests_event_invite_kinds.py
# ──────────────────────────────────────────────────────────────────────────────
# THE THREE KINDS OF EVENT INVITATION, AND WHERE THEY ARE DELIVERED (owner 2026-08-08).
#
# The owner's words: "Admins can send invitations to teams and teams accept it in their mails or
# notifications. Team captains or managers or coaches can accept. The admins can pick where they
# receive the invitations, the normal places, can also decide what kind: if it is fcfs, or single
# per team that's automatically generated and attributed to each team and sent, or it's a single
# general bulk invite."
#
# tests_event_team_invitations.py already proves the thing that must never break: accepting an
# invitation IS an ordinary registration, replayed through views.register_for_event. That file stays
# the guard on that property, and it must stay green, because this file extends its code.
#
# What THIS file proves is the four things that are new, and one thing that had better be unchanged:
#
#   1. THE THREE KINDS actually behave differently in the way the owner described.
#        per_team -> one addressed row per team, all of which may be accepted.
#        fcfs     -> more teams asked than there are places, and the quick ones get in.
#        bulk     -> ONE offer, no addressed rows until somebody answers.
#   2. THE FCFS RACE is safe. Two captains pressing Accept on the last place at the same instant
#      must not both get in. Tested with REAL THREADS against the REAL database, at two levels: the
#      claim primitive, and the whole accept endpoint. A test that only called them one after the
#      other would pass against a lost-update bug, which is the entire hazard here.
#   3. DELIVERY reaches EVERYONE who may answer, by email as well as in-app, each in their own
#      language, and only over the channels the admin picked.
#   4. WHO MAY ANSWER is the same set that may register: owner, captain, vice-captain, manager,
#      coach, and nobody else.
#   5. UNCHANGED: an invited team with an incomplete roster is still refused, in the same words a
#      self-registering team is refused. Proven by comparison against register_for_event itself,
#      not by asserting a hardcoded sentence.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import json
import threading
import uuid

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import (
    Event, EventInvitationCampaign, EventTeamInvitation, TournamentTeam,
)

CREATE_URL = "/events/team-invitations/create/"
LIST_URL = "/events/team-invitations/"
MINE_URL = "/events/team-invitations/mine/"


def _accept_url(invitation_id):
    return f"/events/team-invitations/{invitation_id}/accept/"


def _campaign_accept_url(campaign_id):
    return f"/events/invitation-campaigns/{campaign_id}/accept/"


def _campaign_decline_url(campaign_id):
    return f"/events/invitation-campaigns/{campaign_id}/decline/"


def _campaign_close_url(campaign_id):
    return f"/events/invitation-campaigns/{campaign_id}/close/"


def _cancel_url(invitation_id):
    return f"/events/team-invitations/{invitation_id}/cancel/"


class InviteFixtureMixin:
    """Shared fixtures. Deliberately the same shapes tests_event_team_invitations.py builds, so a
    reader moving between the two files is not re-learning the setup."""

    def _user(self, name, role="player", language="en"):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role=role, status="active", is_active=True, country="Nigeria",
            language=language,
        )

    def _auth(self, user):
        """A real SessionToken, because validate_token is what every endpoint here calls."""
        token = SessionToken.objects.create(
            user=user, token=f"t-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _team(self, label, players=4):
        """A team with its own owner/captain and `players` playing members, so no player is shared
        between two teams (a shared player trips register_for_event's roster conflict for reasons
        unrelated to what is being tested)."""
        owner = self._user(f"{label}_owner")
        team = Team.objects.create(
            team_name=f"Team {label}", join_settings="open",
            team_creator=owner, team_owner=owner, country="Nigeria",
        )
        TeamMembers.objects.create(team=team, member=owner, management_role="team_captain")
        members = [owner]
        for i in range(players - 1):
            player = self._user(f"{label}_p{i}")
            TeamMembers.objects.create(team=team, member=player, management_role="member")
            members.append(player)
        return team, owner, members

    def _event(self, name="Kinds Cup", slug="kinds-cup", capacity=2):
        today = timezone.localdate()
        return Event.objects.create(
            event_name=name, slug=slug,
            participant_type="squad", competition_type="tournament", event_type="virtual",
            max_teams_or_players=capacity, is_public=True, is_draft=False, number_of_stages=1,
            start_date=today + datetime.timedelta(days=7),
            end_date=today + datetime.timedelta(days=7),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=3),
        )

    def _post(self, url, body, user):
        return self.client.post(
            url, data=json.dumps(body), content_type="application/json", **self._auth(user),
        )

    def _roster_ids(self, members):
        return [u.user_id for u in members]


@override_settings(EVENT_INVITE_EMAIL_SYNC=True)
class InvitationKindTests(InviteFixtureMixin, TestCase):
    """The three kinds, delivery, and permissions. Email runs inline (EVENT_INVITE_EMAIL_SYNC) so a
    test can assert what was sent instead of racing the production daemon thread."""

    def setUp(self):
        self.client = Client()
        self.admin = self._user("kind_admin", role="admin")
        self.outsider = self._user("kind_outsider")
        self.event = self._event()
        self.team, self.captain, self.roster = self._team("Alpha")

    # ══════════════════════════════════════════════════════════════════════════
    # 1. per_team: item 34's behaviour, still the default
    # ══════════════════════════════════════════════════════════════════════════
    def test_per_team_writes_one_addressed_row_per_team(self):
        bravo, _, _ = self._team("Bravo")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id],
            "kind": "per_team",
        }, self.admin)

        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(len(body["invited"]), 2)
        self.assertEqual(body["campaign"]["kind"], "per_team")
        self.assertEqual(
            EventTeamInvitation.objects.filter(event=self.event, status="pending").count(), 2)

    def test_kind_defaults_to_per_team_so_an_older_client_is_unchanged(self):
        # The frontend that shipped with item 34 sends no `kind`. It must keep working exactly as it
        # did rather than 400 or silently become a different kind of invitation.
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id],
        }, self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["campaign"]["kind"], "per_team")
        self.assertEqual(res.json()["campaign"]["delivery"], "both")

    # ══════════════════════════════════════════════════════════════════════════
    # 2. fcfs: more teams asked than there are places
    # ══════════════════════════════════════════════════════════════════════════
    def test_fcfs_invites_more_teams_than_it_has_places(self):
        bravo, _, _ = self._team("Bravo")
        charlie, _, _ = self._team("Charlie")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id, charlie.team_id],
            "kind": "fcfs", "slots": 1,
        }, self.admin)

        self.assertEqual(res.status_code, 201, res.content)
        campaign = res.json()["campaign"]
        self.assertEqual(campaign["kind"], "fcfs")
        self.assertEqual(campaign["slots"], 1)
        self.assertEqual(campaign["slots_remaining"], 1)
        # Three teams were ASKED even though only one can get in. That is the point of the kind.
        self.assertEqual(len(res.json()["invited"]), 3)

    def test_fcfs_first_to_accept_takes_the_place_and_the_rest_are_refused(self):
        bravo, bravo_captain, bravo_roster = self._team("Bravo")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id],
            "kind": "fcfs", "slots": 1,
        }, self.admin)
        invites = {i["team_id"]: i["id"] for i in res.json()["invited"]}
        campaign_id = res.json()["campaign"]["campaign_id"]

        first = self._post(_accept_url(invites[self.team.team_id]),
                           {"roster_member_ids": self._roster_ids(self.roster)}, self.captain)
        self.assertEqual(first.status_code, 201, first.content)

        second = self._post(_accept_url(invites[bravo.team_id]),
                            {"roster_member_ids": self._roster_ids(bravo_roster)}, bravo_captain)
        self.assertEqual(second.status_code, 409, second.content)
        self.assertIn("places have been taken", second.json()["message"])

        # The loser is NOT registered, and the campaign closed itself rather than leaving a live
        # Accept button on an offer that is gone.
        self.assertFalse(TournamentTeam.objects.filter(event=self.event, team=bravo).exists())
        self.assertEqual(
            EventInvitationCampaign.objects.get(id=campaign_id).status, "closed")

    def test_a_refused_fcfs_accept_hands_its_place_back(self):
        # A captain whose roster is one player short must not burn a place nobody is standing in.
        bravo, bravo_captain, bravo_roster = self._team("Bravo")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id],
            "kind": "fcfs", "slots": 1,
        }, self.admin)
        invites = {i["team_id"]: i["id"] for i in res.json()["invited"]}
        campaign_id = res.json()["campaign"]["campaign_id"]

        # Two players is below the squad minimum, so register_for_event refuses.
        bad = self._post(_accept_url(invites[self.team.team_id]),
                         {"roster_member_ids": self._roster_ids(self.roster)[:2]}, self.captain)
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(
            EventInvitationCampaign.objects.get(id=campaign_id).seats_claimed, 0,
            "a refused registration must release the place it claimed",
        )

        # And the place is genuinely still available to the other team.
        good = self._post(_accept_url(invites[bravo.team_id]),
                          {"roster_member_ids": self._roster_ids(bravo_roster)}, bravo_captain)
        self.assertEqual(good.status_code, 201, good.content)

    def test_slots_is_refused_on_a_kind_that_cannot_enforce_it(self):
        # A silently ignored limit is how an organizer ends up with more teams than they meant.
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id],
            "kind": "per_team", "slots": 3,
        }, self.admin)
        self.assertEqual(res.status_code, 400)
        self.assertIn("first come", res.json()["message"])

    def test_fcfs_without_slots_leans_on_the_events_own_capacity(self):
        # No campaign ceiling: the event's capacity of 2 is the only limit, and register_for_event
        # enforces it exactly as it does for a team that registered itself.
        teams = [self._team(label) for label in ("Bravo", "Charlie")]
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id] + [t[0].team_id for t in teams],
            "kind": "fcfs",
        }, self.admin)
        self.assertIsNone(res.json()["campaign"]["slots"])
        invites = {i["team_id"]: i["id"] for i in res.json()["invited"]}

        self.assertEqual(
            self._post(_accept_url(invites[self.team.team_id]),
                       {"roster_member_ids": self._roster_ids(self.roster)},
                       self.captain).status_code, 201)
        self.assertEqual(
            self._post(_accept_url(invites[teams[0][0].team_id]),
                       {"roster_member_ids": self._roster_ids(teams[0][2])},
                       teams[0][1]).status_code, 201)

        third = self._post(_accept_url(invites[teams[1][0].team_id]),
                           {"roster_member_ids": self._roster_ids(teams[1][2])}, teams[1][1])
        self.assertEqual(third.status_code, 403)
        self.assertEqual(third.json()["message"], "Registration limit reached.")

    # ══════════════════════════════════════════════════════════════════════════
    # 3. bulk: ONE offer, no addressed rows until somebody answers
    # ══════════════════════════════════════════════════════════════════════════
    def test_bulk_writes_no_addressed_rows(self):
        bravo, _, _ = self._team("Bravo")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id],
            "kind": "bulk",
        }, self.admin)

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["invited"], [], "a bulk invite addresses nobody")
        self.assertEqual(EventTeamInvitation.objects.count(), 0)
        campaign = res.json()["campaign"]
        self.assertEqual(campaign["kind"], "bulk")
        self.assertEqual(campaign["audience_size"], 2)

    def test_a_bulk_offer_appears_on_the_team_page_and_can_be_accepted(self):
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id],
            "kind": "bulk", "message": "Open to our regional teams",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        listing = self.client.get(f"{MINE_URL}?team_id={self.team.team_id}",
                                  **self._auth(self.captain))
        self.assertEqual(listing.status_code, 200, listing.content)
        offers = [r for r in listing.json()["invitations"] if r.get("is_offer")]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["campaign_id"], campaign_id)
        self.assertEqual(offers[0]["message"], "Open to our regional teams")
        self.assertEqual(listing.json()["pending_count"], 1)

        accepted = self._post(_campaign_accept_url(campaign_id), {
            "team_id": self.team.team_id,
            "roster_member_ids": self._roster_ids(self.roster),
        }, self.captain)
        self.assertEqual(accepted.status_code, 201, accepted.content)

        # The ANSWER wrote the row, and the team is genuinely registered.
        row = EventTeamInvitation.objects.get(campaign_id=campaign_id, team=self.team)
        self.assertEqual(row.status, "accepted")
        self.assertTrue(TournamentTeam.objects.filter(event=self.event, team=self.team).exists())

    def test_several_teams_take_up_one_bulk_offer_until_the_event_is_full(self):
        # The owner's "single general bulk invite": one offer, many takers, and the EVENT's capacity
        # (2 here) is what ends it, not a per-campaign counter.
        bravo, bravo_captain, bravo_roster = self._team("Bravo")
        charlie, charlie_captain, charlie_roster = self._team("Charlie")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id, charlie.team_id],
            "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        def take(team, captain, roster):
            return self._post(_campaign_accept_url(campaign_id), {
                "team_id": team.team_id, "roster_member_ids": self._roster_ids(roster),
            }, captain)

        self.assertEqual(take(self.team, self.captain, self.roster).status_code, 201)
        self.assertEqual(take(bravo, bravo_captain, bravo_roster).status_code, 201)

        third = take(charlie, charlie_captain, charlie_roster)
        self.assertEqual(third.status_code, 403)
        self.assertEqual(third.json()["message"], "Registration limit reached.")
        # The refused team left NO row behind: they did not answer, they were turned away.
        self.assertFalse(
            EventTeamInvitation.objects.filter(campaign_id=campaign_id, team=charlie).exists())
        self.assertEqual(TournamentTeam.objects.filter(event=self.event).count(), 2)

    def test_a_team_outside_the_audience_cannot_take_a_bulk_offer(self):
        # Otherwise "general" would mean "public", and any team that learned the id could walk in.
        outside, outside_captain, outside_roster = self._team("Delta")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        blocked = self._post(_campaign_accept_url(campaign_id), {
            "team_id": outside.team_id, "roster_member_ids": self._roster_ids(outside_roster),
        }, outside_captain)
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("not sent to your team", blocked.json()["message"])

    def test_a_bulk_offer_can_be_answered_only_once(self):
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        self.assertEqual(self._post(_campaign_decline_url(campaign_id), {
            "team_id": self.team.team_id, "reason": "clashes with our league",
        }, self.captain).status_code, 200)

        again = self._post(_campaign_accept_url(campaign_id), {
            "team_id": self.team.team_id, "roster_member_ids": self._roster_ids(self.roster),
        }, self.captain)
        self.assertEqual(again.status_code, 400)
        self.assertIn("already declined", again.json()["message"])

    def test_declining_a_bulk_offer_takes_it_off_the_team_page(self):
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]
        self._post(_campaign_decline_url(campaign_id), {"team_id": self.team.team_id}, self.captain)

        listing = self.client.get(f"{MINE_URL}?team_id={self.team.team_id}",
                                  **self._auth(self.captain))
        offers = [r for r in listing.json()["invitations"] if r.get("is_offer")]
        self.assertEqual(offers, [], "an answered offer stops being offered")

    def test_closing_a_campaign_stops_new_answers_but_keeps_the_old_ones(self):
        bravo, bravo_captain, bravo_roster = self._team("Bravo")
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        self.assertEqual(self._post(_campaign_accept_url(campaign_id), {
            "team_id": self.team.team_id, "roster_member_ids": self._roster_ids(self.roster),
        }, self.captain).status_code, 201)

        self.assertEqual(
            self._post(_campaign_close_url(campaign_id), {}, self.admin).status_code, 200)

        late = self._post(_campaign_accept_url(campaign_id), {
            "team_id": bravo.team_id, "roster_member_ids": self._roster_ids(bravo_roster),
        }, bravo_captain)
        self.assertEqual(late.status_code, 400)
        # The team that already accepted keeps its place.
        self.assertTrue(TournamentTeam.objects.filter(event=self.event, team=self.team).exists())

    # ══════════════════════════════════════════════════════════════════════════
    # 4. WHO is told, and HOW
    # ══════════════════════════════════════════════════════════════════════════
    def _staffed_team(self):
        """A team carrying one of every role that may answer, plus a plain member who may not."""
        team, owner, members = self._team("Staffed")
        roles = {
            "vice": "vice_captain",
            "mgr": "manager",
            "coach": "coach",
            "plain": "member",
        }
        people = {}
        for label, role in roles.items():
            person = self._user(f"staffed_{label}")
            TeamMembers.objects.create(team=team, member=person, management_role=role)
            people[label] = person
        people["owner"] = owner
        return team, people, members

    def test_everyone_who_may_answer_is_notified_not_just_the_captain(self):
        team, people, _ = self._staffed_team()
        self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [team.team_id],
        }, self.admin)

        for label in ("owner", "vice", "mgr", "coach"):
            self.assertTrue(
                Notifications.objects.filter(
                    user=people[label], notification_type="event_team_invitation").exists(),
                f"{label} can accept an invitation, so {label} must be told about it",
            )
        # And the plain member, who cannot answer, is not pinged about a decision they cannot make.
        self.assertFalse(
            Notifications.objects.filter(
                user=people["plain"], notification_type="event_team_invitation").exists())

    def test_the_take_me_there_link_opens_a_page_that_exists(self):
        """The deep link must address the team the way the ROUTE does, i.e. by NAME.

        Regression, found in the browser on 2026-08-08: the notification stored the numeric team_id,
        afc_auth.notification_links turns target_type="team" into "/teams/<target_id>" verbatim, and
        app/(user)/teams/[id]/page.tsx resolves that segment as the team NAME. So every invitation
        notification pointed at /teams/817, which is a hard 404. Asserted against the real link
        builder rather than against a hardcoded string, so this still holds if the route changes.
        """
        from afc_auth.notification_links import build_notification_link

        team, people, _ = self._staffed_team()
        self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [team.team_id],
        }, self.admin)

        row = Notifications.objects.filter(
            user=people["owner"], notification_type="event_team_invitation",
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.target_id, team.team_name,
                         "the link must carry the team NAME, which is what /teams/[id] resolves")
        self.assertEqual(build_notification_link(row.target_type, row.target_id),
                         f"/teams/{team.team_name}")

    def test_the_email_button_points_at_the_team_page_url_encoded(self):
        """The same link in the email, where it is a raw href and therefore has to be quoted."""
        from afc_tournament_and_scrims.event_invite_delivery import _invitation_email_html

        team, _people, _ = self._staffed_team()
        team.team_name = "Les Loups & Co"          # a space AND an ampersand, both illegal raw
        team.save(update_fields=["team_name"])

        # The caller resolves the link now (a team page here, an event page for a solo
        # invitation), so the encoding this test pins is applied by that caller. Built the same way
        # deliver_invitation builds it, so the two cannot drift apart unnoticed.
        from urllib.parse import quote

        link_path = f"/teams/{quote(team.team_name, safe='')}"
        html = _invitation_email_html(
            team.team_name, link_path, self.event, "organizer", "", "per_team", "en"
        )
        self.assertIn("/teams/Les%20Loups%20%26%20Co", html)
        self.assertNotIn(f"/teams/{team.team_id}", html)

    def test_every_role_that_may_answer_actually_can(self):
        team, people, members = self._staffed_team()
        for label in ("owner", "vice", "mgr", "coach"):
            invitation = EventTeamInvitation.objects.create(
                event=self.event, team=team, invited_by=self.admin,
            )
            res = self._post(_accept_url(invitation.id),
                             {"roster_member_ids": self._roster_ids(members)}, people[label])
            # 201 the first time, then 409 "already registered" for the rest. Either way the answer
            # came from register_for_event, which is the proof that the ROLE was accepted: a role
            # that may not answer is stopped at 403 before ever reaching it.
            self.assertNotEqual(
                res.status_code, 403,
                f"{label} may register this team, so {label} must be able to accept",
            )

    def test_a_plain_member_still_cannot_answer(self):
        team, people, members = self._staffed_team()
        invitation = EventTeamInvitation.objects.create(
            event=self.event, team=team, invited_by=self.admin,
        )
        res = self._post(_accept_url(invitation.id),
                         {"roster_member_ids": self._roster_ids(members)}, people["plain"])
        self.assertEqual(res.status_code, 403)
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation.id).status, "pending")

    def test_email_reaches_every_eligible_recipient_in_their_own_language(self):
        team, people, _ = self._staffed_team()
        # Three languages across the four people who may answer.
        for label, lang in (("vice", "fr"), ("mgr", "pt"), ("coach", "en")):
            people[label].language = lang
            people[label].save(update_fields=["language"])

        sent = []
        from unittest.mock import patch
        with patch("afc_auth.views.send_email",
                   side_effect=lambda addr, subj, html, **kw: sent.append((addr, subj, kw))):
            res = self._post(CREATE_URL, {
                "event_id": self.event.event_id, "team_ids": [team.team_id],
                "delivery": "both",
            }, self.admin)
        self.assertEqual(res.status_code, 201, res.content)

        by_address = {addr: (subj, kw) for addr, subj, kw in sent}
        # Everyone who may answer got one, which is the same set that was notified in-app.
        self.assertEqual(len(sent), 4, f"expected the 4 decision makers, got {list(by_address)}")

        # Each in their OWN language, from the hand-authored catalog, and flagged prelocalized so
        # send_email does not machine-translate copy that is already French.
        fr_subject = by_address[people["vice"].email][0]
        pt_subject = by_address[people["mgr"].email][0]
        en_subject = by_address[people["coach"].email][0]
        self.assertEqual(fr_subject, f"Votre équipe est invitée à {self.event.event_name}")
        self.assertEqual(pt_subject, f"A sua equipa foi convidada para {self.event.event_name}")
        self.assertEqual(en_subject, f"Your team is invited to {self.event.event_name}")
        self.assertTrue(all(kw.get("prelocalized") for _, kw in by_address.values()))
        self.assertEqual(by_address[people["vice"].email][1].get("language"), "fr")

    def test_the_admin_choice_of_channel_is_honoured(self):
        # "push" alone must write the notification and send NO email. An admin who unticked email
        # meant it.
        sent = []
        from unittest.mock import patch
        with patch("afc_auth.views.send_email", side_effect=lambda *a, **k: sent.append(a)):
            res = self._post(CREATE_URL, {
                "event_id": self.event.event_id, "team_ids": [self.team.team_id],
                "delivery": "push",
            }, self.admin)

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(sent, [])
        self.assertEqual(res.json()["delivered"]["emailed"], 0)
        self.assertGreater(res.json()["delivered"]["pushed"], 0)
        self.assertTrue(Notifications.objects.filter(
            user=self.captain, notification_type="event_team_invitation").exists())

    def test_email_only_sends_no_in_app_notification(self):
        sent = []
        from unittest.mock import patch
        with patch("afc_auth.views.send_email", side_effect=lambda *a, **k: sent.append(a)):
            res = self._post(CREATE_URL, {
                "event_id": self.event.event_id, "team_ids": [self.team.team_id],
                "delivery": "email",
            }, self.admin)

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(sent), 1)     # this team has one decision maker, the owner-captain
        self.assertEqual(res.json()["delivered"]["pushed"], 0)
        self.assertFalse(Notifications.objects.filter(
            user=self.captain, notification_type="event_team_invitation").exists())

    def test_a_delivery_value_naming_no_channel_is_refused(self):
        # An invitation that reaches nobody is worse than a refused request.
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id],
            "delivery": "carrier-pigeon",
        }, self.admin)
        self.assertEqual(res.status_code, 400)
        self.assertIn("delivery must name", res.json()["message"])
        self.assertEqual(EventTeamInvitation.objects.count(), 0)

    # ── reach: what the composer prints before the admin commits ──────────────
    def test_reach_counts_the_people_who_can_answer_not_the_teams(self):
        """The number shown next to the tick boxes counts PEOPLE, deduplicated across teams.

        This exists because the channels became a choice and the three are not equivalent: an
        admin who ticks WhatsApp and reads "invitations sent" would otherwise believe the teams
        were told. The endpoint is what lets the composer say "WhatsApp reaches 0 of these 5".
        """
        team, people, _ = self._staffed_team()   # owner + vice + manager + coach can answer
        res = self.client.get(
            f"/events/team-invitations/reach/?event_id={self.event.event_id}"
            f"&team_ids={team.team_id}",
            **self._auth(self.admin),
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["recipients"], 4, "owner, vice-captain, manager and coach")
        self.assertEqual(body["email"], 4, "every AFC account has an email address")
        self.assertEqual(body["teams"], 1)
        # Nobody in this fixture saved a WhatsApp number, so the honest answer is zero rather than
        # a hopeful one. This is the case the composer paints red.
        self.assertEqual(body["whatsapp"], 0)

    def test_reach_counts_a_shared_person_once(self):
        """One person can run two teams, and must be counted once.

        Note the shape of the fixture: TeamMembers carries a `unique_member_one_team` constraint,
        so nobody can be a MEMBER of two teams. Owning two is still possible, because Team.owner is
        its own FK and _decision_makers adds the owner whether or not a membership row exists. That
        is the real way one person ends up on two invitations, so it is the case tested.
        """
        first, owner, _ = self._team("Shared1")
        second = Team.objects.create(
            team_name="Team Shared2", join_settings="open",
            team_creator=owner, team_owner=owner, country="Nigeria",
        )

        res = self.client.get(
            f"/events/team-invitations/reach/?event_id={self.event.event_id}"
            f"&team_ids={first.team_id},{second.team_id}",
            **self._auth(self.admin),
        )
        self.assertEqual(res.json()["recipients"], 1, "the same person, running both teams")
        self.assertEqual(res.json()["teams"], 2)

    def test_reach_is_not_shown_to_somebody_who_could_not_send(self):
        res = self.client.get(
            f"/events/team-invitations/reach/?event_id={self.event.event_id}"
            f"&team_ids={self.team.team_id}",
            **self._auth(self.outsider),
        )
        self.assertEqual(res.status_code, 403)

    def test_reach_with_no_teams_selected_is_all_zeros(self):
        # The composer asks as soon as the dialog opens, before anything is ticked.
        res = self.client.get(
            f"/events/team-invitations/reach/?event_id={self.event.event_id}&team_ids=",
            **self._auth(self.admin),
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(), {"recipients": 0, "email": 0, "whatsapp": 0, "teams": 0})

    def test_reach_counts_an_opted_in_whatsapp_number(self):
        # The positive case, so the zero above is proof of measurement rather than of a stub that
        # always returns nothing.
        from afc_auth.models import UserProfile

        team, people, _ = self._staffed_team()
        UserProfile.objects.update_or_create(
            user=people["mgr"],
            defaults={"whatsapp_number": "+2348012345678", "whatsapp_opt_in": True},
        )
        # Saved a number but opted OUT: must NOT be counted, because the sender would refuse it.
        UserProfile.objects.update_or_create(
            user=people["coach"],
            defaults={"whatsapp_number": "+2348087654321", "whatsapp_opt_in": False},
        )

        res = self.client.get(
            f"/events/team-invitations/reach/?event_id={self.event.event_id}"
            f"&team_ids={team.team_id}",
            **self._auth(self.admin),
        )
        self.assertEqual(res.json()["whatsapp"], 1, "opted-in only, never the opted-out number")
        self.assertEqual(res.json()["recipients"], 4)

    def test_the_email_body_says_which_kind_of_offer_it_is(self):
        # The three urgency sentences are the reason the email knows about kinds at all: a team that
        # cannot tell "this place is yours" from "first to accept wins" answers on the wrong
        # timescale.
        from .event_invite_delivery import _invitation_email_html

        link = f"/teams/{self.team.team_name}"
        per_team = _invitation_email_html(
            self.team.team_name, link, self.event, "AFC", "", "per_team", "en"
        )
        fcfs = _invitation_email_html(
            self.team.team_name, link, self.event, "AFC", "", "fcfs", "en"
        )
        bulk = _invitation_email_html(
            self.team.team_name, link, self.event, "AFC", "", "bulk", "en"
        )

        self.assertIn("not being offered to anyone else", per_team)
        self.assertIn("first come, first served", fcfs)
        self.assertIn("open invitation", bulk)
        # And the organizer's own note is quoted rather than dropped.
        with_note = _invitation_email_html(
            self.team.team_name, link, self.event, "AFC", "We saved you a slot", "per_team", "en"
        )
        self.assertIn("We saved you a slot", with_note)

    # ══════════════════════════════════════════════════════════════════════════
    # 5. UNCHANGED: accepting is still an ordinary registration
    # ══════════════════════════════════════════════════════════════════════════
    def test_an_invited_team_with_a_bad_roster_is_refused_in_the_same_words(self):
        """The property item 34 exists to protect, re-proven for the new kinds.

        Not asserted against a hardcoded sentence: the same captain also self-registers the same
        team straight through register_for_event, and the two answers are COMPARED. If anybody ever
        gives invited teams their own refusal path, this fails whatever wording either side uses.
        """
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "fcfs",
            "slots": 5,
        }, self.admin)
        invitation_id = res.json()["invited"][0]["id"]
        short_roster = self._roster_ids(self.roster)[:2]

        through_invitation = self._post(
            _accept_url(invitation_id), {"roster_member_ids": short_roster}, self.captain)
        direct = self.client.post(
            "/events/register-for-event/",
            data=json.dumps({
                "event_id": self.event.event_id,
                "team_id": self.team.team_id,
                "roster_member_ids": short_roster,
            }),
            content_type="application/json",
            **self._auth(self.captain),
        )

        self.assertEqual(
            (through_invitation.status_code, through_invitation.json()["message"]),
            (direct.status_code, direct.json()["message"]),
            "An invited team must be refused in exactly the words a self-registering team is.",
        )
        self.assertEqual(through_invitation.json()["message"],
                         "Roster must contain 4 to 6 players.")

    def test_a_bulk_offer_is_held_to_the_same_roster_rule(self):
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = res.json()["campaign"]["campaign_id"]

        refused = self._post(_campaign_accept_url(campaign_id), {
            "team_id": self.team.team_id,
            "roster_member_ids": self._roster_ids(self.roster)[:2],
        }, self.captain)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["message"], "Roster must contain 4 to 6 players.")
        # A refused answer leaves NO row behind, so the team can still take the offer up properly.
        self.assertFalse(EventTeamInvitation.objects.filter(campaign_id=campaign_id).exists())

    # ══════════════════════════════════════════════════════════════════════════
    # 6. Private events: fcfs and bulk share ONE token, per_team keeps its own
    # ══════════════════════════════════════════════════════════════════════════
    def test_cancelling_one_fcfs_invitation_does_not_lock_the_others_out(self):
        """Regression, found by re-reading the code rather than by a failure.

        On a PRIVATE event an fcfs campaign mints ONE shared EventInviteToken that every invited
        team replays on accept. cancel_team_invitation deletes the invitation's token, which is
        correct for a per_team invitation that owns its token outright, and catastrophic for an
        fcfs one: withdrawing a single team's invitation would delete the token the whole campaign
        depends on, and every other team would be refused at the private-event gate.
        """
        self.event.is_public = False
        self.event.save(update_fields=["is_public"])
        bravo, bravo_captain, bravo_roster = self._team("Bravo")

        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [self.team.team_id, bravo.team_id],
            "kind": "fcfs",
        }, self.admin)
        invites = {i["team_id"]: i["id"] for i in res.json()["invited"]}

        # Withdraw ONE of the two.
        self.assertEqual(
            self._post(_cancel_url(invites[self.team.team_id]), {}, self.admin).status_code, 200)

        # The other team must still be able to get in.
        still_works = self._post(
            _accept_url(invites[bravo.team_id]),
            {"roster_member_ids": self._roster_ids(bravo_roster)}, bravo_captain)
        self.assertEqual(
            still_works.status_code, 201,
            f"cancelling one invitation must not revoke the campaign's shared token: "
            f"{still_works.content}",
        )
        self.assertTrue(TournamentTeam.objects.filter(event=self.event, team=bravo).exists())

    def test_cancelling_a_per_team_invitation_still_destroys_its_own_token(self):
        # The other half of the same rule: a token this invitation owns alone dies with it, so a
        # withdrawn invitation cannot still let that team through a private event's door.
        self.event.is_public = False
        self.event.save(update_fields=["is_public"])
        res = self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "per_team",
        }, self.admin)
        invitation_id = res.json()["invited"][0]["id"]
        token_id = EventTeamInvitation.objects.get(id=invitation_id).invite_token_id
        self.assertIsNotNone(token_id)

        self._post(_cancel_url(invitation_id), {}, self.admin)

        from .models import EventInviteToken
        self.assertFalse(EventInviteToken.objects.filter(id=token_id).exists())

    # ══════════════════════════════════════════════════════════════════════════
    # 7. The organizer's list has to show a bulk send, which writes no rows
    # ══════════════════════════════════════════════════════════════════════════
    def test_the_organizer_list_shows_campaigns_including_a_bulk_one(self):
        self._post(CREATE_URL, {
            "event_id": self.event.event_id, "team_ids": [self.team.team_id], "kind": "bulk",
        }, self.admin)

        res = self.client.get(f"{LIST_URL}?event_id={self.event.event_id}", **self._auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["invitations"], [], "a bulk send writes no addressed rows")
        self.assertEqual(len(body["campaigns"]), 1)
        self.assertEqual(body["campaigns"][0]["kind"], "bulk")
        self.assertEqual(body["campaigns"][0]["audience_size"], 1)


@override_settings(EVENT_INVITE_EMAIL_SYNC=True)
class FcfsRaceTests(InviteFixtureMixin, TransactionTestCase):
    """THE RACE, with real threads against the real database.

    TransactionTestCase rather than TestCase on purpose: TestCase wraps each test in a transaction
    that other connections cannot see, so threaded work would either deadlock or read an empty
    database. This class pays the cost of real commits to get a real answer.

    Two ceilings are proven separately because they are enforced in two different places:
      * the CAMPAIGN's own `slots`, guarded by the single UPDATE in claim_slot;
      * the EVENT's capacity, guarded by register_for_event's own select_for_update, which this
        feature leans on rather than re-implementing.
    """

    def setUp(self):
        self.client = Client()
        self.admin = self._user("race_admin", role="admin")
        self.event = self._event(name="Race Cup", slug="race-cup", capacity=1)

    def _run_concurrently(self, fns):
        """Run `fns` on real threads and collect their results in order.

        Each thread closes its own DB connection at the end: Django opens a connection per thread
        and a test that leaks them will hang the suite on teardown when MySQL refuses to drop the
        database.
        """
        results = [None] * len(fns)
        barrier = threading.Barrier(len(fns))

        def wrapped(index, fn):
            try:
                # Every thread waits here, so they hit the contended statement together rather than
                # in whatever order the scheduler happened to start them. Without this the test
                # would usually run sequentially and would pass against a lost-update bug.
                barrier.wait(timeout=10)
                results[index] = fn()
            except Exception as exc:
                results[index] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=wrapped, args=(i, fn)) for i, fn in enumerate(fns)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return results

    def test_only_one_of_many_simultaneous_claims_takes_the_last_place(self):
        """The primitive, hammered: eight threads, one place."""
        campaign = EventInvitationCampaign.objects.create(
            event=self.event, kind="fcfs", slots=1, created_by=self.admin,
        )

        def claim():
            # Each thread re-reads the row so nobody is claiming through a stale in-memory copy,
            # which is exactly what a real request would do.
            return EventInvitationCampaign.objects.get(pk=campaign.pk).claim_slot()

        results = self._run_concurrently([claim] * 8)
        self.assertEqual(
            sum(1 for r in results if r is True), 1,
            f"exactly one of eight simultaneous claims may win, got {results}",
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.seats_claimed, 1)

    def test_two_teams_accepting_the_last_campaign_place_at_once(self):
        """The whole endpoint, raced: two captains press Accept on the last fcfs place together."""
        # Capacity 3 so the EVENT is not what refuses anybody: the campaign's single slot must be.
        self.event.max_teams_or_players = 3
        self.event.save(update_fields=["max_teams_or_players"])

        alpha, alpha_captain, alpha_roster = self._team("RaceAlpha")
        bravo, bravo_captain, bravo_roster = self._team("RaceBravo")
        created = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [alpha.team_id, bravo.team_id],
            "kind": "fcfs", "slots": 1,
        }, self.admin)
        self.assertEqual(created.status_code, 201, created.content)
        invites = {i["team_id"]: i["id"] for i in created.json()["invited"]}

        def accept(team, captain, roster):
            def run():
                return Client().post(
                    _accept_url(invites[team.team_id]),
                    data=json.dumps({"roster_member_ids": self._roster_ids(roster)}),
                    content_type="application/json",
                    **self._auth(captain),
                ).status_code
            return run

        results = self._run_concurrently([
            accept(alpha, alpha_captain, alpha_roster),
            accept(bravo, bravo_captain, bravo_roster),
        ])

        self.assertEqual(sorted(str(r) for r in results), ["201", "409"],
                         f"exactly one captain may take the last place, got {results}")
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, is_waitlisted=False).count(), 1,
            "the loser of the race must not be registered",
        )
        self.assertEqual(
            EventTeamInvitation.objects.filter(
                event=self.event, status="accepted").count(), 1)

    def test_two_teams_accepting_the_last_EVENT_place_at_once(self):
        """The other ceiling: no campaign slots at all, and the event itself has one place.

        This is the case the brief called out as leaning on register_for_event rather than counting
        anything here, so it is worth proving that lean actually holds under contention.
        """
        alpha, alpha_captain, alpha_roster = self._team("CapAlpha")
        bravo, bravo_captain, bravo_roster = self._team("CapBravo")
        created = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [alpha.team_id, bravo.team_id],
            "kind": "fcfs",
        }, self.admin)
        invites = {i["team_id"]: i["id"] for i in created.json()["invited"]}

        def accept(team, captain, roster):
            def run():
                return Client().post(
                    _accept_url(invites[team.team_id]),
                    data=json.dumps({"roster_member_ids": self._roster_ids(roster)}),
                    content_type="application/json",
                    **self._auth(captain),
                ).status_code
            return run

        results = self._run_concurrently([
            accept(alpha, alpha_captain, alpha_roster),
            accept(bravo, bravo_captain, bravo_roster),
        ])

        self.assertEqual(sorted(str(r) for r in results), ["201", "403"],
                         f"one registers, one is told the event is full, got {results}")
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, is_waitlisted=False).count(), 1)

    def test_two_teams_taking_the_last_place_of_a_bulk_offer_at_once(self):
        """A bulk offer has no ceiling of its own, so this is purely the event capacity race, driven
        through the campaign accept endpoint (which materializes rows rather than updating them)."""
        alpha, alpha_captain, alpha_roster = self._team("BulkAlpha")
        bravo, bravo_captain, bravo_roster = self._team("BulkBravo")
        created = self._post(CREATE_URL, {
            "event_id": self.event.event_id,
            "team_ids": [alpha.team_id, bravo.team_id], "kind": "bulk",
        }, self.admin)
        campaign_id = created.json()["campaign"]["campaign_id"]

        def take(team, captain, roster):
            def run():
                return Client().post(
                    _campaign_accept_url(campaign_id),
                    data=json.dumps({
                        "team_id": team.team_id,
                        "roster_member_ids": self._roster_ids(roster),
                    }),
                    content_type="application/json",
                    **self._auth(captain),
                ).status_code
            return run

        results = self._run_concurrently([
            take(alpha, alpha_captain, alpha_roster),
            take(bravo, bravo_captain, bravo_roster),
        ])

        self.assertEqual(sorted(str(r) for r in results), ["201", "403"],
                         f"one takes the place, one is told the event is full, got {results}")
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, is_waitlisted=False).count(), 1)
        self.assertEqual(
            EventTeamInvitation.objects.filter(campaign_id=campaign_id).count(), 1,
            "only the team that got in leaves a row behind",
        )
