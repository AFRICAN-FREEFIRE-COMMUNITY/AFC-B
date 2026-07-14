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
# HAND-AUTHORED per-language copy (en/fr/pt) for the order emails, so a French / Portuguese buyer
# gets natural sentences WITHOUT depending on the DeepL engine (owner 2026-07-13). copy_for returns
# the localized body dict; subject_for returns the localized subject. Callers send prelocalized=True.
from afc_auth.email_i18n import copy_for, subject_for


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


def _delivery_block(order, lang="en"):
    """Render the buyer's delivery details as an email-safe block.

    Reads the delivery snapshot the checkout stored on the Order
    (first_name/last_name/address/city/state/postcode/phone_number). Returns an
    HTML string. These are the SAME fields a vendor sees when fulfilling, so the
    buyer can confirm where their order is going. `lang` is unused here (all values
    are the buyer's own data) but kept for signature symmetry with _summary_table."""
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


def _summary_table(order, lang="en"):
    """Render the full order-summary card (items table + totals + delivery), shared
    by all three emails so the buyer always sees the same recap. Reads the order's
    monetary fields (subtotal/discount_total/tax/total) plus the helpers above.

    i18n (owner 2026-07-13): the static labels (Order #, Subtotal, Discount, Tax, Total,
    Delivery to) come from the hand-authored catalog (template "order_summary") in `lang`.
    The numbers + the buyer's own data are locale-neutral and pass through unchanged."""
    lbl = copy_for("order_summary", lang)
    items = _order_items_rows(order)
    delivery = _delivery_block(order, lang)
    # Only show the discount row when there actually was one (keeps the recap clean).
    discount_row = ""
    if order.discount_total and order.discount_total > 0:
        discount_row = f"""
      <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">{lbl["discount"]}</td>
          <td style="padding:4px 0;font-size:14px;color:#34d27b;" align="right">- {order.discount_total}</td></tr>"""
    return f"""
  <tr><td style="padding:8px 44px 4px;">
    <div style="background:#0a120d;border:1px solid #1d2a22;border-radius:12px;padding:20px 22px;">
      <div style="font-size:13px;letter-spacing:1px;text-transform:uppercase;color:#7c8c83;margin-bottom:10px;">{lbl["order_no"].format(id=order.id)}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {items}
      </table>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">
        <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">{lbl["subtotal"]}</td>
            <td style="padding:4px 0;font-size:14px;color:#cdd6cf;" align="right">{order.subtotal}</td></tr>
        {discount_row}
        <tr><td style="padding:4px 0;font-size:14px;color:#8b988f;" colspan="2">{lbl["tax"]}</td>
            <td style="padding:4px 0;font-size:14px;color:#cdd6cf;" align="right">{order.tax}</td></tr>
        <tr><td style="padding:8px 0 0;font-size:16px;font-weight:700;color:#ffffff;" colspan="2">{lbl["total"]}</td>
            <td style="padding:8px 0 0;font-size:16px;font-weight:700;color:#34d27b;" align="right">{order.total}</td></tr>
      </table>
    </div>
  </td></tr>
  <tr><td style="padding:14px 44px 4px;">
    <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#7c8c83;margin-bottom:6px;">{lbl["delivery_to"]}</div>
    {delivery}
  </td></tr>"""


# ── 1. Order received (sent on PAID) ───────────────────────────────────────────
def order_received_email(order, lang="en"):
    """Build the "we received your order" HTML body (green accent). Sent by
    afc_shop.fulfilment.notify_order_paid the moment a vendor order is paid, so the
    buyer knows AFC has the order and the vendor is preparing it. Copy from the
    hand-authored catalog (template "order_received") in `lang`."""
    c = copy_for("order_received", lang)
    buyer = order.first_name or order.user.username
    buyer_html = f'<span style="color:#e8efe9;font-weight:600;">{buyer}</span>'
    track_link = f'<a href="{SITE_URL}/orders" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/orders</a>'
    inner = f"""
  <tr><td style="padding:38px 44px 6px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{c["heading"]}</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">{c["intro"].format(buyer=buyer_html)}</div>
  </td></tr>
  {_summary_table(order, lang)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">{c["track"].format(link=track_link)}</div>
  </td></tr>"""
    return _email_shell(inner, "green")


# ── 2. Order shipped (sent on the shipped transition) ──────────────────────────
def order_shipped_email(order, lang="en"):
    """Build the "your order is on the way" HTML body (green accent). Sent by
    afc_shop.fulfilment.vendor_mark_shipped when the vendor dispatches the order;
    includes the vendor-picked ship date when one was set. Copy from the hand-authored
    catalog (template "order_shipped") in `lang`."""
    c = copy_for("order_shipped", lang)
    buyer = order.first_name or order.user.username
    buyer_html = f'<span style="color:#e8efe9;font-weight:600;">{buyer}</span>'
    contact_link = f'<a href="{SITE_URL}/contact" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/contact</a>'
    # ship_date is optional (a vendor may ship without having set a date); only show
    # the line when present.
    ship_line = ""
    if order.ship_date:
        ship_line = f"""
    <div style="font-size:14px;color:#cdd6cf;margin-top:10px;">{c["ship_label"]} <span style="color:#e8efe9;font-weight:600;">{order.ship_date.strftime('%d %b %Y')}</span></div>"""
    inner = f"""
  <tr><td style="padding:38px 44px 6px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">{c["heading"]}</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">{c["intro"].format(buyer=buyer_html)}</div>
    {ship_line}
  </td></tr>
  {_summary_table(order, lang)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">{c["questions"].format(link=contact_link)}</div>
  </td></tr>"""
    return _email_shell(inner, "green")


# ── 3. Order completed (sent on the completed transition) ──────────────────────
def order_completed_email(order, lang="en"):
    """Build the "your order is complete" HTML body (green accent). Sent by
    afc_shop.fulfilment.order_mark_completed when the order is closed out as
    delivered. Copy from the hand-authored catalog (template "order_completed") in `lang`."""
    c = copy_for("order_completed", lang)
    buyer = order.first_name or order.user.username
    buyer_html = f'<span style="color:#e8efe9;font-weight:600;">{buyer}</span>'
    shop_link = f'<a href="{SITE_URL}/shop" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com/shop</a>'
    inner = f"""
  <tr><td style="padding:40px 44px 6px;text-align:center;">
    <div style="width:64px;height:64px;line-height:64px;border-radius:50%;background:#0a120d;border:1px solid #2c7a4d;margin:0 auto;font-size:30px;color:#34d27b;">&#10003;</div>
    <div style="font-size:21px;font-weight:700;color:#ffffff;margin-top:18px;">{c["heading"]}</div>
    <div style="font-size:15px;line-height:1.65;color:#aab5ae;margin-top:12px;">{c["intro"].format(buyer=buyer_html)}</div>
  </td></tr>
  {_summary_table(order, lang)}
  <tr><td style="padding:18px 44px 26px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">{c["shop_again"].format(link=shop_link)}</div>
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
    """Send the order-received email for `order`. Called by notify_order_paid.

    i18n (owner 2026-07-13): subject + body come from the hand-authored catalog in the buyer's
    language, so we send prelocalized=True (send_email skips the machine-translation pass)."""
    lang = _order_language(order)
    return send_email(_recipient(order), subject_for("order_received", lang), order_received_email(order, lang), language=lang, prelocalized=True)


def send_order_shipped(order):
    """Send the order-shipped email for `order`. Called by vendor_mark_shipped."""
    lang = _order_language(order)
    return send_email(_recipient(order), subject_for("order_shipped", lang), order_shipped_email(order, lang), language=lang, prelocalized=True)


def send_order_completed(order):
    """Send the order-completed email for `order`. Called by order_mark_completed."""
    lang = _order_language(order)
    return send_email(_recipient(order), subject_for("order_completed", lang), order_completed_email(order, lang), language=lang, prelocalized=True)
