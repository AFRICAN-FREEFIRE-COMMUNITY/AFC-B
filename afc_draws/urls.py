# afc_draws/urls.py - mounted at draws/ by afc/urls.py. See views.py for the request shapes.
from django.urls import path

from . import views

urlpatterns = [
    path("stages/<int:stage_id>/create/", views.create_draw, name="draws_create"),         # POST
    path("events/<int:event_id>/", views.event_draws, name="draws_for_event"),           # GET (public)
    path("<int:draw_id>/board/", views.board, name="draws_board"),                        # GET (public)
    path("<int:draw_id>/open/", views.open_draw, name="draws_open"),                      # POST {closes_at}
    path("<int:draw_id>/window/", views.update_window, name="draws_window"),              # POST {closes_at?, auto_place_at_close?}
    path("<int:draw_id>/close/", views.close_draw, name="draws_close"),                   # POST {place_rest?}
    path("<int:draw_id>/remind/", views.remind, name="draws_remind"),                     # POST
    path("<int:draw_id>/reset/", views.reset_draw, name="draws_reset"),                   # POST
    path("<int:draw_id>/pick/", views.pick_card, name="draws_pick"),                      # POST {card_number}
]
