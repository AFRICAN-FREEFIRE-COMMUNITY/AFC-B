"""afc_wager URL configuration.

NOTE: Not live in v1. The include() in `afc/urls.py` is commented out.
Views are stubbed.
"""

from django.urls import path

from . import views

app_name = "afc_wager"

urlpatterns = [
    path("markets/", views.list_markets, name="list_markets"),
    path("markets/<int:market_id>/", views.get_market, name="get_market"),
    path(
        "markets/<int:market_id>/place/",
        views.place_wager,
        name="place_wager",
    ),
    path(
        "markets/<int:market_id>/cancel/",
        views.cancel_wager,
        name="cancel_wager",
    ),
    path("my-wagers/", views.list_my_wagers, name="list_my_wagers"),
    path("admin/markets/", views.admin_create_market, name="admin_create_market"),
    path(
        "admin/markets/<int:market_id>/lock/",
        views.admin_lock_market,
        name="admin_lock_market",
    ),
    path(
        "admin/markets/<int:market_id>/settle/",
        views.admin_settle_market,
        name="admin_settle_market",
    ),
    path(
        "admin/markets/<int:market_id>/void/",
        views.admin_void_market,
        name="admin_void_market",
    ),
    path(
        "admin/settlement-queue/",
        views.admin_settlement_queue,
        name="admin_settlement_queue",
    ),
]
