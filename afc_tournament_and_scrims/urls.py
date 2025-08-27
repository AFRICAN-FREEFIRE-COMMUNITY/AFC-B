from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    # path('admin-login/', admin_login, name='admin_login'),
    path('create-event/', create_event, name='create_event'),
    path('edit-event', edit_event, name='edit_event'),
    path('create-leaderboard/', create_leaderboard, name='create_leaderboard'),
    path('get-all-events/', get_all_events, name='get_all_events'),
    path('get-event-details/', get_event_details, name='get_event_details'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)