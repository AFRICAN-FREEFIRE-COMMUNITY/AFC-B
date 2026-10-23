"""
afc_player/tests_directory.py
================================================================================
The public players directory, GET /player/directory/ (inbox #153 / #161, owner 2026-10-05: "let
there be a page for players also ... search for and view the profiles of players").

What must hold:
  1. Strangers get it (public, R25) and every row carries named fields only: no uid, email, role,
     status or ban record (R71; the full list was locked to admins on 2026-08-11 for exactly
     those identifiers).
  2. Only players already public elsewhere are listed: on a team, or with a scored match. An
     account that has done neither, a soft-deleted one and a suspended one are not.
  3. Search, country filter, paging and the country options.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_player.tests_directory --keepdb
"""
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team, TeamMembers

URL = "/player/directory/"
ROW_KEYS = {"username", "country", "profile_picture", "in_game_role", "management_role", "team"}


class PlayersDirectoryTests(TestCase):
    def setUp(self):
        self.owner = self._user("dir_owner", country="Ghana")
        self.team = Team.objects.create(team_name="Accra Arrows", team_tag="ACC", join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")
        for name, country in (("kofi_sniper", "Ghana"), ("ade_rusher", "Nigeria"), ("bola_support", "Nigeria")):
            TeamMembers.objects.create(team=self.team, member=self._user(name, country=country),
                                       in_game_role="sniper")
        # Not listed: no team and no match; deleted; suspended.
        self._user("quiet_account", country="Ghana")
        gone = self._user("gone_player", country="Ghana", status="deleted")
        TeamMembers.objects.create(team=self.team, member=gone)
        banned = self._user("benched_player", country="Ghana", status="suspended")
        TeamMembers.objects.create(team=self.team, member=banned)

    def _user(self, username, country="", status="active"):
        return User.objects.create_user(username=username, email=f"{username}@example.com",
                                        password="x", country=country, status=status,
                                        uid=str(abs(hash(username)) % 10**10))

    def _get(self, **params):
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_strangers_get_named_fields_only(self):
        data = self._get()
        self.assertTrue(data["results"])
        for row in data["results"]:
            self.assertEqual(set(row), ROW_KEYS)
        body = str(data)
        self.assertNotIn("@example.com", body)
        self.assertNotIn("suspended", body)

    def test_only_players_already_public_are_listed(self):
        names = [r["username"] for r in self._get()["results"]]
        self.assertEqual(names, ["ade_rusher", "bola_support", "dir_owner", "kofi_sniper"])

    def test_a_row_carries_the_team_and_role(self):
        row = next(r for r in self._get(q="kofi")["results"])
        self.assertEqual(row["team"]["team_name"], "Accra Arrows")
        self.assertEqual(row["in_game_role"], "sniper")
        self.assertEqual(row["country"], "Ghana")

    def test_search_and_country_filter(self):
        self.assertEqual([r["username"] for r in self._get(q="RUSH")["results"]], ["ade_rusher"])
        self.assertEqual([r["username"] for r in self._get(country="Nigeria")["results"]],
                         ["ade_rusher", "bola_support"])
        self.assertEqual(self._get(q="nobody_like_this")["results"], [])

    def test_paging_and_country_options(self):
        first = self._get(limit=3)
        self.assertEqual(first["total_count"], 4)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 3)
        rest = self._get(limit=3, offset=3)
        self.assertEqual([r["username"] for r in rest["results"]], ["kofi_sniper"])
        self.assertFalse(rest["has_more"])
        # Options come from everybody listed, whatever the current filter.
        self.assertEqual([c["label"] for c in self._get(country="Nigeria")["countries"]], ["Ghana", "Nigeria"])

    def test_one_country_under_two_spellings_is_one_option_and_one_filter(self):
        """'NG' and 'Nigeria' are the same country (afc_auth/country_grouping.py)."""
        TeamMembers.objects.create(team=self.team, member=self._user("chidi_ng", country="NG"))
        data = self._get()
        nigeria = [c for c in data["countries"] if c["label"] == "Nigeria"]
        self.assertEqual(len(nigeria), 1)
        self.assertEqual(len(data["countries"]), 2)
        names = [r["username"] for r in self._get(country=nigeria[0]["value"])["results"]]
        self.assertEqual(names, ["ade_rusher", "bola_support", "chidi_ng"])
        row = next(r for r in data["results"] if r["username"] == "chidi_ng")
        self.assertEqual(row["country"], "Nigeria")

    def test_digits_then_letters_then_symbols_like_the_teams_tab(self):
        for name in ("_underscore", "9lives"):
            TeamMembers.objects.create(team=self.team, member=self._user(name, country="Ghana"))
        names = [r["username"] for r in self._get()["results"]]
        self.assertEqual(names[0], "9lives")
        self.assertEqual(names[-1], "_underscore")

    def test_stray_spaces_do_not_put_a_name_first(self):
        # create_user strips the name, older writers did not (production has such rows), so the
        # spaces go in with a queryset update.
        zed = self._user("zed_last", country="Ghana")
        User.objects.filter(pk=zed.pk).update(username="   zed_last")
        TeamMembers.objects.create(team=self.team, member=zed)
        names = [r["username"] for r in self._get()["results"]]
        self.assertEqual(names[-1], "   zed_last")
        self.assertEqual(names[0], "ade_rusher")

    def test_bad_values_are_refused_with_codes(self):
        self.assertEqual(self.client.get(URL, {"q": "x" * 51}).json()["code"], "query_too_long")
        self.assertEqual(self.client.get(URL, {"limit": "many"}).json()["code"], "limit_offset_numbers")

    # ── inbox #166: find a player by Free Fire UID ─────────────────────────────────────────────
    def test_a_full_uid_finds_the_player(self):
        User.objects.filter(username="kofi_sniper").update(uid="123456789")
        data = self._get(q="123456789")
        self.assertEqual([r["username"] for r in data["results"]], ["kofi_sniper"])
        # The row still carries no UID: finding a player by it is not publishing it.
        self.assertNotIn("123456789", str(data["results"]))

    def test_part_of_a_uid_finds_nobody(self):
        """A prefix search would let anybody rebuild a player's UID one digit at a time."""
        User.objects.filter(username="kofi_sniper").update(uid="123456789")
        self.assertEqual(self._get(q="1234567")["results"], [])
        self.assertEqual(self._get(q="56789")["results"], [])

    def test_an_unlisted_player_stays_unlisted_by_uid(self):
        User.objects.filter(username="quiet_account").update(uid="987654321")
        self.assertEqual(self._get(q="987654321")["results"], [])

    def test_short_digits_still_search_names_only(self):
        User.objects.filter(username="kofi_sniper").update(uid="12345")
        self.assertEqual(self._get(q="12345")["results"], [])
