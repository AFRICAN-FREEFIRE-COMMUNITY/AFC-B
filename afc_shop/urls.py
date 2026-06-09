from django.urls import path, include
from .views import *
# Stripe Checkout: the SECOND shop payment provider (added alongside Paystack, see
# afc_shop/stripe_checkout.py). Imported explicitly (not via *) so the three Stripe views are
# clearly sourced. The Paystack routes below (buy-now / verify-paystack-payment / paystack-webhook)
# are unchanged.
from .stripe_checkout import stripe_buy_now, stripe_verify, stripe_webhook
# Marketplace fulfilment state machine (Phase A, afc_shop/fulfilment.py). Imported
# explicitly (not via *) so the vendor transition + queue endpoints are clearly
# sourced. These are the ONE backend API the per-order vendor page AND the Kapso
# WhatsApp flow both call (both SEPARATE follow-ups).
from .fulfilment import (
    vendor_acknowledge_order,
    vendor_set_ship_date,
    vendor_mark_shipped,
    order_mark_completed,
    vendor_my_orders,
)
# Marketplace vendor management + product approval + vendor product CRUD (Phase B1,
# afc_shop/vendors.py). Imported explicitly (not via *) so the admin "Manage vendors"
# / "Product approvals" endpoints and the vendor-dashboard CRUD endpoints are clearly
# sourced. Vendors are INVITE-ONLY (admins create them); admins approve each product.
from .vendors import (
    # A) admin vendor management
    admin_create_vendor,
    admin_list_vendors,
    admin_set_vendor_status,
    admin_assign_product_vendor,
    # B) admin product approval
    admin_list_pending_products,
    admin_approve_product,
    admin_reject_product,
    # C) vendor product CRUD
    vendor_my_products,
    vendor_create_product,
    vendor_update_product,
    vendor_submit_product,
)
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    # path("admin/", admin.site.urls),
    path('add-product/', add_product, name='add_product'),
    path('view-all-products/', view_all_products, name='view_all_products'),          # admin: every status
    path('view-active-products/', view_active_products, name='view_active_products'),  # public: storefront (active only)
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
    path("delete-product-variant/", delete_product_variant, name="delete_product_variant"),
    path("add-to-cart/", add_to_cart, name="add_to_cart"),
    path("get-my-cart/", get_my_cart, name="get_my_cart"),
    path("remove-from-cart/", remove_from_cart, name="remove_from_cart"),
    path("update-cart-item-quantity/", update_cart_item_quantity, name="update_cart_item_quantity"),
    path("clear-cart/", clear_cart, name="clear_cart"),
    path("buy-now/", buy_now, name="buy_now"),
    path("verify-paystack-payment/", verify_paystack_payment, name="verify_paystack_payment"),
    path("paystack-webhook/", paystack_webhook, name="paystack_webhook"),

    # ── Stripe Checkout (second provider, alongside Paystack above) ──
    # stripe-buy-now: CartDetails.tsx POSTs the cart here when the buyer picks Stripe -> returns a
    #                 checkout_url to redirect to. stripe-verify: the success page confirms payment
    #                 by session id. stripe-webhook: Stripe's server-side backstop.
    path("stripe-buy-now/", stripe_buy_now, name="stripe_buy_now"),
    path("stripe-verify/", stripe_verify, name="stripe_verify"),
    path("stripe-webhook/", stripe_webhook, name="stripe_webhook"),
    path("get-my-orders/", get_my_orders, name="get_my_orders"),
    path("get-order-details/", get_order_details, name="get_order_details"),
    path("get-order-details-for-admin/", get_order_details_for_admin, name="get_order_details_for_admin"),
    path("mark-order-as-paid/", mark_order_as_paid, name="mark_order_as_paid"),
    path("delete-coupon/", delete_coupon, name="delete_coupon"),
    path("edit-coupon/", edit_coupon, name="edit_coupon"),
    path("get-weekly-usage-and-saving-generated/", get_weekly_usage_and_saving_generated, name="get_weekly_usage_and_saving_generated"),
    path("deactivate-coupon/", deactivate_coupon, name="deactivate_coupon"),
    path("activate-coupon/", activate_coupon, name="activate_coupon"),
    path("get-total-customer-savings/", get_total_customer_savings, name="get_total_customer_savings"),
    path("get-total-coupon-uses/", get_total_coupon_uses, name="get_total_coupon_uses"),
    path("get-total-revenue-generated/", get_total_revenue_generated, name="get_total_revenue_generated"),
    path("get-coupon-conversion-rate/", get_coupon_conversion_rate, name="get_coupon_conversion_rate"),
    path("get-coupon-details/", get_coupon_details, name="get_coupon_details"),
    path('get-coupon-details-with-code/', get_coupon_details_with_code, name='get_coupon_details_with_code'),
    path("get-all-fulfillments/", get_all_fulfillments, name="get_all_fulfillments"),
    path("test-denom/", test_denom, name="test_denom"),
    path("test-brands/", test_brands, name="test_brands"),

    # ── Category CRUD (admin-managed product categories) ──
    # Powers the user shop category tabs + the admin "Manage Categories" surface.
    path("view-all-categories/", view_all_categories, name="view_all_categories"),       # admin: full list
    path("view-active-categories/", view_active_categories, name="view_active_categories"),  # public: shop tabs
    path("create-category/", create_category, name="create_category"),
    path("edit-category/", edit_category, name="edit_category"),
    path("delete-category/", delete_category, name="delete_category"),

    # ── Product media (multi-image + video gallery) ──
    path("add-product-media/", add_product_media, name="add_product_media"),
    path("delete-product-media/", delete_product_media, name="delete_product_media"),

    # ── Marketplace order fulfilment state machine (Phase A) ──
    # The vendor (or an AFC admin) drives an order through received -> acknowledged
    # -> ship_scheduled -> shipped (+evidence) -> completed. Consumed by the SEPARATE
    # per-order vendor page + the SEPARATE Kapso WhatsApp flow (both POST the same
    # endpoints). vendor-orders is the caller-vendor's PII-scoped fulfilment queue.
    path("fulfilment/acknowledge/", vendor_acknowledge_order, name="vendor_acknowledge_order"),
    path("fulfilment/set-ship-date/", vendor_set_ship_date, name="vendor_set_ship_date"),
    path("fulfilment/mark-shipped/", vendor_mark_shipped, name="vendor_mark_shipped"),
    path("fulfilment/mark-completed/", order_mark_completed, name="order_mark_completed"),
    path("fulfilment/my-orders/", vendor_my_orders, name="vendor_my_orders"),

    # ── Marketplace Phase B1: admin vendor management (INVITE-ONLY) ──
    # The admin shop "Manage vendors" surface. Admins LINK an existing User to a new
    # Vendor (no public application), list/suspend vendors, and re-home products to a
    # vendor. All require_admin (afc_shop/vendors.py cluster A).
    path("admin/vendors/create/", admin_create_vendor, name="admin_create_vendor"),
    path("admin/vendors/list/", admin_list_vendors, name="admin_list_vendors"),
    path("admin/vendors/set-status/", admin_set_vendor_status, name="admin_set_vendor_status"),
    path("admin/vendors/assign-product/", admin_assign_product_vendor, name="admin_assign_product_vendor"),

    # ── Marketplace Phase B1: admin product approval queue ──
    # The admin shop "Product approvals" surface. Lists submitted vendor products and
    # approves/rejects them. Only approved (+ active) vendor products reach the
    # storefront (gate in views.view_active_products). require_admin (vendors.py cluster B).
    path("admin/products/pending/", admin_list_pending_products, name="admin_list_pending_products"),
    path("admin/products/approve/", admin_approve_product, name="admin_approve_product"),
    path("admin/products/reject/", admin_reject_product, name="admin_reject_product"),

    # ── Marketplace Phase B1: vendor self-serve product CRUD ──
    # The vendor dashboard (Phase B2 frontend). Gated to the CALLER's own ACTIVE
    # Vendor (vendors._require_active_vendor). A vendor manages only their own
    # products and can never approve their own (draft -> submitted only). vendors.py
    # cluster C.
    path("vendor/products/", vendor_my_products, name="vendor_my_products"),
    path("vendor/products/create/", vendor_create_product, name="vendor_create_product"),
    path("vendor/products/update/", vendor_update_product, name="vendor_update_product"),
    path("vendor/products/submit/", vendor_submit_product, name="vendor_submit_product"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)