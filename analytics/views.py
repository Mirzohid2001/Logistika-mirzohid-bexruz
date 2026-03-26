import csv
import json
from datetime import date
from datetime import datetime, timedelta
from io import BytesIO

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from analytics.models import AlertEvent, ClientAnalyticsSnapshot, DriverPerformanceSnapshot, MonthlyFinanceReport
from analytics.models import AlertType
from analytics.services import (
    _month_delivered_bounds,
    _revenue_orders_qs,
    aggregate_order_pnl,
    driver_fee_breakdown_delivered_between,
    last_n_calendar_months_end_at,
    rebuild_monthly_reports,
)
from analytics.tasks import rebuild_monthly_reports_task
from common.permissions import WEB_PANEL_GROUPS, groups_required
from drivers.models import Driver, DriverVerificationStatus
from orders.models import Client
from orders.models import Order, OrderStatus
from tracking.models import LocationPing
from openpyxl import Workbook


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def ops_dashboard(request):
    cache_key = "ops_dashboard_v1"
    payload = cache.get(cache_key)
    if payload is None:
        active_orders = list(
            Order.objects.filter(status__in=[OrderStatus.NEW, OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT]).select_related("client")[:25]
        )
        latest_locations = list(LocationPing.objects.select_related("driver", "order")[:20])
        top_drivers = list(DriverPerformanceSnapshot.objects.order_by("-period_year", "-period_month", "-rating_score")[:10])
        monthly_report = MonthlyFinanceReport.objects.order_by("-year", "-month").first()
        report_points = list(MonthlyFinanceReport.objects.order_by("-year", "-month")[:6])
        report_points.reverse()
        top_clients = list(
            ClientAnalyticsSnapshot.objects.order_by("-period_year", "-period_month", "-completed_orders")
            .select_related("client")[:5]
        )
        recent_alerts = list(AlertEvent.objects.select_related("order", "driver")[:10])
        no_live_track_alerts = list(
            AlertEvent.objects.filter(alert_type=AlertType.NO_LIVE_TRACK, resolved=False)
            .select_related("order", "driver")
            .order_by("-created_at")[:20]
        )
        payload = {
            "active_orders": active_orders,
            "latest_locations": latest_locations,
            "top_drivers": top_drivers,
            "monthly_report": monthly_report,
            "report_points": report_points,
            "top_clients": top_clients,
            "recent_alerts": recent_alerts,
            "no_live_track_alerts": no_live_track_alerts,
        }
        cache.set(cache_key, payload, timeout=120)
    else:
        active_orders = payload["active_orders"]
        latest_locations = payload["latest_locations"]
        top_drivers = payload["top_drivers"]
        monthly_report = payload["monthly_report"]
        report_points = payload["report_points"]
        top_clients = payload["top_clients"]
        recent_alerts = payload["recent_alerts"]
        no_live_track_alerts = payload.get("no_live_track_alerts", [])

    threshold = timezone.now() - timedelta(minutes=10)
    for ping in latest_locations:
        ping.is_stale = ping.captured_at < threshold
    finance_chart = {
        "labels": [f"{point.year}-{point.month:02d}" for point in report_points],
        "completed_orders": [int(point.completed_orders) for point in report_points],
        "canceled_orders": [int(point.canceled_orders) for point in report_points],
        "issue_orders": [int(point.issue_orders) for point in report_points],
    }
    client_chart = {
        "labels": [row.client.name for row in top_clients],
        "deliveries": [int(row.completed_orders) for row in top_clients],
        "ratings": [float(row.client_rating_score) for row in top_clients],
    }
    map_points = [
        {
            "driver": ping.driver.full_name,
            "order_id": ping.order_id,
            "lat": float(ping.latitude),
            "lon": float(ping.longitude),
            "captured_at": ping.captured_at.isoformat(),
        }
        for ping in latest_locations
    ]
    pending_driver_verifications = list(
        Driver.objects.filter(verification_status=DriverVerificationStatus.PENDING).order_by(
            "-registration_submitted_at", "-updated_at"
        )[:25]
    )
    return render(
        request,
        "analytics/dashboard.html",
        {
            "active_orders": active_orders,
            "latest_locations": latest_locations,
            "top_drivers": top_drivers,
            "monthly_report": monthly_report,
            "recent_alerts": recent_alerts,
            "no_live_track_alerts": no_live_track_alerts,
            "pending_driver_verifications": pending_driver_verifications,
            "finance_chart_json": json.dumps(finance_chart),
            "client_chart_json": json.dumps(client_chart),
            "map_points_json": json.dumps(map_points),
        },
    )


@staff_member_required
@groups_required("Owner", "Analyst")
def generate_monthly_report(request):
    year_raw = request.GET.get("year", str(datetime.now().year))
    month_raw = request.GET.get("month", str(datetime.now().month))
    year = int(year_raw) if year_raw.isdigit() else datetime.now().year
    month = int(month_raw) if month_raw.isdigit() else datetime.now().month
    month = min(max(month, 1), 12)
    async_mode = request.GET.get("async") == "1"
    if async_mode:
        rebuild_monthly_reports_task.delay(year, month)
        messages.success(request, f"Oylik hisobot navbatga qo'shildi: {year}-{month:02d}")
    else:
        rebuild_monthly_reports(year, month)
        messages.success(request, f"Oylik hisobot yangilandi: {year}-{month:02d}")
    cache.delete("ops_dashboard_v1")
    return redirect("ops-dashboard")


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def export_clients_report_csv(request):
    year_raw = request.GET.get("year", str(datetime.now().year))
    month_raw = request.GET.get("month", str(datetime.now().month))
    year = int(year_raw) if year_raw.isdigit() else datetime.now().year
    month = int(month_raw) if month_raw.isdigit() else datetime.now().month
    month = min(max(month, 1), 12)
    from_raw = request.GET.get("from")
    to_raw = request.GET.get("to")

    if from_raw and to_raw:
        try:
            start = date.fromisoformat(from_raw)
            end = date.fromisoformat(to_raw)
        except ValueError:
            start = None
            end = None
    else:
        start = None
        end = None

    if start and end:
        rows = (
            Order.objects.filter(client__isnull=False, created_at__date__gte=start, created_at__date__lte=end)
            .values("client__name")
            .annotate(
                total_orders=Count("id"),
                completed_orders=Count("id", filter=Q(status=OrderStatus.COMPLETED)),
                sla_breach_count=Count(
                    "id",
                    filter=Q(status=OrderStatus.COMPLETED, delivered_at__isnull=False, client__isnull=False)
                    & Q(delivered_at__gt=F("pickup_time")),
                ),
            )
            .order_by("-completed_orders", "client__name")
        )
    else:
        rows = ClientAnalyticsSnapshot.objects.filter(period_year=year, period_month=month).select_related("client")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="clients_report_{year}_{month:02d}.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Client",
            "TotalOrders",
            "CompletedOrders",
            "YearlyCompletedOrders",
            "SlaBreachCount",
            "SlaBreachRatio",
            "ClientRatingScore",
        ]
    )
    for row in rows:
        if isinstance(row, dict):
            completed = row["completed_orders"] or 0
            breaches = row["sla_breach_count"] or 0
            ratio = round((breaches * 100 / completed), 2) if completed else 0
            writer.writerow([row["client__name"], row["total_orders"], completed, "", breaches, ratio, ""])
        else:
            writer.writerow(
                [
                    row.client.name,
                    row.total_orders,
                    row.completed_orders,
                    row.yearly_completed_orders,
                    row.sla_breach_count,
                    row.sla_breach_ratio,
                    row.client_rating_score,
                ]
            )
    return response


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def export_drivers_report_csv(request):
    year = int(request.GET.get("year", datetime.now().year))
    month = int(request.GET.get("month", datetime.now().month))
    rows = DriverPerformanceSnapshot.objects.filter(period_year=year, period_month=month).select_related("driver")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="drivers_report_{year}_{month:02d}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Driver", "Completed", "Canceled", "Issue", "OnTimeRate", "MonthlyEarnings", "YearlyEarnings", "Rating"])
    for row in rows:
        writer.writerow(
            [
                row.driver.full_name,
                row.completed_count,
                row.cancel_count,
                row.issue_count,
                row.on_time_rate,
                row.monthly_earnings,
                row.yearly_earnings,
                row.rating_score,
            ]
        )
    return response


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def export_drivers_report_xlsx(request):
    year = int(request.GET.get("year", datetime.now().year))
    month = int(request.GET.get("month", datetime.now().month))
    rows = DriverPerformanceSnapshot.objects.filter(period_year=year, period_month=month).select_related("driver")
    wb = Workbook()
    ws = wb.active
    ws.title = "Drivers"
    ws.append(["Driver", "Completed", "Canceled", "Issue", "OnTimeRate", "MonthlyEarnings", "YearlyEarnings", "Rating"])
    total_monthly = 0
    for row in rows:
        ws.append(
            [
                row.driver.full_name,
                row.completed_count,
                row.cancel_count,
                row.issue_count,
                float(row.on_time_rate),
                float(row.monthly_earnings),
                float(row.yearly_earnings),
                float(row.rating_score),
            ]
        )
        total_monthly += float(row.monthly_earnings)
    ws.append([])
    ws.append(["TOTAL MONTHLY", "", "", "", "", total_monthly, "", ""])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="drivers_report_{year}_{month:02d}.xlsx"'
    return response


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def clients_rating_report(request):
    year_raw = request.GET.get("year", str(datetime.now().year))
    month_raw = request.GET.get("month", str(datetime.now().month))
    year = int(year_raw) if year_raw.isdigit() else datetime.now().year
    month = int(month_raw) if month_raw.isdigit() else datetime.now().month
    month = min(max(month, 1), 12)
    top_raw = request.GET.get("top", "20")
    top = int(top_raw) if top_raw.isdigit() else 20
    top = min(max(top, 1), 200)
    search = request.GET.get("search", "").strip()
    active_only = request.GET.get("active_only") == "1"
    qs = ClientAnalyticsSnapshot.objects.filter(period_year=year, period_month=month).select_related("client")
    if active_only:
        qs = qs.filter(client__is_active=True)
    if search:
        qs = qs.filter(client__name__icontains=search)
    qs = qs.order_by("-completed_orders", "-client_rating_score", "client__name")[:top]
    paginator = Paginator(qs, int(getattr(dj_settings, "ANALYTICS_CLIENTS_RATING_PAGE_SIZE", 20) or 20))
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "analytics/clients_rating.html",
        {
            "year": year,
            "month": month,
            "top": top,
            "search": search,
            "active_only": active_only,
            "page_obj": page_obj,
            "rows": page_obj.object_list,
        },
    )


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def clients_monthly_yearly_report(request):
    now = datetime.now()
    year_raw = request.GET.get("year", str(now.year))
    month_raw = request.GET.get("month", str(now.month))
    year = int(year_raw) if year_raw.isdigit() else now.year
    month = int(month_raw) if month_raw.isdigit() else now.month
    month = min(max(month, 1), 12)

    monthly_rows = (
        ClientAnalyticsSnapshot.objects.filter(period_year=year, period_month=month)
        .select_related("client")
        .order_by("-completed_orders", "-client_rating_score", "client__name")
    )
    yearly_rows = (
        ClientAnalyticsSnapshot.objects.filter(period_year=year)
        .values("client__name")
        .annotate(
            yearly_total_orders=Sum("total_orders"),
            yearly_completed_orders=Sum("completed_orders"),
            yearly_avg_rating=Avg("client_rating_score"),
        )
        .order_by("-yearly_completed_orders", "-yearly_avg_rating", "client__name")
    )
    return render(
        request,
        "analytics/clients_reports.html",
        {
            "year": year,
            "month": month,
            "monthly_rows": monthly_rows,
            "yearly_rows": yearly_rows,
        },
    )


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def export_clients_yearly_report_csv(request):
    now = datetime.now()
    year_raw = request.GET.get("year", str(now.year))
    year = int(year_raw) if year_raw.isdigit() else now.year
    rows = (
        ClientAnalyticsSnapshot.objects.filter(period_year=year)
        .values("client__name")
        .annotate(
            yearly_total_orders=Sum("total_orders"),
            yearly_completed_orders=Sum("completed_orders"),
            yearly_avg_rating=Avg("client_rating_score"),
        )
        .order_by("-yearly_completed_orders", "-yearly_avg_rating", "client__name")
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="clients_yearly_report_{year}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Client", "YearlyTotalOrders", "YearlyCompletedOrders", "YearlyAvgRating"])
    for row in rows:
        writer.writerow(
            [
                row["client__name"],
                row["yearly_total_orders"] or 0,
                row["yearly_completed_orders"] or 0,
                row["yearly_avg_rating"] or 0,
            ]
        )
    return response


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def export_clients_report_pdf(request):
    year_raw = request.GET.get("year", str(datetime.now().year))
    month_raw = request.GET.get("month", str(datetime.now().month))
    year = int(year_raw) if year_raw.isdigit() else datetime.now().year
    month = int(month_raw) if month_raw.isdigit() else datetime.now().month
    month = min(max(month, 1), 12)
    rows = (
        ClientAnalyticsSnapshot.objects.filter(period_year=year, period_month=month)
        .select_related("client")
        .order_by("-completed_orders", "-client_rating_score", "client__name")
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(40, y, f"Client report {year}-{month:02d}")
    y -= 24
    pdf.setFont("Helvetica", 10)
    for row in rows:
        line = (
            f"{row.client.name} | total:{row.total_orders} | done:{row.completed_orders} | "
            f"sla breach:{row.sla_breach_count} ({row.sla_breach_ratio}%) | rating:{row.client_rating_score}"
        )
        pdf.drawString(40, y, line[:115])
        y -= 16
        if y < 40:
            pdf.showPage()
            y = 800
            pdf.setFont("Helvetica", 10)
    pdf.save()
    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="clients_report_{year}_{month:02d}.pdf"'
    return response


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def client_360_report(request, client_id: int):
    client = get_object_or_404(Client, pk=client_id)
    snapshots = list(
        client.analytics_snapshots.order_by("-period_year", "-period_month")[:12]
    )
    snapshots.reverse()
    chart = {
        "labels": [f"{row.period_year}-{row.period_month:02d}" for row in snapshots],
        "deliveries": [int(row.completed_orders) for row in snapshots],
        "ratings": [float(row.client_rating_score) for row in snapshots],
        "sla_breach_ratio": [float(row.sla_breach_ratio) for row in snapshots],
    }
    top_routes = (
        client.orders.values("from_location", "to_location")
        .annotate(total=Count("id"))
        .order_by("-total", "from_location", "to_location")[:5]
    )
    issue_reasons = (
        client.orders.filter(status__in=[OrderStatus.ISSUE, OrderStatus.CANCELED])
        .values("comment")
        .annotate(total=Count("id"))
        .order_by("-total")[:5]
    )
    return render(
        request,
        "analytics/client_360.html",
        {
            "client_obj": client,
            "chart_json": json.dumps(chart),
            "top_routes": top_routes,
            "issue_reasons": issue_reasons,
        },
    )


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def accounting_pnl_report(request):
    """
    Buxgalteriya: yetkazilgan buyurtmalar bo‘yicha (delivered_at) xarajatlar va sof natija.
    Klientdan tushum yo‘q modelda client_price / gross_revenue odatda 0; asosiy e’tibor haydovchi va chiqimlarga.
    Davrlar: oylik | so‘nggi 6 oy | yil.
    """
    now = timezone.now()
    period = (request.GET.get("period") or "month").strip().lower()
    if period not in {"month", "6m", "year"}:
        period = "month"
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
    except (TypeError, ValueError):
        year, month = now.year, now.month
    month = max(1, min(12, month))

    if period == "year":
        months = [(year, m) for m in range(1, 13)]
        period_title = f"Yillik — {year}"
        range_start = _month_delivered_bounds(year, 1)[0]
        range_end = _month_delivered_bounds(year, 12)[1]
    elif period == "6m":
        months = last_n_calendar_months_end_at(year, month, 6)
        period_title = (
            f"So‘nggi 6 oy: {months[0][0]}-{months[0][1]:02d} … {months[-1][0]}-{months[-1][1]:02d}"
        )
        range_start = _month_delivered_bounds(months[0][0], months[0][1])[0]
        range_end = _month_delivered_bounds(months[-1][0], months[-1][1])[1]
    else:
        months = [(year, month)]
        period_title = f"Oylik — {year}-{month:02d}"
        range_start, range_end = _month_delivered_bounds(year, month)

    rows = []
    for y, m in months:
        ds, de = _month_delivered_bounds(y, m)
        fin_qs = Order.objects.filter(
            status=OrderStatus.COMPLETED,
            delivered_at__gte=ds,
            delivered_at__lt=de,
        )
        pnl = aggregate_order_pnl(_revenue_orders_qs(fin_qs))
        rows.append({"year": y, "month": m, "period_label": f"{y}-{m:02d}", **pnl})

    from decimal import Decimal

    totals = {
        "order_count": sum(r["order_count"] for r in rows),
        "gross_revenue": sum((r["gross_revenue"] for r in rows), Decimal("0")),
        "total_driver_cost": sum((r["total_driver_cost"] for r in rows), Decimal("0")),
        "total_fuel_cost": sum((r["total_fuel_cost"] for r in rows), Decimal("0")),
        "total_extra_cost": sum((r["total_extra_cost"] for r in rows), Decimal("0")),
        "total_penalty": sum((r["total_penalty"] for r in rows), Decimal("0")),
        "net_margin": sum((r["net_margin"] for r in rows), Decimal("0")),
    }
    driver_rows = driver_fee_breakdown_delivered_between(range_start, range_end)

    return render(
        request,
        "analytics/accounting_pnl.html",
        {
            "period": period,
            "year": year,
            "month": month,
            "month_options": list(range(1, 13)),
            "period_title": period_title,
            "rows": rows,
            "totals": totals,
            "driver_rows": driver_rows,
            "range_note": "Hisob: faqat COMPLETED buyurtmalar, yetkazish vaqti (delivered_at) oralig‘ida. Klientdan tushum yo‘q — «klient maydoni» ustuni texnik.",
        },
    )


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def accounting_pnl_export_csv(request):
    now = timezone.now()
    period = (request.GET.get("period") or "month").strip().lower()
    if period not in {"month", "6m", "year"}:
        period = "month"
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
    except (TypeError, ValueError):
        year, month = now.year, now.month
    month = max(1, min(12, month))

    if period == "year":
        months = [(year, m) for m in range(1, 13)]
        range_start = _month_delivered_bounds(year, 1)[0]
        range_end = _month_delivered_bounds(year, 12)[1]
    elif period == "6m":
        months = last_n_calendar_months_end_at(year, month, 6)
        range_start = _month_delivered_bounds(months[0][0], months[0][1])[0]
        range_end = _month_delivered_bounds(months[-1][0], months[-1][1])[1]
    else:
        months = [(year, month)]
        range_start, range_end = _month_delivered_bounds(year, month)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="accounting_pnl_{period}_{year}_{month:02d}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        [
            "# Shofir: klientdan tushum yo'q; gross_revenue=sum(client_price) odatda 0. Asosiy: driver_cost va net_margin.",
        ]
    )
    writer.writerow(
        [
            "period",
            "completed_orders",
            "gross_revenue",
            "driver_cost",
            "fuel_cost",
            "extra_cost",
            "penalty",
            "net_margin",
        ]
    )
    for y, m in months:
        ds, de = _month_delivered_bounds(y, m)
        fin_qs = Order.objects.filter(
            status=OrderStatus.COMPLETED,
            delivered_at__gte=ds,
            delivered_at__lt=de,
        )
        pnl = aggregate_order_pnl(_revenue_orders_qs(fin_qs))
        writer.writerow(
            [
                f"{y}-{m:02d}",
                pnl["order_count"],
                pnl["gross_revenue"],
                pnl["total_driver_cost"],
                pnl["total_fuel_cost"],
                pnl["total_extra_cost"],
                pnl["total_penalty"],
                pnl["net_margin"],
            ]
        )
    writer.writerow([])
    writer.writerow(["driver_id", "driver_name", "total_driver_fee", "trips"])
    for row in driver_fee_breakdown_delivered_between(range_start, range_end):
        writer.writerow(
            [
                row["driver_id"],
                row["driver__full_name"],
                row["total_fee"] or 0,
                row["trips"],
            ]
        )
    return response


def _live_fleet_payload() -> dict:
    """Biriktirilgan / yo‘lda buyurtmalar: so‘nggi nuqta + iz (Telegram Live yangilanishlari)."""
    from collections import defaultdict

    trail_cap = int(getattr(dj_settings, "FLEET_LIVE_TRAIL_MAX_POINTS", 100) or 100)
    trail_cap = max(15, min(trail_cap, 300))
    active_statuses = [OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT]
    orders = list(
        Order.objects.filter(status__in=active_statuses)
        .select_related("client")
        .order_by("-updated_at")[:100]
    )
    if not orders:
        return {"markers": [], "missing_live": []}

    order_ids = [o.pk for o in orders]
    by_order: defaultdict[int, list] = defaultdict(list)
    current_oid = None
    cnt = 0
    qs = LocationPing.objects.filter(order_id__in=order_ids).order_by("order_id", "-captured_at").values(
        "order_id", "latitude", "longitude", "captured_at", "driver_id"
    )
    for row in qs.iterator(chunk_size=400):
        oid = row["order_id"]
        if oid != current_oid:
            current_oid = oid
            cnt = 0
        if cnt >= trail_cap:
            continue
        by_order[oid].append(row)
        cnt += 1

    driver_ids = {r["driver_id"] for rows in by_order.values() for r in rows if r.get("driver_id")}
    dmap = {d.pk: d.full_name for d in Driver.objects.filter(pk__in=driver_ids)} if driver_ids else {}

    markers = []
    missing_live = []
    stale_before = timezone.now() - timedelta(minutes=10)
    for o in orders:
        rows = by_order.get(o.pk)
        if not rows:
            missing_live.append(
                {
                    "order_id": o.pk,
                    "status": o.get_status_display(),
                    "from_short": (o.from_location or "")[:48],
                    "to_short": (o.to_location or "")[:48],
                }
            )
            continue
        latest = rows[0]
        trail_chrono = list(reversed(rows))
        trail = [{"lat": float(r["latitude"]), "lon": float(r["longitude"])} for r in trail_chrono]
        did = latest.get("driver_id")
        cap = latest["captured_at"]
        is_stale = bool(cap < stale_before) if hasattr(cap, "__lt__") else False
        markers.append(
            {
                "order_id": o.pk,
                "driver": dmap.get(did, "") if did else "",
                "lat": float(latest["latitude"]),
                "lon": float(latest["longitude"]),
                "captured_at": cap.isoformat() if hasattr(cap, "isoformat") else str(cap),
                "status": o.status,
                "status_label": o.get_status_display(),
                "is_stale": is_stale,
                "from_location": o.from_location,
                "to_location": o.to_location,
                "client": str(o.client) if o.client else "",
                "trail": trail,
            }
        )
    return {
        "markers": markers,
        "missing_live": missing_live,
        "counts": {
            "total_orders": len(orders),
            "with_live": len(markers),
            "without_live": len(missing_live),
            "stale_live": len([m for m in markers if m.get("is_stale")]),
        },
    }


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def live_fleet_map(request):
    payload = _live_fleet_payload()
    return render(
        request,
        "analytics/live_fleet.html",
        {
            "fleet_markers": payload["markers"],
            "missing_count": len(payload["missing_live"]),
            "missing_live": payload["missing_live"],
            "fleet_counts": payload.get("counts", {}),
        },
    )


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def live_fleet_data(request):
    return JsonResponse({"ok": True, **_live_fleet_payload(), "server_time": timezone.now().isoformat()})
