"""
afc_wager.payments - Paystack for stakes in, transfers out.

WHY PAYSTACK AND NOT A WALLET: the owner removed coins on 2026-09-18 (inbox #33). A stake is a
one-off charge for exactly the amount staked, and a withdrawal is a transfer from AFC's Paystack
balance to the player's verified bank account. Both reuse what the shop already has:
`afc_shop.paystack_payout._paystack` (the one REST helper with the secret key, never raises) and
the shop's webhook, which delegates `metadata.kind == "wager"` charge events here.

STAKES
    initialize_stake(user, wager)        -> checkout URL (Paystack transaction/initialize)
    verify_stake(reference)              -> (ok, charge_id, amount_kobo) from transaction/verify
    handle_charge_success(data)          -> called by the shop webhook; activates the wager
    Both the webhook and the browser's return (views_public.verify_payment) call
    services.activate_wager, which is idempotent, so whichever arrives first wins and the other
    is a no-op.

TRANSFERS
    resolve_bank_account(bank_code, number) -> (account_name, bank_name) from Paystack
    create_transfer_recipient(...)          -> RCP_ code
    submit_transfer(withdrawal)             -> (ok, {"transfer_code", "reference", "status"})
    handle_transfer_event(event, data)      -> transfer.success / transfer.failed / reversed

OUTBOX: under the test runner and on the scratch server this module calls nothing; the stake
checkout URL is a local placeholder and verify answers success, so a walk can complete a wager
without a card. `afc_auth.outbox.is_live()` is the switch, the same one email and WhatsApp use.
"""
import logging
import secrets

from django.conf import settings

from afc_auth import outbox
from afc_shop.paystack_payout import _paystack

logger = logging.getLogger(__name__)

STAKE_REFERENCE_PREFIX = "afcw_"


def new_reference():
    return f"{STAKE_REFERENCE_PREFIX}{secrets.token_hex(8)}"


def _frontend(request=None):
    """Frontend origin the player is sent back to, matched to the API host: a request that
    arrived on localhost gets FRONTEND_URL_LOCAL, the same rule as afc_auth's Discord and v-ent
    SSO bounces (_discord_frontend_origin). Without a request, production."""
    host = request.get_host() if request is not None else ""
    if "localhost" in host or "127.0.0.1" in host:
        return (getattr(settings, "FRONTEND_URL_LOCAL", "") or "").rstrip("/")
    return (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")


def stake_callback_url(wager, request=None):
    """Where Paystack sends the player after paying: the market page, which then calls
    verify_payment with the reference."""
    return f"{_frontend(request)}/wagers/{wager.market.slug}?paid={wager.paystack_reference}"


def initialize_stake(user, wager, request=None):
    """POST transaction/initialize for exactly the stake. Returns the authorization URL.
    Raises services.WagerError(`payment_init_failed`) when Paystack refuses."""
    from .services import WagerError

    if not outbox.is_live():
        outbox.record("paystack", user.email, "stake", f"initialize {wager.paystack_reference} {wager.total_stake_kobo}")
        return f"{_frontend(request)}/wagers/{wager.market.slug}?paid={wager.paystack_reference}&mock=1"
    ok, body = _paystack("POST", "/transaction/initialize", {
        "email": user.email,
        "amount": int(wager.total_stake_kobo),
        "currency": "NGN",
        "reference": wager.paystack_reference,
        "callback_url": stake_callback_url(wager, request),
        "metadata": {
            "kind": "wager",
            "wager_token": wager.public_token,
            "market": wager.market.slug,
            "user_id": user.pk,
        },
    })
    if not ok:
        logger.warning("wager stake init refused for %s: %s", wager.paystack_reference, body.get("message"))
        raise WagerError("payment_init_failed", "We could not start the payment. Try again shortly.", 502)
    url = (body.get("data") or {}).get("authorization_url") or ""
    if not url:
        raise WagerError("payment_init_failed", "We could not start the payment. Try again shortly.", 502)
    return url


def verify_stake(reference):
    """GET transaction/verify/<reference>. Returns (ok, charge_id, amount_kobo)."""
    if not outbox.is_live():
        return True, "mock-charge", None
    ok, body = _paystack("GET", f"/transaction/verify/{reference}")
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    if not ok or data.get("status") != "success":
        return False, "", None
    return True, str(data.get("id") or ""), int(data.get("amount") or 0)


def handle_charge_success(data):
    """The shop's webhook hands over a charge.success whose metadata.kind is "wager". Returns
    True when a wager was found (activated or already active), False otherwise."""
    from .models import Wager
    from .services import activate_wager

    reference = str(data.get("reference") or "")
    wager = Wager.objects.filter(paystack_reference=reference).select_related("market").first()
    if wager is None:
        logger.warning("wager webhook: no wager for reference %s", reference)
        return False
    amount = int(data.get("amount") or 0)
    if amount and amount != wager.total_stake_kobo:
        # A charge for a different amount is not this stake. Leave it for a human: it is logged
        # with both numbers and the wager stays unpaid.
        logger.error("wager webhook: amount mismatch on %s: charged %s, stake %s",
                     reference, amount, wager.total_stake_kobo)
        return False
    activate_wager(wager, charge_id=str(data.get("id") or ""))
    return True


# ── transfers out ───────────────────────────────────────────────────────────────────────────────
def resolve_bank_account(bank_code, account_number):
    """Paystack tells us whose account this is. Returns (account_name, bank_name)."""
    from .services import WagerError

    if not outbox.is_live():
        return "MOCK ACCOUNT HOLDER", "Mock Bank"
    ok, body = _paystack("GET", f"/bank/resolve?account_number={account_number}&bank_code={bank_code}")
    if not ok:
        raise WagerError("bank_account_unresolved", "We could not confirm that account with the bank.", 400)
    name = ((body.get("data") or {}).get("account_name") or "").strip()
    bank_name = _bank_name(bank_code)
    if not name:
        raise WagerError("bank_account_unresolved", "We could not confirm that account with the bank.", 400)
    return name, bank_name


def _bank_name(bank_code):
    ok, body = _paystack("GET", "/bank?currency=NGN&perPage=200")
    if ok:
        for b in body.get("data") or []:
            if str(b.get("code")) == str(bank_code):
                return str(b.get("name") or "")[:80]
    return ""


def list_banks():
    if not outbox.is_live():
        return [{"code": "058", "name": "Mock Bank"}, {"code": "044", "name": "Mock Access"}]
    ok, body = _paystack("GET", "/bank?currency=NGN&perPage=200")
    if not ok:
        return []
    return [{"code": str(b.get("code")), "name": str(b.get("name") or "")} for b in body.get("data") or []]


def create_transfer_recipient(account_name, account_number, bank_code):
    from .services import WagerError

    if not outbox.is_live():
        return f"RCP_mock_{account_number[-4:]}"
    ok, body = _paystack("POST", "/transferrecipient", {
        "type": "nuban", "name": account_name, "account_number": account_number,
        "bank_code": bank_code, "currency": "NGN",
    })
    if not ok:
        raise WagerError("recipient_failed", "We could not register that bank account for payouts.", 502)
    return ((body.get("data") or {}).get("recipient_code") or "")[:40]


def submit_transfer(withdrawal):
    """POST /transfer for the withdrawal. Returns (ok, detail dict or message)."""
    if not outbox.is_live():
        outbox.record("paystack", withdrawal.user.email, "transfer",
                      f"transfer {withdrawal.public_token} {withdrawal.amount_kobo}")
        return True, {"transfer_code": f"TRF_mock_{withdrawal.public_token}", "reference": withdrawal.public_token,
                      "status": "success"}
    ok, body = _paystack("POST", "/transfer", {
        "source": "balance",
        "amount": int(withdrawal.amount_kobo),
        "recipient": withdrawal.bank_account.recipient_code,
        "reason": "AFC winnings withdrawal",
        "reference": withdrawal.public_token,
    })
    if not ok:
        return False, body.get("message") or "transfer refused"
    data = body.get("data") or {}
    return True, {"transfer_code": str(data.get("transfer_code") or ""), "reference": str(data.get("reference") or ""),
                  "status": str(data.get("status") or "")}


def handle_transfer_event(event, data):
    """transfer.success / transfer.failed / transfer.reversed from the shop's webhook, matched
    by our reference (the withdrawal's public token)."""
    from .models import Withdrawal
    from .services import mark_withdrawal_failed, mark_withdrawal_paid

    reference = str(data.get("reference") or "")
    wd = Withdrawal.objects.filter(public_token=reference).first()
    if wd is None:
        return False
    if event == "transfer.success":
        mark_withdrawal_paid(wd)
    elif event in ("transfer.failed", "transfer.reversed"):
        mark_withdrawal_failed(wd, data.get("reason") or event)
    return True
