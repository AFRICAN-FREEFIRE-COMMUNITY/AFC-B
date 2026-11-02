"""
afc_polls.tests_vote_tracking - votes stay private while an award runs (inbox #454), and the vote
tracking, safety flags, review and Garena's live link (inbox #453, #457), 2 Nov 2026.

WHAT IS WORTH TESTING HERE

  1. NOTHING ABOUT AN OPEN AWARD'S VOTES IS PUBLIC, even when an admin picked "always": no per
     nominee count, no total ballot count, on the poll, the edition and the listing. A plain poll
     on "always" keeps its live results, and a closed award shows its numbers again.
  2. THE RAW IP IS NEVER STORED. A vote keeps the country and region, keyed hashes, the device type,
     the account age and the referring nominee, and no column holds the address.
  3. PICK TIMES SURVIVE A RE-SAVE and a changed pick is a switch; an unchanged one is not.
  4. FLAGS AND REVIEW: the thresholds fire, a removal needs a reason and takes the ballot out of
     every count, the log keeps every step, and a ballot from another poll is 404.
  5. GARENA'S LINK answers counts by token only, folds small cells, and dies on rotate or turn-off.

The location lookup is mocked at its boundary (afc_polls.vote_tracking.region_for_ip): the tests
must not need the 127 MB DB-IP file. afc_polls.geo itself is tested against a fake reader.

Run:
    AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls.tests_vote_tracking -v 2
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from . import geo, vote_tracking
from .models import (
    AwardsEdition,
    Poll,
    PollAnswer,
    PollOption,
    PollPickSwitch,
    PollQuestion,
    PollResponse,
    PollReviewLog,
    PollSheetOpen,
)
from .tests import make_user, open_poll, token_for

LAGOS = {"country": "NG", "region": "Lagos"}
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
VOTER_IP = "41.58.0.1"


def award_edition(slug="kol-2026", **poll_extra):
    """An OPEN award edition with one poll, one question (award) and two nominees."""
    edition = AwardsEdition.objects.create(slug=slug, title="KOL Awards", year=2026,
                                           theme=AwardsEdition.THEME_DETTY_ROYALE)
    poll, question, first, second = open_poll(
        slug=f"{slug}-ballot", kind=Poll.AWARD, edition=edition, awards_edition="KOL Awards",
        results_visibility=Poll.ALWAYS, **poll_extra,
    )
    return edition, poll, question, first, second


def vote(client, poll, question, option, user, ip=VOTER_IP, ua=IPHONE, **body):
    """Cast one vote through the real endpoint, from `ip`, the way the KOL pages do (merge)."""
    payload = {"answers": [{"question_id": question.question_id, "option_ids": [option.option_id]}],
               "merge": True, **body}
    return client.post(
        f"/polls/{poll.slug}/responses/", payload, content_type="application/json",
        HTTP_X_FORWARDED_FOR=ip, HTTP_USER_AGENT=ua, **token_for(user),
    )


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class VotesPrivateWhileOpenTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()
        self.admin = make_user("priv_admin", role="admin")

    def test_an_open_award_on_always_shows_no_count_anywhere(self, _geo):
        vote(self.client, self.poll, self.question, self.first, make_user("priv_voter"))

        detail = self.client.get(f"/polls/{self.poll.slug}/").json()
        self.assertFalse(detail["results_visible"])
        self.assertIsNone(detail["response_count"])
        self.assertTrue(all("votes" not in option for option in detail["questions"][0]["options"]))
        self.assertNotIn("published_winner_votes", detail["questions"][0])

        edition = self.client.get(f"/polls/editions/{self.edition.slug}/").json()
        card = edition["polls"][0]
        self.assertFalse(card["results_visible"])
        self.assertIsNone(card["response_count"])
        self.assertTrue(all("votes" not in option for option in card["questions"][0]["options"]))

        listing = self.client.get("/polls/").json()["results"]
        self.assertIsNone(next(row for row in listing if row["slug"] == self.poll.slug)["response_count"])

    def test_a_manager_still_sees_the_running_count(self, _geo):
        vote(self.client, self.poll, self.question, self.first, make_user("priv_voter2"))
        detail = self.client.get(f"/polls/{self.poll.slug}/", **token_for(self.admin)).json()
        self.assertTrue(detail["results_visible"])
        self.assertEqual(detail["response_count"], 1)

    def test_a_closed_award_shows_its_numbers_again(self, _geo):
        vote(self.client, self.poll, self.question, self.first, make_user("priv_voter3"))
        self.poll.closes_at = timezone.now() - timedelta(minutes=1)
        self.poll.save(update_fields=["closes_at"])
        detail = self.client.get(f"/polls/{self.poll.slug}/").json()
        self.assertTrue(detail["results_visible"])
        self.assertEqual(detail["response_count"], 1)
        votes = {option["option_id"]: option["votes"] for option in detail["questions"][0]["options"]}
        self.assertEqual(votes[self.first.option_id], 1)

    def test_a_plain_poll_on_always_keeps_live_results(self, _geo):
        poll, question, first, _ = open_poll(slug="plain-live", results_visibility=Poll.ALWAYS)
        vote(self.client, poll, question, first, make_user("priv_plain"))
        detail = self.client.get(f"/polls/{poll.slug}/").json()
        self.assertTrue(detail["results_visible"])
        self.assertEqual(detail["response_count"], 1)


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class CaptureTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()

    def test_a_vote_keeps_location_device_age_and_referrer_but_never_the_ip(self, _geo):
        voter = make_user("cap_voter", country="Nigeria")
        voter.date_joined = timezone.now() - timedelta(days=40)
        voter.save(update_fields=["date_joined"])
        result = vote(self.client, self.poll, self.question, self.first, voter,
                      device_key="dev_abcdefghijklmnop1234", ref_option_id=self.first.option_id)
        self.assertEqual(result.status_code, 201)

        response = PollResponse.objects.get(poll=self.poll)
        self.assertEqual((response.ip_country, response.ip_region), ("NG", "Lagos"))
        self.assertEqual(response.device_type, "phone")
        self.assertEqual(response.account_age_days, 40)
        self.assertEqual(response.referrer_option_id, self.first.option_id)
        self.assertEqual(len(response.connection_hash), 64)
        self.assertEqual(len(response.device_hash), 64)
        self.assertEqual(response.voter_key, vote_tracking.voter_key(self.poll, voter))
        stored = [str(getattr(response, field.attname)) for field in PollResponse._meta.concrete_fields]
        self.assertFalse(any(VOTER_IP in value for value in stored))
        self.assertEqual(PollSheetOpen.objects.filter(poll=self.poll).count(), 1)

    def test_a_referrer_from_another_poll_and_a_bad_device_key_are_ignored(self, _geo):
        _, _, _, foreign_option, _ = award_edition(slug="other-ed")
        vote(self.client, self.poll, self.question, self.first, make_user("cap_voter2"),
             device_key="no", ref_option_id=foreign_option.option_id)
        response = PollResponse.objects.get(poll=self.poll)
        self.assertIsNone(response.referrer_option_id)
        self.assertEqual(response.device_hash, "")

    def test_the_referrer_is_first_touch(self, _geo):
        voter = make_user("cap_voter3")
        vote(self.client, self.poll, self.question, self.first, voter, ref_option_id=self.first.option_id)
        vote(self.client, self.poll, self.question, self.second, voter, ref_option_id=self.second.option_id)
        self.assertEqual(PollResponse.objects.get(poll=self.poll).referrer_option_id, self.first.option_id)

    def test_an_anonymous_poll_keeps_buckets_and_hour_times(self, _geo):
        _, poll, question, first, _ = award_edition(slug="anon-ed", anonymous=True)
        voter = make_user("cap_anon")
        voter.date_joined = timezone.now() - timedelta(days=40)
        voter.save(update_fields=["date_joined"])
        vote(self.client, poll, question, first, voter)
        response = PollResponse.objects.get(poll=poll)
        self.assertIsNone(response.respondent_id)
        self.assertEqual(response.account_age_days, 30)
        answer = PollAnswer.objects.get(response=response)
        self.assertEqual((answer.created_at.minute, answer.created_at.second), (0, 0))


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class PickTimesAndSwitchesTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()
        self.voter = make_user("sw_voter")

    def test_a_changed_pick_is_a_switch_and_an_unchanged_one_keeps_its_time(self, _geo):
        vote(self.client, self.poll, self.question, self.first, self.voter)
        first_time = PollAnswer.objects.get(response__poll=self.poll).created_at
        PollAnswer.objects.filter(response__poll=self.poll).update(created_at=first_time - timedelta(hours=5))

        vote(self.client, self.poll, self.question, self.first, self.voter)
        self.assertEqual(PollAnswer.objects.get(response__poll=self.poll).created_at, first_time - timedelta(hours=5))
        self.assertEqual(PollPickSwitch.objects.count(), 0)

        vote(self.client, self.poll, self.question, self.second, self.voter)
        switch = PollPickSwitch.objects.get()
        self.assertEqual((switch.from_option_id, switch.to_option_id), (self.first.option_id, self.second.option_id))
        self.assertGreater(PollAnswer.objects.get(response__poll=self.poll).created_at, first_time - timedelta(hours=1))


class SheetOpenedTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()
        self.url = f"/polls/{self.poll.slug}/sheet-opened/"

    def test_signed_out_is_refused_with_a_code(self):
        response = self.client.post(self.url, {}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_one_row_per_voter_and_failed_uid_links_add_up(self):
        voter = make_user("so_voter")
        for body in ({}, {}, {"failed_check": "uid"}):
            response = self.client.post(self.url, body, content_type="application/json", **token_for(voter))
            self.assertEqual(response.status_code, 200)
        row = PollSheetOpen.objects.get(poll=self.poll)
        self.assertEqual(row.failed_checks, 1)

    def test_an_unknown_check_is_refused(self):
        response = self.client.post(self.url, {"failed_check": "selfie"}, content_type="application/json",
                                    **token_for(make_user("so_voter2")))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "sheet_open_check_unknown")

    def test_an_eligibility_refusal_counts_as_a_failed_check(self):
        voter = make_user("so_refused")
        refused = {"eligible": False, "requirements": [], "match_rule": "all", "snapshot": {}}
        with mock.patch("afc_polls.views.check_eligibility", return_value=refused):
            response = vote(self.client, self.poll, self.question, self.first, voter)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PollSheetOpen.objects.get(poll=self.poll).failed_checks, 1)


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class FlagsAndReviewTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()
        self.admin = make_user("fl_admin", role="admin")
        self.player = make_user("fl_player", role="player")
        self.flags_url = f"/polls/admin/polls/{self.poll.slug}/flags/"

    def _old(self, name, **extra):
        user = make_user(name, **extra)
        user.date_joined = timezone.now() - timedelta(days=400)
        user.save(update_fields=["date_joined"])
        return user

    def test_each_flag_fires_at_its_threshold(self, _geo):
        # Three ballots over one connection (the threshold), two of them from one browser.
        cafe = [self._old(f"fl_cafe{i}", country="Nigeria") for i in range(3)]
        for index, user in enumerate(cafe):
            vote(self.client, self.poll, self.question, self.first, user, ip="102.89.0.9",
                 device_key="dev_sharedsharedshared" if index < 2 else f"dev_unique_device_{index}")
        newcomer = make_user("fl_new", country="Nigeria")
        vote(self.client, self.poll, self.question, self.first, newcomer, ip="102.89.0.20")
        abroad = self._old("fl_abroad", country="Ghana")
        vote(self.client, self.poll, self.question, self.second, abroad, ip="102.89.0.30")
        clean = self._old("fl_clean", country="Nigeria")
        vote(self.client, self.poll, self.question, self.second, clean, ip="102.89.0.40")

        flagged = vote_tracking.compute_flags(self.poll)
        by_user = {PollResponse.objects.get(pk=rid).respondent.username: entry["flags"] for rid, entry in flagged.items()}
        self.assertEqual(set(by_user["fl_cafe0"]), {"shared_connection", "shared_device"})
        self.assertEqual(set(by_user["fl_cafe2"]), {"shared_connection"})
        self.assertEqual(by_user["fl_new"], ["new_account"])
        self.assertEqual(by_user["fl_abroad"], ["country_mismatch"])
        self.assertNotIn("fl_clean", by_user)

    def test_flags_endpoint_is_admin_only_and_filters_by_state(self, _geo):
        vote(self.client, self.poll, self.question, self.first, make_user("fl_new2"))
        self.assertEqual(self.client.get(self.flags_url).status_code, 401)
        self.assertEqual(self.client.get(self.flags_url, **token_for(self.player)).status_code, 403)
        body = self.client.get(self.flags_url, **token_for(self.admin)).json()
        self.assertEqual(body["total_count"], 1)
        row = body["results"][0]
        self.assertEqual(row["voter"], "fl_new2")
        self.assertEqual(row["flags"], ["new_account"])
        self.assertEqual(row["picks"], [{"award": "Pick one", "nominee": "First"}])
        self.assertEqual(self.client.get(self.flags_url + "?state=removed", **token_for(self.admin)).json()["total_count"], 0)
        self.assertEqual(self.client.get(self.flags_url + "?state=bogus", **token_for(self.admin)).status_code, 400)

    def test_remove_needs_a_reason_leaves_every_count_and_is_logged(self, _geo):
        bad = make_user("fl_bad")
        vote(self.client, self.poll, self.question, self.first, bad)
        vote(self.client, self.poll, self.question, self.first, self._old("fl_good"))
        response = PollResponse.objects.get(respondent=bad)
        url = f"/polls/admin/polls/{self.poll.slug}/responses/{response.response_id}/review/"

        refused = self.client.post(url, {"action": "remove", "reason": ""}, content_type="application/json",
                                   **token_for(self.admin))
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["code"], "review_reason_required")
        self.assertEqual(self.client.post(url, {"action": "remove", "reason": "Same phone as fl_good"},
                                          content_type="application/json", **token_for(self.player)).status_code, 403)

        done = self.client.post(url, {"action": "remove", "reason": "Account made an hour before voting"},
                                content_type="application/json", **token_for(self.admin))
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["review_state"], "removed")

        metrics = self.client.get(f"/polls/admin/editions/{self.edition.slug}/metrics/", **token_for(self.admin)).json()
        poll_block = metrics["polls"][0]
        votes = {row["option_id"]: row["votes"] for row in poll_block["questions"][0]["options"]}
        self.assertEqual(votes[self.first.option_id], 1)
        self.assertEqual(poll_block["responses"], 1)
        self.assertEqual(poll_block["tracking"]["removed"], 1)

        restore = self.client.post(url, {"action": "restore"}, content_type="application/json", **token_for(self.admin))
        self.assertEqual(restore.json()["review_state"], "")
        self.assertEqual(list(PollReviewLog.objects.filter(response=response).values_list("action", flat=True)),
                         ["restore", "remove"])

    def test_a_ballot_from_another_poll_is_not_found(self, _geo):
        _, other_poll, other_question, other_first, _ = award_edition(slug="other-ed2")
        vote(self.client, other_poll, other_question, other_first, make_user("fl_other"))
        foreign = PollResponse.objects.get(poll=other_poll)
        url = f"/polls/admin/polls/{self.poll.slug}/responses/{foreign.response_id}/review/"
        response = self.client.post(url, {"action": "clear"}, content_type="application/json", **token_for(self.admin))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "ballot_not_found")


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class MetricsTrackingTests(TestCase):
    def test_the_metrics_answer_carries_where_when_and_how(self, _geo):
        edition, poll, question, first, second = award_edition()
        admin = make_user("mt_admin", role="admin")
        voter = make_user("mt_voter", country="Nigeria")
        vote(self.client, poll, question, first, voter, ref_option_id=first.option_id)
        self.client.post(f"/polls/{poll.slug}/sheet-opened/", {}, content_type="application/json",
                         **token_for(make_user("mt_browser")))

        body = self.client.get(f"/polls/admin/editions/{edition.slug}/metrics/", **token_for(admin)).json()
        self.assertEqual(body["live_view_token"], "")
        poll_block = body["polls"][0]
        option = next(row for row in poll_block["questions"][0]["options"] if row["option_id"] == first.option_id)
        self.assertEqual(option["countries"], [{"code": "NG", "votes": 1}])
        self.assertEqual(option["ip_countries"], [{"code": "NG", "votes": 1}])
        self.assertEqual(option["regions"], [{"country": "NG", "region": "Lagos", "votes": 1}])
        self.assertEqual(len(option["by_day"]), 1)
        self.assertEqual(len(option["by_hour"]), 1)
        self.assertEqual((option["referred_ballots"], option["referred_votes"]), (1, 1))
        tracking = poll_block["tracking"]
        self.assertEqual((tracking["started"], tracking["finished"]), (2, 1))
        self.assertEqual(tracking["awards_per_ballot"], [{"awards": 1, "ballots": 1}])
        self.assertEqual(tracking["devices"], [{"device": "phone", "ballots": 1}])
        empty = next(row for row in poll_block["questions"][0]["options"] if row["option_id"] == second.option_id)
        self.assertEqual(empty["regions"], [])


@mock.patch("afc_polls.vote_tracking.region_for_ip", return_value=dict(LAGOS))
class GarenaLiveLinkTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.question, self.first, self.second = award_edition()
        self.admin = make_user("lv_admin", role="admin")
        self.link_url = f"/polls/admin/editions/{self.edition.slug}/live-link/"

    def test_link_lifecycle_and_what_it_shows(self, _geo):
        self.assertEqual(self.client.post(self.link_url, **token_for(make_user("lv_player", role="player"))).status_code, 403)
        token = self.client.post(self.link_url, **token_for(self.admin)).json()["live_view_token"]
        self.assertRegex(token, r"^lv_[0-9a-f]{32}$")
        for index in range(6):
            vote(self.client, self.poll, self.question, self.first, make_user(f"lv_v{index}"))
        with mock.patch("afc_polls.vote_tracking.region_for_ip", return_value={"country": "GH", "region": "Accra"}):
            vote(self.client, self.poll, self.question, self.second, make_user("lv_gh"))

        live = self.client.get(f"/polls/live/{token}/")
        self.assertEqual(live.status_code, 200)
        options = {row["label"]: row for row in live.json()["polls"][0]["questions"][0]["options"]}
        self.assertEqual(options["First"]["votes"], 6)
        self.assertEqual(options["First"]["countries"], [{"code": "NG", "votes": 6}])
        self.assertEqual(options["Second"]["countries"], [{"code": "", "other": True, "votes": 1}])
        self.assertNotIn("voter", str(live.json()))

        rotated = self.client.post(self.link_url, **token_for(self.admin)).json()["live_view_token"]
        self.assertNotEqual(rotated, token)
        gone = self.client.get(f"/polls/live/{token}/")
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(gone.json()["code"], "live_view_not_found")
        self.client.delete(self.link_url, **token_for(self.admin))
        self.assertEqual(self.client.get(f"/polls/live/{rotated}/").status_code, 404)
        self.assertEqual(self.client.get("/polls/live/not-a-token/").status_code, 404)


class GeoAndDeviceTests(TestCase):
    def tearDown(self):
        geo.reset_reader()

    def test_private_and_bad_addresses_are_empty_without_touching_the_file(self):
        with mock.patch("afc_polls.geo._open_reader") as opener:
            self.assertEqual(geo.region_for_ip("10.0.0.1"), {"country": "", "region": ""})
            self.assertEqual(geo.region_for_ip("not an ip"), {"country": "", "region": ""})
            opener.assert_not_called()

    def test_a_record_becomes_country_code_and_region_only(self):
        record = {"country": {"iso_code": "ng"}, "subdivisions": [{"names": {"en": "Ogun State"}}],
                  "city": {"names": {"en": "Ikenne"}}, "location": {"latitude": 6.8}}
        fake = mock.Mock()
        fake.get.return_value = record
        with mock.patch("afc_polls.geo._open_reader", return_value=fake):
            self.assertEqual(geo.region_for_ip("105.112.0.1"), {"country": "NG", "region": "Ogun State"})

    def test_a_missing_file_is_empty_not_an_error(self):
        with self.settings(GEOIP_CITY_DB="C:/nowhere/none.mmdb"):
            geo.reset_reader()
            self.assertEqual(geo.region_for_ip("105.112.0.1"), {"country": "", "region": ""})

    def test_device_types(self):
        cases = {
            IPHONE: "phone",
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)": "tablet",
            "Mozilla/5.0 (Linux; Android 14; SM-A146P) Mobile Safari/537.36": "phone",
            "Mozilla/5.0 (Linux; Android 13; SM-X200) Safari/537.36": "tablet",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0": "computer",
            "": "",
        }
        for ua, expected in cases.items():
            self.assertEqual(vote_tracking.device_type(ua), expected, ua)


class ReservedEditionSlugTests(TestCase):
    def test_an_edition_named_live_never_takes_the_live_page_address(self):
        admin = make_user("slug_admin", role="admin")
        response = self.client.post("/polls/admin/editions/", {"title": "Live"}, content_type="application/json",
                                    **token_for(admin))
        self.assertIn(response.status_code, (200, 201))
        self.assertEqual(response.json()["slug"], "live-2")
