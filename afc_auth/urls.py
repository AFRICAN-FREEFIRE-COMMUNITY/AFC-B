from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    # path('admin-login/', admin_login, name='admin_login'),
    path('signup/', signup, name='signup'),
    path('verify/<uidb64>/<token>/', verify_token, name='verify_token'),
    path('login/', login, name='login'),
    
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)