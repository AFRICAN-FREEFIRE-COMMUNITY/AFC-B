from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.utils.text import slugify

# Create your models here.


# ─────────────────────────────────────────────────────────────────────────────
# Category — admin-managed product categories (generalisation of the shop).
#
# WHY this exists:
#   The shop started life selling only Free Fire diamonds. The `Product` model
#   already carried a free-form `product_type` CharField, but the *set* of
#   categories was hard-coded (backend choices + a frontend `shopProductTypes`
#   constant). To sell arbitrary physical goods (thumbsleeves, phone coolers,
#   gaming phones, jerseys) the admin needs to ADD / EDIT / REMOVE categories
#   without a code change. This table makes categories real data.
#
# HOW it connects:
#   - `Product.category` (FK below) points here. `Product.product_type` is kept
#     for backward-compat (existing diamond rows + the legacy frontend constant).
#   - Admin CRUD: create_category / edit_category / delete_category in views.py
#     (consumed by the admin "Manage Categories" UI on /a/shop/inventory).
#   - User shop category tabs come from `view_active_categories` (public), which
#     returns only `is_active=True` rows (consumed by ShopClient.tsx tabs).
#   - `is_physical` drives copy/icon choices on the frontend (physical goods show
#     shipping copy; digital topups like diamonds deliver to the game UID).
# ─────────────────────────────────────────────────────────────────────────────
class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    # slug is the stable machine value the frontend filters products by; it also
    # maps onto the legacy `Product.product_type` string for back-compat.
    slug = models.SlugField(max_length=90, unique=True, blank=True, db_index=True)

    description = models.TextField(blank=True)

    # Physical goods require shipping (address collected at checkout); digital
    # goods (diamonds) deliver to the game UID. Drives frontend copy + icons.
    is_physical = models.BooleanField(default=True)

    # Controls visibility on the user shop. Inactive categories stay in the DB
    # (so existing products keep their category) but are hidden from the tabs.
    is_active = models.BooleanField(default=True)

    # Display order of the category tabs on the user shop (lower = first).
    ordering = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Tabs render in `ordering` then alphabetical name, matching the admin list.
        ordering = ["ordering", "name"]
        verbose_name_plural = "Categories"

    def save(self, *args, **kwargs):
        # Auto-generate a unique slug from the name when one is not supplied,
        # mirroring the Coupon.save() slug pattern already used in this file.
        if not self.slug:
            base = slugify(self.name)
            slug = base
            i = 2
            while Category.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────────────────
# Vendor — an AFC-invited third-party seller in the marketplace.
#
# WHY this exists (marketplace Phase A, spec: WEBSITE/tasks/marketplace-design.md):
#   The shop is becoming a multi-vendor marketplace. A Vendor is a partner who
#   lists physical/design products; AFC collects the buyer's money and the vendor
#   fulfils the order (acknowledge -> ship-date -> shipped + evidence). This row
#   is the seller's identity + contact channels + (later) payout account.
#
# INVITE-ONLY (owner decision 2026-06-09): there is NO public "Sell on AFC"
#   application and NO pending/approval state on the Vendor itself. An AFC admin
#   CREATES a Vendor directly and links it to an existing User login (the same way
#   sponsors/organizers are granted). `status` only ever flips active<->suspended;
#   it is never "pending". (Each submitted PRODUCT is still approved separately in
#   a later phase, but the Vendor account itself is granted, not applied for.)
#
# HOW it connects:
#   - `user` FK -> afc_auth.User: the partner's login. The fulfilment endpoints in
#     afc_shop/fulfilment.py gate each order transition on
#     `order.items -> variant.product.vendor.user == caller` (the vendor) OR an
#     AFC admin, so a vendor can only act on their own orders.
#   - `Product.vendor` FK (below) links each product a vendor sells back here.
#     Existing AFC/diamond products have vendor=None (first-party AFC stock).
#   - `whatsapp_number` + `stripe_account_id` are seams for the SEPARATE follow-ups
#     (Kapso WhatsApp fulfilment channel; Stripe Connect payouts in Phase B3); they
#     are stored now so the model does not need re-migrating when those land.
#   - `created_by` FK -> the admin who granted access (audit trail).
# ─────────────────────────────────────────────────────────────────────────────
class Vendor(models.Model):
    STATUS = (
        ("active", "Active"),        # vendor can sell + fulfil
        ("suspended", "Suspended"), # access revoked by an admin (no new orders)
    )

    # The partner's login. CASCADE: if the underlying User is deleted, the vendor
    # identity goes with it (a vendor cannot exist without a login to act as).
    user = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.CASCADE,
        related_name="vendor_accounts",
    )

    display_name = models.CharField(max_length=120)
    # Where buyer/fulfilment notifications about THIS vendor's orders would CC, and
    # where the vendor receives the "you have a new order" email (notify_vendor).
    contact_email = models.EmailField(blank=True)
    # Kapso WhatsApp destination for the SEPARATE WhatsApp send follow-up. Stored
    # now (blank for vendors without WhatsApp) so notify_vendor can plug in later.
    whatsapp_number = models.CharField(max_length=30, blank=True)

    status = models.CharField(max_length=20, choices=STATUS, default="active")

    # ── Payout rail (which provider AFC uses to pay THIS vendor out, Phase B3) ──────
    # AFC's marketplace vendors are MAJORITY AFRICAN, and Stripe Connect does NOT pay
    # out to Nigerian / most-African bank accounts. PAYSTACK does (Nigeria/Ghana/SA/
    # Kenya) and the shop already CHARGES buyers via Paystack, so PAYSTACK TRANSFERS is
    # the PRIMARY/DEFAULT payout rail. Stripe Connect stays only for the non-African
    # vendors Stripe can actually reach. fulfilment.order_mark_completed reads this to
    # route a completed order's payout to the right settle function (provider-aware):
    #   - "paystack" -> afc_shop/paystack_payout.settle_order_payout_paystack(order)
    #   - "stripe"   -> afc_shop/connect.settle_order_payout(order)
    PAYOUT_PROVIDER = (
        ("paystack", "Paystack Transfers"),  # default: African/Nigerian vendors + local bank
        ("stripe", "Stripe Connect"),        # non-African vendors only (Stripe can't reach NGN banks)
    )
    payout_provider = models.CharField(
        max_length=20, choices=PAYOUT_PROVIDER, default="paystack"
    )

    # ── Paystack Transfers bank details (the PRIMARY rail; African vendors) ─────────
    # The vendor's LOCAL bank account AFC transfers their share to via Paystack
    # Transfers. Set by afc_shop/paystack_payout.vendor_save_bank: the vendor picks a
    # bank (bank_code) from list_banks, enters their account_number, AFC resolves the
    # account_name via Paystack /bank/resolve (so the vendor confirms it is correct),
    # then creates a Paystack Transfer RECIPIENT (paystack_recipient_code) used as the
    # transfer destination at payout time. All blank until the vendor saves their bank.
    bank_code = models.CharField(max_length=20, blank=True)      # Paystack bank code (e.g. "058")
    bank_name = models.CharField(max_length=120, blank=True)     # human-readable bank name (display)
    account_number = models.CharField(max_length=20, blank=True) # the vendor's account number (NUBAN)
    account_name = models.CharField(max_length=120, blank=True)  # resolved holder name (from Paystack)
    # The Paystack Transfer Recipient code (RCP_...) created from the bank details above.
    # This is the `recipient` passed to POST /transfer at payout time (the Paystack
    # equivalent of Stripe Connect's stripe_account_id below).
    paystack_recipient_code = models.CharField(max_length=120, blank=True)

    # Stripe Connect account id, set during the Stripe payout onboarding (NON-AFRICAN
    # vendors only; Stripe cannot pay out to NGN/most-African banks). Blank for the
    # Paystack-default vendors. afc_shop/connect.py writes/reads this.
    stripe_account_id = models.CharField(max_length=120, blank=True)

    # The admin who granted this vendor access. SET_NULL so removing an admin user
    # never deletes the vendor record (preserves the audit trail).
    created_by = models.ForeignKey(
        "afc_auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vendors_created",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.display_name} ({self.status})"


class Product(models.Model):
    # Legacy free-form type kept for backward-compat with existing diamond rows
    # and the old frontend `shopProductTypes` constant. New products should set
    # `category` (the FK below); `product_type` is mirrored from the category
    # slug on write so old code paths that read `product_type` keep working.
    PRODUCT_TYPES = (
        ("diamonds", "Diamonds"),
        ("bundle", "Bundle"),
        ("gun_skin", "Gun Skin"),
    )

    STATUS = (
        ("active", "Active"),          # visible + purchasable
        ("inactive", "Inactive"),      # hidden from customers
        ("archived", "Archived"),      # soft deleted (optional)
    )

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # Legacy string category. Still written (kept in sync with `category.slug`)
    # so existing reads — e.g. the diamond-only frontend filter — never break.
    product_type = models.CharField(max_length=40, choices=PRODUCT_TYPES)

    # New structured category (admin-managed). Nullable so existing diamond rows
    # — created before this table existed — stay valid until backfilled.
    # on_delete=SET_NULL: deleting a category must never cascade-delete products.
    category = models.ForeignKey(
        "Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )

    # ── Marketplace: which vendor sells this product (Phase A) ──────────────────
    # null/blank for FIRST-PARTY AFC stock (existing diamond products keep
    # vendor=None). A non-null vendor marks this as a marketplace product whose
    # orders flow through the fulfilment state machine in afc_shop/fulfilment.py.
    # SET_NULL on delete: removing a Vendor must NEVER cascade-delete their
    # products (they fall back to "no vendor" rather than vanishing). The reverse
    # accessor is `vendor.products`. fulfilment.py reads
    # `variant.product.vendor` to decide whether an order needs vendor fulfilment.
    vendor = models.ForeignKey(
        "Vendor",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )

    # ── Marketplace: product approval workflow (Phase B1) ───────────────────────
    # AFC approves every VENDOR-submitted product before it can reach buyers, but
    # FIRST-PARTY AFC stock (vendor=None: diamonds, existing physical goods) must
    # stay live without any approval step. To get both behaviours from one column
    # the default is "approved":
    #   - Every product that ALREADY exists (the migration back-fills this default)
    #     and every admin-created product (add_product) is "approved" on day one,
    #     so nothing in today's catalogue changes.
    #   - A VENDOR-created product (vendor_create_product in views.py) is explicitly
    #     set to "draft", so it is invisible to the storefront until an admin
    #     approves it.
    # The storefront gate that enforces this lives in view_active_products
    # (status="active" AND (vendor IS NULL OR approval_status="approved")), so an
    # unapproved vendor product can never be bought even if its status is "active".
    #
    # Lifecycle (vendor side -> admin side):
    #   draft     -- vendor edits freely, not yet sent for review
    #   submitted -- vendor pushed it to AFC (vendor_submit_product); shows in the
    #                admin approval queue (admin_list_pending_products)
    #   approved  -- an admin approved it (admin_approve_product); now sellable
    #   rejected  -- an admin rejected it with a reason (admin_reject_product); the
    #                vendor may edit and re-submit (rejected -> submitted)
    # A vendor can NEVER move a product to "approved" (only admin endpoints do).
    APPROVAL_STATUS = (
        ("draft", "Draft"),          # vendor draft, not submitted (storefront-hidden)
        ("submitted", "Submitted"),  # awaiting AFC review (storefront-hidden)
        ("approved", "Approved"),    # AFC approved -> sellable (also the back-compat default)
        ("rejected", "Rejected"),    # AFC rejected (storefront-hidden); vendor can re-submit
    )
    # default="approved": back-compat. Existing rows + admin/diamond products stay
    # live; only vendor_create_product overrides this to "draft".
    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_STATUS, default="approved"
    )
    # When the vendor submitted the product for review (set by vendor_submit_product).
    submitted_at = models.DateTimeField(null=True, blank=True)
    # The admin who approved this product. SET_NULL so removing an admin user never
    # deletes the product (preserves the approval audit trail), mirroring Vendor.created_by.
    approved_by = models.ForeignKey(
        "afc_auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products_approved",
    )
    # Why an admin rejected the product (shown back to the vendor so they can fix it).
    rejection_reason = models.TextField(blank=True)

    slug = models.SlugField(unique=True, blank=True, null=True, db_index=True)

    # product-level image (optional). Variants can also have images.
    # NOTE: this single image is the legacy/primary image. Additional images and
    # videos live in the `ProductMedia` table (related_name="media") so a product
    # can carry a whole gallery. `image` is kept as the card thumbnail fallback.
    image = models.ImageField(upload_to="products/", null=True, blank=True)

    # whether product has limited stock
    is_limited_stock = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=STATUS, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.product_type})"


class ProductVariant(models.Model):
    """
    A product can have variants:
      - Diamonds: 100 / 310 / 520 etc
      - Bundle: Basic / Pro
      - Gun skin: Different rarity or weapon type
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=64, unique=True)

    title = models.CharField(max_length=255, blank=True)  # e.g. "310 Diamonds"
    price = models.DecimalField(max_digits=10, decimal_places=2)

    # For diamonds specifically
    diamonds_amount = models.PositiveIntegerField(default=0)

    # For gun skins/bundles you can store metadata
    meta = models.JSONField(default=dict, blank=True)

    # variant-level image (optional)
    image = models.ImageField(upload_to="product_variants/", null=True, blank=True)

    # stock only matters if product.is_limited_stock=True
    stock_qty = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)  # variant can be disabled independently
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Mintroute
    ean = models.CharField(max_length=50, blank=True, null=True)

    def is_in_stock(self):
        if not self.product.is_limited_stock:
            return True
        return self.stock_qty > 0

    def __str__(self):
        return f"{self.product.name} - {self.title or self.sku}"


# ─────────────────────────────────────────────────────────────────────────────
# ProductMedia — a gallery of images AND videos for a product.
#
# WHY this exists:
#   `Product.image` only holds ONE image. The shop now sells physical goods that
#   need several angles plus short demo videos. This child table lets a product
#   carry many media items in a defined order (a carousel on the card/detail).
#
# HOW it connects:
#   - FK back to `Product` (related_name="media"). Listing/detail views serialise
#     `product.media.all()` into a `media` array (url, media_type, ordering).
#   - Admin uploads: add_product_media / delete_product_media in views.py,
#     wired into the Add/Edit Product modals (multiple images + videos).
#   - Frontend renders the array in a fixed-dimension gallery with object-fit so
#     odd source sizes still fit the layout (ShopClient card + ProductDetailPage).
#   - Size limits are enforced server-side in the upload view (images and videos
#     have separate caps) — the model only stores the validated file.
# ─────────────────────────────────────────────────────────────────────────────
class ProductMedia(models.Model):
    MEDIA_TYPES = (
        ("image", "Image"),
        ("video", "Video"),
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,   # media is meaningless without its product
        related_name="media",
    )

    # Stored as a generic FileField so the same column holds both images and
    # short videos. `media_type` tells the frontend which element to render
    # (<img> vs <video>). upload_to keeps shop media separate on disk.
    file = models.FileField(upload_to="product_media/")
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPES, default="image")

    # Lower ordering shows first in the gallery; the primary image is ordering=0.
    ordering = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ordering", "id"]

    def __str__(self):
        return f"{self.product.name} - {self.media_type} #{self.ordering}"


class Coupon(models.Model):
    DISCOUNT_TYPE = (
        ("percent", "Percent"),
        ("fixed", "Fixed Amount"),
    )

    code = models.CharField(max_length=40, unique=True)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    slug = models.SlugField(unique=True)

    is_active = models.BooleanField(default=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)

    def is_valid_now(self):
        if not self.is_active:
            return False
        now = timezone.now()
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now > self.end_at:
            return False
        if self.max_uses is not None and self.used_count >= self.max_uses:
            return False
        return True

    
    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.code)
            slug = base
            i = 2
            while Coupon.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)


    def __str__(self):
        return self.code


class Order(models.Model):
    STATUS = (
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
        ("fulfilled", "Fulfilled"),   # delivered to player/game account
        ("refunded", "Refunded"),
    )

    user = models.ForeignKey("afc_auth.User", on_delete=models.CASCADE, related_name="orders")
    status = models.CharField(max_length=20, choices=STATUS, default="pending")

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    coupon = models.ForeignKey(Coupon, null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True)

    # optional fields for delivery (for Free Fire topups you might need UID/IGN)
    game_uid = models.CharField(max_length=80, blank=True)
    in_game_name = models.CharField(max_length=80, blank=True)

    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=50, blank=True)
    state = models.CharField(max_length=50, blank=True)
    postcode = models.CharField(max_length=20, blank=True)

    tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    # ── Payment provider (which gateway took the money for THIS order) ──────────────
    # The shop now supports TWO checkout providers side by side: Paystack (the original,
    # NGN card/bank/USSD) and Stripe Checkout (added alongside, charges via Stripe Checkout
    # Session with Adaptive Pricing). `provider` records which one a given order went
    # through so verify/webhook can route correctly and admins can tell them apart.
    # Default "paystack" keeps every pre-existing order untouched (back-compat).
    #   - buy_now (views.py) creates orders with the default -> provider="paystack".
    #   - stripe_buy_now (stripe_checkout.py) sets provider="stripe".
    PROVIDER = (
        ("paystack", "Paystack"),
        ("stripe", "Stripe"),
    )
    provider = models.CharField(max_length=20, choices=PROVIDER, default="paystack")

    # Paystack data
    paystack_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    paystack_transaction_id = models.CharField(max_length=120, blank=True)
    payment_method = models.CharField(max_length=50, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    # ── Stripe data (mirrors the Paystack fields above, only set when provider="stripe") ──
    # stripe_session_id     : the Checkout Session id (cs_...), used by stripe_verify to
    #                         retrieve the session and confirm payment_status == "paid".
    # stripe_payment_intent : the PaymentIntent id (pi_...), the charge reference (stored on
    #                         payment for parity with the event payments flow / future refunds).
    stripe_session_id = models.CharField(max_length=120, blank=True)
    stripe_payment_intent = models.CharField(max_length=120, blank=True)

    # ── Marketplace fulfilment lifecycle (Phase A) ──────────────────────────────
    # ONLY used by physical/vendor orders (an order containing a Product with a
    # non-null vendor). Digital orders (diamond topups) leave all of these
    # null/blank and keep using the per-item `Fulfillment` model above; this
    # order-level lifecycle is the vendor ship-out flow, NOT the voucher flow.
    #
    # The state machine + the transition endpoints that read/write these fields
    # live in afc_shop/fulfilment.py (notify_order_paid sets the initial
    # "received"; vendor_acknowledge_order -> "acknowledged"; vendor_set_ship_date
    # -> "ship_scheduled" + ship_date; vendor_mark_shipped -> "shipped" +
    # shipped_at; order_mark_completed -> "completed" + completed_at). The buyer
    # gets a branded email (afc_shop/emails.py) at received/shipped/completed.
    FULFILMENT_STATE = (
        ("received", "Received"),            # paid, awaiting vendor acknowledgement
        ("acknowledged", "Acknowledged"),    # vendor has seen the order
        ("ship_scheduled", "Ship scheduled"),# vendor committed a ship date
        ("shipped", "Shipped"),              # dispatched (+ photo/video evidence)
        ("completed", "Completed"),          # delivered / closed out
        ("cancelled", "Cancelled"),          # cancelled (-> refund, no payout)
    )
    # null=True/blank=True: digital orders never enter this lifecycle.
    fulfilment_state = models.CharField(
        max_length=20, choices=FULFILMENT_STATE, null=True, blank=True
    )
    # Vendor-picked dispatch date (set at the acknowledged -> ship_scheduled step).
    ship_date = models.DateField(null=True, blank=True)
    # Transition timestamps (one per milestone), set as the order advances.
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)


    def __str__(self):
        return f"Order #{self.id} - {self.user.username} - {self.status}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)

    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)
    coupon_code = models.CharField(max_length=40, null=True, blank=True)

    # snapshot fields (so if product name changes, order keeps original)
    product_name_snapshot = models.CharField(max_length=255)
    variant_title_snapshot = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.order.id} - {self.product_name_snapshot} x{self.quantity}"


class Cart(models.Model):
    user = models.ForeignKey("afc_auth.User", on_delete=models.CASCADE, related_name="cart")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    coupon = models.ForeignKey(Coupon, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"Cart {self.cart.id} - {self.variant.product.name} x{self.quantity}"


class Fulfillment(models.Model):
    STATUS = (
        ("queued", "Queued"),
        ("processing", "Processing"),
        ("delivered", "Delivered"),
        ("failed", "Failed"),
    )

    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="fulfillments")
    item = models.ForeignKey("OrderItem", on_delete=models.CASCADE, related_name="fulfillment_records")

    status = models.CharField(max_length=20, choices=STATUS, default="queued")

    notes = models.TextField(blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Fulfillment {self.id} - Order {self.order.id} - {self.status}"


# ─────────────────────────────────────────────────────────────────────────────
# FulfillmentEvidence — proof-of-shipment media for a marketplace order.
#
# WHY this exists (marketplace Phase A):
#   When a vendor marks an order "shipped" they attach photo/video evidence (a
#   packed box, a tracking slip). Buyers and AFC admins can later see proof the
#   order was actually dispatched. The SAME table will also store inbound media
#   from the WhatsApp (Kapso) fulfilment channel in the SEPARATE follow-up, so
#   the page and the bot record evidence identically.
#
# HOW it connects:
#   - `order` FK (related_name="evidence"): one order can carry several files.
#   - Written by `vendor_mark_shipped` in afc_shop/fulfilment.py from the uploaded
#     files on the shipped transition (and, later, by the Kapso inbound-media
#     webhook). Read by the per-order vendor page + admin order detail.
#   - `uploaded_by` FK -> the vendor User (or admin) who attached it; SET_NULL so
#     deleting that user never deletes the evidence trail.
# ─────────────────────────────────────────────────────────────────────────────
class FulfillmentEvidence(models.Model):
    KIND = (
        ("image", "Image"),
        ("video", "Video"),
    )

    order = models.ForeignKey(
        "Order",
        on_delete=models.CASCADE,   # evidence is meaningless without its order
        related_name="evidence",
    )

    # Generic FileField so the one column holds both images and short videos;
    # `kind` tells the frontend which element to render. upload_to keeps the
    # fulfilment proofs separate on disk from product media.
    media = models.FileField(upload_to="fulfilment_evidence/")
    kind = models.CharField(max_length=10, choices=KIND, default="image")

    uploaded_by = models.ForeignKey(
        "afc_auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fulfilment_evidence_uploads",
    )

    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Evidence {self.id} - Order {self.order.id} ({self.kind})"


class Redemption(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="redemptions")
    product_variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    redeemed_by = models.ForeignKey("afc_auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    savings = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    def __str__(self):
        return f"Redemption {self.coupon.code} - {self.product_variant}"
    

class ShopChangeLog(models.Model):
    ACTIONS = (
        ("product_created", "Product Created"),
        ("product_updated", "Product Updated"),
        ("product_deleted", "Product Deleted"),
        ("variant_created", "Variant Created"),
        ("variant_updated", "Variant Updated"),
        ("variant_deleted", "Variant Deleted"),
        ("coupon_created", "Coupon Created"),
        ("coupon_updated", "Coupon Updated"),
        ("coupon_deleted", "Coupon Deleted"),
        ("order_status_updated", "Order Status Updated"),
    )

    admin_user = models.ForeignKey("afc_auth.User", on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=50, choices=ACTIONS)

    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    variant = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.SET_NULL)
    coupon = models.ForeignKey(Coupon, null=True, blank=True, on_delete=models.SET_NULL)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL)

    details = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)


class MintrouteLog(models.Model):
    request_payload = models.JSONField()
    response_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# ─────────────────────────────────────────────────────────────────────────────
# VendorPayout — the ledger of what AFC owes / has paid each vendor (Phase B3).
#
# WHY this exists (marketplace Phase B3, spec: WEBSITE/tasks/marketplace-design.md):
#   AFC is the CUSTODIAN of marketplace money (same posture as the event paid-events
#   escrow in afc_tournament_and_scrims/event_payments.py): the buyer pays AFC
#   (Stripe Checkout or Paystack), the funds land in AFC's balance, the vendor
#   fulfils, and THEN AFC transfers the vendor's share out via Stripe Connect. This
#   table is the per-order ledger row that records each of those obligations so the
#   money trail is auditable and a transfer is never created twice for one order.
#
#   One VendorPayout per (vendor, order): when an order reaches
#   fulfilment_state="completed" (order_mark_completed in afc_shop/fulfilment.py),
#   afc_shop/connect.py computes the vendor share (order.total minus the platform
#   fee) and either:
#     - creates a Stripe Transfer to vendor.stripe_account_id and records this row
#       status="paid" + stripe_transfer_id, OR
#     - if the vendor has NOT finished Connect onboarding (no stripe_account_id /
#       payouts not enabled), records status="owed" so an admin can release it later
#       (admin_release_owed_payouts) once the vendor is onboarded.
#
# HOW it connects:
#   - `vendor` FK -> Vendor: who is owed/paid. `order` FK -> Order: which sale this
#     payout settles (OneToOne in effect; enforced by unique_together so a completed
#     order can only ever spawn ONE payout, the idempotency guard).
#   - WRITTEN BY: afc_shop/connect.py
#       * settle_order_payout(order)  -> called from fulfilment.order_mark_completed
#         on the shipped -> completed transition (best-effort; never blocks the
#         transition or 500s the request).
#       * admin_release_owed_payouts  -> retries the Stripe Transfer for "owed" rows.
#   - READ BY: admin_list_vendor_payouts (the admin payouts dashboard).
#   - The PLATFORM FEE is settings.MARKETPLACE_FEE_PERCENT (default 0) applied at
#     settle time; stored on the row (platform_fee) so a later fee change does not
#     rewrite history.
# ─────────────────────────────────────────────────────────────────────────────
class VendorPayout(models.Model):
    STATUS = (
        # AFC owes the vendor but has NOT transferred yet (vendor not onboarded to
        # Connect, or a transfer attempt failed). An admin releases these later.
        ("owed", "Owed"),
        # An admin has marked the payout for release but the transfer has not yet
        # succeeded (transient seam; admin_release_owed_payouts moves owed -> paid).
        ("released", "Released"),
        # The Stripe Transfer succeeded; stripe_transfer_id is set. Terminal.
        ("paid", "Paid"),
    )

    # The vendor being paid. CASCADE: a payout has no meaning without its vendor; if
    # the vendor identity is deleted the ledger rows go with it (mirrors how Vendor
    # cascades from its User).
    vendor = models.ForeignKey(
        "Vendor",
        on_delete=models.CASCADE,
        related_name="payouts",
    )

    # The completed order this payout settles. CASCADE for the same reason as vendor.
    # unique=True (one payout per order) is the idempotency guard: settle_order_payout
    # uses get_or_create on this FK so a re-completed/retried order never double-pays.
    order = models.OneToOneField(
        "Order",
        on_delete=models.CASCADE,
        related_name="vendor_payout",
    )

    # The vendor's SHARE actually transferred (order.total minus platform_fee). Stored
    # in the order's currency (the shop charges in one currency, settings.SHOP_CURRENCY).
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    # AFC's cut taken off the top (order.total * MARKETPLACE_FEE_PERCENT / 100). Stored
    # so a later fee change does not rewrite past payouts. 0 when the fee is 0 (default).
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    status = models.CharField(max_length=20, choices=STATUS, default="owed")

    # The Stripe Transfer id (tr_...) once the transfer succeeds. Blank while "owed".
    stripe_transfer_id = models.CharField(max_length=120, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    # Set when the row reaches status="paid" (the transfer succeeded).
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Payout to {self.vendor.display_name} for order #{self.order_id} ({self.status})"