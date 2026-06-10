"""
Tests for afc_team views.

search_teams (GET /team/search-teams/) — the team typeahead powering <TeamSearchSelect/> in the
Standalone Leaderboards wizard. Mirrors afc_auth.views.search_users: Bearer auth, q>=2, icontains,
{results:[{team_id,team_name,team_tag,country}], total_count} shape.
"""
from django.test import TestCase, Client

from afc_auth.models import User, SessionToken
from afc_team.models import Team


def _make_user(username):
    u = User.objects.create(username=username, email=f"{username}@x.com", full_name=username, role="player", password="x")
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


class SearchTeamsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user, self.tok = _make_user("searcher")
        self.dynasty = Team.objects.create(team_name="Dynasty Esports", team_tag="DYN", country="NG",
                                            join_settings="open", team_owner=self.user, team_creator=self.user)
        self.dynamo = Team.objects.create(team_name="Dynamo FC", team_tag="DMO", country="GH",
                                          join_settings="open", team_owner=self.user, team_creator=self.user)
        self.other = Team.objects.create(team_name="Falcons", team_tag="FAL", country="KE",
                                         join_settings="open", team_owner=self.user, team_creator=self.user)

    def _get(self, q=None, limit=None, tok=None):
        params = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        headers = {"HTTP_AUTHORIZATION": f"Bearer {tok}"} if tok else {}
        return self.client.get("/team/search-teams/", params, **headers)

    def test_requires_auth(self):
        self.assertEqual(self._get(q="dyn").status_code, 400)  # no token

    def test_q_under_two_chars_returns_empty(self):
        body = self._get(q="d", tok=self.tok).json()
        self.assertEqual(body, {"results": [], "total_count": 0})

    def test_matches_team_name_icontains(self):
        body = self._get(q="dyn", tok=self.tok).json()
        names = {r["team_name"] for r in body["results"]}
        self.assertEqual(names, {"Dynasty Esports", "Dynamo FC"})
        self.assertEqual(body["total_count"], 2)

    def test_result_shape(self):
        body = self._get(q="Dynasty", tok=self.tok).json()
        row = body["results"][0]
        self.assertEqual(set(row.keys()), {"team_id", "team_name", "team_tag", "country"})
        self.assertEqual(row["team_tag"], "DYN")
        self.assertEqual(row["country"], "NG")

    def test_matches_team_tag(self):
        body = self._get(q="FAL", tok=self.tok).json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["team_name"], "Falcons")

    def test_limit_capped(self):
        body = self._get(q="Dyn", limit=1, tok=self.tok).json()
        self.assertEqual(len(body["results"]), 1)   # page size honored
        self.assertEqual(body["total_count"], 2)    # but total reflects all matches
