"""
afc_auth/views_admin_nav.py

The numbers on the admin menu (inbox #57, owner 2026-09-27: "build all unbuilt"; the approved
mockup mockups/admin-menu/admin-menu-mockup.html shows them as "Badges count things waiting for
you (open tickets, reports, shop approvals)").

GET auth/admin/nav-counts/
    Auth:     Bearer SessionToken (validate_token). Signed out: 400 / 401 like every other read.
    Response: 200 {"counts": {"tickets": 3, "reports": 4, "approvals": 2}}
              Only the queues the caller may open are present, each decided by the SAME check the
              queue's own endpoint uses, so a badge can never show a number its page would refuse:
                tickets   : open support tickets        afc_support.views._is_support_staff
                reports   : open + reviewing reports    afc_auth.views_player_reports._is_report_moderator
                approvals : vendor products submitted   require_admin (afc_shop.vendors.admin_list_pending_products)
              A person with none of them gets {"counts": {}}.
    Caller:   components/nav-main.tsx (useAdminNavCounts), polled while the admin panel is open.
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import UserReport
from afc_auth.views import validate_token
from afc_auth.views_player_reports import _is_report_moderator
from afc_shop.models import Product
from afc_support.models import SupportTicket
from afc_support.views import _is_support_staff

# Report states that still wait for a moderator
_WAITING_REPORT_STATES = ("open", "reviewing")


@api_view(["GET"])
def admin_nav_counts(request):
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token.", "code": "invalid_missing_authorization_token"}, status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ", 1)[1].strip())
    if not user:
        return Response({"message": "Invalid or expired session token.", "code": "invalid_expired_session_token"}, status=status.HTTP_401_UNAUTHORIZED)

    counts = {}
    if _is_support_staff(user):
        counts["tickets"] = SupportTicket.objects.filter(status=SupportTicket.STATUS_OPEN).count()
    if _is_report_moderator(user):
        counts["reports"] = UserReport.objects.filter(status__in=_WAITING_REPORT_STATES).count()
    # The approval queue's own gate is require_admin (coarse role "admin")
    if user.role == "admin":
        counts["approvals"] = Product.objects.filter(approval_status="submitted").count()
    return Response({"counts": counts}, status=status.HTTP_200_OK)
