from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.utils.text import slugify

# Create your models here.

class Product(models.Model):
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
    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPES)
    slug = models.SlugField(unique=True, blank=True, null=True, db_index=True)

    # product-level image (optional). Variants can also have images.
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
    coupon_code = models.CharField(max_length=40, blank=True)

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