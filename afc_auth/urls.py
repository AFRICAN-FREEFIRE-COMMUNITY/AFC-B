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
    path('send-verification-token/', send_verification_token, name='send_verification_token'),
    path('verify-token/', verify_token, name='verify_token'),
    path('resend-token/', resend_token, name='resend_token'),
    path('reset-password/', reset_password, name='reset_password'),
    # path('reset-password/<uidb64>/<token>/', reset_password, name='reset_password'),
    path('verify-code/', verify_code, name='verify_code'),
    path('resend-verification-code/', resend_verification_code, name='resend_verification_code'),
    path('edit-profile/', edit_profile, name='edit_profile'),
    path('get-user-profile/', get_user_profile, name='get_user_profile'),
    path('contact-us/', contact_us, name='contact_us'),
    
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)