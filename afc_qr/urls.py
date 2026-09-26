"""afc_qr.urls - mounted at qr/ in afc/urls.py. What each endpoint takes and answers: afc_qr/views.py."""
from django.urls import path

from . import views

urlpatterns = [
    path("link/", views.create_link, name="qr_create_link"),              # POST, public
    path("info/<str:token>/", views.link_info, name="qr_link_info"),      # GET, public, not counted
    path("scan/<str:token>/", views.scan, name="qr_scan"),               # POST, public, counted
    path("stats/<str:token>/", views.link_stats, name="qr_link_stats"),  # GET, page owner only
]
