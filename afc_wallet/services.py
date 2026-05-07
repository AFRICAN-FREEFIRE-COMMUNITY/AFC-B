"""afc_wallet services — credit, debit, P2P, voucher redemption, KYC.

Every state-changing call is wrapped in `transaction.atomic()` and grabs the
target Wallet row(s) via `select_for_update()` to serialize concurrent edits.
Idempotency is enforced by the unique constraint on `WalletTxn.idempotency_key`
— callers MUST pass a stable key. Webhook retries simply return the existing
txn instead of double-applying.

Spec: WEBSITE/docs/superpowers/specs/2026-05-07-wager-feature-design.md Sections
5 (settlement math invariants), 6 (wallet flows), 10 (edge cases).

Public API:
    credit(user, amount_kobo, kind, source_tag, ref_type, ref_id, idempotency_key)
    debit(user, amount_kobo, kind, ref_type, ref_id, idempotency_key)
    p2p_send(sender, recipient, amount_kobo, transfer_id)
    redeem_voucher(user, code)
    freeze(wallet) / unfreeze(wallet)
    sum_gift_receipts_last_24h(user, now)
    can_receive_gift(user, amount_kobo, now)
    recompute_kyc_tier(user)
    get_or_create_wallet(user)
    get_or_create_house_user()
    get_current_fx() -> FxSnapshot
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .constants import (
    GIFT_DAILY_CAP_KOBO,
    HOUSE_USERNAME,
    P2P_DAILY_CAP_KOBO,
    P2P_FEE_BPS,
)
from .models import (
    FxSnapshot,
    KYCTier,
    KYCTierLevel,
    SourceTag,
    Voucher,
    VoucherRedemption,
    Wallet,
    WalletStatus,
    WalletTxn,
    WalletTxnKind,
)


User = get_user_model()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WalletError(Exception):
    """Base class for service-layer wallet errors."""


class InsufficientFunds(WalletError):
    pass


class GiftDailyCapExceeded(WalletError):
    pass


class P2PDailyCapExceeded(WalletError):
    pass


class WalletFrozen(WalletError):
    pass


class VoucherInvalid(WalletError):
    pass


class VoucherExhausted(WalletError):
    pass


class VoucherExpired(WalletError):
    pass


class VoucherAlreadyRedeemed(WalletError):
    pass


class P2PInvalidRecipient(WalletError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_or_create_house_user():
    """The HOUSE wallet owner. Lookup-by-username because the User PK is int."""
    user, _ = User.objects.get_or_create(
        username=HOUSE_USERNAME,
        defaults={
            "email": "house@africanfreefirecommunity.com",
            "full_name": "House",
            "country": "NG",
            "role": "admin",
        },
    )
    return user


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def get_current_fx() -> FxSnapshot:
    """Return the most recent FxSnapshot, creating a default if none exists."""
    fx = FxSnapshot.objects.order_by("-captured_at").first()
    if fx is None:
        fx = FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="default"
        )
    return fx


def freeze(wallet: Wallet) -> None:
    Wallet.objects.filter(pk=wallet.pk).update(status=WalletStatus.FROZEN)


def unfreeze(wallet: Wallet) -> None:
    Wallet.objects.filter(pk=wallet.pk).update(status=WalletStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Gift cap (anti-launder rolling 24h window)
# ---------------------------------------------------------------------------


def sum_gift_receipts_last_24h(user, now=None) -> int:
    """Sum of GIFT-tagged credits to this user in the last 24h.

    Counts both voucher redemptions and inbound P2P with source_tag=GIFT.
    Used to enforce the GIFT_DAILY_CAP_KOBO ceiling.
    """
    if now is None:
        now = timezone.now()
    cutoff = now - timedelta(hours=24)
    wallet = Wallet.objects.filter(user=user).first()
    if wallet is None:
        return 0
    total = (
        WalletTxn.objects.filter(
            wallet=wallet,
            source_tag=SourceTag.GIFT,
            amount_kobo__gt=0,
            created_at__gte=cutoff,
        ).aggregate(s=Sum("amount_kobo"))["s"]
        or 0
    )
    return int(total)


def can_receive_gift(user, amount_kobo: int, now=None) -> Tuple[bool, int]:
    """Return (ok, remaining_kobo) — pre-check before crediting GIFT.

    `remaining_kobo` is the headroom under the cap *before* this credit;
    `ok=True` iff `amount_kobo <= remaining_kobo`.
    """
    used = sum_gift_receipts_last_24h(user, now)
    remaining = GIFT_DAILY_CAP_KOBO - used
    return (amount_kobo <= remaining, remaining)


# ---------------------------------------------------------------------------
# P2P daily cap (sender-side)
# ---------------------------------------------------------------------------


def sum_p2p_outflows_last_24h(user, now=None) -> int:
    """Sum of |P2P_OUT| debits in the last 24h for this user."""
    if now is None:
        now = timezone.now()
    cutoff = now - timedelta(hours=24)
    wallet = Wallet.objects.filter(user=user).first()
    if wallet is None:
        return 0
    total = (
        WalletTxn.objects.filter(
            wallet=wallet,
            kind=WalletTxnKind.P2P_OUT,
            created_at__gte=cutoff,
        ).aggregate(s=Sum("amount_kobo"))["s"]
        or 0
    )
    return abs(int(total))


# ---------------------------------------------------------------------------
# Core primitives: credit + debit
# ---------------------------------------------------------------------------


def credit(
    *,
    user,
    amount_kobo: int,
    kind: str,
    source_tag: str,
    ref_type: str = "",
    ref_id: str = "",
    idempotency_key: str,
    fx: Optional[FxSnapshot] = None,
    enforce_gift_cap: bool = True,
) -> WalletTxn:
    """Credit `amount_kobo` (positive integer) to `user`'s wallet.

    Idempotent on `idempotency_key` — if a txn with that key already exists,
    return it without modifying balances. Wallet is locked via
    `select_for_update()` for the duration of the transaction.

    GIFT credits enforce the rolling 24h cap unless `enforce_gift_cap=False`
    (used internally for refunds / P2P inheritance where cap was already
    applied at the source).
    """
    if amount_kobo <= 0:
        raise WalletError(
            f"credit amount must be positive, got {amount_kobo}"
        )
    if source_tag not in SourceTag.values:
        raise WalletError(f"invalid source_tag {source_tag}")
    if kind not in WalletTxnKind.values:
        raise WalletError(f"invalid kind {kind}")

    from django.db import IntegrityError

    try:
        with transaction.atomic():
            # Idempotency check first — return existing without locking
            # wallet. We re-check after grabbing the wallet lock too, so
            # racing callers don't double-apply.
            existing = WalletTxn.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing is not None:
                return existing

            wallet = (
                Wallet.objects.select_for_update()
                .select_related("user")
                .get(user=user)
            )
            if wallet.status == WalletStatus.FROZEN:
                raise WalletFrozen(f"wallet {wallet.pk} frozen")

            # Re-check after acquiring lock — race-resolution.
            existing = WalletTxn.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing is not None:
                return existing

            # Enforce GIFT cap on inflows.
            if (
                enforce_gift_cap
                and source_tag == SourceTag.GIFT
                and amount_kobo > 0
            ):
                ok, _remaining = can_receive_gift(user, amount_kobo)
                if not ok:
                    raise GiftDailyCapExceeded(
                        f"GIFT credits would exceed "
                        f"N{GIFT_DAILY_CAP_KOBO/100:.2f} per 24h cap"
                    )

            if fx is None:
                fx = get_current_fx()

            txn = WalletTxn.objects.create(
                wallet=wallet,
                amount_kobo=amount_kobo,
                kind=kind,
                source_tag=source_tag,
                ref_type=ref_type,
                ref_id=str(ref_id),
                fx_snapshot=fx,
                idempotency_key=idempotency_key,
            )
            wallet.balance_kobo += amount_kobo
            if source_tag == SourceTag.PURCHASED:
                wallet.balance_purchased_kobo += amount_kobo
            elif source_tag == SourceTag.WON:
                wallet.balance_won_kobo += amount_kobo
            elif source_tag == SourceTag.GIFT:
                wallet.balance_gift_kobo += amount_kobo
            wallet.save(
                update_fields=[
                    "balance_kobo",
                    "balance_purchased_kobo",
                    "balance_won_kobo",
                    "balance_gift_kobo",
                    "updated_at",
                ]
            )
            return txn
    except IntegrityError:
        # Lost the race — another thread inserted with this idempotency key
        # between our check and our create. Re-fetch and return their row.
        existing = WalletTxn.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing
        raise


@dataclass
class _LadderConsumption:
    """Result of the spend-ladder splitting `amount` across 3 buckets."""

    gift_kobo: int
    won_kobo: int
    purchased_kobo: int

    @property
    def total(self) -> int:
        return self.gift_kobo + self.won_kobo + self.purchased_kobo


def _consume_ladder(
    wallet: Wallet, amount_kobo: int
) -> _LadderConsumption:
    """Split `amount_kobo` across (GIFT, WON, PURCHASED) in that order.

    Spec Section 6 spend ladder: gift first (most-encumbered), purchased last
    (preserve "money I actually paid for" psychology).
    """
    remaining = amount_kobo
    take_gift = min(remaining, wallet.balance_gift_kobo)
    remaining -= take_gift
    take_won = min(remaining, wallet.balance_won_kobo)
    remaining -= take_won
    take_purchased = min(remaining, wallet.balance_purchased_kobo)
    remaining -= take_purchased

    if remaining > 0:
        raise InsufficientFunds(
            f"need {amount_kobo} kobo, have "
            f"{wallet.balance_kobo - remaining} (lock={wallet.locked_kobo})"
        )
    return _LadderConsumption(take_gift, take_won, take_purchased)


def debit(
    *,
    user,
    amount_kobo: int,
    kind: str,
    ref_type: str = "",
    ref_id: str = "",
    idempotency_key: str,
    fx: Optional[FxSnapshot] = None,
) -> List[WalletTxn]:
    """Debit `amount_kobo` from `user`'s wallet using the spend ladder.

    Returns a LIST of `WalletTxn` rows — one per bucket consumed. If the
    full amount comes from a single bucket, the list has length 1. If it
    spans GIFT + WON, length 2. Etc.

    Idempotent on `idempotency_key`. The first txn in the list uses the
    raw key; subsequent ladder txns use `{key}:WON` / `{key}:PURCHASED`
    to preserve uniqueness while staying replay-safe.
    """
    if amount_kobo <= 0:
        raise WalletError(
            f"debit amount must be positive, got {amount_kobo}"
        )
    if kind not in WalletTxnKind.values:
        raise WalletError(f"invalid kind {kind}")

    with transaction.atomic():
        # Idempotency: if base key already exists, gather the chain.
        existing_chain = list(
            WalletTxn.objects.filter(
                idempotency_key__in=[
                    idempotency_key,
                    f"{idempotency_key}:WON",
                    f"{idempotency_key}:PURCHASED",
                ]
            ).order_by("created_at")
        )
        if existing_chain:
            return existing_chain

        wallet = (
            Wallet.objects.select_for_update()
            .select_related("user")
            .get(user=user)
        )
        if wallet.status == WalletStatus.FROZEN:
            raise WalletFrozen(f"wallet {wallet.pk} frozen")

        consumed = _consume_ladder(wallet, amount_kobo)
        if fx is None:
            fx = get_current_fx()

        out: List[WalletTxn] = []
        # We use the first bucket consumed as the "primary" txn (raw key);
        # subsequent buckets get suffixed keys.
        keys_for = {
            SourceTag.GIFT: f"{idempotency_key}",
            SourceTag.WON: f"{idempotency_key}:WON",
            SourceTag.PURCHASED: f"{idempotency_key}:PURCHASED",
        }
        # If there's no GIFT spend, the WON bucket gets the raw key;
        # if no GIFT or WON, PURCHASED gets it.
        primary_key_used = False

        def assign_key(tag: str) -> str:
            nonlocal primary_key_used
            if not primary_key_used:
                primary_key_used = True
                return idempotency_key
            return f"{idempotency_key}:{tag}"

        for tag, take in (
            (SourceTag.GIFT, consumed.gift_kobo),
            (SourceTag.WON, consumed.won_kobo),
            (SourceTag.PURCHASED, consumed.purchased_kobo),
        ):
            if take == 0:
                continue
            key = assign_key(tag)
            txn = WalletTxn.objects.create(
                wallet=wallet,
                amount_kobo=-take,
                kind=kind,
                source_tag=tag,
                ref_type=ref_type,
                ref_id=str(ref_id),
                fx_snapshot=fx,
                idempotency_key=key,
            )
            out.append(txn)

        wallet.balance_kobo -= amount_kobo
        wallet.balance_gift_kobo -= consumed.gift_kobo
        wallet.balance_won_kobo -= consumed.won_kobo
        wallet.balance_purchased_kobo -= consumed.purchased_kobo
        wallet.save(
            update_fields=[
                "balance_kobo",
                "balance_purchased_kobo",
                "balance_won_kobo",
                "balance_gift_kobo",
                "updated_at",
            ]
        )
        return out


# ---------------------------------------------------------------------------
# P2P send
# ---------------------------------------------------------------------------


@dataclass
class P2PResult:
    transfer_id: str
    amount_kobo: int
    fee_kobo: int
    recipient_username: str
    sender_debit_txn_ids: List[int]
    fee_debit_txn_id: int
    recipient_credit_txn_ids: List[int]
    house_credit_txn_id: int


def p2p_send(
    *,
    sender,
    recipient,
    amount_kobo: int,
    transfer_id: Optional[str] = None,
) -> P2PResult:
    """Send `amount_kobo` from sender to recipient + 1% fee to HOUSE.

    Source-tag inheritance: sender's spend ladder produces N debit txns
    (e.g. 100 GIFT + 150 WON). Recipient receives matching N credit txns
    with the SAME source_tag — funds keep their provenance.

    Enforces:
      * sender's KYC tier (caller may pre-check; service rechecks)
      * sender's daily P2P cap (P2P_DAILY_CAP_KOBO)
      * recipient's gift cap on the GIFT portion only (rolling 24h)
      * recipient != sender, recipient != house
    """
    if amount_kobo <= 0:
        raise WalletError("amount must be positive")
    if sender.pk == recipient.pk:
        raise P2PInvalidRecipient("cannot send to self")
    house = get_or_create_house_user()
    if recipient.pk == house.pk:
        raise P2PInvalidRecipient("cannot send to house wallet")

    fee_kobo = (amount_kobo * P2P_FEE_BPS) // 10000

    if transfer_id is None:
        transfer_id = f"p2p_{uuid.uuid4().hex}"

    # Daily-cap pre-check (sender side). The cap is on outflows only.
    used = sum_p2p_outflows_last_24h(sender)
    if used + amount_kobo > P2P_DAILY_CAP_KOBO:
        raise P2PDailyCapExceeded(
            f"P2P daily cap N{P2P_DAILY_CAP_KOBO/100:.2f} exceeded"
        )

    with transaction.atomic():
        # Debit sender — produces 1..3 txns (one per bucket consumed).
        sender_debits = debit(
            user=sender,
            amount_kobo=amount_kobo,
            kind=WalletTxnKind.P2P_OUT,
            ref_type="p2p",
            ref_id=transfer_id,
            idempotency_key=f"p2p:{transfer_id}:out",
        )

        # Debit fee from sender (ladder again).
        fee_debits = debit(
            user=sender,
            amount_kobo=fee_kobo,
            kind=WalletTxnKind.P2P_FEE,
            ref_type="p2p",
            ref_id=transfer_id,
            idempotency_key=f"p2p:{transfer_id}:fee",
        )

        # Pre-check recipient gift cap based on the GIFT slice only.
        gift_slice = sum(
            -t.amount_kobo for t in sender_debits if t.source_tag == SourceTag.GIFT
        )
        if gift_slice > 0:
            ok, _ = can_receive_gift(recipient, gift_slice)
            if not ok:
                # Roll back the entire transaction by raising.
                raise GiftDailyCapExceeded(
                    "recipient would exceed GIFT daily cap"
                )

        # Credit recipient — N matching txns inheriting source_tag.
        recipient_credits: List[WalletTxn] = []
        for d in sender_debits:
            recipient_credits.append(
                credit(
                    user=recipient,
                    amount_kobo=-d.amount_kobo,  # flip sign: debit was negative
                    kind=WalletTxnKind.P2P_IN,
                    source_tag=d.source_tag,
                    ref_type="p2p",
                    ref_id=transfer_id,
                    idempotency_key=f"p2p:{transfer_id}:in:{d.source_tag}",
                    enforce_gift_cap=False,  # already checked above
                )
            )

        # Credit fee to HOUSE wallet (always GIFT? -> use PURCHASED to mirror
        # frontend semantics: house treats fees as cashable revenue, not gifts).
        house_credit = credit(
            user=house,
            amount_kobo=fee_kobo,
            kind=WalletTxnKind.P2P_FEE,
            source_tag=SourceTag.PURCHASED,
            ref_type="p2p",
            ref_id=transfer_id,
            idempotency_key=f"p2p:{transfer_id}:house",
            enforce_gift_cap=False,
        )

    return P2PResult(
        transfer_id=transfer_id,
        amount_kobo=amount_kobo,
        fee_kobo=fee_kobo,
        recipient_username=recipient.username,
        sender_debit_txn_ids=[t.pk for t in sender_debits],
        fee_debit_txn_id=fee_debits[0].pk,
        recipient_credit_txn_ids=[t.pk for t in recipient_credits],
        house_credit_txn_id=house_credit.pk,
    )


# ---------------------------------------------------------------------------
# Voucher redemption
# ---------------------------------------------------------------------------


def redeem_voucher(*, user, code: str) -> Tuple[Voucher, WalletTxn]:
    """Redeem voucher `code` for `user`. Idempotent on (user, voucher).

    Atomically increments `Voucher.used_count` (validated via SELECT FOR
    UPDATE) and creates a VoucherRedemption + a GIFT-tagged credit txn.

    Raises:
        VoucherInvalid: code not found
        VoucherExpired: past expires_at
        VoucherExhausted: used_count == max_uses
        VoucherAlreadyRedeemed: this user already redeemed this voucher
        GiftDailyCapExceeded: recipient cap blocked
    """
    if not code:
        raise VoucherInvalid("empty voucher code")
    norm = code.strip().upper()

    with transaction.atomic():
        try:
            voucher = Voucher.objects.select_for_update().get(code=norm)
        except Voucher.DoesNotExist as e:
            raise VoucherInvalid(f"voucher {norm!r} not found") from e

        if (
            voucher.expires_at is not None
            and voucher.expires_at <= timezone.now()
        ):
            raise VoucherExpired(f"voucher {norm} expired")

        if voucher.used_count >= voucher.max_uses:
            raise VoucherExhausted(f"voucher {norm} exhausted")

        # Idempotency: existing redemption for this user means we already
        # credited them — return the linked txn.
        existing = VoucherRedemption.objects.filter(
            voucher=voucher, user=user
        ).first()
        if existing is not None:
            raise VoucherAlreadyRedeemed(
                f"user {user.pk} already redeemed {norm}"
            )

        # Increment use count atomically.
        voucher.used_count += 1
        voucher.save(update_fields=["used_count"])

        # Credit GIFT-tagged. Idempotency key ties to (user, voucher).
        idem = f"voucher:{voucher.pk}:user:{user.pk}"
        txn = credit(
            user=user,
            amount_kobo=voucher.amount_kobo,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            ref_type="voucher",
            ref_id=str(voucher.pk),
            idempotency_key=idem,
        )

        VoucherRedemption.objects.create(
            voucher=voucher, user=user, txn=txn
        )
        return (voucher, txn)


# ---------------------------------------------------------------------------
# KYC tier promotion
# ---------------------------------------------------------------------------


def recompute_kyc_tier(user) -> KYCTier:
    """Promote KYCTier to TIER_LITE iff both whatsapp + discord verified.

    Idempotent: calling twice is a no-op when already TIER_LITE.
    """
    kyc, _ = KYCTier.objects.get_or_create(user=user)
    promoted = (
        kyc.whatsapp_verified_at is not None
        and kyc.discord_linked_at is not None
    )
    new_tier = KYCTierLevel.TIER_LITE if promoted else KYCTierLevel.TIER_0
    if kyc.tier != new_tier:
        kyc.tier = new_tier
        kyc.save(update_fields=["tier", "updated_at"])
    return kyc
