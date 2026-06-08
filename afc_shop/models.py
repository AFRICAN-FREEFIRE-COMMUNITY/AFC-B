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

    # Paystack data
    paystack_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    paystack_transaction_id = models.CharField(max_length=120, blank=True)
    payment_method = models.CharField(max_length=50, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)


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