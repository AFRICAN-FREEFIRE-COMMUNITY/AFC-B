"""
Tests for the ADMIN players list - afc_player/views.py::admin_list_players (owner 2026-08-11).

WHY THIS FILE EXISTS
  Support is regularly handed a Free Fire UID and nothing else ("this is me, fix my account"), and
  until now there was nowhere to type it: the admin Players tab filtered on the in-game name only,
  and the endpoint behind it did not return a UID at all.

  The obvious fix - add `uid` and `email` to the list the tab already loads - would have been a
  data leak, because that list (get_all_users) is UNAUTHENTICATED and returns every account on the
  site. `User.uid` is a LOGIN IDENTIFIER (afc_auth/backends.py resolves one typed string against
  username OR uid OR email), so publishing it hands an anonymous caller two of the three ways to
  name any account. That is the defect fixed in afc_team on 2026-08-08 (BE 90ee597e); this file is
  what stops it being reintroduced here.

WHAT IS COVERED
  • the new endpoint refuses an anonymous caller, a bad token and a non-admin, with ONE body, so it
    cannot be used to test whether a token belongs to staff;
  • an admin (base role OR a granular UserRoles row) gets the rows, WITH uid and email;
  • the shared row builder still produces the same stats/team/ban columns for both endpoints;
  • ⚠ THE REGRESSION TEST THAT MATTERS: get_all_users still returns NEITHER uid NOR email, and the
    raw values do not appear anywhere in its response bytes.

Run: python manage.py test afc_player.tests_admin_player_list
"""
from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles

PUBLIC_URL = "/player/get-all-players/"
ADMIN_URL = "/player/admin/list-players/"


class AdminPlayerListTests(TestCase):

    def setUp(self):
        self.client = Client()

        self.admin = self._user("boss", "boss@gmail.com", role="admin", uid="1111111111")
        # An account with NO base admin role but a granular one: the same predicate
        # afc_auth.views.search_users uses treats this as staff, and so must this endpoint.
        self.news_admin = self._user("news_guy", "news@gmail.com", role="player",
                                     granular=["news_admin"], uid="2222222222")
        self.player = self._user("just_a_player", "player@gmail.com", uid="3333333333")

    # ── fixtures ─────────────────────────────────────────────────────────────────────────────
    def _user(self, username, email, role="player", granular=(), uid=None):
        user = User.objects.create(
            username=username, email=email, full_name=username.title(), role=role,
            password=make_password("CorrectHorse!9"), uid=uid, language="en",
        )
        for name in granular:
            row, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
            UserRoles.objects.create(user=user, role=row)
        return user

    def _session(self, user):
        """Mint a SessionToken directly (project rule: never type a password to get a session)."""
        return SessionToken.objects.create(
            user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
        ).token

    def _get(self, url, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(url, **headers)

    def _row_for(self, payload, username):
        return next(r for r in payload["users"] if r["name"] == username)

    # ── §1  the gate ─────────────────────────────────────────────────────────────────────────
    def test_anonymous_is_refused(self):
        resp = self._get(ADMIN_URL)
        self.assertEqual(resp.status_code, 401)

    def test_bad_token_is_refused(self):
        self.assertEqual(self._get(ADMIN_URL, token="not-a-real-token").status_code, 401)

    def test_plain_player_is_refused(self):
        self.assertEqual(self._get(ADMIN_URL, token=self._session(self.player)).status_code, 401)

    def test_every_refusal_reads_the_same(self):
        """A distinct 403 for "valid token, not staff" would confirm the token is good. One body."""
        bodies = {
            self._get(ADMIN_URL).content,
            self._get(ADMIN_URL, token="not-a-real-token").content,
            self._get(ADMIN_URL, token=self._session(self.player)).content,
        }
        self.assertEqual(len(bodies), 1, bodies)

    def test_no_uid_or_email_leaks_on_a_refusal(self):
        resp = self._get(ADMIN_URL, token=self._session(self.player))
        self.assertNotIn(b"3333333333", resp.content)
        self.assertNotIn(b"player@gmail.com", resp.content)

    # ── §2  the happy path ───────────────────────────────────────────────────────────────────
    def test_admin_gets_uid_and_email(self):
        resp = self._get(ADMIN_URL, token=self._session(self.admin))
        self.assertEqual(resp.status_code, 200, resp.content)
        row = self._row_for(resp.json(), "just_a_player")
        self.assertEqual(row["uid"], "3333333333")
        self.assertEqual(row["email"], "player@gmail.com")

    def test_granular_role_counts_as_admin(self):
        """No base role=="admin", one UserRoles row. Same predicate as search_users."""
        resp = self._get(ADMIN_URL, token=self._session(self.news_admin))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_missing_uid_reads_as_empty_string_not_null(self):
        """The frontend searches [name, uid, email] as strings; a None would need a guard there."""
        User.objects.filter(pk=self.player.pk).update(uid=None)
        row = self._row_for(self._get(ADMIN_URL, token=self._session(self.admin)).json(),
                            "just_a_player")
        self.assertEqual(row["uid"], "")

    def test_shared_columns_match_the_public_endpoint(self):
        """One row builder feeds both, so the stats/team/ban columns cannot drift apart."""
        admin_row = self._row_for(self._get(ADMIN_URL, token=self._session(self.admin)).json(),
                                  "just_a_player")
        public_row = self._row_for(self._get(PUBLIC_URL).json(), "just_a_player")
        for key in ("user_id", "name", "team_name", "total_kills", "total_wins", "total_mvps",
                    "status", "role"):
            self.assertEqual(admin_row[key], public_row[key], key)

    # ── §3  THE REGRESSION TEST. Read the module docstring before changing this. ──────────────
    def test_public_endpoint_still_leaks_neither_uid_nor_email(self):
        resp = self._get(PUBLIC_URL)
        self.assertEqual(resp.status_code, 200)

        row = self._row_for(resp.json(), "just_a_player")
        self.assertNotIn("uid", row)
        self.assertNotIn("email", row)

        # Belt and braces: the values themselves are absent from the whole response, not merely
        # under a different key name.
        self.assertNotIn(b"3333333333", resp.content)
        self.assertNotIn(b"player@gmail.com", resp.content)
