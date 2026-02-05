# from django.db import models

# # Create your models here.


# # class Product(models.Model):
# #     STATUS_CHOICES = [
# #         ('in_stock', 'In Stock'),
# #         ('out_of_stock', 'Out of Stock'),
# #     ]

# #     id = models.AutoField(primary_key=True)
# #     name = models.CharField(max_length=255)
# #     description = models.TextField()
# #     diamonds = models.PositiveIntegerField(default=0)
# #     price = models.DecimalField(max_digits=10, decimal_places=2)
# #     image = models.ImageField(upload_to='products/')
# #     stock = models.PositiveIntegerField(default=0)
# #     created_at = models.DateTimeField(auto_now_add=True)
# #     updated_at = models.DateTimeField(auto_now=True)
# #     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_stock')

# #     def save(self, *args, **kwargs):
# #         if self.stock == 0:
# #             self.status = 'out_of_stock'
# #         else:
# #             self.status = 'in_stock'
# #         super().save(*args, **kwargs)

# #     def __str__(self):
# #         return self.name


# from django.db import models
# from django.conf import settings
# from decimal import Decimal
# import uuid


# class ProductCategory(models.Model):
#     """
#     Example: Diamonds, Bundles, Gun Skins
#     """
#     name = models.CharField(max_length=50, unique=True)
#     slug = models.SlugField(unique=True)

#     def __str__(self):
#         return self.name


# class Product(models.Model):
#     """
#     Generic product (digital).
#     Example: "Diamonds Topup", "Elite Bundle", "AK Skin"
#     Variants hold the real purchasable units (e.g. 110 diamonds, 231 diamonds, etc.)
#     """
#     PRODUCT_TYPE_CHOICES = [
#         ("diamonds", "Diamonds"),
#         ("bundle", "Bundle"),
#         ("gun_skin", "Gun Skin"),
#     ]

#     STATUS_CHOICES = [
#         ("active", "Active"),
#         ("inactive", "Inactive"),
#         ("archived", "Archived"),
#     ]

#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT, related_name="products")
#     product_type = models.CharField(max_length=20, choices=PRODUCT_TYPE_CHOICES)

#     name = models.CharField(max_length=255)
#     description = models.TextField(blank=True)
#     image = models.ImageField(upload_to="products/", null=True, blank=True)

#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
#     is_featured = models.BooleanField(default=False)

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     def __str__(self):
#         return self.name


# class ProductVariant(models.Model):
#     """
#     The actual purchasable unit.
#     Diamonds example: 110 diamonds, 231 diamonds...
#     Bundle example: "Weekly Membership"
#     Gun skin example: "AK - Dragon" (platform/region restrictions if needed)
#     """
#     STOCK_MODE_CHOICES = [
#         ("unlimited", "Unlimited"),   # typical for diamonds
#         ("limited", "Limited"),       # typical for skins/bundles if you want inventory
#     ]

#     STATUS_CHOICES = [
#         ("active", "Active"),
#         ("inactive", "Inactive"),
#     ]

#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")

#     title = models.CharField(max_length=255)  # e.g. "110 Diamonds", "AK Skin - Red", etc.
#     sku = models.CharField(max_length=80, unique=True)

#     price = models.DecimalField(max_digits=10, decimal_places=2)

#     # for diamonds/topup packs:
#     diamonds_amount = models.PositiveIntegerField(default=0)

#     # inventory:
#     stock_mode = models.CharField(max_length=20, choices=STOCK_MODE_CHOICES, default="unlimited")
#     stock_quantity = models.PositiveIntegerField(default=0)

#     # optional business rules:
#     max_per_order = models.PositiveIntegerField(default=0)  # 0 = no limit
#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     @property
#     def in_stock(self) -> bool:
#         if self.stock_mode == "unlimited":
#             return True
#         return self.stock_quantity > 0

#     def __str__(self):
#         return f"{self.product.name} - {self.title}"


# class Order(models.Model):
#     STATUS_CHOICES = [
#         ("pending", "Pending"),
#         ("paid", "Paid"),
#         ("processing", "Processing"),
#         ("fulfilled", "Fulfilled"),
#         ("failed", "Failed"),
#         ("cancelled", "Cancelled"),
#         ("refunded", "Refunded"),
#     ]

#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

#     currency = models.CharField(max_length=10, default="NGN")
#     subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
#     discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
#     total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

#     # delivery info needed for topups (and sometimes bundles)
#     game_uid = models.CharField(max_length=50, blank=True, null=True)       # Free Fire UID
#     game_nickname = models.CharField(max_length=80, blank=True, null=True)  # optional
#     server_region = models.CharField(max_length=50, blank=True, null=True)  # optional

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)


# class OrderItem(models.Model):
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")

#     variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="order_items")
#     product_name_snapshot = models.CharField(max_length=255)   # snapshot for history
#     variant_title_snapshot = models.CharField(max_length=255)

#     unit_price = models.DecimalField(max_digits=10, decimal_places=2)
#     quantity = models.PositiveIntegerField(default=1)

#     line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

#     # for “played” logic etc not needed here—this is shop
#     created_at = models.DateTimeField(auto_now_add=True)

#     def save(self, *args, **kwargs):
#         self.line_total = (self.unit_price or Decimal("0")) * self.quantity
#         super().save(*args, **kwargs)


# class Payment(models.Model):
#     STATUS_CHOICES = [
#         ("initiated", "Initiated"),
#         ("successful", "Successful"),
#         ("failed", "Failed"),
#         ("reversed", "Reversed"),
#     ]

#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="payment")

#     provider = models.CharField(max_length=30, default="paystack")
#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="initiated")

#     amount = models.DecimalField(max_digits=12, decimal_places=2)
#     currency = models.CharField(max_length=10, default="NGN")

#     paystack_reference = models.CharField(max_length=100, unique=True)
#     paystack_access_code = models.CharField(max_length=100, blank=True, null=True)

#     raw_response = models.JSONField(default=dict, blank=True)

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)


# class Fulfillment(models.Model):
#     """
#     Track delivery of digital goods (topup done / bundle delivered / skin delivered).
#     """
#     STATUS_CHOICES = [
#         ("queued", "Queued"),
#         ("processing", "Processing"),
#         ("delivered", "Delivered"),
#         ("failed", "Failed"),
#     ]

#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="fulfillments")
#     item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="fulfillment_records")

#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
#     notes = models.TextField(blank=True, null=True)
#     provider_payload = models.JSONField(default=dict, blank=True)  # any API response if you integrate vendor later

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)


# class Coupon(models.Model):
#     code = models.CharField(max_length=30, unique=True)
#     is_active = models.BooleanField(default=True)

#     # either percent or fixed
#     percent_off = models.PositiveIntegerField(default=0)  # 0-100
#     amount_off = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

#     min_order_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
#     max_uses = models.PositiveIntegerField(default=0)  # 0 = unlimited
#     used_count = models.PositiveIntegerField(default=0)

#     expires_at = models.DateTimeField(blank=True, null=True)


# class StockMovement(models.Model):
#     variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE)
#     change = models.IntegerField()  # +10, -1
#     reason = models.CharField(max_length=100, blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)


from django.db import models
from django.utils import timezone
from decimal import Decimal

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
    slug = models.SlugField(unique=True)  # for SEO-friendly URLs

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

    active = models.BooleanField(default=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)

    def is_valid_now(self):
        if not self.active:
            return False
        now = timezone.now()
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now > self.end_at:
            return False
        if self.max_uses is not None and self.used_count >= self.max_uses:
            return False
        return True

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
    coupon_code = models.CharField(max_length=40, blank=True)

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
    """
    For redeeming codes (e.g. from giveaways or promotions).
    """
    code = models.CharField(max_length=50, unique=True)
    product_variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    redeemed_by = models.ForeignKey("afc_auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    savings = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    def __str__(self):
        return f"Redemption {self.code} - {self.product_variant}"