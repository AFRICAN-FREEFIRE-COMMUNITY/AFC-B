"""afc_mail addresses (prefix mail/). Each view's docstring in afc_mail/views.py says who calls it."""
from django.urls import path

from . import views

urlpatterns = [
    path("o/<str:token>.gif", views.open_pixel, name="mail_open_pixel"),
    path("c/<str:token>/<int:index>/", views.click, name="mail_click"),
    path("unsubscribe/<str:token>/", views.unsubscribe, name="mail_unsubscribe"),
    path("resubscribe/<str:token>/", views.resubscribe, name="mail_resubscribe"),
    path("ses-events/", views.ses_events, name="mail_ses_events"),
    path("preferences/", views.preferences, name="mail_preferences"),
    path("campaigns/", views.campaigns, name="mail_campaigns"),
    path("campaigns/<str:token>/", views.campaign_detail, name="mail_campaign_detail"),
    path("campaigns/<str:token>/audience/", views.campaign_audience, name="mail_campaign_audience"),
    path("campaigns/<str:token>/preview/", views.campaign_preview, name="mail_campaign_preview"),
    path("campaigns/<str:token>/test/", views.campaign_test, name="mail_campaign_test"),
    path("campaigns/<str:token>/send/", views.campaign_send, name="mail_campaign_send"),
    path("campaigns/<str:token>/cancel/", views.campaign_cancel, name="mail_campaign_cancel"),
    path("campaigns/<str:token>/start-from/", views.campaign_start_from, name="mail_campaign_start_from"),
    path("campaigns/<str:token>/design/", views.campaign_design, name="mail_campaign_design"),
    path("audience/", views.audience, name="mail_audience"),
    path("assets/", views.assets, name="mail_assets"),
    path("templates/", views.templates, name="mail_templates"),
    path("templates/<str:token>/", views.template_detail, name="mail_template_detail"),
]
