from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.utils import timezone
from django.db import connection
from decimal import Decimal, InvalidOperation

from orders.forms import OrderCreateForm
from orders.models import Order, OrderStatus
from drivers.models import Driver, DriverStatus, DriverVerificationStatus
from analytics.models import MonthlyFinanceReport


@staff_member_required
def home(request):
    now = timezone.now()
    month_report = MonthlyFinanceReport.objects.filter(year=now.year, month=now.month).first()
    context = {
        "active_orders_count": Order.objects.filter(
            status__in=[OrderStatus.NEW, OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT]
        ).count(),
        "completed_today_count": Order.objects.filter(
            status=OrderStatus.COMPLETED,
            delivered_at__date=now.date(),
        ).count(),
        "drivers_available_count": Driver.objects.filter(status=DriverStatus.AVAILABLE).count(),
        "drivers_busy_count": Driver.objects.filter(status=DriverStatus.BUSY).count(),
        "pending_driver_verifications_count": Driver.objects.filter(
            verification_status=DriverVerificationStatus.PENDING
        ).count(),
        "month_report": month_report,
        "safe_mode": False,
    }
    try:
        context["latest_orders"] = list(Order.objects.select_related("client").all()[:10])
    except InvalidOperation:
        context["safe_mode"] = True
        context["latest_orders"] = _latest_orders_safe()
    return render(request, "blog/home.html", context)


def _latest_orders_safe():
    rows = []
    status_map = dict(OrderStatus.choices)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT o.id, c.name, o.from_location, o.to_location, o.status, o.client_price
            FROM orders_order o
            LEFT JOIN orders_client c ON c.id = o.client_id
            ORDER BY o.created_at DESC
            LIMIT 10
            """
        )
        for oid, client_name, from_location, to_location, status, client_price in cursor.fetchall():
            rows.append(
                {
                    "pk": oid,
                    "client_name": client_name or "-",
                    "from_location": from_location or "",
                    "to_location": to_location or "",
                    "status_display": status_map.get(status, status),
                    "client_price": _safe_decimal(client_price),
                }
            )
    return rows


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")
