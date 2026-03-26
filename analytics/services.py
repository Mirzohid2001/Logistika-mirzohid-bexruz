from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import models
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone as django_timezone

from drivers.models import Driver
from orders.models import Client, Order, OrderStatus

from .models import AnalyticsSettings, ClientAnalyticsSnapshot, DriverPerformanceSnapshot, MonthlyFinanceReport


def _revenue_orders_qs(base_qs):
    """P&L uchun buyurtmalar: default faqat COMPLETED. gross_revenue = Sum(client_price) — Shofir modelida klientdan tushum yo‘q, maydon odatda 0."""
    if getattr(django_settings, "ANALYTICS_REVENUE_SUM_COMPLETED_ONLY", True):
        return base_qs.filter(status=OrderStatus.COMPLETED)
    return base_qs


def _month_delivered_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    d0 = date(year, month, 1)
    if month == 12:
        d1 = date(year + 1, 1, 1)
    else:
        d1 = date(year, month + 1, 1)
    tz = django_timezone.get_current_timezone()
    return (
        django_timezone.make_aware(datetime.combine(d0, time.min), tz),
        django_timezone.make_aware(datetime.combine(d1, time.min), tz),
    )


def aggregate_order_pnl(order_qs):
    """Buyurtmalar to‘plami bo‘yicha P&L (Decimal). gross_revenue nomi tarixiy; klientdan tushum yo‘q bo‘lsa bu yig‘indi 0 bo‘ladi."""
    a = order_qs.aggregate(
        n=Count("id"),
        g=Sum("client_price"),
        d=Sum("driver_fee"),
        f=Sum("fuel_cost"),
        e=Sum("extra_cost"),
        p=Sum("penalty_amount"),
    )

    def dec(v):
        return Decimal(v or 0)

    gross, driver, fuel, extra, pen = dec(a["g"]), dec(a["d"]), dec(a["f"]), dec(a["e"]), dec(a["p"])
    net = gross - driver - fuel - extra - pen
    return {
        "order_count": a["n"] or 0,
        "gross_revenue": gross,
        "total_driver_cost": driver,
        "total_fuel_cost": fuel,
        "total_extra_cost": extra,
        "total_penalty": pen,
        "net_margin": net,
    }


def rebuild_monthly_reports(year: int, month: int) -> None:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    base_qs = Order.objects.filter(created_at__date__gte=start, created_at__date__lt=end)
    delivered_start, delivered_end = _month_delivered_bounds(year, month)
    fin_qs = Order.objects.filter(
        status=OrderStatus.COMPLETED,
        delivered_at__gte=delivered_start,
        delivered_at__lt=delivered_end,
    )
    totals = base_qs.aggregate(
        canceled_orders=Count("id", filter=Q(status=OrderStatus.CANCELED)),
        issue_orders=Count("id", filter=Q(status=OrderStatus.ISSUE)),
    )
    pnl = aggregate_order_pnl(_revenue_orders_qs(fin_qs))
    gross_revenue = pnl["gross_revenue"]
    driver_cost = pnl["total_driver_cost"]
    fuel_cost = pnl["total_fuel_cost"]
    extra_cost = pnl["total_extra_cost"]
    penalty = pnl["total_penalty"]
    net_margin = pnl["net_margin"]
    completed_count = pnl["order_count"]
    settings_obj = AnalyticsSettings.objects.first()

    on_time_count = 0
    for order in fin_qs.select_related("client"):
        if not order.client:
            continue
        breached = _is_sla_breached(order, order.client.sla_minutes, settings_obj)
        if not breached:
            on_time_count += 1

    on_time_rate = (
        Decimal(on_time_count * 100 / completed_count).quantize(Decimal("0.01"))
        if completed_count
        else Decimal("0")
    )
    MonthlyFinanceReport.objects.update_or_create(
        year=year,
        month=month,
        defaults={
            "gross_revenue": gross_revenue,
            "total_driver_cost": driver_cost,
            "total_fuel_cost": fuel_cost,
            "total_extra_cost": extra_cost,
            "total_penalty": penalty,
            "total_margin": net_margin,
            "net_margin": net_margin,
            "completed_orders": completed_count,
            "canceled_orders": totals["canceled_orders"] or 0,
            "issue_orders": totals["issue_orders"] or 0,
            "on_time_rate": on_time_rate,
        },
    )

    _rebuild_client_snapshots(base_qs, year, month)
    _rebuild_driver_snapshots(base_qs, year, month)


def _rebuild_client_snapshots(base_qs, year: int, month: int) -> None:
    settings = AnalyticsSettings.objects.first()
    completed_weight = Decimal(settings.rating_completed_weight if settings else 70)
    quality_weight = Decimal(settings.rating_quality_weight if settings else 30)
    sla_penalty_weight = Decimal(settings.sla_breach_penalty_weight if settings else 20)
    for client in Client.objects.filter(is_active=True):
        qs = base_qs.filter(client=client)
        total_orders = qs.count()
        if total_orders == 0:
            continue
        completed_orders = qs.filter(status=OrderStatus.COMPLETED).count()
        issue_orders = qs.filter(status=OrderStatus.ISSUE).count()
        canceled_orders = qs.filter(status=OrderStatus.CANCELED).count()
        sla_breach_count = 0
        for order in qs.filter(status=OrderStatus.COMPLETED, delivered_at__isnull=False):
            if _is_sla_breached(order, client.sla_minutes, settings):
                sla_breach_count += 1
        sla_breach_ratio = Decimal(sla_breach_count * 100 / completed_orders).quantize(Decimal("0.01")) if completed_orders else Decimal("0")
        yearly_completed_orders = client.orders.filter(
            created_at__year=year, status=OrderStatus.COMPLETED
        ).count()
        success_ratio = Decimal(completed_orders) / Decimal(total_orders)
        quality_ratio = Decimal("1") - (Decimal(issue_orders + canceled_orders) / Decimal(total_orders))
        raw_score = completed_weight * success_ratio + quality_weight * quality_ratio
        penalty = (sla_breach_ratio / Decimal("100")) * sla_penalty_weight
        client_rating_score = max(Decimal("0"), raw_score - penalty).quantize(Decimal("0.01"))
        revenue_subset = _revenue_orders_qs(qs)
        total_revenue = revenue_subset.aggregate(value=Sum("client_price"))["value"] or Decimal("0")
        total_margin = sum((order.gross_margin for order in revenue_subset), Decimal("0"))
        if getattr(django_settings, "ANALYTICS_REVENUE_SUM_COMPLETED_ONLY", True):
            denom = completed_orders if completed_orders else 0
            avg_order_value = (
                (total_revenue / Decimal(denom)).quantize(Decimal("0.01")) if denom else Decimal("0")
            )
        else:
            avg_order_value = (total_revenue / total_orders).quantize(Decimal("0.01"))
        ClientAnalyticsSnapshot.objects.update_or_create(
            client=client,
            period_year=year,
            period_month=month,
            defaults={
                "total_orders": total_orders,
                "completed_orders": completed_orders,
                "yearly_completed_orders": yearly_completed_orders,
                "sla_breach_count": sla_breach_count,
                "sla_breach_ratio": sla_breach_ratio,
                "client_rating_score": client_rating_score,
                "total_revenue": total_revenue,
                "avg_order_value": avg_order_value,
                "total_margin": total_margin,
            },
        )


def _is_sla_breached(order: Order, sla_minutes: int, settings: AnalyticsSettings | None) -> bool:
    if not order.delivered_at:
        return False
    base_mode = settings.sla_base if settings else AnalyticsSettings.SlaBase.PICKUP_TIME
    if base_mode == AnalyticsSettings.SlaBase.EXPLICIT_DEADLINE:
        if not order.sla_deadline_at:
            return False
        return order.delivered_at > order.sla_deadline_at
    if base_mode == AnalyticsSettings.SlaBase.ACTUAL_START_AT and order.actual_start_at:
        deadline = order.actual_start_at + timedelta(minutes=sla_minutes)
        return order.delivered_at > deadline
    deadline = order.pickup_time + timedelta(minutes=sla_minutes)
    return order.delivered_at > deadline


def _rebuild_driver_snapshots(base_qs, year: int, month: int) -> None:
    for driver in Driver.objects.all():
        assignments = driver.assignments.filter(order__in=base_qs).select_related("order")
        total_orders = assignments.count()
        if total_orders == 0:
            continue
        completed_orders = assignments.filter(order__status=OrderStatus.COMPLETED).count()
        cancel_orders = assignments.filter(order__status=OrderStatus.CANCELED).count()
        issue_orders = assignments.filter(order__status=OrderStatus.ISSUE).count()
        if getattr(django_settings, "ANALYTICS_REVENUE_SUM_COMPLETED_ONLY", True):
            earnings_assignments = assignments.filter(order__status=OrderStatus.COMPLETED)
        else:
            earnings_assignments = assignments
        monthly_earnings = earnings_assignments.aggregate(value=Sum("order__driver_fee"))["value"] or Decimal("0")
        yearly_earnings = driver.assignments.filter(
            order__created_at__year=year, order__status=OrderStatus.COMPLETED
        ).aggregate(value=Sum("order__driver_fee"))["value"] or Decimal("0")
        on_time_rate = Decimal(completed_orders * 100 / total_orders).quantize(Decimal("0.01"))
        avg_delivery = assignments.aggregate(
            value=Avg(models.ExpressionWrapper(models.F("order__delivered_at") - models.F("order__pickup_time"), output_field=models.DurationField()))
        )["value"]
        avg_delivery_minutes = int(avg_delivery.total_seconds() // 60) if avg_delivery else 0
        rating_score = (
            Decimal("70") * (on_time_rate / Decimal("100"))
            + Decimal("20") * (Decimal(completed_orders) / Decimal(total_orders))
            + Decimal("10") * (Decimal("1") - Decimal(issue_orders) / Decimal(total_orders))
        ).quantize(Decimal("0.01"))
        DriverPerformanceSnapshot.objects.update_or_create(
            driver=driver,
            period_year=year,
            period_month=month,
            defaults={
                "completed_count": completed_orders,
                "cancel_count": cancel_orders,
                "issue_count": issue_orders,
                "on_time_rate": on_time_rate,
                "avg_delivery_time_minutes": avg_delivery_minutes,
                "monthly_earnings": monthly_earnings,
                "yearly_earnings": yearly_earnings,
                "rating_score": rating_score,
            },
        )


def last_n_calendar_months_end_at(year: int, month: int, n: int = 6) -> list[tuple[int, int]]:
    """Oxirgi oy inclusive; oldinga n-1 oy (jami n ta oy)."""
    out: list[tuple[int, int]] = []
    y, m = year, month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    out.reverse()
    return out


def driver_fee_breakdown_delivered_between(delivered_start, delivered_end):
    """Haydovchiga tushgan jami (buyurtma driver_fee) va reyslar soni."""
    from dispatch.models import Assignment

    fin_qs = Order.objects.filter(
        status=OrderStatus.COMPLETED,
        delivered_at__gte=delivered_start,
        delivered_at__lt=delivered_end,
    )
    if not fin_qs.exists():
        return []
    return list(
        Assignment.objects.filter(order__in=fin_qs)
        .values("driver_id", "driver__full_name")
        .annotate(total_fee=Sum("order__driver_fee"), trips=Count("id"))
        .order_by("-total_fee")
    )
