"""afc_referrals.urls - mounted at referrals/ in afc/urls.py. What each endpoint takes and answers:
afc_referrals/views.py."""
from django.urls import path

from . import views

urlpatterns = [
    path("r/<str:code>/", views.landing, name="referrals_landing"),
    path("click/", views.click, name="referrals_click"),
    path("claim/", views.claim, name="referrals_claim"),
    path("mine/", views.mine, name="referrals_mine"),
    path("admin/programs/", views.admin_programs, name="referrals_admin_programs"),
    path("admin/programs/<slug:slug>/", views.admin_program, name="referrals_admin_program"),
    path("admin/programs/<slug:slug>/referrals/", views.admin_program_referrals, name="referrals_admin_referrals"),
    path("admin/programs/<slug:slug>/rewards/", views.admin_program_rewards, name="referrals_admin_rewards"),
    path("admin/programs/<slug:slug>/export/", views.admin_program_export, name="referrals_admin_export"),
    path("admin/programs/<slug:slug>/award-ranks/", views.admin_award_ranks, name="referrals_admin_award_ranks"),
    path("admin/referrals/<str:token>/decide/", views.admin_decide, name="referrals_admin_decide"),
    path("admin/rewards/<str:token>/deliver/", views.admin_deliver, name="referrals_admin_deliver"),
]
