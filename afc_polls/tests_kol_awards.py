"""
afc_polls.tests_kol_awards - the pieces added for the themed KOL Awards pages (inbox #419).

WHAT IS WORTH TESTING HERE
  1. THE THEME IS DATA. An edition carries `theme`, defaults to "default", is writable only through
     the admin edition endpoint, and an unknown value falls back rather than reaching the page.
  2. A NOMINEE HAS AN ADDRESS. Options get a slug unique inside their question, a slug survives a
     rename, and the legacy fallback agrees with slugify.
  3. ONE VOTE PER AWARD, AND A VOTE ON ONE AWARD KEEPS THE OTHERS. The themed site is one page per
     award, so submit_response takes {"merge": true}: it replaces only the questions it names.
     A second pick in the same award REPLACES the first (never two), and edition_detail reports
     `your_answers` so the page can say what a re-vote replaces.
  4. THE FILM UPLOAD OBEYS R70/R87: the bytes decide the type, the name on disk is opaque, the size
     is capped, an HTML file wearing .mp4 is refused, and a manager of another poll cannot reach the
     option by changing its id.

Run:
    AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls.tests_kol_awards -v 2
"""
import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import AwardsEdition, Poll, PollAnswer, PollOption, PollQuestion, option_slug
from .tests import make_user, token_for

# The smallest thing sniff_upload calls an MP4: a box whose type field reads "ftyp".
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
FILM_ROOT = tempfile.mkdtemp(prefix="kol-films-")


def kol_edition(slug="kol-test"):
    """A themed edition with ONE poll of two awards, voting open. Returns (edition, poll, q1, q2)."""
    edition = AwardsEdition.objects.create(
        slug=slug, title="KOL Awards", year=2026, theme=AwardsEdition.THEME_DETTY_ROYALE,
        voting_opens_at=timezone.now() - timedelta(hours=1),
        voting_closes_at=timezone.now() + timedelta(days=2),
    )
    poll = Poll.objects.create(
        slug=f"{slug}-ballot", title="KOL Awards ballot", kind=Poll.AWARD, edition=edition,
        awards_edition=slug, visibility=Poll.PUBLIC,
        opens_at=timezone.now() - timedelta(hours=1), closes_at=timezone.now() + timedelta(days=2),
    )
    q1 = PollQuestion.objects.create(poll=poll, order=0, prompt="Best Streamer")
    q2 = PollQuestion.objects.create(poll=poll, order=1, prompt="Funniest Creator")
    for question in (q1, q2):
        PollOption.objects.create(question=question, order=0, label="ARMYKID")
        PollOption.objects.create(question=question, order=1, label="JOKKIE")
    return edition, poll, q1, q2


def make_admin(username="kolboss"):
    # role="admin" is one of the roles afc_polls.permissions.is_polls_admin accepts.
    return make_user(username, role="admin")


class ThemeTests(TestCase):
    def test_default_theme_and_serialized_on_the_edition(self):
        plain = AwardsEdition.objects.create(slug="plain", title="Plain")
        self.assertEqual(plain.theme, "default")
        edition, *_ = kol_edition()
        response = APIClient().get(f"/polls/editions/{edition.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["edition"]["theme"], "detty-royale")

    def test_admin_sets_theme_and_an_unknown_value_falls_back(self):
        admin = make_admin()
        client = APIClient()
        created = client.post("/polls/admin/editions/", {"title": "Themed", "theme": "detty-royale"},
                              format="json", **token_for(admin))
        self.assertEqual(created.status_code, 201)
        slug = created.json()["slug"]
        self.assertEqual(AwardsEdition.objects.get(slug=slug).theme, "detty-royale")
        client.patch(f"/polls/admin/editions/{slug}/", {"theme": "<script>"}, format="json", **token_for(admin))
        self.assertEqual(AwardsEdition.objects.get(slug=slug).theme, "default")

    def test_a_player_cannot_set_the_theme(self):
        edition = AwardsEdition.objects.create(slug="plain2", title="Plain")
        response = APIClient().patch(f"/polls/admin/editions/{edition.slug}/", {"theme": "detty-royale"},
                                     format="json", **token_for(make_user("justafan")))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AwardsEdition.objects.get(pk=edition.pk).theme, "default")


class NomineeSlugTests(TestCase):
    def test_slug_is_unique_inside_a_question_and_stays_after_a_rename(self):
        _, _, q1, q2 = kol_edition()
        first = PollOption.objects.create(question=q1, order=5, label="Vic Vix")
        twin = PollOption.objects.create(question=q1, order=6, label="VIC VIX")
        self.assertEqual(first.slug, "vic-vix")
        self.assertEqual(twin.slug, "vic-vix-2")
        # The same name in ANOTHER award is the same slug: nominees are addressed under their award.
        self.assertEqual(PollOption.objects.create(question=q2, order=5, label="Vic Vix").slug, "vic-vix")
        first.label = "Vic Vix FF"
        first.save()
        self.assertEqual(PollOption.objects.get(pk=first.pk).slug, "vic-vix")

    def test_fallback_for_a_row_the_backfill_has_not_reached(self):
        _, _, q1, _ = kol_edition()
        option = PollOption.objects.create(question=q1, order=9, label="Riley Is Vanitas")
        PollOption.objects.filter(pk=option.pk).update(slug="")
        option.refresh_from_db()
        self.assertEqual(option_slug(option), "riley-is-vanitas")

    def test_edition_detail_carries_slug_and_empty_film_url(self):
        edition, *_ = kol_edition()
        options = APIClient().get(f"/polls/editions/{edition.slug}/").json()["polls"][0]["questions"][0]["options"]
        self.assertEqual([o["slug"] for o in options], ["armykid", "jokkie"])
        self.assertEqual([o["film_url"] for o in options], ["", ""])


class VoteTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.q1, self.q2 = kol_edition()
        self.voter = make_user("fan1")
        self.client = APIClient()

    def post(self, answers, merge=True, user=None):
        return self.client.post(f"/polls/{self.poll.slug}/responses/", {"answers": answers, "merge": merge},
                                format="json", **token_for(user or self.voter))

    def opt(self, question, label):
        return PollOption.objects.get(question=question, label=label).option_id

    def test_signed_out_vote_is_refused(self):
        response = self.client.post(f"/polls/{self.poll.slug}/responses/", {"answers": []}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_a_vote_on_one_award_keeps_the_pick_on_the_other(self):
        first = self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "ARMYKID")]}])
        self.assertEqual(first.status_code, 201)
        second = self.post([{"question_id": self.q2.question_id, "option_ids": [self.opt(self.q2, "JOKKIE")]}])
        self.assertEqual(second.status_code, 200)
        picks = {a.question_id: a.option.label for a in PollAnswer.objects.all()}
        self.assertEqual(picks, {self.q1.question_id: "ARMYKID", self.q2.question_id: "JOKKIE"})

    def test_a_second_pick_in_the_same_award_replaces_the_first(self):
        self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "ARMYKID")]}])
        self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "JOKKIE")]}])
        rows = PollAnswer.objects.filter(question=self.q1)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().option.label, "JOKKIE")

    def test_two_options_in_one_award_are_refused(self):
        response = self.post([{"question_id": self.q1.question_id,
                               "option_ids": [self.opt(self.q1, "ARMYKID"), self.opt(self.q1, "JOKKIE")]}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PollAnswer.objects.count(), 0)

    def test_an_option_from_another_award_is_refused(self):
        response = self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q2, "JOKKIE")]}])
        self.assertEqual(response.status_code, 400)

    def test_without_merge_the_old_behaviour_replaces_the_whole_sheet(self):
        self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "ARMYKID")]}])
        self.post([{"question_id": self.q2.question_id, "option_ids": [self.opt(self.q2, "JOKKIE")]}], merge=False)
        self.assertEqual(list(PollAnswer.objects.values_list("question_id", flat=True)), [self.q2.question_id])

    def test_voting_closed_refuses(self):
        Poll.objects.filter(pk=self.poll.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        response = self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "ARMYKID")]}])
        self.assertEqual(response.status_code, 403)

    def test_your_answers_reported_to_the_caller_only(self):
        self.post([{"question_id": self.q1.question_id, "option_ids": [self.opt(self.q1, "ARMYKID")]}])
        mine = self.client.get(f"/polls/editions/{self.edition.slug}/", **token_for(self.voter)).json()["polls"][0]
        self.assertEqual(mine["your_answers"], {str(self.q1.question_id): [self.opt(self.q1, "ARMYKID")]})
        anonymous = APIClient().get(f"/polls/editions/{self.edition.slug}/").json()["polls"][0]
        self.assertEqual(anonymous["your_answers"], {})
        other = APIClient().get(f"/polls/editions/{self.edition.slug}/",
                                **token_for(make_user("fan2"))).json()["polls"][0]
        self.assertEqual(other["your_answers"], {})


@override_settings(MEDIA_ROOT=FILM_ROOT)
class FilmUploadTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.q1, _ = kol_edition()
        self.option = self.q1.options.first()
        self.admin = make_admin()
        self.client = APIClient()
        self.url = f"/polls/admin/polls/{self.poll.slug}/options/{self.option.option_id}/film/"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(FILM_ROOT, ignore_errors=True)

    def upload(self, data, name="film.mp4", user=None):
        return self.client.post(self.url, {"film": SimpleUploadedFile(name, data)}, format="multipart",
                                **token_for(user or self.admin))

    def test_mp4_is_stored_under_an_opaque_name_and_served_back(self):
        response = self.upload(MP4_BYTES, name="Armykid Final.MP4")
        self.assertEqual(response.status_code, 200)
        self.option.refresh_from_db()
        self.assertRegex(self.option.film.name, r"^awards/films/[0-9a-f]{32}\.mp4$")
        self.assertTrue(response.json()["film_url"].endswith(self.option.film.name))
        shown = APIClient().get(f"/polls/editions/{self.edition.slug}/").json()["polls"][0]["questions"][0]["options"][0]
        self.assertTrue(shown["film_url"].endswith(self.option.film.name))

    def test_webm_is_accepted_and_replaces_the_old_file(self):
        self.upload(MP4_BYTES)
        first = PollOption.objects.get(pk=self.option.pk).film.name
        self.upload(WEBM_BYTES, name="x.webm")
        second = PollOption.objects.get(pk=self.option.pk).film
        self.assertTrue(second.name.endswith(".webm"))
        self.assertFalse(second.storage.exists(first))

    def test_html_wearing_an_mp4_name_is_refused(self):
        response = self.upload(b"<html><script>alert(1)</script></html>", name="film.mp4")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "film_type_refused")
        self.assertFalse(PollOption.objects.get(pk=self.option.pk).film)

    def test_an_image_is_refused(self):
        response = self.upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, name="film.mp4")
        self.assertEqual(response.status_code, 400)

    def test_too_large_is_refused(self):
        response = self.upload(MP4_BYTES + b"\x00" * (25 * 1024 * 1024))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "film_too_large")

    def test_a_player_cannot_upload(self):
        response = self.upload(MP4_BYTES, user=make_user("plainfan"))
        self.assertEqual(response.status_code, 403)

    def test_signed_out_cannot_upload(self):
        response = APIClient().post(self.url, {"film": SimpleUploadedFile("f.mp4", MP4_BYTES)}, format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_an_option_of_another_poll_is_not_reachable_through_this_poll(self):
        _, _, other_q, _ = kol_edition("kol-other")
        stranger_option = other_q.options.first()
        url = f"/polls/admin/polls/{self.poll.slug}/options/{stranger_option.option_id}/film/"
        response = self.client.post(url, {"film": SimpleUploadedFile("f.mp4", MP4_BYTES)}, format="multipart",
                                    **token_for(self.admin))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PollOption.objects.get(pk=stranger_option.pk).film)

    def test_delete_removes_the_film(self):
        self.upload(MP4_BYTES)
        name = PollOption.objects.get(pk=self.option.pk).film.name
        response = self.client.delete(self.url, **token_for(self.admin))
        self.assertEqual(response.status_code, 200)
        option = PollOption.objects.get(pk=self.option.pk)
        self.assertFalse(option.film)
        self.assertFalse(option.film.storage.exists(name))


@override_settings(MEDIA_ROOT=FILM_ROOT)
class WinnerFilmTests(TestCase):
    """The winner film is a second, separate film per nominee (inbox #430)."""

    def setUp(self):
        self.edition, self.poll, self.q1, _ = kol_edition("kol-win")
        self.option = self.q1.options.first()
        self.admin = make_admin("winboss")
        self.url = f"/polls/admin/polls/{self.poll.slug}/options/{self.option.option_id}/winner-film/"

    def test_winner_film_is_stored_apart_from_the_nominee_film(self):
        response = APIClient().post(self.url, {"film": SimpleUploadedFile("w.mp4", MP4_BYTES)}, format="multipart",
                                    **token_for(self.admin))
        self.assertEqual(response.status_code, 200)
        self.assertIn("winner_film_url", response.json())
        self.option.refresh_from_db()
        self.assertTrue(self.option.winner_film)
        self.assertFalse(self.option.film)
        shown = APIClient().get(f"/polls/editions/{self.edition.slug}/").json()["polls"][0]["questions"][0]["options"][0]
        self.assertTrue(shown["winner_film_url"].endswith(self.option.winner_film.name))
        self.assertEqual(shown["film_url"], "")

    def test_html_refused_and_player_refused(self):
        client = APIClient()
        bad = client.post(self.url, {"film": SimpleUploadedFile("w.mp4", b"<html></html>")}, format="multipart",
                          **token_for(self.admin))
        self.assertEqual(bad.status_code, 400)
        denied = client.post(self.url, {"film": SimpleUploadedFile("w.mp4", MP4_BYTES)}, format="multipart",
                             **token_for(make_user("winfan")))
        self.assertEqual(denied.status_code, 403)

    def test_delete_removes_only_the_winner_film(self):
        client = APIClient()
        client.post(self.url, {"film": SimpleUploadedFile("w.mp4", MP4_BYTES)}, format="multipart", **token_for(self.admin))
        client.post(self.url.replace("winner-film", "film"), {"film": SimpleUploadedFile("n.mp4", MP4_BYTES)},
                    format="multipart", **token_for(self.admin))
        client.delete(self.url, **token_for(self.admin))
        self.option.refresh_from_db()
        self.assertFalse(self.option.winner_film)
        self.assertTrue(self.option.film)
