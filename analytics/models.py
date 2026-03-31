from django.db import models

from drivers.models import Driver
from orders.models import Order


class DailyKPI(models.Model):
    date = models.DateField(unique=True)
    total_orders = models.PositiveIntegerField(default=0)
    completed_orders = models.PositiveIntegerField(default=0)
    canceled_orders = models.PositiveIntegerField(default=0)
    avg_final_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    on_time_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Daily KPI"


class OrderAnalyticsSnapshot(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="analytics_snapshot")
    assignment_minutes = models.PositiveIntegerField(default=0)
    transit_minutes = models.PositiveIntegerField(default=0)
    delay_minutes = models.PositiveIntegerField(default=0)
    distance_km = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DriverPerformanceSnapshot(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="performance_snapshots")
    period_year = models.PositiveIntegerField()
    period_month = models.PositiveIntegerField()
    completed_count = models.PositiveIntegerField(default=0)
    cancel_count = models.PositiveIntegerField(default=0)
    issue_count = models.PositiveIntegerField(default=0)
    on_time_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    avg_delivery_time_minutes = models.PositiveIntegerField(default=0)
    monthly_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    yearly_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("driver", "period_year", "period_month")
        ordering = ["-period_year", "-period_month", "driver__full_name"]


class ClientAnalyticsSnapshot(models.Model):
    client = models.ForeignKey("orders.Client", on_delete=models.CASCADE, related_name="analytics_snapshots")
    period_year = models.PositiveIntegerField()
    period_month = models.PositiveIntegerField()
    total_orders = models.PositiveIntegerField(default=0)
    completed_orders = models.PositiveIntegerField(default=0)
    yearly_completed_orders = models.PositiveIntegerField(default=0)
    sla_breach_count = models.PositiveIntegerField(default=0)
    sla_breach_ratio = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    client_rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    avg_order_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_margin = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("client", "period_year", "period_month")
        ordering = ["-period_year", "-period_month", "client__name"]


class MonthlyFinanceReport(models.Model):
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()
    gross_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_driver_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_fuel_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_extra_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_penalty = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_margin = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_margin = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    completed_orders = models.PositiveIntegerField(default=0)
    canceled_orders = models.PositiveIntegerField(default=0)
    issue_orders = models.PositiveIntegerField(default=0)
    on_time_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("year", "month")
        ordering = ["-year", "-month"]


class AnalyticsSettings(models.Model):
    class SlaBase(models.TextChoices):
        PICKUP_TIME = "pickup_time", "Pickup time"
        ACTUAL_START_AT = "actual_start_at", "Actual start time"
        EXPLICIT_DEADLINE = "explicit_deadline", "Explicit order deadline"

    name = models.CharField(max_length=50, default="default", unique=True)
    rating_completed_weight = models.DecimalField(max_digits=5, decimal_places=2, default=70)
    rating_quality_weight = models.DecimalField(max_digits=5, decimal_places=2, default=30)
    sla_breach_penalty_weight = models.DecimalField(max_digits=5, decimal_places=2, default=20)
    sla_base = models.CharField(max_length=30, choices=SlaBase.choices, default=SlaBase.PICKUP_TIME)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Analytics setting"
        verbose_name_plural = "Analytics settings"

    def __str__(self) -> str:
        return self.name


class AlertType(models.TextChoices):
    SLA_ESCALATION = "sla_escalation", "SLA escalation"
    ROUTE_DEVIATION = "route_deviation", "Route deviation"
    GPS_SPOOFING = "gps_spoofing", "GPS spoofing risk"
    IMPOSSIBLE_SPEED = "impossible_speed", "Impossible speed"
    IDLE_ANOMALY = "idle_anomaly", "Long idle anomaly"
    DRIVER_DOC_EXPIRED = "driver_doc_expired", "Driver documents expired"
    DRIVER_KETDIK_WEBAPP = "driver_ketdik_webapp", "Haydovchi «Ketdik» (marshrut mini-ilova)"
    FUEL_SHORTAGE = "fuel_shortage", "Fuel shortage (loaded vs delivered)"
    NO_LIVE_TRACK = "no_live_track", "Live tracking not started after Ketdik"


class AlertEvent(models.Model):
    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE, related_name="alert_events", null=True, blank=True)
    driver = models.ForeignKey("drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True, related_name="alert_events")
    alert_type = models.CharField(max_length=40, choices=AlertType.choices)
    threshold_minutes = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=255)
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("order", "alert_type", "threshold_minutes")
        indexes = [
            models.Index(fields=["order", "alert_type"]),
            models.Index(fields=["alert_type", "resolved"]),
        ]
