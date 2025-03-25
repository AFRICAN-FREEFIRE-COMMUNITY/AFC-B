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
    # path('logout/', logout, name='logout'),
    # path('forgot-password/', forgot_password, name='forgot_password'),
    # path('reset-password/<uidb64>/<token>/', reset_password, name='reset_password'),
    path('verify-code/', verify_code, name='verify_code'),
    
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)