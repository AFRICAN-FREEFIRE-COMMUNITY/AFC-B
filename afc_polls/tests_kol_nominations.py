"""
afc_polls.tests_kol_nominations - the KOL creators' nomination round (AFC inbox #633 / #634).

Design: WEBSITE/tasks/kol-nominations-design-2026-11-25.md. Plan: WEBSITE/tasks/plan-kol-nominations-2026-11-26.md.

WHAT IS WORTH TESTING HERE
  1. THE ROUND'S POLL IS INVISIBLE TO EVERY PUBLIC DOOR. It is a DRAFT with no edition, so /polls, /awards and the
     normal submit door never list it, show it or take a vote on it. Only the code door writes to it.
  2. ONE CODE, ONE CREATOR, ONE BALLOT. Codes are stored hashed, typed sloppily they still work, a wrong one is
     counted and limited, a replaced one stops working, and the ballot belongs to the CREATOR (a new code keeps the
     picks).
  3. AT MOST SIX, ONE PERSON ONCE, NO COUNTS. The server refuses a seventh pick whatever the page sent and keeps a
     person once per award. Self-picks are allowed (owner, 26 Nov 2026, #637).
  5. WRITE-INS. Anybody added by handle is a candidate for every creator, never a voter, capped per creator, and a
     duplicate merges into the person it repeats.
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

    def test_ballot_lists_me_and_hides_switched_off(self):
        # Owner, 26 Nov 2026 (#637): "they should be able to nominate themselves now". The creator is on their own list.
        KolCreator.objects.filter(pk=self.others[0].pk).update(active=False)
        data = ballot(self.client, self.edition).json()
        ids = [c["creator_id"] for c in data["candidates"]]
        self.assertIn(self.me.pk, ids)
        self.assertNotIn(self.others[0].pk, ids)
        self.assertEqual(len(ids), len(self.NAMES) - 1)
        self.assertTrue(all(c["write_in"] is False for c in data["candidates"]))
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

    def test_self_pick_allowed(self):
        answer = save(self.client, self.edition, {self.q(2): [self.me.pk, self.others[0].pk]})
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["picks"][self.q(2)], [self.me.pk, self.others[0].pk])
        self.assertEqual(tally(self.round)[self.awards[2].question_id][0]["picks"], 1)

    def test_same_person_twice_in_one_award_saved_once(self):
        answer = save(self.client, self.edition, {self.q(0): [self.others[0].pk, self.others[0].pk, self.others[1].pk]})
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["picks"][self.q(0)], [self.others[0].pk, self.others[1].pk])
        self.assertEqual(tally(self.round)[self.awards[0].question_id][0]["picks"], 1)

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
            # #637: self-nomination is allowed now, so no language may still tell them "never yourself".
            for old in ("never yourself", "jamais vous-même", "nunca a si"):
                self.assertNotIn(old, text, country)
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


class KolRoundPollIsSealedTests(TestCase):
    """The round's poll is reachable ONLY through the code door. Found in the Chrome walk on 26 Nov: it was listed
    in /a/polls, so an admin could open it in the poll builder and publish it, and a published poll takes votes
    through the ordinary submit door from anybody signed in, with no code and none of the round's rules."""

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition)
        self.round = open_round(self.edition)
        self.poll = self.round.poll
        self.admin = APIClient()
        self.admin.credentials(**token_for(make_user("kolsealadmin", role="admin")))

    def test_not_in_admin_poll_list(self):
        answer = self.admin.get("/polls/admin/polls/?limit=100")
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn(self.poll.slug, [row["slug"] for row in answer.json()["results"]])

    def test_poll_builder_cannot_reach_it(self):
        base = f"/polls/admin/polls/{self.poll.slug}/"
        self.assertEqual(self.admin.get(base).status_code, 404)
        self.assertEqual(self.admin.patch(base, {"visibility": "public"}, format="json").status_code, 404)
        self.assertEqual(self.admin.put(f"{base}questions/", {"questions": []}, format="json").status_code, 404)
        self.assertEqual(self.admin.get(f"{base}results/").status_code, 404)
        self.assertEqual(self.admin.delete(base).status_code, 404)
        self.poll.refresh_from_db()
        self.assertEqual(self.poll.visibility, Poll.DRAFT)
        self.assertEqual(self.poll.questions.count(), 6)

    def test_published_by_hand_it_still_takes_no_ordinary_vote(self):
        Poll.objects.filter(pk=self.poll.pk).update(visibility=Poll.PUBLIC, opens_at=timezone.now() - timedelta(hours=1))
        voter = make_user("kolsealvoter")
        question = self.poll.questions.order_by("order").first()
        option = question.options.first()
        answer = APIClient().post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": question.question_id, "option_ids": [option.option_id]}]},
            format="json", **token_for(voter),
        )
        self.assertEqual(answer.status_code, 404)
        self.assertFalse(PollResponse.objects.filter(poll=self.poll).exists())
        self.assertEqual(APIClient().get(f"/polls/{self.poll.slug}/").status_code, 404)
        self.assertNotIn(self.poll.slug, [row["slug"] for row in APIClient().get("/polls/").json()["results"]])

    def test_ordinary_polls_still_work(self):
        plain = Poll.objects.create(slug="plain-poll", title="Plain", visibility=Poll.PUBLIC,
                                    opens_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(APIClient().get(f"/polls/{plain.slug}/").status_code, 200)
        self.assertEqual(self.admin.get(f"/polls/admin/polls/{plain.slug}/").status_code, 200)


class KolDoorIgnoresDjangoSessionTests(TestCase):
    """The code endpoints know the creator by the code and the kol_ballot cookie only. A browser that ALSO holds a
    Django admin session on the API host (Layo testing, say) must not be asked for a CSRF token on save: DRF's
    default SessionAuthentication would, so these views carry no DRF authentication at all."""

    def test_save_works_with_a_django_session_and_csrf_checks_on(self):
        cache.clear()
        edition = kol_edition()
        me, other, *_ = add_creators(edition)
        round_ = open_round(edition)
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(make_user("kolcsrfstaff", role="admin"))
        self.assertEqual(door(client, edition, new_code(me)).status_code, 200)
        question = round_.poll.questions.order_by("order").first()
        answer = save(client, edition, {str(question.question_id): [other.pk]})
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(client.post(f"/polls/kol-nominations/{edition.slug}/signout/").status_code, 200)


# ── WRITE-INS (owner, 26 Nov 2026, inbox #636 / #637) ────────────────────────────────────────────────────────────────
# "creators can add and nominate anybody ... If a new person is aded by one creator, everyone else should be able to
# see that person. obviously shouldnt be able to nominate a person more than once per category."


def write_in(client, edition, handle, name=None):
    body = {"handle": handle} if name is None else {"handle": handle, "name": name}
    return client.post(f"/polls/kol-nominations/{edition.slug}/write-in/", body, format="json")


class KolWriteInTests(TestCase):
    NAMES = ("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF")

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.me, self.rex, self.ama, self.kwame = add_creators(self.edition, names=self.NAMES)
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        self.client = APIClient()
        self.assertEqual(door(self.client, self.edition, new_code(self.me)).status_code, 200)

    def q(self, index=0):
        return str(self.awards[index].question_id)

    def test_write_in_becomes_a_shared_candidate(self):
        answer = write_in(self.client, self.edition, "@New_Guy", "New Guy")
        self.assertEqual(answer.status_code, 200, answer.content)
        body = answer.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["candidate"]["handle"], "new_guy")
        self.assertEqual(body["candidate"]["name"], "New Guy")
        self.assertTrue(body["candidate"]["write_in"])
        row = KolCreator.objects.get(edition=self.edition, tiktok_handle="new_guy")
        self.assertEqual((row.origin, row.added_by_id, row.active), (KolCreator.WRITE_IN, self.me.pk, True))
        self.assertEqual(row.options.count(), 6)                        # a candidate in every award
        self.assertFalse(KolVoterCode.objects.filter(creator=row).exists())
        # another creator sees the person, with nothing about who added them and no count
        other = APIClient()
        door(other, self.edition, new_code(self.rex))
        seen = [c for c in ballot(other, self.edition).json()["candidates"] if c["handle"] == "new_guy"]
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0].keys()), {"creator_id", "name", "handle", "country", "write_in"})
        self.assertNotIn(self.me.name, json.dumps(seen))

    def test_profile_link_is_read_as_the_handle(self):
        answer = write_in(self.client, self.edition, "https://www.tiktok.com/@Lord.Rex_FF?lang=en")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["candidate"]["handle"], "lord.rex_ff")

    def test_handle_on_the_list_is_that_candidate(self):
        answer = write_in(self.client, self.edition, " @Lord_Rex ")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertFalse(answer.json()["created"])
        self.assertEqual(answer.json()["candidate"]["creator_id"], self.rex.pk)
        self.assertFalse(answer.json()["candidate"]["write_in"])
        self.assertEqual(KolCreator.objects.filter(edition=self.edition).count(), 4)

    def test_own_handle_is_my_own_candidate(self):
        answer = write_in(self.client, self.edition, "@jokkie")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["candidate"]["creator_id"], self.me.pk)

    def test_same_write_in_twice_is_one_person(self):
        first = write_in(self.client, self.edition, "new_guy").json()["candidate"]
        other = APIClient()
        door(other, self.edition, new_code(self.rex))
        second = write_in(other, self.edition, "@NEW_GUY", "Another Spelling").json()
        self.assertFalse(second["created"])
        self.assertEqual(second["candidate"]["creator_id"], first["creator_id"])
        self.assertEqual(KolCreator.objects.filter(edition=self.edition, tiktok_handle="new_guy").count(), 1)
        self.assertEqual(KolCreator.objects.get(tiktok_handle="new_guy").name, "new_guy")   # the first one stays

    def test_write_in_picks_save_and_count(self):
        new_id = write_in(self.client, self.edition, "new_guy").json()["candidate"]["creator_id"]
        answer = save(self.client, self.edition, {self.q(0): [new_id, self.rex.pk]})
        self.assertEqual(answer.status_code, 200, answer.content)
        other = APIClient()
        door(other, self.edition, new_code(self.ama))
        save(other, self.edition, {self.q(0): [new_id]})
        top = tally(self.round)[self.awards[0].question_id][0]
        self.assertEqual((top["creator_id"], top["picks"]), (new_id, 2))

    def test_bad_handles_refused(self):
        for bad in ("", "   ", "@", "a", "x" * 25, "bad handle!", "no/slash", "émile", None, 12):
            answer = write_in(self.client, self.edition, bad)
            self.assertEqual(answer.status_code, 400, bad)
            self.assertEqual(answer.json()["code"], "kol_bad_handle", bad)
        self.assertEqual(KolCreator.objects.filter(edition=self.edition).count(), 4)

    def test_name_is_cleaned_and_defaults_to_the_handle(self):
        long_name = "  Big   Name\u0007 " + "y" * 80
        made = write_in(self.client, self.edition, "big_name", long_name).json()["candidate"]
        self.assertTrue(made["name"].startswith("Big Name "))
        self.assertLessEqual(len(made["name"]), 60)
        self.assertNotIn("\u0007", made["name"])
        self.assertEqual(write_in(self.client, self.edition, "no_name").json()["candidate"]["name"], "no_name")

    def test_write_in_needs_the_cookie(self):
        answer = write_in(APIClient(), self.edition, "new_guy")
        self.assertEqual(answer.status_code, 401)
        self.assertEqual(answer.json()["code"], "kol_code_needed")

    def test_write_in_only_while_open(self):
        KolNominationRound.objects.filter(pk=self.round.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(write_in(self.client, self.edition, "new_guy").json()["code"], "kol_round_closed")
        KolNominationRound.objects.filter(pk=self.round.pk).update(
            opens_at=timezone.now() + timedelta(days=1), closes_at=timezone.now() + timedelta(days=2))
        answer = write_in(self.client, self.edition, "new_guy")
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_round_not_open")
        self.assertFalse(KolCreator.objects.filter(tiktok_handle="new_guy").exists())

    def test_additions_capped_per_creator(self):
        with mock.patch("afc_polls.kol_nominations.WRITE_INS_PER_CREATOR", 2):
            self.assertEqual(write_in(self.client, self.edition, "one_new").status_code, 200)
            self.assertEqual(write_in(self.client, self.edition, "two_new").status_code, 200)
            answer = write_in(self.client, self.edition, "three_new")
            self.assertEqual(answer.status_code, 429)
            self.assertEqual(answer.json()["code"], "kol_write_ins_too_many")
            # someone already a candidate is never an addition, so the cap does not stop picking them
            self.assertEqual(write_in(self.client, self.edition, "lord_rex").status_code, 200)
            self.assertEqual(write_in(self.client, self.edition, "one_new").status_code, 200)

    def test_additions_rate_limited_per_minute(self):
        with mock.patch("afc_polls.kol_nominations.WRITE_INS_PER_MINUTE", 2):
            self.assertEqual(write_in(self.client, self.edition, "one_new").status_code, 200)
            self.assertEqual(write_in(self.client, self.edition, "two_new").status_code, 200)
            answer = write_in(self.client, self.edition, "three_new")
        self.assertEqual(answer.status_code, 429)
        self.assertEqual(answer.json()["code"], "kol_write_in_too_fast")

    def test_switched_off_handle_is_refused(self):
        made = write_in(self.client, self.edition, "junk_name").json()["candidate"]
        KolCreator.objects.filter(pk=made["creator_id"]).update(active=False)
        answer = write_in(self.client, self.edition, "junk_name")
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "kol_handle_unavailable")
        ids = [c["creator_id"] for c in ballot(self.client, self.edition).json()["candidates"]]
        self.assertNotIn(made["creator_id"], ids)

    def test_merged_handle_leads_to_the_person_it_was_merged_into(self):
        made = write_in(self.client, self.edition, "lordrex").json()["candidate"]
        KolCreator.objects.filter(pk=made["creator_id"]).update(active=False, merged_into=self.rex)
        answer = write_in(self.client, self.edition, "@lordrex")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["candidate"]["creator_id"], self.rex.pk)

    def test_a_write_in_cannot_vote(self):
        made = write_in(self.client, self.edition, "new_guy").json()["candidate"]
        admin = APIClient()
        admin.credentials(**token_for(make_user("kolwiboss", role="admin")))
        base = f"/polls/admin/kol-nominations/{self.edition.slug}/"
        everyone = read_csv(admin.post(f"{base}codes/", {}, format="json"))
        self.assertNotIn("new_guy", [row["tiktok_handle"] for row in everyone])
        chosen = read_csv(admin.post(f"{base}codes/", {"creator_ids": [made["creator_id"]]}, format="json"))
        self.assertEqual(chosen, [])
        # even a code made by hand for a write-in does not open the door
        code = new_code(KolCreator.objects.get(pk=made["creator_id"]))
        self.assertEqual(door(APIClient(), self.edition, code).json()["code"], "kol_code_wrong")


class KolWriteInAdminTests(TestCase):
    """Admin side of write-ins: listed with their origin, switched off like anybody, and merged into the person they
    duplicate so that one person is never counted twice in one award (#637)."""

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.me, self.rex, self.ama, self.kwame = add_creators(self.edition)
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        self.admin = APIClient()
        self.admin.credentials(**token_for(make_user("kolmergeboss", role="admin")))
        self.base = f"/polls/admin/kol-nominations/{self.edition.slug}/"
        self.mine = APIClient()
        door(self.mine, self.edition, new_code(self.me))
        self.dup = write_in(self.mine, self.edition, "lordrex", "Lord Rex").json()["candidate"]["creator_id"]

    def q(self, index=0):
        return str(self.awards[index].question_id)

    def row(self, creator_id):
        return next(c for c in self.admin.get(self.base).json()["creators"] if c["creator_id"] == creator_id)

    def test_admin_lists_write_ins(self):
        dup = self.row(self.dup)
        self.assertEqual(dup["origin"], "write-in")
        self.assertFalse(dup["has_code"])
        self.assertFalse(dup["voted"])
        self.assertIsNone(dup["merged_into"])
        self.assertNotIn("added_by", dup)                    # who added whom stays private, like who picked whom
        self.assertEqual(self.row(self.me.pk)["origin"], "list")
        self.assertEqual(self.row(self.me.pk)["write_ins_added"], 1)
        self.assertEqual(self.row(self.rex.pk)["write_ins_added"], 0)

    def test_merge_moves_picks_and_never_counts_one_person_twice(self):
        save(self.mine, self.edition, {self.q(0): [self.dup, self.rex.pk], self.q(1): [self.dup]})
        theirs = APIClient()
        door(theirs, self.edition, new_code(self.ama))
        save(theirs, self.edition, {self.q(0): [self.dup]})
        answer = self.admin.patch(f"{self.base}creators/{self.dup}/", {"merge_into": self.rex.pk}, format="json")
        self.assertEqual(answer.status_code, 200, answer.content)
        dup = KolCreator.objects.get(pk=self.dup)
        self.assertEqual((dup.active, dup.merged_into_id), (False, self.rex.pk))
        mine = ballot(self.mine, self.edition).json()
        self.assertEqual(mine["picks"][self.q(0)], [self.rex.pk])          # held both: one left
        self.assertEqual(mine["picks"][self.q(1)], [self.rex.pk])          # held the duplicate only: moved
        self.assertNotIn(self.dup, [c["creator_id"] for c in mine["candidates"]])
        self.assertEqual(ballot(theirs, self.edition).json()["picks"][self.q(0)], [self.rex.pk])
        top = tally(self.round)[self.awards[0].question_id]
        self.assertEqual(top, [{"creator_id": self.rex.pk, "name": self.rex.name, "picks": 2}])
        self.assertEqual(self.row(self.dup)["merged_into"], self.rex.pk)

    def test_merge_refusals(self):
        other = kol_edition(slug="kol-merge-elsewhere")
        stranger = add_creators(other, names=("Far Away",))[0]
        KolCreator.objects.filter(pk=self.kwame.pk).update(active=False)
        cases = [
            (self.rex.pk, self.ama.pk, "kol_merge_not_write_in"),     # a creator on the list is never merged away
            (self.dup, self.dup, "kol_bad_merge"),
            (self.dup, self.kwame.pk, "kol_bad_merge"),                # switched off
            (self.dup, stranger.pk, "kol_bad_merge"),                  # another edition
            (self.dup, 999999, "kol_bad_merge"),
            (self.dup, "x", "kol_bad_merge"),
        ]
        for source, target, code in cases:
            answer = self.admin.patch(f"{self.base}creators/{source}/", {"merge_into": target}, format="json")
            self.assertEqual(answer.status_code, 400, (source, target))
            self.assertEqual(answer.json()["code"], code, (source, target))
        self.assertTrue(KolCreator.objects.get(pk=self.dup).active)

    def test_switch_off_write_in(self):
        answer = self.admin.patch(f"{self.base}creators/{self.dup}/", {"active": False}, format="json")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertNotIn(self.dup, [c["creator_id"] for c in ballot(self.mine, self.edition).json()["candidates"]])
        # switched back on, it is a plain candidate again (a merge undone by hand starts clean)
        KolCreator.objects.filter(pk=self.dup).update(merged_into=self.rex)
        self.admin.patch(f"{self.base}creators/{self.dup}/", {"active": True}, format="json")
        self.assertIsNone(KolCreator.objects.get(pk=self.dup).merged_into_id)
        self.assertIn(self.dup, [c["creator_id"] for c in ballot(self.mine, self.edition).json()["candidates"]])

    def test_import_turns_a_write_in_into_a_list_creator(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "kol.json"
        path.write_text(json.dumps([{"name": "Lord Rex Two", "tiktok_handle": "@lordrex", "country": "Ghana"}]),
                        encoding="utf-8")
        call_command("kol_import_creators", str(path), "--edition", self.edition.slug, stdout=io.StringIO())
        row = KolCreator.objects.get(pk=self.dup)
        self.assertEqual((row.origin, row.country), (KolCreator.LIST, "Ghana"))


# ── AWARD KEYS (#638), COPY CODES (#640), SPARE CODES (#639): owner, 26 Nov 2026 ─────────────────────────────────────
# "Can there be an explanation for what each of ten categories mean." (the six awards, explained by key on the page)
# "Should be able to copy codes from the site." (codes kept ENCRYPTED as well, read back by poll admins only)
# "generate new codes that are not linked to anybody in case of we find creators who should be able to vote, but are
#  not on the list." (SPARE codes: the first person to open one says who they are and becomes a creator)

AWARD_KEYS_EXPECTED = ["creator", "breakthrough", "educational", "streamer", "voice", "funniest"]


def admin_client(username):
    client = APIClient()
    client.credentials(**token_for(make_user(username, role="admin")))
    return client


class KolAwardKeyTests(TestCase):
    def test_awards_carry_stable_keys(self):
        cache.clear()
        edition = kol_edition()
        me, *_ = add_creators(edition)
        open_round(edition)
        client = APIClient()
        door(client, edition, new_code(me))
        self.assertEqual([a["key"] for a in ballot(client, edition).json()["awards"]], AWARD_KEYS_EXPECTED)
        admin = admin_client("kolkeyboss")
        round_awards = admin.get(f"/polls/admin/kol-nominations/{edition.slug}/").json()["round"]["awards"]
        self.assertEqual([a["key"] for a in round_awards], AWARD_KEYS_EXPECTED)


class KolCopyCodeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition, names=("Jokkie", "Lord Rex", "Ines Jogos"))
        KolCreator.objects.filter(name="Ines Jogos").update(country="Mozambique")
        self.round = open_round(self.edition)
        self.admin = admin_client("kolcopyboss")
        self.base = f"/polls/admin/kol-nominations/{self.edition.slug}/"

    def row(self, handle):
        return next(c for c in self.admin.get(self.base).json()["creators"] if c["handle"] == handle)

    def test_admin_reads_back_a_live_code_with_its_message(self):
        rows = {r["tiktok_handle"]: r for r in read_csv(self.admin.post(f"{self.base}codes/", {}, format="json"))}
        for handle in ("jokkie", "ines_jogos"):
            answer = self.admin.get(f"{self.base}codes/{self.row(handle)['code_id']}/")
            self.assertEqual(answer.status_code, 200, answer.content)
            self.assertEqual(answer["Cache-Control"], "no-store")
            body = answer.json()
            self.assertEqual(body["code"], rows[handle]["code"])
            self.assertEqual(body["link"], rows[handle]["link"])
            self.assertEqual(body["message"], rows[handle]["message"])
        self.assertIn("boletim", self.admin.get(f"{self.base}codes/{self.row('ines_jogos')['code_id']}/").json()["message"])

    def test_code_stored_encrypted_never_plain(self):
        code = new_code(self.creators[0])
        row = KolVoterCode.objects.get(creator=self.creators[0], revoked_at__isnull=True)
        self.assertTrue(row.code_sealed)
        stored = " ".join(str(v) for v in KolVoterCode.objects.filter(pk=row.pk).values_list().first())
        self.assertNotIn(code, stored)
        self.assertNotIn(normalize_code(code), stored)

    def test_a_replaced_code_cannot_be_read(self):
        new_code(self.creators[0])
        old_id = self.row("jokkie")["code_id"]
        new_code(self.creators[0])
        answer = self.admin.get(f"{self.base}codes/{old_id}/")
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()["code"], "kol_code_not_found")
        self.assertNotEqual(self.row("jokkie")["code_id"], old_id)

    def test_only_poll_admins(self):
        new_code(self.creators[0])
        code_id = self.row("jokkie")["code_id"]
        self.assertEqual(APIClient().get(f"{self.base}codes/{code_id}/").status_code, 401)
        player = APIClient()
        player.credentials(**token_for(make_user("kolcopyplayer")))
        self.assertEqual(player.get(f"{self.base}codes/{code_id}/").status_code, 403)
        self.assertEqual(player.delete(f"{self.base}codes/{code_id}/").status_code, 403)

    def test_a_code_of_another_edition_is_404(self):
        other = kol_edition(slug="kol-copy-elsewhere")
        stranger = add_creators(other, names=("Far Away",))[0]
        new_code(stranger)
        code_id = KolVoterCode.objects.get(creator=stranger).code_id
        self.assertEqual(self.admin.get(f"{self.base}codes/{code_id}/").status_code, 404)

    def test_a_code_made_before_copying_existed_says_so(self):
        new_code(self.creators[0])
        KolVoterCode.objects.filter(creator=self.creators[0]).update(code_sealed="")
        answer = self.admin.get(f"{self.base}codes/{self.row('jokkie')['code_id']}/")
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()["code"], "kol_code_unreadable")

    def test_admin_revokes_a_code(self):
        code = new_code(self.creators[0])
        code_id = self.row("jokkie")["code_id"]
        self.assertEqual(self.admin.delete(f"{self.base}codes/{code_id}/").status_code, 200)
        self.assertEqual(door(APIClient(), self.edition, code).json()["code"], "kol_code_wrong")
        self.assertFalse(self.row("jokkie")["has_code"])


class KolSpareCodeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.me, self.rex, *_ = add_creators(self.edition)
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        self.admin = admin_client("kolspareboss")
        self.base = f"/polls/admin/kol-nominations/{self.edition.slug}/"

    def spares(self, n):
        return read_csv(self.admin.post(f"{self.base}codes/", {"spare": n}, format="json"))

    def claim(self, client, **body):
        return client.post(f"/polls/kol-nominations/{self.edition.slug}/claim/", body, format="json")

    def test_admin_makes_spare_codes(self):
        rows = self.spares(3)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertRegex(row["code"], CODE_RE)
            self.assertEqual((row["name"], row["tiktok_handle"]), ("", ""))
            self.assertIn(row["code"], row["message"])
            self.assertNotIn("AFC", row["message"])
        self.assertEqual(KolVoterCode.objects.filter(creator__isnull=True, spare=True).count(), 3)
        spare = self.admin.get(self.base).json()["spare_codes"]
        self.assertEqual(len(spare), 3)
        self.assertTrue(all(s["claimed_by"] is None for s in spare))
        copied = self.admin.get(f"{self.base}codes/{spare[0]['code_id']}/?lang=fr").json()
        self.assertIn(copied["code"], {r["code"] for r in rows})
        self.assertIn("Bonjour", copied["message"])

    def test_spare_count_bounds(self):
        for bad in (0, 51, "x", True):
            answer = self.admin.post(f"{self.base}codes/", {"spare": bad}, format="json")
            self.assertEqual(answer.status_code, 400, bad)
            self.assertEqual(answer.json()["code"], "kol_bad_spare_count", bad)

    def test_a_spare_code_asks_who_you_are(self):
        code = self.spares(1)[0]["code"]
        client = APIClient()
        answer = door(client, self.edition, code)
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertTrue(answer.json()["needs_claim"])
        self.assertIn(COOKIE_NAME, answer.cookies)
        again = ballot(client, self.edition)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["code"], "kol_claim_needed")
        self.assertEqual(write_in(client, self.edition, "someone").json()["code"], "kol_claim_needed")

    def test_claim_makes_a_new_creator_who_votes(self):
        code = self.spares(1)[0]["code"]
        client = APIClient()
        door(client, self.edition, code)
        answer = self.claim(client, name="New Star", handle="@New_Star", country="Ghana")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["me"]["name"], "New Star")
        star = KolCreator.objects.get(edition=self.edition, tiktok_handle="new_star")
        self.assertEqual((star.origin, star.country, star.active), (KolCreator.LIST, "Ghana", True))
        self.assertEqual(star.options.count(), 6)
        self.assertEqual(KolVoterCode.objects.get(spare=True).creator_id, star.pk)
        saved = save(client, self.edition, {str(self.awards[0].question_id): [self.rex.pk, star.pk]})
        self.assertEqual(saved.status_code, 200, saved.content)
        # the same code opens that creator's ballot from now on, on any phone, without asking again
        other_phone = door(APIClient(), self.edition, code)
        self.assertEqual(other_phone.json()["me"]["creator_id"], star.pk)
        self.assertNotIn("needs_claim", other_phone.json())
        spare = self.admin.get(self.base).json()["spare_codes"][0]
        self.assertEqual(spare["claimed_by"]["handle"], "new_star")
        self.assertIn("new_star", [c["handle"] for c in self.admin.get(self.base).json()["creators"]])

    def test_claim_joins_a_write_in_of_that_handle(self):
        mine = APIClient()
        door(mine, self.edition, new_code(self.me))
        added = write_in(mine, self.edition, "new_star", "New Star").json()["candidate"]["creator_id"]
        client = APIClient()
        door(client, self.edition, self.spares(1)[0]["code"])
        answer = self.claim(client, name="Star Official", handle="new_star", country="Ghana")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["me"]["creator_id"], added)
        row = KolCreator.objects.get(pk=added)
        self.assertEqual((row.origin, row.country), (KolCreator.LIST, "Ghana"))
        self.assertEqual(KolCreator.objects.filter(tiktok_handle="new_star").count(), 1)

    def test_claim_refusals(self):
        client = APIClient()
        door(client, self.edition, self.spares(1)[0]["code"])
        listed = self.claim(client, name="Fake Rex", handle="lord_rex")
        self.assertEqual(listed.status_code, 400)
        self.assertEqual(listed.json()["code"], "kol_claim_listed")
        self.assertEqual(self.claim(client, name="x", handle="bad handle!").json()["code"], "kol_bad_handle")
        self.assertEqual(self.claim(APIClient(), name="x", handle="ok_handle").status_code, 401)
        self.assertIsNone(KolVoterCode.objects.get(spare=True).creator_id)
        linked = APIClient()
        door(linked, self.edition, new_code(self.me))
        already = self.claim(linked, name="x", handle="ok_handle")
        self.assertEqual(already.status_code, 400)
        self.assertEqual(already.json()["code"], "kol_already_claimed")

    def test_claim_closed_round(self):
        client = APIClient()
        door(client, self.edition, self.spares(1)[0]["code"])
        KolNominationRound.objects.filter(pk=self.round.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        answer = self.claim(client, name="Late", handle="late_one")
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_round_closed")

    def test_a_revoked_spare_code_stops(self):
        code = self.spares(1)[0]["code"]
        code_id = self.admin.get(self.base).json()["spare_codes"][0]["code_id"]
        self.assertEqual(self.admin.delete(f"{self.base}codes/{code_id}/").status_code, 200)
        self.assertEqual(door(APIClient(), self.edition, code).json()["code"], "kol_code_wrong")
        self.assertEqual(self.admin.get(self.base).json()["spare_codes"], [])

    def test_bulk_codes_never_make_spares(self):
        self.admin.post(f"{self.base}codes/", {}, format="json")
        self.assertFalse(KolVoterCode.objects.filter(creator__isnull=True).exists())


# ── Garena's access (#641, owner 26 Nov 2026: "How do i give the garena admins access to the backend or their own
# page?" then "Both"): the kol_admin role (Garena's KOL staff run the round from /a/polls) and the round on
# Garena's private live page (/awards/live/<token>). ──

def kol_admin_user(username, *extra_roles):
    """A Garena KOL admin: a plain player account holding the kol_admin role, granted the way /a/settings does."""
    from afc_auth.models import Roles, UserRoles

    user = make_user(username)
    for name in ("kol_admin", *extra_roles):
        role, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
        UserRoles.objects.create(user=user, role=role)
    return user


def client_for(user):
    client = APIClient()
    client.credentials(**token_for(user))
    return client


class KolAdminRoleGrantTests(TestCase):
    """The role exists, Settings can grant it, and granting it never makes anybody AFC staff."""

    def setUp(self):
        from afc_auth.models import Roles, UserRoles

        self.head = make_user("rolesboss", role="admin")
        head_role, _ = Roles.objects.get_or_create(role_name="head_admin", defaults={"description": "Head Admin"})
        UserRoles.objects.create(user=self.head, role=head_role)
        self.garena = make_user("garenastaff")

    def grant(self, *names):
        from afc_auth.models import Roles
        from afc_auth.views import ensure_role_rows

        ensure_role_rows()
        ids = list(Roles.objects.filter(role_name__in=names).values_list("role_id", flat=True))
        self.assertEqual(len(ids), len(names))
        return client_for(self.head).post("/auth/edit-user-roles/", {
            "username": self.garena.username, "email": self.garena.email, "new_role_ids": ids}, format="json")

    def test_role_is_seeded(self):
        from afc_auth.models import Roles
        from afc_auth.views import ensure_role_rows

        ensure_role_rows()
        self.assertTrue(Roles.objects.filter(role_name="kol_admin").exists())

    def test_granting_kol_admin_keeps_the_player_role(self):
        answer = self.grant("kol_admin")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.garena.refresh_from_db()
        self.assertEqual(self.garena.role, "player")

    def test_a_staff_role_beside_it_still_makes_an_admin(self):
        self.assertEqual(self.grant("kol_admin", "news_admin").status_code, 200)
        self.garena.refresh_from_db()
        self.assertEqual(self.garena.role, "admin")


class KolAdminRoleTests(TestCase):
    """A kol_admin runs the round on an edition that has one, and reaches nothing else in polls."""

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition)
        self.round = open_round(self.edition)
        self.other = AwardsEdition.objects.create(slug="afc-awards-2026", title="AFC Awards 2026", year=2026)
        self.garena = client_for(kol_admin_user("garenakol"))
        self.base = f"/polls/admin/kol-nominations/{self.edition.slug}/"

    def test_runs_the_round(self):
        data = self.garena.get(self.base)
        self.assertEqual(data.status_code, 200, data.content)
        self.assertEqual(len(data.json()["creators"]), 4)
        later = timezone.now() + timedelta(days=4)
        dates = self.garena.post(self.base, {"opens_at": (timezone.now() - timedelta(hours=1)).isoformat(),
                                             "closes_at": later.isoformat()}, format="json")
        self.assertEqual(dates.status_code, 200, dates.content)
        self.assertEqual(len(read_csv(self.garena.post(f"{self.base}codes/", {}, format="json"))), 4)
        row = next(c for c in self.garena.get(self.base).json()["creators"] if c["handle"] == "jokkie")
        copy = self.garena.get(f"{self.base}codes/{row['code_id']}/")
        self.assertEqual(copy.status_code, 200, copy.content)
        self.assertTrue(copy.json()["code"].startswith("KOL-"))
        self.assertEqual(len(read_csv(self.garena.post(f"{self.base}codes/", {"spare": 2}, format="json"))), 2)
        switch = self.garena.patch(f"{self.base}creators/{self.creators[1].pk}/", {"active": False}, format="json")
        self.assertEqual(switch.status_code, 200, switch.content)
        self.assertEqual(self.garena.delete(f"{self.base}codes/{row['code_id']}/").status_code, 200)

    def test_only_editions_with_a_round(self):
        for slug in (self.other.slug, "no-such-edition"):
            answer = self.garena.get(f"/polls/admin/kol-nominations/{slug}/")
            self.assertEqual(answer.status_code, 404, slug)
            self.assertEqual(answer.json()["code"], "awards_edition_not_found")
        setup = self.garena.post(f"/polls/admin/kol-nominations/{self.other.slug}/", {}, format="json")
        self.assertEqual(setup.status_code, 404)
        self.assertFalse(KolNominationRound.objects.filter(edition=self.other).exists())
        listed = self.garena.get("/polls/admin/kol-nominations/")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([e["slug"] for e in listed.json()["results"]], [self.edition.slug])
        self.assertEqual(listed.json()["results"][0]["theme"], AwardsEdition.THEME_DETTY_ROYALE)

    def test_editions_list_is_admin_only(self):
        self.assertEqual(APIClient().get("/polls/admin/kol-nominations/").status_code, 401)
        player = client_for(make_user("kollistplayer"))
        answer = player.get("/polls/admin/kol-nominations/")
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["code"], "kol_admin_only")
        self.assertEqual(admin_client("kollistboss").get("/polls/admin/kol-nominations/").status_code, 200)

    def test_nothing_else_in_polls(self):
        self.assertEqual(self.garena.get("/polls/admin/polls/").json().get("results", []), [])
        self.assertEqual(self.garena.get("/polls/admin/editions/").status_code, 403)
        self.assertEqual(self.garena.get(f"/polls/admin/editions/{self.edition.slug}/metrics/").status_code, 403)
        self.assertEqual(self.garena.post(f"/polls/admin/editions/{self.other.slug}/live-link/").status_code, 403)

    def test_live_link_of_the_round_edition(self):
        made = self.garena.post(f"/polls/admin/editions/{self.edition.slug}/live-link/")
        self.assertEqual(made.status_code, 200, made.content)
        token = made.json()["live_view_token"]
        self.assertTrue(token.startswith("lv_"))
        self.assertEqual(self.garena.get(self.base).json()["edition"]["live_view_token"], token)
        off = self.garena.delete(f"/polls/admin/editions/{self.edition.slug}/live-link/")
        self.assertEqual(off.json()["live_view_token"], "")


class KolPartnerIsNotStaffTests(TestCase):
    """The four admin reads that let in "any granular role" do not let in a partner role."""

    def setUp(self):
        self.garena = client_for(kol_admin_user("garenaread"))
        self.target = make_user("someplayer")

    def test_player_details_refused(self):
        answer = self.garena.post("/player/get-player-details/", {"player_id": self.target.pk}, format="json")
        self.assertEqual(answer.status_code, 403)
        self.assertNotIn("someplayer@example.com", answer.content.decode())

    def test_admin_player_list_refused(self):
        self.assertEqual(self.garena.get("/player/admin/list-players/").status_code, 401)

    def test_search_does_not_match_by_email(self):
        found = self.garena.get("/auth/search-users/", {"q": "example.com"}).json()
        self.assertEqual(found["results"], [])
        staff = admin_client("searchboss").get("/auth/search-users/", {"q": "example.com"}).json()
        self.assertTrue(staff["results"])

    def test_dashboard_refused(self):
        self.assertEqual(self.garena.get("/auth/admin/dashboard-stats/").status_code, 403)


class KolLivePageTests(TestCase):
    """The round on Garena's private live page: counts per candidate, nothing about voters."""

    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition, names=("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF"))
        self.jokkie, self.rex, self.ama, self.kwame = self.creators
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        AwardsEdition.objects.filter(pk=self.edition.pk).update(live_view_token="lv_" + "a" * 32)
        self.url = f"/polls/live/lv_{'a' * 32}/"

    def vote(self, voter, picks):
        client = APIClient()
        self.assertEqual(door(client, self.edition, new_code(voter)).status_code, 200)
        answer = save(client, self.edition, picks)
        self.assertEqual(answer.status_code, 200, answer.content)
        return client

    def test_no_picks_yet(self):
        block = APIClient().get(self.url).json()["nominations"]
        self.assertEqual(block["state"], "open")
        self.assertEqual(block["ballots"], 0)
        self.assertEqual(block["voters"], 4)
        self.assertEqual([a["key"] for a in block["awards"]], AWARD_KEYS_EXPECTED)
        self.assertTrue(all(a["total_picks"] == 0 and a["candidates"] == [] for a in block["awards"]))

    def test_counts_per_award(self):
        first = str(self.awards[0].question_id)
        self.vote(self.jokkie, {first: [self.rex.pk, self.ama.pk]})
        client = self.vote(self.kwame, {first: [self.rex.pk]})
        write_in(client, self.edition, "new_star", "New Star")
        star = KolCreator.objects.get(tiktok_handle="new_star")
        save(client, self.edition, {first: [self.rex.pk, star.pk]})
        data = APIClient().get(self.url).json()
        block = data["nominations"]
        self.assertEqual(block["ballots"], 2)
        self.assertEqual(block["voters"], 4)                       # write-ins never vote
        award = block["awards"][0]
        self.assertEqual(award["total_picks"], 4)
        self.assertEqual([(c["name"], c["picks"]) for c in award["candidates"]],
                         [("Lord Rex", 2), ("Ama Plays", 1), ("New Star", 1)])
        star_row = award["candidates"][2]
        self.assertEqual((star_row["handle"], star_row["write_in"]), ("new_star", True))
        self.assertEqual(award["candidates"][0]["country"], "Nigeria")
        text = json.dumps(data)
        for leak in ("creator_id", "option", "respondent", "code", "jokkie\"", "kwame"):
            self.assertNotIn(leak, text, leak)

    def test_removed_ballots_and_switched_off_creators_not_counted(self):
        first = str(self.awards[0].question_id)
        self.vote(self.jokkie, {first: [self.rex.pk, self.ama.pk]})
        self.vote(self.kwame, {first: [self.rex.pk]})
        PollResponse.objects.filter(respondent_key=respondent_key_for(self.round, self.kwame)).update(
            review_state=PollResponse.REVIEW_REMOVED)
        KolCreator.objects.filter(pk=self.ama.pk).update(active=False)
        block = APIClient().get(self.url).json()["nominations"]
        self.assertEqual(block["ballots"], 1)
        self.assertEqual([(c["name"], c["picks"]) for c in block["awards"][0]["candidates"]], [("Lord Rex", 1)])

    def test_edition_without_a_round(self):
        other = AwardsEdition.objects.create(slug="afc-awards-2026", title="AFC Awards 2026", year=2026,
                                             live_view_token="lv_" + "b" * 32)
        self.assertIsNone(APIClient().get(f"/polls/live/{other.live_view_token}/").json()["nominations"])
