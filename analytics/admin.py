from django.contrib import admin

from .models import (
    AlertEvent,
    AlertType,
    AnalyticsSettings,
    ClientAnalyticsSnapshot,
    DailyKPI,
    DriverPerformanceSnapshot,
    MonthlyFinanceReport,
    OrderAnalyticsSnapshot,
)


@admin.register(DailyKPI)
class DailyKPIAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "date",
        "total_orders",
        "completed_orders",
        "canceled_orders",
        "avg_final_price",
        "on_time_rate",
        "created_at",
        "updated_at",
    )


@admin.register(OrderAnalyticsSnapshot)
class OrderAnalyticsSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order",
        "assignment_minutes",
        "transit_minutes",
        "delay_minutes",
        "distance_km",
        "margin_amount",
        "created_at",
        "updated_at",
    )


@admin.register(DriverPerformanceSnapshot)
class DriverPerformanceSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "driver",
        "period_year",
        "period_month",
        "completed_count",
        "cancel_count",
        "issue_count",
        "on_time_rate",
        "monthly_earnings",
        "rating_score",
        "avg_delivery_time_minutes",
        "created_at",
        "updated_at",
    )
    list_filter = ("period_year", "period_month")
    search_fields = ("driver__full_name", "driver__phone")


@admin.register(ClientAnalyticsSnapshot)
class ClientAnalyticsSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "client",
        "period_year",
        "period_month",
        "total_orders",
        "completed_orders",
        "yearly_completed_orders",
        "sla_breach_count",
        "sla_breach_ratio",
        "client_rating_score",
        "total_revenue",
        "avg_order_value",
        "total_margin",
        "created_at",
        "updated_at",
    )
    list_filter = ("period_year", "period_month", "client")
    search_fields = ("client__name",)


@admin.register(MonthlyFinanceReport)
class MonthlyFinanceReportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "year",
        "month",
        "gross_revenue",
        "total_driver_cost",
        "total_fuel_cost",
        "total_extra_cost",
        "total_penalty",
        "net_margin",
        "total_margin",
        "completed_orders",
        "on_time_rate",
        "canceled_orders",
        "issue_orders",
        "created_at",
        "updated_at",
    )
    list_filter = ("year", "month")


@admin.register(AnalyticsSettings)
class AnalyticsSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "sla_base",
        "rating_completed_weight",
        "rating_quality_weight",
        "sla_breach_penalty_weight",
        "created_at",
        "updated_at",
    )


class FuelShortageOnlyFilter(admin.SimpleListFilter):
    title = "Kamomad hodisasi"
    parameter_name = "fuel_shortage"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Faqat kamomad (FUEL_SHORTAGE)"),
            ("no", "Kamomaddan tashqari"),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "yes":
            return queryset.filter(alert_type=AlertType.FUEL_SHORTAGE)
        if value == "no":
            return queryset.exclude(alert_type=AlertType.FUEL_SHORTAGE)
        return queryset


@admin.register(AlertEvent)
class AlertEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order",
        "driver",
        "alert_type",
        "shortage_kg_display",
        "shortage_penalty_display",
        "threshold_minutes",
        "message",
        "resolved",
        "created_at",
    )
    list_filter = ("alert_type", FuelShortageOnlyFilter, "resolved")
    search_fields = ("order__id", "driver__full_name", "message")

    @admin.display(description="Kamomad (kg)")
    def shortage_kg_display(self, obj: AlertEvent):
        if not obj.order_id or obj.alert_type != AlertType.FUEL_SHORTAGE:
            return "-"
        return obj.order.shortage_kg if obj.order.shortage_kg is not None else "-"

    @admin.display(description="Penalti (ball)")
    def shortage_penalty_display(self, obj: AlertEvent):
        if not obj.order_id or obj.alert_type != AlertType.FUEL_SHORTAGE:
            return "-"
        points = int(obj.order.shortage_penalty_points or 0)
        return f"-{points}" if points > 0 else "0"
