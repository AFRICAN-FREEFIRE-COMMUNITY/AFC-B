"""Shared money + business constants for the AFC wager + wallet feature.

Mirrors `frontend/lib/utils.ts` 1:1. Any drift here breaks parity tests.
"""

# Money math
KOBO_PER_COIN = 50_000  # 1 coin = N500 = 50,000 kobo
COIN_NGN = 500  # 1 coin = N500
KOBO_PER_NGN = 100  # 1 NGN = 100 kobo

# Minimum amounts
MIN_DEPOSIT_KOBO = 50_000  # N500 = 1 coin
MIN_WAGER_KOBO = 10_000  # N100 = 0.2 coins
MIN_WITHDRAW_KOBO = 250_000  # N2,500

# House identity
# The system "house" wallet receives rake + cancel fees + P2P fees.
# Resolved at runtime via `username = HOUSE_USERNAME` since User PK is an int.
HOUSE_USERNAME = "house"
HOUSE_USER_ID = "house"  # str alias; matches frontend lib/utils.ts

# Rake / fees (basis points, 10000 = 100%)
RAKE_BPS = 500  # 5% — Decision 6
CANCEL_FEE_BPS = 100  # 1% — Decision 10
P2P_FEE_BPS = 100  # 1%

# Daily caps
P2P_DAILY_CAP_KOBO = 2_500_000_000  # N25M
GIFT_DAILY_CAP_KOBO = 10_000_000  # N100,000 — anti-launder gift receipt cap


# Voucher rules
VOUCHER_MAX_AMOUNT_KOBO = 10_000_000  # N100,000 single voucher cap (matches gift cap)


# 2-admin co-sign threshold
COSIGN_THRESHOLD_KOBO = 500_000_000  # N5M
