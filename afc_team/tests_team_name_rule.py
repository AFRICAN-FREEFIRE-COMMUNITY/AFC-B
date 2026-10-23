"""A team name needs a letter or number, and is not a page's name (inbox #276, 2026-10-16).

Found on production: team 767 is called "." and team 250 "---". A browser folds /teams/. into
/teams, so the "." team had no page at all. The rule lives in afc_team/names.py; create_team checks
it, edit_team checks it on a rename only.

Styled names are real names: players write ＧＨＯＳＴ༒ＣＬＡＮ, 𝙉𝙀𝙓𝙐𝙎, ᴀʜ, 丅乄 (all on production), and every
one of them must still be accepted. The invisible Hangul filler is a "letter" to Python but draws
nothing, so a name made only of it is refused.

Run: .venv\\Scripts\\python.exe manage.py test afc_team.tests_team_name_rule
"""
from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers
from afc_team.names import team_name_problem

CREATE = "/team/create-team/"
EDIT = "/team/edit-team/"


def _user(username):
    u = User.objects.create(username=username, email=f"{username}@x.com", full_name=username,
                            role="player", password="x")
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


class TeamNameRuleUnitTests(TestCase):
    def test_names_with_no_letter_or_number_are_refused(self):
        for name in (".", "---", "..", "   ", "!!!", "ㅤㅤ", " ㅤ ", "⁣", ""):
            self.assertEqual(team_name_problem(name), "team_name_needs_letter", repr(name))

    def test_styled_names_from_production_are_accepted(self):
        for name in ("ＧＨＯＳＴ༒ＣＬＡＮ", "𝙉𝙀𝙓𝙐𝙎 𝐞𝐒𝐩𝐨𝐫𝐭𝐬  ✞𓁋", "ᴀʜ│ᴍᴀɴᴜᴇʟ ㅤ웃",
                     "丅⁣乄ᏚႮᏢᎡᎬᎷᎬ乄豈", "VORTEX ESPORTS", "7", "A."):
            self.assertIsNone(team_name_problem(name), repr(name))

    def test_a_page_name_is_reserved_whatever_the_case(self):
        for name in ("create", "Create", " CREATE "):
            self.assertEqual(team_name_problem(name), "team_name_reserved", repr(name))
        self.assertIsNone(team_name_problem("Create Gaming"))


class CreateTeamNameTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user, self.tok = _user("namerule")

    def _create(self, name):
        return self.client.post(CREATE, {"team_name": name, "join_settings": "open"},
                                HTTP_AUTHORIZATION=f"Bearer {self.tok}")

    def test_a_dot_is_refused_with_a_code(self):
        """THE BUG: team 767 on production is called '.'."""
        res = self._create(".")
        self.assertEqual(res.status_code, 400, res.content[:300])
        self.assertEqual(res.json()["code"], "team_name_needs_letter")
        self.assertFalse(Team.objects.filter(team_name=".").exists())

    def test_dashes_and_invisible_names_are_refused(self):
        for name in ("---", "ㅤㅤ"):
            res = self._create(name)
            self.assertEqual(res.json().get("code"), "team_name_needs_letter", repr(name))
        self.assertEqual(Team.objects.count(), 0)

    def test_a_page_name_is_refused(self):
        res = self._create("Create")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "team_name_reserved")

    def test_a_styled_name_is_created(self):
        res = self._create("ＧＨＯＳＴ༒ＣＬＡＮ")
        self.assertIn(res.status_code, (200, 201), res.content[:300])
        self.assertTrue(Team.objects.filter(team_name="ＧＨＯＳＴ༒ＣＬＡＮ").exists())


class EditTeamNameTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner, self.tok = _user("dotowner")
        # An existing team that already breaks the rule, as on production.
        self.team = Team.objects.create(team_name=".", team_tag="LLLL", join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_owner")

    def _edit(self, **data):
        return self.client.post(EDIT, {"team_id": self.team.team_id, **data},
                                HTTP_AUTHORIZATION=f"Bearer {self.tok}")

    def test_renaming_to_a_dot_is_refused(self):
        self.team.team_name = "Accra Hawks"
        self.team.save()
        res = self._edit(team_name=".")
        self.assertEqual(res.status_code, 400, res.content[:300])
        self.assertEqual(res.json()["code"], "team_name_needs_letter")
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_name, "Accra Hawks")

    def test_an_old_name_does_not_block_other_edits(self):
        """The form posts the whole team, unchanged name included: that must still save."""
        res = self._edit(team_name=".", team_description="New words")
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_description, "New words")

    def test_a_good_rename_works(self):
        res = self._edit(team_name="VORTEX ESPORTS")
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_name, "VORTEX ESPORTS")
