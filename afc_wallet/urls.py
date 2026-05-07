"""afc_wallet URL configuration.

NOTE: These URLs are NOT live in v1. The include() in `afc/urls.py` is
commented out. They are listed here so wiring them later is a one-line change.
"""

from django.urls import path

from . import views

app_name = "afc_wallet"

urlpatterns = [
    # Balance + history
    path("balance/", views.get_balance, name="get_balance"),
    path("transactions/", views.list_transactions, name="list_transactions"),
    # Deposits
    path("deposit/start/", views.start_deposit, name="start_deposit"),
    path(
        "deposit/webhook/paystack/",
        views.paystack_webhook,
        name="paystack_webhook",
    ),
    path(
        "deposit/webhook/stripe/", views.stripe_webhook, name="stripe_webhook"
    ),
    path(
        "deposit/webhook/crypto/", views.crypto_webhook, name="crypto_webhook"
    ),
    # Vouchers
    path("redeem-voucher/", views.redeem_voucher, name="redeem_voucher"),
    # P2P
    path("p2p/send/", views.p2p_send, name="p2p_send"),
    # Withdrawals
    path("withdraw/", views.withdraw, name="withdraw"),
    # KYC
    path(
        "verify/whatsapp/start/",
        views.verify_whatsapp_start,
        name="verify_whatsapp_start",
    ),
    path(
        "verify/whatsapp/confirm/",
        views.verify_whatsapp_confirm,
        name="verify_whatsapp_confirm",
    ),
    path("verify/discord/", views.verify_discord, name="verify_discord"),
    path("kyc/status/", views.kyc_status, name="kyc_status"),
]
