"""afc_wallet — STUBBED views for v1.

Goal in this milestone is models + services + tests. Live HTTP endpoints
land in a later milestone. Each view here returns a stable {"status":
"stubbed"} payload but is wired with the auth + transactional decorators
that the live implementation will keep.

The URL include in `afc/urls.py` is COMMENTED OUT, so these are never
reachable in v1. They exist so the integration switch is a one-line uncomment.
"""

from django.db import transaction
from rest_framework import permissions, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response


_STUBBED = {"status": "stubbed", "message": "Endpoint not live in v1."}


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def get_balance(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def list_transactions(request):
    return Response({**_STUBBED, "results": []}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def start_deposit(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def paystack_webhook(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def stripe_webhook(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def crypto_webhook(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def redeem_voucher(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def p2p_send(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def withdraw(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def verify_whatsapp_start(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def verify_whatsapp_confirm(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def verify_discord(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def kyc_status(request):
    return Response(_STUBBED, status=status.HTTP_200_OK)
