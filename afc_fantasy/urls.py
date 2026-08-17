"""
afc_fantasy.urls - routes for the Fantasy League, mounted at `fantasy/` in afc/urls.py.

PUBLIC ROUTES FIRST, then the entry routes, then admin. Literal sub-paths (`admin/...`) are declared
BEFORE `<slug>/` so a league can never be created with the slug "admin" and shadow the whole admin
surface. Django matches in order, so this is the ordering that makes that impossible rather than
unlikely.

    GET    fantasy/                                  the leagues a fan can see
    GET    fantasy/admin/leagues/                    the leagues you manage
    POST   fantasy/admin/leagues/                    create one
    GET    fantasy/admin/leagues/<slug>/             read one
    PATCH  fantasy/admin/leagues/<slug>/             edit one (rules frozen once open)
    POST   fantasy/admin/leagues/<slug>/prices/      price the pool (dry_run to preview)
    POST   fantasy/admin/leagues/<slug>/open/        let fans in
    POST   fantasy/admin/leagues/<slug>/recompute/   rebuild scores from current results
    GET    fantasy/<slug>/                           one league
    GET    fantasy/<slug>/players/                   the priced pool
    GET    fantasy/<slug>/standings/                 the table
    GET    fantasy/<slug>/my-squad/                  your squad
    PUT    fantasy/<slug>/my-squad/                  save it (dry_run to validate only)
"""
from django.urls import path

from . import admin_views, views

urlpatterns = [
    path("", views.list_leagues, name="fantasy_list"),

    # ── admin, declared before <slug>/ so no league slug can shadow it ────────────────────────
    path("admin/leagues/", admin_views.admin_leagues, name="fantasy_admin_leagues"),
    path("admin/leagues/<slug:slug>/", admin_views.admin_league, name="fantasy_admin_league"),
    path("admin/leagues/<slug:slug>/prices/", admin_views.admin_build_prices,
         name="fantasy_admin_prices"),
    path("admin/leagues/<slug:slug>/open/", admin_views.admin_open_league,
         name="fantasy_admin_open"),
    path("admin/leagues/<slug:slug>/recompute/", admin_views.admin_recompute,
         name="fantasy_admin_recompute"),

    # ── the fan-facing league ─────────────────────────────────────────────────────────────────
    path("<slug:slug>/", views.league_detail, name="fantasy_league"),
    path("<slug:slug>/players/", views.league_players, name="fantasy_players"),
    path("<slug:slug>/standings/", views.league_table, name="fantasy_standings"),
    path("<slug:slug>/my-squad/", views.my_squad, name="fantasy_my_squad"),
]
