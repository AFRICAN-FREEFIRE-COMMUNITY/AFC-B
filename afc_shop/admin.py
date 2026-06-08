from django.contrib import admin

from .models import Category, Product, ProductMedia, ProductVariant

# Register your models here.

# Register the new shop generalisation models so categories + media are visible
# and editable from the Django admin (alongside the existing API-driven flows).


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_physical", "is_active", "ordering")
    list_filter = ("is_physical", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class ProductMediaInline(admin.TabularInline):
    # Edit a product's image/video gallery inline on the product page.
    model = ProductMedia
    extra = 1


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "product_type", "category", "status", "is_limited_stock")
    list_filter = ("status", "category", "is_limited_stock")
    search_fields = ("name",)
    inlines = [ProductVariantInline, ProductMediaInline]
