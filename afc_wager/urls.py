"""
afc_wager.urls - every address of the wager feature. Mounted at `wagers/` by afc/urls.py.

Player side (views_public) first, then the CMS under `admin/` (views_admin). Every address is
named so a test can reverse it; the frontend reads them through lib/api/wagers.ts.
"""
from django.urls import path

from . import views_admin as adm, views_public as pub

urlpatterns = [
    # ── player and visitor ──
    path("settings/", pub.public_settings, name="wager-settings"),
    path("markets/", pub.list_markets, name="wager-markets"),
    path("markets/<slug:slug>/", pub.market_detail, name="wager-market"),
    path("markets/<slug:slug>/place/", pub.place, name="wager-place"),
    path("payments/verify/", pub.verify_payment, name="wager-verify-payment"),
    path("mine/", pub.my_wagers, name="wager-mine"),
    path("wager/<str:token>/cancel/", pub.cancel, name="wager-cancel"),
    path("winnings/", pub.winnings, name="wager-winnings"),
    path("winnings/ledger/", pub.ledger, name="wager-winnings-ledger"),
    path("winnings/banks/", pub.banks, name="wager-banks"),
    path("winnings/bank-accounts/", pub.bank_accounts, name="wager-bank-accounts"),
    path("winnings/withdraw/", pub.withdraw, name="wager-withdraw"),
    path("winnings/withdrawals/<str:token>/cancel/", pub.cancel_withdrawal, name="wager-withdrawal-cancel"),
    path("limits/", pub.limits, name="wager-limits"),
    path("limits/cooloff/", pub.cooloff, name="wager-cooloff"),
    path("limits/self-exclude/", pub.self_exclude, name="wager-self-exclude"),
    path("kyc/", pub.kyc, name="wager-kyc"),
    path("kyc/whatsapp/start/", pub.kyc_whatsapp_start, name="wager-kyc-start"),
    path("kyc/whatsapp/verify/", pub.kyc_whatsapp_verify, name="wager-kyc-verify"),

    # ── CMS ──
    path("admin/settings/", adm.admin_settings, name="wager-admin-settings"),
    path("admin/templates/", adm.templates, name="wager-admin-templates"),
    path("admin/templates/<slug:code>/", adm.template_detail, name="wager-admin-template"),
    path("admin/pickers/events/", adm.picker_events, name="wager-admin-picker-events"),
    path("admin/pickers/events/<int:event_id>/", adm.picker_event, name="wager-admin-picker-event"),
    path("admin/overview/", adm.overview, name="wager-admin-overview"),
    path("admin/queue/", adm.settlement_queue, name="wager-admin-queue"),
    path("admin/markets/", adm.markets, name="wager-admin-markets"),
    path("admin/markets/create/", adm.create_market, name="wager-admin-market-create"),
    path("admin/markets/<slug:slug>/", adm.market_admin_detail, name="wager-admin-market"),
    path("admin/markets/<slug:slug>/publish/", adm.publish_market, name="wager-admin-market-publish"),
    path("admin/markets/<slug:slug>/lock/", adm.lock_market, name="wager-admin-market-lock"),
    path("admin/markets/<slug:slug>/reopen/", adm.reopen_market, name="wager-admin-market-reopen"),
    path("admin/markets/<slug:slug>/void/", adm.void_market, name="wager-admin-market-void"),
    path("admin/markets/<slug:slug>/suggest/", adm.suggest_market, name="wager-admin-market-suggest"),
    path("admin/markets/<slug:slug>/settle/", adm.settle_market, name="wager-admin-market-settle"),
    path("admin/markets/<slug:slug>/wagers/", adm.market_wagers, name="wager-admin-market-wagers"),
    path("admin/users/", adm.users, name="wager-admin-users"),
    path("admin/users/<str:username>/", adm.user_detail, name="wager-admin-user"),
    path("admin/users/<str:username>/freeze/", adm.user_freeze, name="wager-admin-user-freeze"),
    path("admin/users/<str:username>/adjust/", adm.user_adjust, name="wager-admin-user-adjust"),
    path("admin/users/<str:username>/limits/", adm.user_limits, name="wager-admin-user-limits"),
    path("admin/ledger/", adm.admin_ledger, name="wager-admin-ledger"),
    path("admin/withdrawals/", adm.withdrawals, name="wager-admin-withdrawals"),
    path("admin/withdrawals/<str:token>/approve/", adm.withdrawal_approve, name="wager-admin-withdrawal-approve"),
    path("admin/withdrawals/<str:token>/reject/", adm.withdrawal_reject, name="wager-admin-withdrawal-reject"),
    path("admin/withdrawals/<str:token>/mark-paid/", adm.withdrawal_mark_paid, name="wager-admin-withdrawal-mark-paid"),
    path("admin/adjustments/", adm.adjustments, name="wager-admin-adjustments"),
    path("admin/adjustments/<int:adjustment_id>/cosign/", adm.adjustment_cosign, name="wager-admin-adjustment-cosign"),
    path("admin/kyc/", adm.kyc_list, name="wager-admin-kyc"),
    path("admin/kyc/<str:username>/force/", adm.kyc_force, name="wager-admin-kyc-force"),
]
