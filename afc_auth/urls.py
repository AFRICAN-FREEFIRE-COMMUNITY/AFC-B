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
    path('create-news/', create_news, name='create_news'),
    path('edit-news/', edit_news, name='edit_news'),
    path('get-news-detail/', get_news_detail, name='get_news_detail'),
    path('get-all-news/', get_all_news, name='get_all_news'),
    path('delete-news/', delete_news, name='delete_news'),
    path('add-role/', add_role, name='add_role'),
    path('get-admin-info/', get_admin_info, name='get_admin_info'),
    path('get-all-roles/', get_all_roles, name='get_all_roles'),
    path('get-all-user-and-user-roles/', get_all_user_and_user_roles, name='get_all_user_and_user_roles'),
    path('suspend-user/', suspend_user, name='suspend_user'),
    path('activate-user/', activate_user, name='activate_user'),
    path('assign-roles-to-user/', assign_roles_to_user, name='assign_roles_to_user'),
    path('edit-user-roles/', edit_user_roles, name='edit_user_roles'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)