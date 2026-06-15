"""
afc_shop/emails.py
================================================================================
Buyer-facing transactional emails for the MARKETPLACE FULFILMENT lifecycle
(Phase A, spec: WEBSITE/tasks/marketplace-design.md). Three branded emails sent to
the buyer as a vendor order moves through the state machine:

  - order_received_email  -> "We received your order"   (sent on PAID, by
                             afc_shop.fulfilment.notify_order_paid)
  - order_shipped_email   -> "Your order is on the way"  (sent on the shipped
                             transition, by afc_shop.fulfilment.vendor_mark_shipped)
  - order_completed_email -> "Your order is complete"    (sent on the completed
                             transition, by afc_shop.fulfilment.order_mark_completed)

WHY A SEPARATE MODULE
  The afc_auth email builders (_email_shell + email_verification_code/_welcome/...)
  cover ACCOUNT mail. Shop fulfilment mail lives here so the shop owns its own copy
  while still riding the SAME branded shell (one consistent AFC look across every
  email the user receives). We REUSE, never re-implement, the shell.

HOW IT CONNECTS
  - `_email_shell` (the dark branded wrapper) + `send_email` (the Office365 SMTP
    sender) are imported from afc_auth.views, the exact same path the account
    emails use. send_email(to, subject, html) builds the MIME message and sends it;
    a mail failure returns False and is swallowed by the caller (mail must never
    block an order transition).
  - Called only from afc_shop/fulfilment.py (the state machine). Each builder takes
    a paid `Order` and renders the order summary + delivery info from the order's
    snapshot fields (first_name/last_name/email/address/... and order.items).
  - EMAIL-SAFE HTML ONLY (tables + inline styles), matching the afc_auth builders,
    so it renders consistently across mail clients.

COPY RULE: no em/en dashes anywhere in buyer-facing copy (AFC hard rule); use
commas, periods, or a spaced hyphen.
"""

from afc_auth.views import _email_shell, send_email, SITE_URL


# ── Small render helpers (shared by the three builders below) ──────────────────
def _order_items_rows(order):
    """Render the order's line items as email-safe table rows.

    Reads `order.items` (OrderItem rows) and uses each item's snapshot fields
    (product_name_snapshot / variant_title_snapshot / quantity / line_total) so the
    email shows exactly what was bought, even if the product later changes. Returns
    an HTML string of <tr> rows to drop into the summary table."""
    rows = ""
    for item in order.items.all():
        # Variant title is optional; only append it when present, in parentheses.
        name = item.product_name_snapshot
        if item.variant_title_snapshot:
            name = f"{name} ({item.variant_title_snapshot})"
        rows += f"""
      <tr>
        <td style="padding:8px 0;font-size:14px;color:#cdd6cf;border-bottom:1px solid #1d2a22;">{name}</td>
        <td style="padding:8px 0;font-size:14px;color:#8b988f;border-bottom:1px solid #1d2a22;" align="center">x{item.quantity}</td>
        <td style="padding:8px 0;font-size:14px;color:#e8efe9;border-bottom:1px solid #1d2a22;" align="right">{item.line_total}</td>
      </tr>"""
    return rows


def _delivery_block(order):
    """Render the buyer's delivery details as an email-safe block.

    Reads the delivery snapshot the checkout stored on the Order
    (first_name/last_name/address/city/state/postcode/phone_number). Returns an
    HTML string. These are the SAME fields a vendor sees when fulfilling, so the
    buyer can confirm where their order is going."""
    full_name = f"{order.first_name} {order.last_name}".strip()
    # Build the address line from whatever parts are present (some may be blank).
    parts = [p for p in [order.address, order.city, order.state, order.postcode] if p]
    address_line = ", ".join(parts)
    return f"""
    <div style="font-size:13px;line-height:1.7;color:#aab5ae;">
      <div style="color:#e8efe9;font-weight:600;">{full_name}</div>
      <div>{address_line}</div>
      <div>{order.phone_number}</div>
    </div>"""


def _summary_table(order):
    """Render the full order-summary card (items table + totals + delivery), shared
    by all three emails so the buyer always sees the same recap. Reads the order's
    monetary fields (subtotal/discount_total/tax/total) plus the helpers above."""
    items = _order_items_rows(order)
    delivery = _delivery_block(order)
    # Only show the discount row when there actually was one (keeps the recap clean).
    discount_row = ""
    if order.discount_total and order.discount_total > 0:
        discount_row = f"""
      <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">Discount</td>
          <td style="padding:4px 0;font-size:14px;color:#34d27b;" align="right">- {order.discount_total}</td></tr>"""
    return f"""
  <tr><td style="padding:8px 44px 4px;">
    <div style="background:#0a120d;border:1px solid #1d2a22;border-radius:12px;padding:20px 22px;">
      <div style="font-size:13px;letter-spacing:1px;text-transform:uppercase;color:#7c8c83;margin-bottom:10px;">Order #{order.id}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {items}
      </table>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">
        <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">Subtotal</td>
            <td style="padding:4px 0;font-size:14px;color:#cdd6cf;" align="right">{order.subtotal}</td></tr>
        {discount_row}
        <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">Tax</td>
            <td style="padding:4px 0;font-size:14px;color:#cdd6cf;" align="right">{order.tax}</td></tr>
        <tr><td style="padding:8px 0 0;font-size:16px;font-weight:700;color:#ffffff;" colspan="2">Total</td>
            <td style="padding:8px 0 0;font-size:16px;font-weight:700;color:#34d27b;" align="right">{order.total}</td></tr>
      </table>
    </div>
  </td></tr>
  <tr><td style="padding:14px 44px 4px;">
    <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#7c8c83;margin-bottom:6px;">Delivery to</div>
    {delivery}
  </td></tr>"""


# ── 1. Order received (sent on PAID) ───────────────────────────────────────────
def order_received_email(order):
    """Build the "we received your order" HTML body (green accent). Sent by
    afc_shop.fulfilment.notify_order_paid the moment a vendor order is paid, so the
    buyer knows AFC has the order and the vendor is preparing it."""
    buyer = order.first_name or order.user.username
    inner = f"""
  <tr><td style="padding:38px 44px 6px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">We received your order</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">Hi <span style="color:#e8efe9;font-weight:600;">{buyer}</span>, thank you for your purchase. Your payment is confirmed and the seller is preparing your order. We will email you again when it ships.</div>
  </td></tr>
  {_summary_table(order)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">You can track this order any time at <a href="{SITE_URL}/orders" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/orders</a>.</div>
  </td></tr>"""
    return _email_shell(inner, "green")


# ── 2. Order shipped (sent on the shipped transition) ──────────────────────────
def order_shipped_email(order):
    """Build the "your order is on the way" HTML body (green accent). Sent by
    afc_shop.fulfilment.vendor_mark_shipped when the vendor dispatches the order;
    includes the vendor-picked ship date when one was set."""
    buyer = order.first_name or order.user.username
    # ship_date is optional (a vendor may ship without having set a date); only show
    # the line when present.
    ship_line = ""
    if order.ship_date:
        ship_line = f"""
    <div style="font-size:14px;color:#cdd6cf;margin-top:10px;">Estimated ship date: <span style="color:#e8efe9;font-weight:600;">{order.ship_date.strftime('%d %b %Y')}</span></div>"""
    inner = f"""
  <tr><td style="padding:38px 44px 6px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">Your order is on the way</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">Good news, <span style="color:#e8efe9;font-weight:600;">{buyer}</span>. Your order has been shipped and is heading to you.</div>
    {ship_line}
  </td></tr>
  {_summary_table(order)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">Questions about delivery? Reach us at <a href="{SITE_URL}/contact" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/contact</a>.</div>
  </td></tr>"""
    return _email_shell(inner, "green")


# ── 3. Order completed (sent on the completed transition) ──────────────────────
def order_completed_email(order):
    """Build the "your order is complete" HTML body (green accent). Sent by
    afc_shop.fulfilment.order_mark_completed when the order is closed out as
    delivered."""
    buyer = order.first_name or order.user.username
    inner = f"""
  <tr><td style="padding:40px 44px 6px;text-align:center;">
    <div style="width:64px;height:64px;line-height:64px;border-radius:50%;background:#0a120d;border:1px solid #2c7a4d;margin:0 auto;font-size:30px;color:#34d27b;">&#10003;</div>
    <div style="font-size:21px;font-weight:700;color:#ffffff;margin-top:18px;">Your order is complete</div>
    <div style="font-size:15px;line-height:1.65;color:#aab5ae;margin-top:12px;">Thank you, <span style="color:#e8efe9;font-weight:600;">{buyer}</span>. Your order has been delivered and is now complete. We hope you enjoy it.</div>
  </td></tr>
  {_summary_table(order)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">Shop again any time at <a href="{SITE_URL}/shop" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/shop</a>.</div>
  </td></tr>"""
    return _email_shell(inner, "green")


# ── Convenience senders (subject + recipient + best-effort send) ───────────────
# The fulfilment state machine calls these so it never has to know SMTP details.
# Each returns the send_email result (True/False) but the caller ignores it: a mail
# failure must NEVER block an order transition (mirrors the afc_auth best-effort
# pattern). The recipient is the order's delivery email, falling back to the buyer
# account email.

def _recipient(order):
    """The address an order email goes to: the checkout delivery email, falling
    back to the buyer's account email."""
    return order.email or order.user.email


def _order_language(order):
    """The buyer's preferred locale for this order's emails ("en"/"fr"/"pt").

    i18n (owner 2026-06-15): reads the buyer account's User.language and falls back to "en" when it
    is blank/missing. send_email (afc_auth.views) uses it to localize the subject + body, so a French
    or Portuguese buyer gets their order mail in their own language. Guarded so a missing user.language
    can never break sending."""
    try:
        return (getattr(order.user, "language", "") or "en")
    except Exception:
        return "en"


def send_order_received(order):
    """Send the order-received email for `order`. Called by notify_order_paid."""
    return send_email(_recipient(order), "We received your order", order_received_email(order), language=_order_language(order))


def send_order_shipped(order):
    """Send the order-shipped email for `order`. Called by vendor_mark_shipped."""
    return send_email(_recipient(order), "Your order is on the way", order_shipped_email(order), language=_order_language(order))


def send_order_completed(order):
    """Send the order-completed email for `order`. Called by order_mark_completed."""
    return send_email(_recipient(order), "Your order is complete", order_completed_email(order), language=_order_language(order))
