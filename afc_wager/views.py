"""afc_wager — STUBBED views for v1.

Endpoints are wired with auth + transactional decorators ready to flip
live in a follow-up PR. Until then, every view returns {"status": "stubbed"}
and the URL include is commented out in afc/urls.py.
"""

from django.db import transaction
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response


_STUB = {"status": "stubbed", "message": "Endpoint not live in v1."}


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def list_markets(request):
    return Response({**_STUB, "results": []})


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def get_market(request, market_id):
    return Response({**_STUB, "market_id": market_id})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def place_wager(request, market_id):
    return Response(_STUB)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def cancel_wager(request, market_id):
    return Response(_STUB)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def list_my_wagers(request):
    return Response({**_STUB, "results": []})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def admin_create_market(request):
    return Response(_STUB)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def admin_lock_market(request, market_id):
    return Response(_STUB)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def admin_settle_market(request, market_id):
    return Response(_STUB)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def admin_void_market(request, market_id):
    return Response(_STUB)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def admin_settlement_queue(request):
    return Response({**_STUB, "results": []})
