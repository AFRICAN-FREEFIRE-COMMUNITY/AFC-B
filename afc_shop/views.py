from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import Product
from afc_auth.models import User


# Create your views here.


@api_view(['POST'])
def add_new_product(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Get product data
    name = request.data.get('name')
    description = request.data.get('description')
    diamonds = request.data.get('diamonds')
    price = request.data.get('price')
    image = request.FILES.get('image')
    stock = request.data.get('stock')

    # Validate required fields
    if not all([name, description, diamonds, price, stock]):
        return Response({"message": "All fields are required."}, status=400)

    product = Product.objects.create(
        name=name,
        description=description,
        diamonds=diamonds,
        price=price,
        image=image,
        stock=stock
    )

    return Response({'message': 'Product added successfully', 'product_id': product.id}, status=201)



@api_view(['POST'])
def edit_product(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    product_id = request.data.get('product_id')

    if not product_id:
        return Response({'message': 'Product ID is required.'}, status=400)

    try:
        product = Product.objects.get(id=product_id)
    except Product.DoesNotExist:
        return Response({'message': 'Product not found.'}, status=404)

    # Update fields only if provided
    product.name = request.data.get('name', product.name)
    product.description = request.data.get('description', product.description)
    product.diamonds = request.data.get('diamonds', product.diamonds)
    product.price = request.data.get('price', product.price)

    # Check if a new image was uploaded
    if request.FILES.get('image'):
        product.image = request.FILES.get('image')

    product.stock = request.data.get('stock', product.stock)

    product.save()  # status will be updated automatically here

    return Response({'message': 'Product updated successfully.'}, status=200)


@api_view(['POST'])
def delete_product(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    product_id = request.data.get('product_id')

    if not product_id:
        return Response({'message': 'Product ID is required.'}, status=400)

    try:
        product = Product.objects.get(id=product_id)
    except Product.DoesNotExist:
        return Response({'message': 'Product not found.'}, status=404)

    product.delete()

    return Response({'message': 'Product deleted successfully.'}, status=200)



@api_view(['GET'])
def list_products(request):
    status_filter = request.query_params.get('status')  # optional filter: in_stock or out_of_stock

    if status_filter:
        products = Product.objects.filter(status=status_filter)
    else:
        products = Product.objects.all()

    product_list = []
    for product in products:
        product_list.append({
            'id': product.id,
            'name': product.name,
            'description': product.description,
            'diamonds': product.diamonds,
            'price': str(product.price),
            'image_url': request.build_absolute_uri(product.image.url) if product.image else None,
            'stock': product.stock,
            'status': product.status,
            'created_at': product.created_at,
            'updated_at': product.updated_at
        })

    return Response({'products': product_list}, status=200)
