# AFC Wager Feature — Backend Branch (`feature/wager`)

This branch ships the **model + service layer** for the parimutuel wager
engine and the 4-rail AFC Coin wallet. It is intentionally **not live in
v1** — see "Status" below.

> Frontend mock + UI: see `AFC_Frontend` repo, branch `feature/wager`.
> Spec: `WEBSITE/docs/superpowers/specs/2026-05-07-wager-feature-design.md`.
> Plan: `WEBSITE/docs/superpowers/plans/2026-05-07-wager-feature-phase-1.md`.

---

## What's in this branch

### New apps

| App | Purpose | Endpoints |
|---|---|---|
| `afc_wallet` | 4-rail wallet, ledger, KYC-Lite, vouchers, withdrawals, audit log | stubbed (commented in `afc/urls.py`) |
| `afc_wager` | Markets, options, wagers, lines, settlement engine, payouts, rake | stubbed (commented in `afc/urls.py`) |

### Files of note

```
backend/
├── afc_wallet/
│   ├── models.py             # 9 models per spec Section 4
│   ├── services.py           # credit, debit, p2p, redeem_voucher, KYC
│   ├── constants.py          # mirrors frontend lib/utils.ts (KOBO_PER_COIN, RAKE_BPS, ...)
│   ├── adapters/             # Paystack/Stripe/NowPayments/WhatsApp OTP signature stubs
│   ├── fixtures/wallet_seed.json     # 9 users + 9 wallets + 9 KYC + FX
│   └── tests/                # 44 tests
├── afc_wager/
│   ├── models.py             # MarketTemplate, Market, MarketOption, Wager, WagerLine,
│   │                         #   Settlement, Payout, RakeTxn
│   ├── settlement.py         # compute_settlement() pure math + settle_market() DB
│   ├── adapters/stats_reader.py  # 8 grader functions (match_winner, mvp, most_kills, ...)
│   ├── fixtures/             # 9 MarketTemplates + 1 demo Event + 1 demo Market
│   └── tests/                # 30 tests including hypothesis property + concurrency
├── afc_auth/migrations/0003_userprofile_wager_fields.py
│                              # +5 nullable fields on UserProfile
├── shared-fixtures/wager-scenarios.json
│                              # vendored from frontend; parity test source of truth
├── afc/test_settings.py       # sqlite-only settings for the wallet/wager test suite
├── afc/test_urls.py            # empty URLConf (legacy URLs pull broken deps)
└── manage_test.py              # entrypoint that always uses test_settings
```

### Modified files

- `backend/afc/settings.py` — `INSTALLED_APPS += ['afc_wallet', 'afc_wager']`
- `backend/afc/urls.py` — `path("wallet/", ...)` and `path("wager/", ...)`
  added but **commented out**. Endpoints are stubbed in v1; flipping this
  comment requires confirming the views are wired.
- `backend/afc_auth/models.py` — `UserProfile` gets 5 nullable additive
  fields: `whatsapp_number`, `whatsapp_verified_at`, `discord_user_id`,
  `discord_linked_at`, `show_on_leaderboard`.
- `backend/requirements.txt` — adds `hypothesis==6.115.5` (cryptography
  was already pinned).

---

## Status

- ✅ Models in place + migration files committed
- ✅ Service layer (credit, debit, p2p, vouchers, KYC) implemented + tested
- ✅ Settlement engine matches the frontend TS engine on shared fixtures
- ✅ Concurrency tests for double-credit, double-debit, double-settle,
       voucher race
- ✅ Property tests via hypothesis (200+50+100+50 examples) for the 4 core
       invariants
- ✅ Stats reader graders for all 9 MarketTemplate codes
- ✅ Admin override + audit log fields persisted on Settlement
- ⚠ Endpoints are STUBBED — they return `{"status": "stubbed"}` and are
       behind a commented `include()` in `afc/urls.py`. Wiring them live
       is a follow-up PR.
- ⚠ Real Paystack/Stripe/NowPayments HTTP calls are stubbed — only
       signature-verify primitives are in place.
- ⚠ WhatsApp OTP is mock — accepts only `"000000"`.

---

## Running the tests

The full project's `manage.py test` command pulls a heavy dependency
tree (pandas, easyocr, sympy, etc.) and several existing apps have
pre-existing Python 3.11 issues that block the global test runner. To
exercise the wallet + wager suite in isolation, use the dedicated test
settings:

```bash
cd backend
python manage_test.py test afc_wallet afc_wager
```

This:
- Uses sqlite (`test_db.sqlite3` for migrations, `:memory:` for the test
  runner clone).
- Loads only `afc_auth`, `afc_team`, `afc_tournament_and_scrims`,
  `afc_wallet`, `afc_wager` — the minimum the wager feature touches.
- Disables migrations for the legacy apps (CREATE TABLE direct from
  models.py via syncdb) — afc_wallet + afc_wager keep their migrations
  so that path is exercised.
- Bumps sqlite busy timeout to 60s for thread-race tests.

Expected output: **74 tests pass** in <2s.

```
Ran 74 tests in 1.666s
OK
```

### Running a single test

```bash
python manage_test.py test afc_wallet.tests.test_services_p2p
python manage_test.py test afc_wager.tests.test_settlement_parity
```

### Loading the demo fixtures

```bash
python manage_test.py loaddata afc_wallet/fixtures/wallet_seed.json
python manage_test.py loaddata afc_wager/fixtures/market_templates.json
python manage_test.py loaddata afc_wager/fixtures/wager_demo_seed.json
```

---

## What's next (Phase 2+)

1. **Wire endpoints live.** Uncomment the wallet/wager `include()` in
   `afc/urls.py`, replace stubbed view bodies with the service-layer
   calls (the URL paths and decorators are already in place).
2. **Real adapter HTTP.** Plug in `requests` calls in
   `afc_wallet/adapters/paystack.py`, `stripe.py`, `nowpayments.py`.
   Webhooks get signature-verified using the helpers already shipped.
3. **Celery scheduling.** Flesh out
   `afc_wager/tasks.py::lock_market_at_time` and `settle_market` task
   bodies so the daily scheduler closes locked markets.
4. **DRF pagination + filtering.** Markets list, txns list, withdrawals
   list — all need the standard AFC TanStack-table-friendly response
   envelope.
5. **Admin role wiring.** `afc_auth.UserRoles` gets new role rows
   `wager_admin`, `wallet_admin` (the spec lists them but they don't
   exist as `Roles` rows yet).

---

## Spec invariants enforced

After every settlement on the shared-fixture scenarios + 200 hypothesis
runs, ALL of these hold:

```
sum(WagerLine.stake_kobo where wager.market = M)  ==  M.total_pool_kobo
sum(Payout.amount_kobo) + RakeTxn.amount_kobo     ==  M.total_pool_kobo
all Payout.amount_kobo >= 0
0 <= dust_kobo <= len(winning_lines)
```

Sum-of-credits-equals-sum-of-debits (the global double-entry invariant)
is a property of the wallet ledger; a CI gate in Phase 2 will replay all
WalletTxns and assert `sum(amount_kobo) == 0`.
