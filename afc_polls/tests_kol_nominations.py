"""
afc_polls.tests_kol_nominations - the KOL creators' nomination round (AFC inbox #633 / #634).

Design: WEBSITE/tasks/kol-nominations-design-2026-11-25.md. Plan: WEBSITE/tasks/plan-kol-nominations-2026-11-26.md.

WHAT IS WORTH TESTING HERE
  1. THE ROUND'S POLL IS INVISIBLE TO EVERY PUBLIC DOOR. It is a DRAFT with no edition, so /polls, /awards and the
     normal submit door never list it, show it or take a vote on it. Only the code door writes to it.
  2. ONE CODE, ONE CREATOR, ONE BALLOT. Codes are stored hashed, typed sloppily they still work, a wrong one is
     counted and limited, a replaced one stops working, and the ballot belongs to the CREATOR (a new code keeps the
     picks).
  3. NEVER YOURSELF, AT MOST SIX, NO COUNTS. The server refuses a self-pick and a seventh pick whatever the page sent.
  4. THE ADMIN SIDE never stores an email and never writes AFC into the message a creator receives.

Run (see memory reference_backend_tests_in_worktree):
    AFC_TEST_DB_NAME=test_afc_kolnom python manage.py test afc_polls.tests_kol_nominations -v 2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

import csv
import io
import json
import pathlib
import re
import tempfile
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command

from .kol_messages import message_for
from .kol_nominations import AWARDS, COOKIE_NAME, ensure_round, new_code, normalize_code, respondent_key_for, tally
from .models import (
    AwardsEdition, KolCreator, KolCreatorOption, KolNominationRound, KolVoterCode, Poll, PollOption, PollPickSwitch,
    PollQuestion, PollResponse,
)
from .tests import make_user, token_for


def kol_edition(slug="kol-2026", hidden=True):
    """The themed KOL edition. Hidden by default: that is how it sits while the creators nominate."""
    return AwardsEdition.objects.create(
        slug=slug, title="KOL Awards 2026", year=2026, theme=AwardsEdition.THEME_DETTY_ROYALE, hidden=hidden,
    )


def add_creators(edition, names=("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF")):
    """One KolCreator per name; the handle is the name, lower case, without spaces."""
    return [
        KolCreator.objects.create(
            edition=edition, name=name, tiktok_handle=name.lower().replace(" ", "_"), country="Nigeria",
        )
        for name in names
    ]


def open_round(edition, **dates):
    """ensure_round with the round open now, unless dates are given."""
    now = timezone.now()
    return ensure_round(
        edition,
        opens_at=dates.get("opens_at", now - timedelta(hours=1)),
        closes_at=dates.get("closes_at", now + timedelta(days=3)),
    )


class KolRoundBuildTests(TestCase):
    def test_round_poll_is_draft_and_editionless(self):
        edition = kol_edition()
        add_creators(edition)
        poll = open_round(edition).poll
        self.assertEqual(poll.visibility, Poll.DRAFT)
        self.assertIsNone(poll.edition_id)
        questions = list(poll.questions.order_by("order"))
        self.assertEqual([q.prompt for q in questions], list(AWARDS))
        self.assertEqual(len(questions), 6)
        for question in questions:
            self.assertEqual(question.answer_type, PollQuestion.MULTIPLE_CHOICE)
            self.assertEqual(question.config.get("max_choices"), 6)

    def test_ensure_round_is_idempotent(self):
        edition = kol_edition()
        add_creators(edition)
        first = open_round(edition)
        second = ensure_round(edition)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Poll.objects.filter(kol_round__edition=edition).count(), 1)
        self.assertEqual(first.poll.questions.count(), 6)
        self.assertEqual(PollOption.objects.filter(question__poll=first.poll).count(), 6 * 4)
        self.assertEqual(KolCreatorOption.objects.filter(creator__edition=edition).count(), 6 * 4)

    def test_new_creator_gets_options_on_rerun(self):
        edition = kol_edition()
        add_creators(edition)
        round_ = open_round(edition)
        add_creators(edition, names=("Zara Clips",))
        ensure_round(edition)
        self.assertEqual(PollOption.objects.filter(question__poll=round_.poll).count(), 6 * 5)
        self.assertEqual(KolCreator.objects.get(name="Zara Clips").options.count(), 6)

    def test_round_not_in_edition_detail(self):
        edition = kol_edition()
        add_creators(edition)
        round_ = open_round(edition)
        admin = make_user("kolnomadmin", role="admin")
        answer = APIClient().get(f"/polls/editions/{edition.slug}/", **token_for(admin))
        self.assertEqual(answer.status_code, 200)
        slugs = [poll["slug"] for poll in answer.json()["polls"]]
        self.assertNotIn(round_.poll.slug, slugs)

    def test_state_by_dates(self):
        now = timezone.now()
        edition = kol_edition(hidden=True)
        add_creators(edition)
        round_ = open_round(edition)
        self.assertEqual(round_.state(), KolNominationRound.OPEN)       # a hidden edition is still open
        round_.opens_at, round_.closes_at = now + timedelta(hours=1), now + timedelta(days=1)
        self.assertEqual(round_.state(), KolNominationRound.NOT_OPEN)
        round_.opens_at, round_.closes_at = now - timedelta(days=2), now - timedelta(minutes=1)
        self.assertEqual(round_.state(), KolNominationRound.CLOSED)
        round_.opens_at, round_.closes_at = None, now + timedelta(days=1)
        self.assertEqual(round_.state(), KolNominationRound.NOT_OPEN)       # no opening date: not open

    def test_respondent_key_is_per_creator(self):
        edition = kol_edition()
        jokkie, rex, *_ = add_creators(edition)
        round_ = open_round(edition)
        self.assertEqual(respondent_key_for(round_, jokkie), respondent_key_for(round_, jokkie))
        self.assertNotEqual(respondent_key_for(round_, jokkie), respondent_key_for(round_, rex))
        self.assertEqual(len(respondent_key_for(round_, jokkie)), 64)


CODE_RE = re.compile(r"^KOL-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}$")


def door(client, edition, code, ip="198.51.100.7"):
    return client.post(f"/polls/kol-nominations/{edition.slug}/door/", {"code": code}, format="json", REMOTE_ADDR=ip)


def ballot(client, edition):
    return client.get(f"/polls/kol-nominations/{edition.slug}/ballot/")


class KolDoorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.jokkie, self.rex, *_ = add_creators(self.edition)
        self.round = open_round(self.edition)

    def test_code_shape_and_alphabet(self):
        codes = {new_code(self.jokkie) for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for code in codes:
            self.assertRegex(code, CODE_RE)

    def test_new_code_revokes_the_old_one(self):
        new_code(self.jokkie)
        new_code(self.jokkie)
        self.assertEqual(KolVoterCode.objects.filter(creator=self.jokkie, revoked_at__isnull=True).count(), 1)
        self.assertEqual(KolVoterCode.objects.filter(creator=self.jokkie).count(), 2)

    def test_code_stored_hashed(self):
        code = new_code(self.jokkie)
        row = KolVoterCode.objects.get(creator=self.jokkie)
        stored = " ".join(str(value) for value in KolVoterCode.objects.filter(pk=row.pk).values_list().first())
        self.assertNotIn(code, stored)
        self.assertNotIn(normalize_code(code), stored)
        self.assertEqual(len(row.code_hash), 64)

    def test_door_accepts_sloppy_code(self):
        code = new_code(self.jokkie)
        sloppy = " " + code.lower().replace("-", " ") + " "
        answer = door(APIClient(), self.edition, sloppy)
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["me"]["creator_id"], self.jokkie.pk)
        bare = code.replace("KOL-", "").replace("-", "")
        self.assertEqual(door(APIClient(), self.edition, bare).status_code, 200)

    def test_door_sets_httponly_cookie(self):
        code = new_code(self.jokkie)
        client = APIClient()
        answer = door(client, self.edition, code)
        cookie = answer.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/polls/kol-nominations/")
        self.assertNotIn(normalize_code(code), cookie.value.upper().replace("-", ""))
        self.assertGreater(int(cookie["max-age"]), 0)
        self.assertEqual(ballot(client, self.edition).status_code, 200)    # the cookie alone opens it

    def test_wrong_code_counts_and_refuses(self):
        new_code(self.jokkie)
        answer = door(APIClient(), self.edition, "KOL-AAAA-BBBB")
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "kol_code_wrong")
        self.round.refresh_from_db()
        self.assertEqual(self.round.failed_attempts, 1)

    def test_eleventh_wrong_try_is_429(self):
        client = APIClient()
        for _ in range(10):
            self.assertEqual(door(client, self.edition, "KOL-AAAA-BBBB").status_code, 400)
        answer = door(client, self.edition, "KOL-AAAA-BBBB")
        self.assertEqual(answer.status_code, 429)
        self.assertEqual(answer.json()["code"], "kol_code_too_many")
        # a right code from that connection is held too, and another connection is untouched
        self.assertEqual(door(client, self.edition, new_code(self.jokkie)).status_code, 429)
        self.assertEqual(door(APIClient(), self.edition, "KOL-AAAA-BBBB", ip="203.0.113.9").status_code, 400)

    def test_reissued_code_kills_old_cookie(self):
        client = APIClient()
        self.assertEqual(door(client, self.edition, new_code(self.jokkie)).status_code, 200)
        new_code(self.jokkie)
        answer = ballot(client, self.edition)
        self.assertEqual(answer.status_code, 401)
        self.assertEqual(answer.json()["code"], "kol_code_replaced")

    def test_no_cookie_asks_for_a_code(self):
        answer = ballot(APIClient(), self.edition)
        self.assertEqual(answer.status_code, 401)
        self.assertEqual(answer.json()["code"], "kol_code_needed")

    def test_forged_cookie_asks_for_a_code(self):
        client = APIClient()
        client.cookies[COOKIE_NAME] = "eyJjIjoxfQ:forged:signature"
        self.assertEqual(ballot(client, self.edition).json()["code"], "kol_code_needed")

    def test_switched_off_creator_code_refused(self):
        code = new_code(self.jokkie)
        KolCreator.objects.filter(pk=self.jokkie.pk).update(active=False)
        answer = door(APIClient(), self.edition, code)
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "kol_code_wrong")

    def test_code_of_another_edition_refused(self):
        other = kol_edition(slug="kol-other")
        stranger = add_creators(other, names=("Other Guy",))[0]
        open_round(other)
        self.assertEqual(door(APIClient(), self.edition, new_code(stranger)).json()["code"], "kol_code_wrong")

    def test_unknown_edition_is_404(self):
        answer = APIClient().post("/polls/kol-nominations/nope/door/", {"code": "x"}, format="json")
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()["code"], "kol_round_not_found")

    def test_signout_clears_cookie(self):
        client = APIClient()
        door(client, self.edition, new_code(self.jokkie))
        answer = client.post(f"/polls/kol-nominations/{self.edition.slug}/signout/")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.cookies[COOKIE_NAME].value, "")
        self.assertEqual(ballot(client, self.edition).status_code, 401)


def save(client, edition, picks, **extra):
    return client.put(f"/polls/kol-nominations/{edition.slug}/ballot/", {"picks": picks, **extra}, format="json")


class KolBallotTests(TestCase):
    NAMES = ("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF", "Zara Clips", "Bisi Live", "Tunde X", "Nana Q", "Femi B")

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition, names=self.NAMES)
        self.me = self.creators[0]
        self.others = self.creators[1:]
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        self.client = APIClient()
        self.assertEqual(door(self.client, self.edition, new_code(self.me)).status_code, 200)

    def q(self, index=0):
        return str(self.awards[index].question_id)

    def test_ballot_hides_me_and_switched_off(self):
        KolCreator.objects.filter(pk=self.others[0].pk).update(active=False)
        data = ballot(self.client, self.edition).json()
        ids = [c["creator_id"] for c in data["candidates"]]
        self.assertNotIn(self.me.pk, ids)
        self.assertNotIn(self.others[0].pk, ids)
        self.assertEqual(len(ids), len(self.NAMES) - 2)
        self.assertEqual([a["name"] for a in data["awards"]], list(AWARDS))
        self.assertTrue(all(a["max"] == 6 for a in data["awards"]))
        self.assertNotIn("option", str(data))           # option ids never leave the server

    def test_save_and_reload_picks(self):
        picks = {self.q(0): [self.others[0].pk, self.others[1].pk], self.q(5): [self.others[2].pk]}
        answer = save(self.client, self.edition, picks)
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["dropped"], [])
        again = ballot(self.client, self.edition).json()
        self.assertEqual(again["picks"][self.q(0)], [self.others[0].pk, self.others[1].pk])
        self.assertEqual(again["picks"][self.q(5)], [self.others[2].pk])
        self.assertEqual(again["picks"][self.q(1)], [])
        self.assertIsNotNone(again["saved_at"])

    def test_zero_picks_allowed(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk]})
        answer = save(self.client, self.edition, {})
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(ballot(self.client, self.edition).json()["picks"][self.q(0)], [])

    def test_seventh_pick_refused(self):
        answer = save(self.client, self.edition, {self.q(0): [c.pk for c in self.others[:7]]})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "kol_too_many_picks")
        self.assertEqual(save(self.client, self.edition, {self.q(0): [c.pk for c in self.others[:6]]}).status_code, 200)

    def test_self_pick_refused_even_if_posted(self):
        answer = save(self.client, self.edition, {self.q(2): [self.me.pk, self.others[0].pk]})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "kol_self_pick")
        self.assertFalse(PollResponse.objects.filter(poll=self.round.poll).exists())

    def test_bad_picks_refused(self):
        for bad in ({"999999": [self.others[0].pk]}, {self.q(0): ["x"]}, {self.q(0): [999999]}, ["nope"]):
            answer = save(self.client, self.edition, bad)
            self.assertEqual(answer.status_code, 400, bad)
            self.assertEqual(answer.json()["code"], "kol_bad_picks", bad)

    def test_switched_off_pick_dropped(self):
        gone = self.others[0]
        KolCreator.objects.filter(pk=gone.pk).update(active=False)
        answer = save(self.client, self.edition, {self.q(0): [gone.pk, self.others[1].pk]})
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["dropped"], [gone.pk])
        self.assertEqual(answer.json()["picks"][self.q(0)], [self.others[1].pk])

    def test_closed_round_read_only(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk]})
        KolNominationRound.objects.filter(pk=self.round.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        data = ballot(self.client, self.edition).json()
        self.assertEqual(data["round"]["state"], "closed")
        self.assertEqual(data["picks"][self.q(0)], [self.others[0].pk])
        answer = save(self.client, self.edition, {self.q(0): []})
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_round_closed")

    def test_not_open_round_refuses_save(self):
        KolNominationRound.objects.filter(pk=self.round.pk).update(opens_at=timezone.now() + timedelta(days=1))
        answer = save(self.client, self.edition, {self.q(0): [self.others[0].pk]})
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_round_not_open")

    def test_save_needs_the_cookie(self):
        answer = save(APIClient(), self.edition, {self.q(0): [self.others[0].pk]})
        self.assertEqual(answer.status_code, 401)
        self.assertEqual(answer.json()["code"], "kol_code_needed")

    def test_one_row_per_creator_across_codes(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk]})
        fresh = APIClient()
        door(fresh, self.edition, new_code(self.me))
        self.assertEqual(ballot(fresh, self.edition).json()["picks"][self.q(0)], [self.others[0].pk])
        save(fresh, self.edition, {self.q(0): [self.others[3].pk]})
        rows = PollResponse.objects.filter(poll=self.round.poll)
        self.assertEqual(rows.count(), 1)
        self.assertIsNone(rows.first().respondent_id)
        self.assertEqual(rows.first().respondent_key, respondent_key_for(self.round, self.me))
        self.assertEqual(ballot(fresh, self.edition).json()["picks"][self.q(0)], [self.others[3].pk])

    def test_pick_switch_recorded(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk]})
        save(self.client, self.edition, {self.q(0): [self.others[1].pk]})
        self.assertEqual(PollPickSwitch.objects.filter(poll=self.round.poll).count(), 1)

    def test_tracking_stamped_without_a_user(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk]}, device_key="devkey-abcdefghijklmnop")
        row = PollResponse.objects.get(poll=self.round.poll)
        self.assertEqual(len(row.voter_key), 64)
        self.assertEqual(len(row.device_hash), 64)
        self.assertIsNone(row.account_age_days)

    def test_normal_submit_door_refuses_round_poll(self):
        voter = make_user("kolnomsneak")
        option = PollOption.objects.filter(question=self.awards[0]).first()
        answer = APIClient().post(
            f"/polls/{self.round.poll.slug}/responses/",
            {"answers": [{"question_id": self.awards[0].question_id, "option_ids": [option.option_id]}]},
            format="json", **token_for(voter),
        )
        self.assertIn(answer.status_code, (403, 404))
        self.assertFalse(PollResponse.objects.filter(poll=self.round.poll).exists())
        self.assertEqual(APIClient().get(f"/polls/{self.round.poll.slug}/", **token_for(voter)).status_code, 404)

    def test_save_rate_limited(self):
        with mock.patch("afc_polls.kol_nominations.SAVES_PER_MINUTE", 2):
            self.assertEqual(save(self.client, self.edition, {}).status_code, 200)
            self.assertEqual(save(self.client, self.edition, {}).status_code, 200)
            answer = save(self.client, self.edition, {})
        self.assertEqual(answer.status_code, 429)
        self.assertEqual(answer.json()["code"], "kol_save_too_many")

    def test_tally_counts_active_only(self):
        save(self.client, self.edition, {self.q(0): [self.others[0].pk, self.others[1].pk]})
        second = APIClient()
        door(second, self.edition, new_code(self.others[2]))
        save(second, self.edition, {self.q(0): [self.others[0].pk]})
        KolCreator.objects.filter(pk=self.others[1].pk).update(active=False)
        rows = tally(self.round)[self.awards[0].question_id]
        self.assertEqual(rows, [{"creator_id": self.others[0].pk, "name": self.others[0].name, "picks": 2}])
        self.assertEqual(tally(self.round)[self.awards[1].question_id], [])


def read_csv(answer):
    text = b"".join(answer.streaming_content).decode("utf-8") if answer.streaming else answer.content.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


class KolAdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition)
        self.admin = make_user("kolnomboss", role="admin")
        self.client = APIClient()
        self.client.credentials(**token_for(self.admin))
        self.base = f"/polls/admin/kol-nominations/{self.edition.slug}/"

    def setup_round(self, **body):
        now = timezone.now()
        body = {"opens_at": (now - timedelta(hours=1)).isoformat(), "closes_at": (now + timedelta(days=5)).isoformat(),
                **body}
        return self.client.post(self.base, body, format="json")

    def test_admin_only(self):
        self.assertEqual(APIClient().get(self.base).status_code, 401)
        player = APIClient()
        player.credentials(**token_for(make_user("kolnomplayer")))
        answer = player.get(self.base)
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_admin_only")
        self.assertEqual(player.post(f"{self.base}codes/", {}, format="json").status_code, 403)
        self.assertEqual(
            player.patch(f"{self.base}creators/{self.creators[0].pk}/", {"active": False}, format="json").status_code,
            403)

    def test_setup_round_with_dates(self):
        self.assertIsNone(self.client.get(self.base).json()["round"])
        answer = self.setup_round()
        self.assertEqual(answer.status_code, 200, answer.content)
        data = self.client.get(self.base).json()
        self.assertEqual(data["round"]["state"], "open")
        self.assertEqual(len(data["creators"]), 4)
        self.assertEqual(KolNominationRound.objects.get(edition=self.edition).poll.questions.count(), 6)
        later = (timezone.now() + timedelta(days=9)).isoformat()
        earlier = (timezone.now() + timedelta(days=1)).isoformat()
        bad = self.setup_round(opens_at=later, closes_at=earlier)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["code"], "kol_bad_dates")
        self.assertEqual(self.setup_round(opens_at="not a date").json()["code"], "kol_bad_dates")

    def test_csv_has_codes_once(self):
        self.setup_round()
        first = self.client.post(f"{self.base}codes/", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first["Content-Type"].split(";")[0], "text/csv")
        self.assertIn("attachment", first["Content-Disposition"])
        self.assertEqual(first["Cache-Control"], "no-store")
        rows = read_csv(first)
        self.assertEqual(len(rows), 4)
        self.assertEqual(list(rows[0].keys()), ["name", "tiktok_handle", "country", "code", "link", "message"])
        for row in rows:
            self.assertRegex(row["code"], CODE_RE)
            self.assertTrue(row["link"].endswith(f"/awards/{self.edition.slug}/nominate#{row['code']}"))
            self.assertIn(row["code"], row["message"])
        # every code works at the door
        self.assertEqual(door(APIClient(), self.edition, rows[0]["code"]).status_code, 200)
        # nobody is left without a code, so a second export is empty
        self.assertEqual(read_csv(self.client.post(f"{self.base}codes/", {}, format="json")), [])
        data = self.client.get(self.base).json()
        self.assertTrue(all(c["has_code"] for c in data["creators"]))

    def test_csv_for_chosen_creators_reissues(self):
        self.setup_round()
        old = read_csv(self.client.post(f"{self.base}codes/", {}, format="json"))
        target = self.creators[1]
        rows = read_csv(self.client.post(f"{self.base}codes/", {"creator_ids": [target.pk]}, format="json"))
        self.assertEqual([row["tiktok_handle"] for row in rows], [target.tiktok_handle])
        old_code = next(row["code"] for row in old if row["tiktok_handle"] == target.tiktok_handle)
        self.assertEqual(door(APIClient(), self.edition, old_code).json()["code"], "kol_code_wrong")
        self.assertEqual(door(APIClient(), self.edition, rows[0]["code"]).status_code, 200)

    def test_voted_shows_after_a_save(self):
        self.setup_round()
        rows = read_csv(self.client.post(f"{self.base}codes/", {}, format="json"))
        creator = APIClient()
        door(creator, self.edition, rows[0]["code"])
        question = KolNominationRound.objects.get(edition=self.edition).poll.questions.order_by("order").first()
        me = KolCreator.objects.get(tiktok_handle=rows[0]["tiktok_handle"])
        other = KolCreator.objects.exclude(pk=me.pk).first()
        save(creator, self.edition, {str(question.question_id): [other.pk]})
        data = self.client.get(self.base).json()
        voted = {c["creator_id"]: c["voted"] for c in data["creators"]}
        self.assertTrue(voted[me.pk])
        self.assertEqual(sum(voted.values()), 1)
        self.assertEqual(data["tally"][str(question.question_id)][0]["creator_id"], other.pk)
        self.assertNotIn("picks_by_voter", str(data))

    def test_switch_off_revokes_code(self):
        self.setup_round()
        rows = read_csv(self.client.post(f"{self.base}codes/", {}, format="json"))
        target = KolCreator.objects.get(tiktok_handle=rows[0]["tiktok_handle"])
        answer = self.client.patch(f"{self.base}creators/{target.pk}/", {"active": False}, format="json")
        self.assertEqual(answer.status_code, 200)
        self.assertFalse(KolCreator.objects.get(pk=target.pk).active)
        self.assertEqual(door(APIClient(), self.edition, rows[0]["code"]).json()["code"], "kol_code_wrong")
        self.assertEqual(self.client.patch(f"{self.base}creators/999999/", {"active": True}, format="json").status_code,
                         404)
        self.assertEqual(
            self.client.patch(f"{self.base}creators/{target.pk}/", {"active": "yes"}, format="json").status_code, 400)

    def test_creator_of_another_edition_unreachable(self):
        other = kol_edition(slug="kol-elsewhere")
        stranger = add_creators(other, names=("Far Away",))[0]
        answer = self.client.patch(f"{self.base}creators/{stranger.pk}/", {"active": False}, format="json")
        self.assertEqual(answer.status_code, 404)
        self.assertTrue(KolCreator.objects.get(pk=stranger.pk).active)

    def test_message_language_by_country(self):
        expect = {"Mozambique": "boletim", "Togo": "bulletin", "Ivory Coast": "bulletin", "Nigeria": "ballot"}
        for country, word in expect.items():
            creator = KolCreator(name="Ama", tiktok_handle="ama", country=country, edition=self.edition)
            text = message_for(creator, "KOL-AAAA-BBBB", "https://x.test/n#KOL-AAAA-BBBB", timezone.now(),
                               "KOL Awards 2026")
            self.assertIn(word, text, country)
            self.assertIn("KOL-AAAA-BBBB", text)
            self.assertNotIn("AFC", text)
            self.assertNotIn("African", text)
            for dash in (chr(0x2013), chr(0x2014)):
                self.assertNotIn(dash, text)

    def test_import_is_idempotent_and_never_stores_email(self):
        rows = [
            {"name": "Jokkie", "tiktok_handle": "@Jokkie", "country": "Nigeria", "status": "Added", "tiktok_id": "123",
             "email": "jokkie@example.com"},
            {"name": "Brand New", "tiktok_handle": "brand_new", "country": "Togo", "status": "New", "tiktok_id": "",
             "email": "new@example.com"},
        ]
        path = pathlib.Path(tempfile.mkdtemp()) / "kol.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        out = io.StringIO()
        call_command("kol_import_creators", str(path), "--edition", self.edition.slug, stdout=out)
        call_command("kol_import_creators", str(path), "--edition", self.edition.slug, stdout=out)
        self.assertEqual(KolCreator.objects.filter(edition=self.edition).count(), 5)
        jokkie = KolCreator.objects.get(edition=self.edition, tiktok_handle="jokkie")
        self.assertEqual((jokkie.sheet_status, jokkie.tiktok_id), ("Added", "123"))
        self.assertIn("1 created", out.getvalue())
        stored = json.dumps(list(KolCreator.objects.values()), default=str)
        self.assertNotIn("@example.com", stored)
        self.setup_round()
        rows = read_csv(self.client.post(f"{self.base}codes/", {}, format="json"))
        self.assertNotIn("@example.com", json.dumps(rows))
