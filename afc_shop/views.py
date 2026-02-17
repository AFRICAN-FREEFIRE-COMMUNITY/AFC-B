from decimal import Decimal
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import require_admin, validate_token
from afc_leaderboard import models
from .models import Cart, CartItem, Coupon, Fulfillment, Order, OrderItem, Product, ProductVariant, Redemption
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
            "username": o.user.username if o.user else None,
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

    orders = _orders_in_range(
        timezone.now().replace(hour=0, minute=0, second=0, microsecond=0),
        timezone.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    )
    data = [{
        "order_id": o.id,
        "user_id": o.user_id,
        "username": o.user.username if o.user else None,
        "status": o.status,
        "subtotal": str(o.subtotal),
        "discount_total": str(o.discount_total),
        "total": str(o.total),
        "coupon_code": o.coupon_code,
        "created_at": o.created_at,
    } for o in orders]
    return Response({"orders": data}, status=200)







@api_view(["GET"])
def orders_this_week(request):
    admin, err = require_admin(request)
    if err: return err

    orders = _orders_in_range(
        timezone.now() - timedelta(days=timezone.now().weekday()),
        timezone.now()
    )
    data = [{
        "order_id": o.id,
        "user_id": o.user_id,
        "username": o.user.username if o.user else None,
        "status": o.status,
        "subtotal": str(o.subtotal),
        "discount_total": str(o.discount_total),
        "total": str(o.total),
        "coupon_code": o.coupon_code,
        "created_at": o.created_at,
    } for o in orders]
    return Response({"orders": data}, status=200)


@api_view(["GET"])
def orders_this_month(request):
    admin, err = require_admin(request)
    if err: return err

    orders = _orders_in_range(
        timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        timezone.now()
    )
    data = [{
        "order_id": o.id,
        "user_id": o.user_id,
        "username": o.user.username if o.user else None,
        "status": o.status,
        "subtotal": str(o.subtotal),
        "discount_total": str(o.discount_total),
        "total": str(o.total),
        "coupon_code": o.coupon_code,
        "created_at": o.created_at,
    } for o in orders]



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
        "active": c.is_active,
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

    #check if similar code has ever been used before, if yes and it's inactive, we can allow reuse but if it's active we should reject
    existing = Coupon.objects.filter(code=code).first()
    if existing:
        return Response({"message": "A coupon with this code already exists."}, status=400)
    

    c = Coupon.objects.create(
        code=code,
        discount_type=discount_type,
        discount_value=Decimal(str(discount_value)),
        is_active=bool(request.data.get("active", True)),
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
    coupon_code = request.data.get("coupon_code", "").strip().upper()

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
        
    
    # Coupon validation (if provided)
    coupon = None
    if coupon_code:
        coupon = Coupon.objects.filter(code=coupon_code).first()
        if not coupon:
            return Response({"message": "Invalid coupon code."}, status=400)
        if not coupon.is_valid_now():
            return Response({"message": "This coupon is not valid at the moment."}, status=400)

    # ---------------- CREATE / UPDATE CART ----------------
    with transaction.atomic():

        cart, _ = Cart.objects.get_or_create(user=user)

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            variant=variant,
            defaults={"quantity": quantity},
            coupon=coupon
        )

        if not created:
            new_qty = cart_item.quantity + quantity

            if variant.product.is_limited_stock and new_qty > variant.stock_qty:
                return Response({
                    "message": f"Cannot add more. Only {variant.stock_qty} available."
                }, status=400)

            cart_item.quantity = new_qty
            cart_item.coupon = coupon
            cart_item.save(update_fields=["quantity", "coupon"])

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
            "coupon": item.coupon.code if item.coupon else None,
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
            "in_stock": item.variant.is_in_stock(),
            "coupon": item.coupon.code if item.coupon else None,
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


# import requests
# from decimal import Decimal
# from django.db import transaction
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from django.conf import settings


# @api_view(["POST"])
# def buy_now(request):
#     # -------- AUTH --------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     # -------- INPUT --------
#     variant_id = request.data.get("variant_id")
#     quantity = request.data.get("quantity", 1)
#     game_uid = request.data.get("game_uid")
#     in_game_name = request.data.get("in_game_name", "")

#     if not variant_id:
#         return Response({"message": "variant_id is required."}, status=400)

#     try:
#         quantity = int(quantity)
#         if quantity <= 0:
#             raise ValueError
#     except:
#         return Response({"message": "quantity must be a positive number."}, status=400)

#     variant = ProductVariant.objects.select_related("product").filter(
#         id=variant_id,
#         is_active=True
#     ).first()

#     if not variant:
#         return Response({"message": "Product not found or inactive."}, status=404)

#     if variant.product.status != "active":
#         return Response({"message": "Product not available."}, status=400)

#     # -------- STOCK CHECK --------
#     if variant.product.is_limited_stock and quantity > variant.stock_qty:
#         return Response({
#             "message": f"Only {variant.stock_qty} available."
#         }, status=400)

#     unit_price = variant.price
#     subtotal = unit_price * quantity
#     total = subtotal  # coupon logic can be added later

#     # -------- CREATE ORDER --------
#     with transaction.atomic():
#         order = Order.objects.create(
#             user=user,
#             status="pending",
#             subtotal=subtotal,
#             total=total,
#             game_uid=game_uid,
#             in_game_name=in_game_name
#         )

#         OrderItem.objects.create(
#             order=order,
#             variant=variant,
#             quantity=quantity,
#             unit_price=unit_price,
#             line_total=subtotal,
#             product_name_snapshot=variant.product.name,
#             variant_title_snapshot=variant.title or ""
#         )

#         # -------- PAYSTACK INIT --------
#         amount_kobo = int(total * 100)

#         headers = {
#             "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#             "Content-Type": "application/json",
#         }

#         payload = {
#             "email": user.email,
#             "amount": amount_kobo,
#             "reference": f"ORDER-{order.id}",
#             "callback_url": settings.PAYSTACK_CALLBACK_URL,
#             "metadata": {
#                 "order_id": order.id,
#                 "user_id": user.id
#             }
#         }

#         response = requests.post(
#             "https://api.paystack.co/transaction/initialize",
#             json=payload,
#             headers=headers,
#             timeout=30
#         )

#         paystack_response = response.json()

#         if not paystack_response.get("status"):
#             return Response({
#                 "message": "Failed to initialize payment.",
#                 "error": paystack_response
#             }, status=400)

#         authorization_url = paystack_response["data"]["authorization_url"]
#         reference = paystack_response["data"]["reference"]

#         # Save reference
#         order.coupon_code = reference  # reuse field or create payment model
#         order.save(update_fields=["coupon_code"])

#     return Response({
#         "message": "Payment initialized successfully.",
#         "authorization_url": authorization_url,
#         "reference": reference,
#         "order_id": order.id
#     }, status=200)


# import requests
# from decimal import Decimal
# from django.db import transaction
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from django.conf import settings


# @api_view(["POST"])
# def buy_now(request):
#     # -------- AUTH --------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     # -------- INPUT --------
#     variant_id = request.data.get("variant_id")
#     quantity = request.data.get("quantity", 1)
#     game_uid = request.data.get("game_uid")
#     in_game_name = request.data.get("in_game_name", "")

#     if not variant_id:
#         return Response({"message": "variant_id is required."}, status=400)

#     try:
#         quantity = int(quantity)
#         if quantity <= 0:
#             raise ValueError
#     except:
#         return Response({"message": "quantity must be a positive number."}, status=400)

#     variant = ProductVariant.objects.select_related("product").filter(
#         id=variant_id,
#         is_active=True
#     ).first()

#     if not variant:
#         return Response({"message": "Product not found or inactive."}, status=404)

#     if variant.product.status != "active":
#         return Response({"message": "Product not available."}, status=400)

#     # -------- STOCK CHECK --------
#     if variant.product.is_limited_stock and quantity > variant.stock_qty:
#         return Response({
#             "message": f"Only {variant.stock_qty} available."
#         }, status=400)

#     unit_price = variant.price
#     subtotal = unit_price * quantity
#     total = subtotal  # coupon logic can be added later

#     # -------- CREATE ORDER --------
#     with transaction.atomic():
#         order = Order.objects.create(
#             user=user,
#             status="pending",
#             subtotal=subtotal,
#             total=total,
#             game_uid=game_uid,
#             in_game_name=in_game_name
#         )

#         OrderItem.objects.create(
#             order=order,
#             variant=variant,
#             quantity=quantity,
#             unit_price=unit_price,
#             line_total=subtotal,
#             product_name_snapshot=variant.product.name,
#             variant_title_snapshot=variant.title or ""
#         )

#         # -------- PAYSTACK INIT --------
#         amount_kobo = int(total * 100)

#         headers = {
#             "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#             "Content-Type": "application/json",
#         }

#         payload = {
#             "email": user.email,
#             "amount": amount_kobo,
#             "reference": f"ORDER-{order.id}",
#             "callback_url": settings.PAYSTACK_CALLBACK_URL,
#             "metadata": {
#                 "order_id": order.id,
#                 "user_id": user.id
#             }
#         }

#         response = requests.post(
#             "https://api.paystack.co/transaction/initialize",
#             json=payload,
#             headers=headers,
#             timeout=30
#         )

#         paystack_response = response.json()

#         if not paystack_response.get("status"):
#             return Response({
#                 "message": "Failed to initialize payment.",
#                 "error": paystack_response
#             }, status=400)

#         authorization_url = paystack_response["data"]["authorization_url"]
#         reference = paystack_response["data"]["reference"]

#         # Save reference
#         order.coupon_code = reference  # reuse field or create payment model
#         order.save(update_fields=["coupon_code"])

#     return Response({
#         "message": "Payment initialized successfully.",
#         "authorization_url": authorization_url,
#         "reference": reference,
#         "order_id": order.id
#     }, status=200)


import uuid
import requests
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


TAX_RATE = Decimal("0.075")  # 7.5%


from decimal import Decimal, ROUND_HALF_UP

# TAX_RATE = Decimal("0.10")  # 10%


@api_view(["POST"])
def buy_now(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session"}, status=401)

    items = request.data.get("items", [])

    if not isinstance(items, list) or not items:
        return Response({"message": "Items are required."}, status=400)

    # -------- Customer Info --------
    required_fields = [
        "first_name", "last_name", "email",
        "phone_number", "address", "city",
        "state", "postcode"
    ]

    for field in required_fields:
        if not request.data.get(field):
            return Response({"message": f"{field} is required."}, status=400)

    subtotal = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_discount = Decimal("0.00")

    order_items_to_create = []

    # -------- Validate & Calculate --------
    for item in items:
        variant_id = item.get("variant_id")
        quantity = int(item.get("quantity", 1))
        coupon_code = item.get("coupon_code")

        if quantity <= 0:
            return Response({"message": "Invalid quantity."}, status=400)

        try:
            variant = ProductVariant.objects.select_related("product").get(
                id=variant_id,
                is_active=True
            )
        except ProductVariant.DoesNotExist:
            return Response({"message": f"Product {variant_id} not found."}, status=404)

        if not variant.is_in_stock():
            return Response({"message": f"{variant.title} is out of stock."}, status=400)

        if variant.product.is_limited_stock and variant.stock_qty < quantity:
            return Response({"message": f"Insufficient stock for {variant.title}."}, status=400)

        unit_price = variant.price
        base_price = (unit_price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # -------- TAX (Before Discount) --------
        tax_amount = (base_price * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        discount_amount = Decimal("0.00")
        applied_coupon = None

        # -------- COUPON --------
        if coupon_code:
            try:
                coupon = Coupon.objects.get(code=coupon_code, is_active=True)
            except Coupon.DoesNotExist:
                return Response({"message": f"Coupon {coupon_code} invalid."}, status=400)

            if not coupon.is_valid_now():
                return Response({"message": f"Coupon {coupon_code} expired or invalid."}, status=400)

            if base_price < coupon.min_order_amount:
                return Response({"message": f"Coupon {coupon_code} minimum not reached."}, status=400)

            if coupon.discount_type == "percent":
                discount_amount = (base_price * (coupon.discount_value / Decimal("100"))).quantize(Decimal("0.01"))
            else:
                discount_amount = coupon.discount_value

            # Prevent over-discount
            if discount_amount > base_price:
                discount_amount = base_price

            applied_coupon = coupon

        line_total = base_price + tax_amount - discount_amount

        subtotal += base_price
        total_tax += tax_amount
        total_discount += discount_amount

        order_items_to_create.append({
            "variant": variant,
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": line_total,
            "coupon": applied_coupon,
            "discount": discount_amount
        })

    grand_total = subtotal + total_tax - total_discount

    # -------- Create Order --------
    with transaction.atomic():

        order = Order.objects.create(
            user=user,
            subtotal=subtotal,
            discount_total=total_discount,
            total=grand_total,
            status="pending",
            # coupon_code=applied_coupon.code if applied_coupon else None,  # item-level coupons
            first_name=request.data.get("first_name"),
            last_name=request.data.get("last_name"),
            email=request.data.get("email"),
            phone_number=request.data.get("phone_number"),
            address=request.data.get("address"),
            city=request.data.get("city"),
            state=request.data.get("state"),
            postcode=request.data.get("postcode"),
        )

        order_items = []
        for item in order_items_to_create:
            order_items.append(
                OrderItem(
                    order=order,
                    variant=item["variant"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                    line_total=item["line_total"],
                    product_name_snapshot=item["variant"].product.name,
                    variant_title_snapshot=item["variant"].title or item["variant"].sku,
                    coupon_code=item["coupon"].code if item["coupon"] else None,
                    discount_amount=item["discount"]
                )
            )

        OrderItem.objects.bulk_create(order_items)

    # -------- Paystack Init --------
    reference = f"PS_{uuid.uuid4().hex}"
    order.paystack_reference = reference
    order.save(update_fields=["paystack_reference"])

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "email": request.data.get("email"),
        "amount": int(grand_total * 100),
        "reference": reference,
        "callback_url": settings.PAYSTACK_CALLBACK_URL,
        "metadata": {
            "order_id": str(order.id),
            "user_id": user.user_id,
        }
    }

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers=headers,
        json=payload
    )

    data = response.json()

    if not data.get("status"):
        order.status = "failed"
        order.save(update_fields=["status"])
        return Response({"message": "Payment initialization failed."}, status=400)

    return Response({
        "authorization_url": data["data"]["authorization_url"],
        "reference": reference,
        "order_id": order.id,
        "subtotal": str(subtotal),
        "tax": str(total_tax),
        "discount": str(total_discount),
        "total": str(grand_total)
    }, status=200)



# @api_view(["POST"])
# def buy_now(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid token"}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid session"}, status=401)

#     items = request.data.get("items", [])

#     if not isinstance(items, list) or not items:
#         return Response({"message": "Items are required."}, status=400)

#     # -------- Customer Info --------
#     required_fields = [
#         "first_name", "last_name", "email",
#         "phone_number", "address", "city",
#         "state", "postcode"
#     ]

#     for field in required_fields:
#         if not request.data.get(field):
#             return Response({"message": f"{field} is required."}, status=400)

#     first_name = request.data.get("first_name")
#     last_name = request.data.get("last_name")
#     email = request.data.get("email")
#     phone_number = request.data.get("phone_number")
#     address = request.data.get("address")
#     city = request.data.get("city")
#     state = request.data.get("state")
#     postcode = request.data.get("postcode")

#     subtotal = Decimal("0.00")
#     order_items_to_create = []

#     # -------- Validate Items --------
#     for item in items:
#         variant_id = item.get("variant_id")
#         quantity = int(item.get("quantity", 1))

#         if quantity <= 0:
#             return Response({"message": "Invalid quantity provided."}, status=400)

#         try:
#             variant = ProductVariant.objects.select_related("product").get(
#                 id=variant_id,
#                 is_active=True
#             )
#         except ProductVariant.DoesNotExist:
#             return Response({"message": f"Product {variant_id} not found."}, status=404)

#         if not variant.is_in_stock():
#             return Response({"message": f"{variant.title} is out of stock."}, status=400)

#         if variant.product.is_limited_stock and variant.stock_qty < quantity:
#             return Response({"message": f"Insufficient stock for {variant.title}."}, status=400)

#         unit_price = variant.price
#         line_total = unit_price * quantity

#         subtotal += line_total

#         order_items_to_create.append({
#             "variant": variant,
#             "quantity": quantity,
#             "unit_price": unit_price,
#             "line_total": line_total
#         })

#     # -------- Calculate Tax --------
#     tax_amount = (subtotal * TAX_RATE).quantize(Decimal("0.01"))
#     total = subtotal + tax_amount

#     # -------- Create Order --------
#     with transaction.atomic():

#         order = Order.objects.create(
#             user=user,
#             subtotal=subtotal,
#             total=total,
#             status="pending",
#             first_name=first_name,
#             last_name=last_name,
#             email=email,
#             phone_number=phone_number,
#             address=address,
#             city=city,
#             state=state,
#             postcode=postcode,
#             tax=tax_amount
#         )

#         order_items = []
#         for item in order_items_to_create:
#             order_items.append(
#                 OrderItem(
#                     order=order,
#                     variant=item["variant"],
#                     quantity=item["quantity"],
#                     unit_price=item["unit_price"],
#                     line_total=item["line_total"],
#                     product_name_snapshot=item["variant"].product.name,
#                     variant_title_snapshot=item["variant"].title or item["variant"].sku,
#                 )
#             )

#         OrderItem.objects.bulk_create(order_items)

#     # -------- Initialize Paystack --------
#     reference = f"PS_{uuid.uuid4().hex}"

#     order.paystack_reference = reference
#     order.save(update_fields=["paystack_reference"])

#     headers = {
#         "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#         "Content-Type": "application/json",
#     }

#     payload = {
#         "email": email,
#         "amount": int(total * 100),  # convert to kobo
#         "reference": reference,
#         "callback_url": settings.PAYSTACK_CALLBACK_URL,
#         "metadata": {
#             "order_id": str(order.id),
#             "user_id": user.user_id,
#         }
#     }

#     response = requests.post(
#         "https://api.paystack.co/transaction/initialize",
#         headers=headers,
#         json=payload
#     )

#     data = response.json()

#     if not data.get("status"):
#         order.status = "failed"
#         order.save(update_fields=["status"])

#         return Response({"message": "Payment initialization failed."}, status=400)

#     return Response({
#         "authorization_url": data["data"]["authorization_url"],
#         "reference": reference,
#         "order_id": order.id,
#         "subtotal": str(subtotal),
#         "tax": str(tax_amount),
#         "total": str(total)
#     }, status=200)




import requests
from decimal import Decimal
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.conf import settings


from django.utils import timezone


@api_view(["POST"])
def verify_paystack_payment(request):
    reference = request.data.get("reference")

    if not reference:
        return Response({"message": "reference is required."}, status=400)

    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
    }

    verify_url = f"https://api.paystack.co/transaction/verify/{reference}"
    response = requests.get(verify_url, headers=headers, timeout=30)
    paystack_response = response.json()

    if not paystack_response.get("status"):
        return Response({"message": "Verification failed."}, status=400)

    data = paystack_response.get("data", {})

    if data.get("status") != "success":
        return Response({"message": "Payment not successful."}, status=400)

    metadata = data.get("metadata", {})
    order_id = metadata.get("order_id")

    order = Order.objects.select_related("user").prefetch_related(
        "items__variant__product"
    ).filter(id=order_id).first()

    if not order:
        return Response({"message": "Order not found."}, status=404)

    if order.status == "paid":
        return Response({"message": "Already verified."}, status=200)

    expected_amount_kobo = int(order.total * 100)

    if data.get("amount") != expected_amount_kobo:
        return Response({"message": "Amount mismatch."}, status=400)

    # -------- Extract Paystack Info --------
    transaction_id = str(data.get("id"))
    payment_channel = data.get("channel")  # card / bank / ussd / transfer
    paid_at = data.get("paid_at")

    # Optional: card info
    authorization = data.get("authorization", {})
    card_type = authorization.get("card_type")
    bank = authorization.get("bank")

    payment_method = payment_channel

    if payment_channel == "card" and card_type:
        payment_method = f"{card_type} ({bank})"

    # -------- Process Order --------
    with transaction.atomic():

        # Prevent double processing
        if order.status == "paid":
            return Response({"message": "Already processed"}, status=200)

        transaction_id = str(data.get("id"))
        payment_channel = data.get("channel")

        order.status = "paid"
        order.paystack_transaction_id = transaction_id
        order.payment_method = payment_channel
        order.paid_at = timezone.now()
        order.save(update_fields=[
            "status",
            "paystack_transaction_id",
            "payment_method",
            "paid_at"
        ])

        # ✅ Increment Coupon Usage
        if order.coupon:
            order.coupon.used_count += 1
            order.coupon.save(update_fields=["used_count"])

            # Create redemption record (avoid duplicates)
            if not Redemption.objects.filter(
                coupon=order.coupon,
                redeemed_by=order.user,
                redeemed_at__isnull=False,
                order_amount=order.total
            ).exists():

                Redemption.objects.create(
                    coupon=order.coupon,
                    product_variant=order.items.first().variant,
                    redeemed_by=order.user,
                    redeemed_at=timezone.now(),
                    order_amount=order.total,
                    savings=order.discount_total
                )

        # Reduce stock
        for item in order.items.all():
            variant = item.variant
            if variant.product.is_limited_stock:
                if variant.stock_qty < item.quantity:
                    order.status = "failed"
                    order.save(update_fields=["status"])
                    return Response({"message": "Stock inconsistency"}, status=400)

                variant.stock_qty -= item.quantity
                variant.save(update_fields=["stock_qty"])

        # Create fulfillment
        for item in order.items.all():
            Fulfillment.objects.create(
                order=order,
                item=item,
                status="queued"
            )


    return Response({
        "message": "Payment verified successfully.",
        "order_id": order.id,
        "transaction_id": transaction_id,
        "payment_method": payment_method,
        "status": "paid"
    }, status=200)



# @api_view(["POST"])
# def verify_paystack_payment(request):
#     reference = request.data.get("reference")

#     if not reference:
#         return Response({"message": "reference is required."}, status=400)

#     # -------- VERIFY WITH PAYSTACK --------
#     headers = {
#         "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
#     }

#     verify_url = f"https://api.paystack.co/transaction/verify/{reference}"

#     response = requests.get(verify_url, headers=headers, timeout=30)
#     paystack_response = response.json()

#     if not paystack_response.get("status"):
#         return Response({
#             "message": "Failed to verify transaction.",
#             "error": paystack_response
#         }, status=400)

#     data = paystack_response.get("data", {})

#     if data.get("status") != "success":
#         return Response({"message": "Payment not successful."}, status=400)

#     amount_paid_kobo = data.get("amount")
#     metadata = data.get("metadata", {})
#     order_id = metadata.get("order_id")

#     if not order_id:
#         return Response({"message": "Invalid metadata from Paystack."}, status=400)

#     # -------- FIND ORDER --------
#     order = Order.objects.select_related("user").prefetch_related("items__variant__product").filter(id=order_id).first()

#     if not order:
#         return Response({"message": "Order not found."}, status=404)

#     if order.status == "paid":
#         return Response({"message": "Order already verified."}, status=200)

#     expected_amount_kobo = int(order.total * 100)

#     if amount_paid_kobo != expected_amount_kobo:
#         return Response({
#             "message": "Amount mismatch.",
#             "expected": expected_amount_kobo,
#             "paid": amount_paid_kobo
#         }, status=400)

#     # -------- SUCCESS → PROCESS ORDER --------
#     with transaction.atomic():

#         # Mark order paid
#         order.status = "paid"
#         order.save(update_fields=["status"])

#         # Reduce stock if limited
#         for item in order.items.all():
#             variant = item.variant

#             if variant.product.is_limited_stock:
#                 if variant.stock_qty < item.quantity:
#                     return Response({
#                         "message": f"Stock error for {variant.product.name}"
#                     }, status=400)

#                 variant.stock_qty -= item.quantity
#                 variant.save(update_fields=["stock_qty"])

#         # Create fulfillment records
#         for item in order.items.all():
#             Fulfillment.objects.create(
#                 order=order,
#                 item=item,
#                 status="queued"
#             )

#     return Response({
#         "message": "Payment verified successfully.",
#         "order_id": order.id,
#         "status": "paid"
#     }, status=200)


import hmac
import hashlib
import json
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt


@api_view(["POST"])
@csrf_exempt
def paystack_webhook(request):

    # -------- Verify Signature --------
    signature = request.headers.get("x-paystack-signature")
    body = request.body

    computed_signature = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512
    ).hexdigest()

    if signature != computed_signature:
        return Response({"message": "Invalid signature"}, status=400)

    payload = json.loads(body)
    event = payload.get("event")

    if event != "charge.success":
        return Response({"message": "Event ignored"}, status=200)

    data = payload.get("data", {})
    reference = data.get("reference")
    metadata = data.get("metadata", {})

    order_id = metadata.get("order_id")

    try:
        order = Order.objects.select_related().prefetch_related("items__variant__product").get(id=order_id)
    except Order.DoesNotExist:
        return Response({"message": "Order not found"}, status=404)

    # Prevent double processing
    if order.status == "paid":
        return Response({"message": "Already processed"}, status=200)

    with transaction.atomic():

        # Prevent double processing
        if order.status == "paid":
            return Response({"message": "Already processed"}, status=200)

        transaction_id = str(data.get("id"))
        payment_channel = data.get("channel")

        order.status = "paid"
        order.paystack_transaction_id = transaction_id
        order.payment_method = payment_channel
        order.paid_at = timezone.now()
        order.save(update_fields=[
            "status",
            "paystack_transaction_id",
            "payment_method",
            "paid_at"
        ])

        # ✅ Increment Coupon Usage
        if order.coupon:
            order.coupon.used_count += 1
            order.coupon.save(update_fields=["used_count"])

            # Create redemption record (avoid duplicates)
            if not Redemption.objects.filter(
                coupon=order.coupon,
                redeemed_by=order.user,
                redeemed_at__isnull=False,
                order_amount=order.total
            ).exists():

                Redemption.objects.create(
                    coupon=order.coupon,
                    product_variant=order.items.first().variant,
                    redeemed_by=order.user,
                    redeemed_at=timezone.now(),
                    order_amount=order.total,
                    savings=order.discount_total
                )

        # Reduce stock
        for item in order.items.all():
            variant = item.variant
            if variant.product.is_limited_stock:
                if variant.stock_qty < item.quantity:
                    order.status = "failed"
                    order.save(update_fields=["status"])
                    return Response({"message": "Stock inconsistency"}, status=400)

                variant.stock_qty -= item.quantity
                variant.save(update_fields=["stock_qty"])

        # Create fulfillment
        for item in order.items.all():
            Fulfillment.objects.create(
                order=order,
                item=item,
                status="queued"
            )

    return Response({"message": "Payment processed successfully"}, status=200)

# import hmac
# import hashlib
# import json
# from decimal import Decimal
# from django.http import HttpResponse
# from django.conf import settings
# from django.db import transaction
# from django.views.decorators.csrf import csrf_exempt


# @csrf_exempt
# def paystack_webhook(request):
#     if request.method != "POST":
#         return HttpResponse(status=400)

#     payload = request.body
#     signature = request.headers.get("x-paystack-signature")

#     if not signature:
#         return HttpResponse(status=400)

#     # 🔐 Verify signature
#     computed_signature = hmac.new(
#         settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
#         payload,
#         hashlib.sha512
#     ).hexdigest()

#     if computed_signature != signature:
#         return HttpResponse(status=400)

#     event = json.loads(payload)
#     event_type = event.get("event")

#     # Only handle successful charge
#     if event_type != "charge.success":
#         return HttpResponse(status=200)

#     data = event.get("data", {})
#     reference = data.get("reference")
#     amount_paid_kobo = data.get("amount")
#     metadata = data.get("metadata", {})
#     order_id = metadata.get("order_id")

#     if not order_id:
#         return HttpResponse(status=400)

#     try:
#         order = Order.objects.select_related("user").prefetch_related(
#             "items__variant__product"
#         ).get(id=order_id)
#     except Order.DoesNotExist:
#         return HttpResponse(status=404)

#     # Already processed?
#     if order.status == "paid":
#         return HttpResponse(status=200)

#     expected_amount_kobo = int(order.total * 100)

#     if amount_paid_kobo != expected_amount_kobo:
#         return HttpResponse(status=400)

#     # ✅ Process payment
#     with transaction.atomic():

#         order.status = "paid"
#         order.save(update_fields=["status"])

#         for item in order.items.all():
#             variant = item.variant

#             if variant.product.is_limited_stock:
#                 if variant.stock_qty < item.quantity:
#                     return HttpResponse(status=400)

#                 variant.stock_qty -= item.quantity
#                 variant.save(update_fields=["stock_qty"])

#         for item in order.items.all():
#             Fulfillment.objects.create(
#                 order=order,
#                 item=item,
#                 status="queued"
#             )

#     return HttpResponse(status=200)


@api_view(["GET"])
def get_my_orders(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    orders = Order.objects.filter(user=user).order_by("-created_at")

    data = []
    for order in orders:
        data.append({
            "order_id": order.id,
            "status": order.status,
            "subtotal": str(order.subtotal),
            "total": str(order.total),
            "created_at": order.created_at,
            "tax": str(order.tax),
            "items": [{
                "product_name": item.product_name_snapshot,
                "variant_title": item.variant_title_snapshot,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
            } for item in order.items.all()]
        })

    return Response({"orders": data}, status=200)


@api_view(["GET"])
def get_order_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    order_id = request.GET.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related("user").prefetch_related("items__variant__product").get(
            id=order_id,
            user=user
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)

    data = {
        "order_id": order.id,
        "status": order.status,
        "subtotal": str(order.subtotal),
        "total": str(order.total),
        "created_at": order.created_at,
        "tax": str(order.tax),
        "items": [{
            "product_name": item.product_name_snapshot,
            "variant_title": item.variant_title_snapshot,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
        } for item in order.items.all()]
    }

    return Response({"order": data}, status=200)



@api_view(["GET"])
def get_order_details_for_admin(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    order_id = request.GET.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related("user").prefetch_related("items__variant__product").get(
            id=order_id
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)

    data = {
        "order_id": order.id,
        "user_id": order.user.user_id,
        "username": order.user.username if order.user else None,
        "status": order.status,
        "subtotal": str(order.subtotal),
        "total": str(order.total),
        "created_at": order.created_at,
        "tax": str(order.tax),
        "first_name": order.first_name,
        "last_name": order.last_name,
        "email": order.email,
        "phone_number": order.phone_number,
        "address": order.address,
        "city": order.city,
        "state": order.state,
        "date": order.created_at.date(),
        "status": order.status,
        "transaction_id": order.paystack_transaction_id,
        "reference": order.paystack_reference,
        "items": [{
            "product_name": item.product_name_snapshot,
            "variant_title": item.variant_title_snapshot,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
        } for item in order.items.all()]
    }

    return Response({"order": data}, status=200)


@api_view(["POST"])
def mark_order_as_paid(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    order_id = request.data.get("order_id")
    if not order_id:
        return Response({"message": "order_id is required."}, status=400)

    try:
        order = Order.objects.select_related().prefetch_related("items__variant__product").get(
            id=order_id
        )
    except Order.DoesNotExist:
        return Response({"message": "Order not found."}, status=404)

    if order.status == "paid":
        return Response({"message": "Order is already marked as paid."}, status=200)

    with transaction.atomic():
        order.status = "paid"
        order.save(update_fields=["status"])

        # Reduce stock
        for item in order.items.all():
            variant = item.variant
            if variant.product.is_limited_stock:
                variant.stock_qty -= item.quantity
                variant.save(update_fields=["stock_qty"])


    return Response({"message": "Order marked as paid successfully."}, status=200)


@api_view(["POST"])
def delete_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.delete()

    return Response({"message": "Coupon deleted successfully."}, status=200)


@api_view(["POST"])
def deactivate_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.is_active = False
    coupon.save(update_fields=["is_active"])

    return Response({"message": "Coupon deactivated successfully."}, status=200)


@api_view(["POST"])
def activate_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    coupon.is_active = True
    coupon.save(update_fields=["is_active"])

    return Response({"message": "Coupon activated successfully."}, status=200)


@api_view(["POST"])
def edit_coupon(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    code = request.data.get("code")
    discount_type = request.data.get("discount_type")
    discount_value = request.data.get("discount_value")
    max_uses = request.data.get("max_uses")
    min_order_amount = request.data.get("min_order_amount")
    expiry_date = request.data.get("expiry_date")
    description = request.data.get("description")

    if code:
        coupon.code = code
    if discount_type in ["percentage", "fixed"]:
        coupon.discount_type = discount_type
    if discount_value:
        try:
            coupon.discount_value = Decimal(discount_value)
        except:
            return Response({"message": "Invalid discount_value."}, status=400)
    if max_uses is not None:
        try:
            coupon.max_uses = int(max_uses)
        except:
            return Response({"message": "Invalid max_uses."}, status=400)
    if min_order_amount is not None:
        try:
            coupon.min_order_amount = Decimal(min_order_amount)
        except:
            return Response({"message": "Invalid min_order_amount."}, status=400)
    if expiry_date:
        try:
            coupon.end_at = timezone.datetime.fromisoformat(expiry_date)
        except:
            return Response({"message": "Invalid expiry_date format."}, status=400)
    if description is not None:
        coupon.description = description
        
    coupon.save(update_fields=[
        "code", "discount_type", "discount_value", "max_uses",
        "min_order_amount", "end_at", "description"
    ])

    return Response({"message": "Coupon updated successfully."}, status=200)


@api_view(["POST"])
def get_total_customer_savings(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)


    # get total savings without using Sum aggregation to avoid Decimal issues
    total_savings = Decimal("0.00")
    redemptions = Redemption.objects.filter(coupon=coupon).values_list("savings", flat=True)
    for savings in redemptions:
        if savings:
            total_savings += savings

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_customer_savings": str(total_savings)
    }, status=200)


@api_view(["POST"])
def get_total_coupon_uses(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    total_uses = Redemption.objects.filter(coupon=coupon).count()

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_uses": total_uses
    }, status=200)


# @api_view(["POST"])
# def get_total_spent_using_coupon(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user or not user.role == "admin":
#         return Response({"message": "Unauthorized access."}, status=403)

#     coupon_id = request.data.get("coupon_id")
#     if not coupon_id:
#         return Response({"message": "coupon_id is required."}, status=400)

#     try:
#         coupon = Coupon.objects.get(id=coupon_id)
#     except Coupon.DoesNotExist:
#         return Response({"message": "Coupon not found."}, status=404)

#     total_spent = Redemption.objects.filter(coupon=coupon).aggregate(
#         total_spent=models.Sum("order_total_at_redemption")
#     )["total_spent"] or Decimal("0.00")

#     return Response({
#         "coupon_id": coupon.id,
#         "code": coupon.code,
#         "total_spent_using_coupon": str(total_spent)
#     }, status=200)


@api_view(["POST"])
def get_total_revenue_generated(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    total_revenue = Redemption.objects.filter(coupon=coupon).aggregate(
        total_revenue=models.Sum("final_amount_after_discount")
    )["total_revenue"] or Decimal("0.00")

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "total_revenue_generated_from_coupon": str(total_revenue)
    }, status=200)


@api_view(["POST"])
def get_weekly_usage_and_saving_generated(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    one_week_ago = timezone.now() - timezone.timedelta(days=7)

    weekly_data = Redemption.objects.filter(
        coupon=coupon,
        redeemed_at__gte=one_week_ago
    ).annotate(
        day=models.functions.TruncDay("redeemed_at")
    ).values("day").annotate(
        total_uses=models.Count("id"),
        total_savings=models.Sum("savings_amount")
    ).order_by("day")

    result = []
    for entry in weekly_data:
        result.append({
            "date": entry["day"].date(),
            "uses": entry["total_uses"],
            "savings": str(entry["total_savings"] or Decimal("0.00"))
        })

    return Response({
        "coupon_id": coupon.id,
        "code": coupon.code,
        "weekly_usage_and_saving": result
    }, status=200)


from django.db.models import Count
from decimal import Decimal
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.utils import timezone


@api_view(["GET"])
def get_coupon_conversion_rate(request, slug):

    coupon = get_object_or_404(Coupon, slug=slug)

    # Orders since coupon was created
    total_orders = Order.objects.filter(
        created_at__gte=coupon.start_at if coupon.start_at else coupon.created_at
    ).count()

    if total_orders == 0:
        return Response({
            "coupon": coupon.code,
            "conversion_rate": "0%",
            "used_orders": 0,
            "total_orders_since_creation": 0
        })

    used_orders = Order.objects.filter(
        coupon=coupon,
        status="paid"
    ).count()

    conversion_rate = (Decimal(used_orders) / Decimal(total_orders)) * 100

    return Response({
        "coupon": coupon.code,
        "used_orders": used_orders,
        "total_orders_since_creation": total_orders,
        "conversion_rate_percent": round(conversion_rate, 2)
    })


@api_view(["POST"])
def get_coupon_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_id = request.data.get("coupon_id")
    if not coupon_id:
        return Response({"message": "coupon_id is required."}, status=400)

    try:
        coupon = Coupon.objects.get(id=coupon_id)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    data = {
        "id": coupon.id,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "max_uses": coupon.max_uses,
        "used_count": coupon.used_count,
        "min_order_amount": str(coupon.min_order_amount),
        "expiry_date": coupon.end_at,
        "is_active": coupon.is_active,
        "description": coupon.description
    }

    return Response({"coupon_details": data}, status=200)


@api_view(["POST"])
def get_coupon_details_with_code(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user or not user.role == "admin":
        return Response({"message": "Unauthorized access."}, status=403)

    coupon_code = request.data.get("coupon_code")
    if not coupon_code:
        return Response({"message": "coupon_code is required."}, status=400)

    try:
        coupon = Coupon.objects.get(code=coupon_code)
    except Coupon.DoesNotExist:
        return Response({"message": "Coupon not found."}, status=404)

    data = {
        "id": coupon.id,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "max_uses": coupon.max_uses,
        "used_count": coupon.used_count,
        "min_order_amount": str(coupon.min_order_amount),
        "expiry_date": coupon.end_at,
        "is_active": coupon.is_active,
        "description": coupon.description
    }

    return Response({"coupon_details": data}, status=200)

