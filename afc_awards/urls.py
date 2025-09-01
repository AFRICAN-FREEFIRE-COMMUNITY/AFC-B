from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path('categories/', include([
        path('add/', add_new_category),
        path('view/', view_all_categories),
        path('delete/', delete_category),
    ])),
    path('nominees/', include([
        path('add/', add_new_nominee),
        path('view/', view_all_nominees),
        path('delete/', delete_nominee),
    ])),
    path('category-nominee/', include([
        path('add/', add_nominee_to_category),
        path('view/', view_nominee_in_category),
        path('remove/', remove_nominee_from_category),
    ])),
    path('votes/', include([
        path('submit/', submit_votes),
    ])),
    path('sections/', include([
        path('add/', add_section),
    ])),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)