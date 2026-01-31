from django.urls import path, include
from .views import *
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    path('add-product/', add_product, name='add_product'),
    path('view-all-products/', view_all_products, name='view_all_products'),
    path('edit-product/', edit_product, name='edit_product'),
    path('delete-product/', delete_product, name='delete_product'),
    path('deactivate-product/', deactivate_product, name='deactivate_product'),
    path('view-current-stock-status/', view_current_stock_status, name='view_current_stock_status'),
    path('view-all-orders/', view_all_orders, name='view_all_orders'),
    path('orders-today/', orders_today, name='orders_today'),
    path('orders-this-week/', orders_this_week, name='orders_this_week'),
    path('orders-this-month/', orders_this_month, name='orders_this_month'),
    path('view-all-coupons/', view_all_coupons, name='view_all_coupons'),
    path('create-coupon/', create_coupon, name='create_coupon'),
    path('activate-product/', activate_product, name='activate_product'),
    path('view-product-details/', view_product_details, name='view_product_details'),
    path("add-product-variant/", add_product_variant, name="add_product_variant"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)