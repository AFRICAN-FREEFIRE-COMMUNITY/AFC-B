from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    path('upload-match-results/<tournament_id>/<match_number>/', upload_match_results, name='upload_match_results'),
    path('create-tournament/', create_tournament, name='create_tournament')
    
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)