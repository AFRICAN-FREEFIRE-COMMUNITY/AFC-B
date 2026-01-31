from decimal import Decimal
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import require_admin, validate_token
from .models import Cart, CartItem, Coupon, Order, Product, ProductVariant
from afc_auth.models import User

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.db.models import Sum, Count, F
from django.shortcuts import get_object_or_404
from datetime import timedelta



# Create your views here.


# @api_view(['POST'])
# def add_new_product(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

#     # Identify admin/moderator
#     try:
#         user = User.objects.get(session_token=session_token)
#         if user.role not in ["admin", "moderator"]:
#             return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     # Get product data
#     name = request.data.get('name')
#     description = request.data.get('description')
#     diamonds = request.data.get('diamonds')
#     price = request.data.get('price')
#     image = request.FILES.get('image')
#     stock = request.data.get('stock')

#     # Validate required fields
#     if not all([name, description, diamonds, price, stock]):
#         return Response({"message": "All fields are required."}, status=400)

#     product = Product.objects.create(
#         name=name,
#         description=description,
#         diamonds=diamonds,
#         price=price,
#         image=image,
#         stock=stock
#     )

#     return Response({'message': 'Product added successfully', 'product_id': product.id}, status=201)



# @api_view(['POST'])
# def edit_product(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

#     # Identify admin/moderator
#     try:
#         user = User.objects.get(session_token=session_token)
#         if user.role not in ["admin", "moderator"]:
#             return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     product_id = request.data.get('product_id')

#     if not product_id:
#         return Response({'message': 'Product ID is required.'}, status=400)

#     try:
#         product = Product.objects.get(id=product_id)
#     except Product.DoesNotExist:
#         return Response({'message': 'Product not found.'}, status=404)

#     # Update fields only if provided
#     product.name = request.data.get('name', product.name)
#     product.description = request.data.get('description', product.description)
#     product.diamonds = request.data.get('diamonds', product.diamonds)
#     product.price = request.data.get('price', product.price)

#     # Check if a new image was uploaded
#     if request.FILES.get('image'):
#         product.image = request.FILES.get('image')

#     product.stock = request.data.get('stock', product.stock)

#     product.save()  # status will be updated automatically here

#     return Response({'message': 'Product updated successfully.'}, status=200)


# @api_view(['POST'])
# def delete_product(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

#     # Identify admin/moderator
#     try:
#         user = User.objects.get(session_token=session_token)
#         if user.role not in ["admin", "moderator"]:
#             return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     product_id = request.data.get('product_id')

#     if not product_id:
#         return Response({'message': 'Product ID is required.'}, status=400)

#     try:
#         product = Product.objects.get(id=product_id)
#     except Product.DoesNotExist:
#         return Response({'message': 'Product not found.'}, status=404)

#     product.delete()

#     return Response({'message': 'Product deleted successfully.'}, status=200)



# @api_view(['GET'])
# def list_products(request):
#     status_filter = request.query_params.get('status')  # optional filter: in_stock or out_of_stock

#     if status_filter:
#         products = Product.objects.filter(status=status_filter)
#     else:
#         products = Product.objects.all()

#     product_list = []
#     for product in products:
#         product_list.append({
#             'id': product.id,
#             'name': product.name,
#             'description': product.description,
#             'diamonds': product.diamonds,
#             'price': str(product.price),
#             'image_url': request.build_absolute_uri(product.image.url) if product.image else None,
#             'stock': product.stock,
#             'status': product.status,
#             'created_at': product.created_at,
#             'updated_at': product.updated_at
#         })

#     return Response({'products': product_list}, status=200)


@api_view(["POST"])
def add_product(request):
    admin, err = require_admin(request)
    if err: return err

    name = request.data.get("name")
    product_type = request.data.get("product_type")
    description = request.data.get("description", "")
    is_limited_stock = bool(request.data.get("is_limited_stock", False))
    status_val = request.data.get("status", "active")

    variants = request.data.get("variants", [])  # list of {sku,title,price,diamonds_amount,stock_qty,meta}

    if not name or product_type not in ["diamonds", "bundle", "gun_skin"]:
        return Response({"message": "name and valid product_type are required."}, status=400)

    if not isinstance(variants, list) or len(variants) == 0:
        return Response({"message": "variants must be a non-empty list."}, status=400)

    product = Product.objects.create(
        name=name,
        description=description,
        product_type=product_type,
        is_limited_stock=is_limited_stock,
        status=status_val
    )

    created_variants = []
    for v in variants:
        sku = v.get("sku")
        price = v.get("price")
        if not sku or price is None:
            return Response({"message": "Each variant needs sku and price."}, status=400)

        pv = ProductVariant.objects.create(
            product=product,
            sku=sku,
            title=v.get("title", ""),
            price=price,
            diamonds_amount=int(v.get("diamonds_amount") or 0),
            meta=v.get("meta") or {},
            stock_qty=int(v.get("stock_qty") or 0),
            is_active=bool(v.get("is_active", True)),
        )
        created_variants.append(pv.id)

    return Response({
        "message": "Product created.",
        "product_id": product.id,
        "variant_ids": created_variants
    }, status=201)


@api_view(["GET"])
def view_all_products(request):
    admin, err = require_admin(request)
    if err: return err

    qs = Product.objects.all().order_by("-created_at").prefetch_related("variants")

    data = []
    for p in qs:
        data.append({
            "id": p.id,
            "name": p.name,
            "type": p.product_type,
            "status": p.status,
            "is_limited_stock": p.is_limited_stock,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "variants": [{
                "id": v.id,
                "sku": v.sku,
                "title": v.title,
                "price": str(v.price),
                "diamonds_amount": v.diamonds_amount,
                "stock_qty": v.stock_qty,
                "is_active": v.is_active,
                "in_stock": v.is_in_stock(),
                "created_at": v.created_at,
                "updated_at": v.updated_at,
            } for v in p.variants.all()]
        })
    return Response({"products": data}, status=200)


@api_view(["POST"])
def edit_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    # product fields
    for field in ["name", "description", "product_type", "status", "is_limited_stock"]:
        if field in request.data:
            setattr(product, field, request.data.get(field))
    product.save()

    # variant updates (optional)
    variants = request.data.get("variants")  # list of {id, ...fields}
    if variants is not None:
        if not isinstance(variants, list):
            return Response({"message": "variants must be a list."}, status=400)

        for v in variants:
            vid = v.get("id")
            if not vid:
                continue
            pv = ProductVariant.objects.filter(id=vid, product=product).first()
            if not pv:
                continue

            for f in ["sku", "title", "price", "diamonds_amount", "stock_qty", "is_active", "meta"]:
                if f in v:
                    setattr(pv, f, v.get(f))
            pv.save()

    return Response({"message": "Product updated."}, status=200)


@api_view(["POST"])
def add_product_variant(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)

    sku = request.data.get("sku")
    price = request.data.get("price")
    if not sku or price is None:
        return Response({"message": "sku and price are required."}, status=400)

    pv = ProductVariant.objects.create(
        product=product,
        sku=sku,
        title=request.data.get("title", ""),
        price=price,
        diamonds_amount=int(request.data.get("diamonds_amount") or 0),
        meta=request.data.get("meta") or {},
        stock_qty=int(request.data.get("stock_qty") or 0),
        is_active=bool(request.data.get("is_active", True)),
    )

    return Response({
        "message": "Variant added to product.",
        "variant_id": pv.id
    }, status=201)


@api_view(["POST"])
def delete_product_variant(request):
    admin, err = require_admin(request)
    if err: 
        return err
    variant_id = request.data.get("variant_id")
    if not variant_id:
        return Response({"message": "variant_id is required."}, status=400)
    variant = get_object_or_404(ProductVariant, id=variant_id)
    variant.delete()
    return Response({"message": "Product variant deleted."}, status=200)


@api_view(["POST"])
def delete_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    product.status = "archived"
    product.save(update_fields=["status"])

    return Response({"message": "Product archived (soft deleted)."}, status=200)


@api_view(["POST"])
def deactivate_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    product.status = "inactive"
    product.save(update_fields=["status"])

    return Response({"message": "Product deactivated (hidden from customers)."}, status=200)


@api_view(["POST"])
def activate_product(request):
    admin, err = require_admin(request)
    if err: return err

    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"message": "product_id is required."}, status=400)

    product = get_object_or_404(Product, id=product_id)
    product.status = "active"
    product.save(update_fields=["status"])

    return Response({"message": "Product activated."}, status=200)


@api_view(["GET"])
def view_current_stock_status(request):
    admin, err = require_admin(request)
    if err: return err

    qs = ProductVariant.objects.select_related("product").all().order_by("product__name")

    data = []
    for v in qs:
        data.append({
            "product_id": v.product.id,
            "product_name": v.product.name,
            "variant_id": v.id,
            "sku": v.sku,
            "variant_title": v.title,
            "is_limited_stock": v.product.is_limited_stock,
            "stock_qty": v.stock_qty,
            "in_stock": v.is_in_stock(),
            "active": v.is_active and v.product.status == "active",
        })
    return Response({"stock": data}, status=200)


@api_view(["GET"])
def view_all_orders(request):
    admin, err = require_admin(request)
    if err: return err

    qs = Order.objects.all().prefetch_related("items__variant__product").order_by("-created_at")[:500]

    data = []
    for o in qs:
        data.append({
            "order_id": o.id,
            "user_id": o.user_id,
            "status": o.status,
            "subtotal": str(o.subtotal),
            "discount_total": str(o.discount_total),
            "total": str(o.total),
            "coupon_code": o.coupon_code,
            "created_at": o.created_at,
            "items": [{
                "variant_id": it.variant_id,
                "product": it.product_name_snapshot,
                "variant": it.variant_title_snapshot,
                "qty": it.quantity,
                "unit_price": str(it.unit_price),
                "line_total": str(it.line_total),
            } for it in o.items.all()]
        })
    return Response({"orders": data}, status=200)


def _orders_in_range(start_dt, end_dt):
    return Order.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt)

@api_view(["GET"])
def orders_today(request):
    admin, err = require_admin(request)
    if err: return err

    now = timezone.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    qs = _orders_in_range(start, end)
    return Response({
        "count": qs.count(),
        "paid": qs.filter(status="paid").count(),
        "revenue_paid": str(qs.filter(status="paid").aggregate(s=Sum("total"))["s"] or 0),
    }, status=200)

@api_view(["GET"])
def orders_this_week(request):
    admin, err = require_admin(request)
    if err: return err

    now = timezone.now()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)  # Monday
    end = start + timedelta(days=7)

    qs = _orders_in_range(start, end)
    return Response({
        "count": qs.count(),
        "paid": qs.filter(status="paid").count(),
        "revenue_paid": str(qs.filter(status="paid").aggregate(s=Sum("total"))["s"] or 0),
    }, status=200)

@api_view(["GET"])
def orders_this_month(request):
    admin, err = require_admin(request)
    if err: return err

    now = timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # get first day of next month safely
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    qs = _orders_in_range(start, end)
    return Response({
        "count": qs.count(),
        "paid": qs.filter(status="paid").count(),
        "revenue_paid": str(qs.filter(status="paid").aggregate(s=Sum("total"))["s"] or 0),
    }, status=200)



@api_view(["GET"])
def view_all_coupons(request):
    admin, err = require_admin(request)
    if err: return err

    qs = Coupon.objects.all().order_by("-id")
    data = [{
        "id": c.id,
        "code": c.code,
        "discount_type": c.discount_type,
        "discount_value": str(c.discount_value),
        "active": c.active,
        "start_at": c.start_at,
        "end_at": c.end_at,
        "min_order_amount": str(c.min_order_amount),
        "max_uses": c.max_uses,
        "used_count": c.used_count,
        "is_valid_now": c.is_valid_now(),
    } for c in qs]

    return Response({"coupons": data}, status=200)


@api_view(["POST"])
def create_coupon(request):
    admin, err = require_admin(request)
    if err: return err

    code = (request.data.get("code") or "").strip().upper()
    discount_type = request.data.get("discount_type")
    discount_value = request.data.get("discount_value")

    if not code or discount_type not in ["percent", "fixed"] or discount_value is None:
        return Response({"message": "code, discount_type, discount_value are required."}, status=400)

    c = Coupon.objects.create(
        code=code,
        discount_type=discount_type,
        discount_value=Decimal(str(discount_value)),
        active=bool(request.data.get("active", True)),
        min_order_amount=Decimal(str(request.data.get("min_order_amount", "0"))),
        max_uses=request.data.get("max_uses") or None,
        start_at=request.data.get("start_at") or None,  # if you're sending ISO string, parse it properly
        end_at=request.data.get("end_at") or None,
    )

    return Response({"message": "Coupon created.", "coupon_id": c.id}, status=201)

@api_view(["GET"])
def view_product_details(request):
    # admin, err = require_admin(request)
    # if err: return err
    product_id = request.GET.get("product_id")

    product = get_object_or_404(Product, id=product_id)

    data = {
        "id": product.id,
        "name": product.name,
        "type": product.product_type,
        "description": product.description,
        "status": product.status,
        "is_limited_stock": product.is_limited_stock,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "variants": [{
            "id": v.id,
            "sku": v.sku,
            "title": v.title,
            "price": str(v.price),
            "diamonds_amount": v.diamonds_amount,
            "stock_qty": v.stock_qty,
            "is_active": v.is_active,
            "in_stock": v.is_in_stock(),
            "meta": v.meta,
        } for v in product.variants.all()]
    }
    return Response({"product": data}, status=200)


from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.shortcuts import get_object_or_404
from decimal import Decimal

@api_view(["POST"])
def add_to_cart(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # ---------------- INPUT ----------------
    variant_id = request.data.get("variant_id")
    quantity = request.data.get("quantity", 1)

    if not variant_id:
        return Response({"message": "variant_id is required."}, status=400)

    try:
        quantity = int(quantity)
    except:
        return Response({"message": "quantity must be a number."}, status=400)

    if quantity <= 0:
        return Response({"message": "quantity must be at least 1."}, status=400)

    variant = get_object_or_404(ProductVariant, id=variant_id)

    # ---------------- VALIDATION ----------------
    if not variant.is_active:
        return Response({"message": "This product variant is not available."}, status=400)

    if variant.product.status != "active":
        return Response({"message": "This product is not available."}, status=400)

    if not variant.is_in_stock():
        return Response({"message": "This item is out of stock."}, status=400)

    # ---------------- STOCK CHECK ----------------
    if variant.product.is_limited_stock:
        if quantity > variant.stock_qty:
            return Response({
                "message": f"Only {variant.stock_qty} left in stock."
            }, status=400)

    # ---------------- CREATE / UPDATE CART ----------------
    with transaction.atomic():

        cart, _ = Cart.objects.get_or_create(user=user)

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            variant=variant,
            defaults={"quantity": quantity}
        )

        if not created:
            new_qty = cart_item.quantity + quantity

            if variant.product.is_limited_stock and new_qty > variant.stock_qty:
                return Response({
                    "message": f"Cannot add more. Only {variant.stock_qty} available."
                }, status=400)

            cart_item.quantity = new_qty
            cart_item.save(update_fields=["quantity"])

    # ---------------- RESPONSE SUMMARY ----------------
    items = cart.items.select_related("variant__product")

    subtotal = Decimal("0.00")
    response_items = []

    for item in items:
        line_total = item.variant.price * item.quantity
        subtotal += line_total

        response_items.append({
            "cart_item_id": item.id,
            "variant_id": str(item.variant.id),
            "product_name": item.variant.product.name,
            "variant_title": item.variant.title,
            "unit_price": str(item.variant.price),
            "quantity": item.quantity,
            "line_total": str(line_total),
        })

    return Response({
        "message": "Item added to cart.",
        "cart": {
            "cart_id": cart.id,
            "items": response_items,
            "subtotal": str(subtotal),
            "total_items": items.count(),
        }
    }, status=200)


@api_view(["GET"])
def get_my_cart(request):
    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart = Cart.objects.filter(user=user).first()

    if not cart:
        return Response({
            "cart": {
                "cart_id": None,
                "items": [],
                "subtotal": "0.00",
                "total_items": 0
            }
        }, status=200)

    items = cart.items.select_related("variant__product")

    subtotal = Decimal("0.00")
    response_items = []

    for item in items:
        line_total = item.variant.price * item.quantity
        subtotal += line_total

        response_items.append({
            "cart_item_id": item.id,
            "variant_id": str(item.variant.id),
            "product_name": item.variant.product.name,
            "variant_title": item.variant.title,
            "unit_price": str(item.variant.price),
            "quantity": item.quantity,
            "line_total": str(line_total),
            "in_stock": item.variant.is_in_stock()
        })

    return Response({
        "cart": {
            "cart_id": cart.id,
            "items": response_items,
            "subtotal": str(subtotal),
            "total_items": items.count(),
        }
    }, status=200)


@api_view(["POST"])
def remove_from_cart(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart_item_id = request.data.get("cart_item_id")
    if not cart_item_id:
        return Response({"message": "cart_item_id is required."}, status=400)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart not found."}, status=404)

    cart_item = CartItem.objects.filter(id=cart_item_id, cart=cart).first()
    if not cart_item:
        return Response({"message": "Item not found in your cart."}, status=404)

    cart_item.delete()

    return Response({"message": "Item removed from cart."}, status=200)


@api_view(["POST"])
def update_cart_item_quantity(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart_item_id = request.data.get("cart_item_id")
    quantity = request.data.get("quantity")

    if not cart_item_id or quantity is None:
        return Response({"message": "cart_item_id and quantity are required."}, status=400)

    try:
        quantity = int(quantity)
    except:
        return Response({"message": "quantity must be a number."}, status=400)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart not found."}, status=404)

    cart_item = CartItem.objects.select_related("variant__product").filter(
        id=cart_item_id,
        cart=cart
    ).first()

    if not cart_item:
        return Response({"message": "Item not found in your cart."}, status=404)

    variant = cart_item.variant

    if quantity <= 0:
        cart_item.delete()
        return Response({"message": "Item removed from cart."}, status=200)

    if variant.product.is_limited_stock and quantity > variant.stock_qty:
        return Response({
            "message": f"Only {variant.stock_qty} available in stock."
        }, status=400)

    cart_item.quantity = quantity
    cart_item.save(update_fields=["quantity"])

    return Response({"message": "Cart updated successfully."}, status=200)


@api_view(["POST"])
def clear_cart(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    cart = Cart.objects.filter(user=user).first()
    if not cart:
        return Response({"message": "Cart already empty."}, status=200)

    cart.items.all().delete()

    return Response({"message": "Cart cleared successfully."}, status=200)
