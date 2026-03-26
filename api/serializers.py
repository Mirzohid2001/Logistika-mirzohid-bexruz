from rest_framework import serializers

from analytics.models import ClientAnalyticsSnapshot, DriverPerformanceSnapshot
from drivers.models import Driver
from orders.models import Client, Order


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name", "contact_name", "phone", "sla_minutes", "payment_terms", "is_active"]


class DriverSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = ["id", "full_name", "phone", "telegram_user_id", "status"]


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = [
            "id",
            "client_id",
            "from_location",
            "to_location",
            "cargo_type",
            "weight_ton",
            "status",
            "pickup_time",
            "delivered_at",
            "client_price",
            "driver_fee",
            "created_at",
        ]


class DriverPerformanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverPerformanceSnapshot
        fields = [
            "driver_id",
            "period_year",
            "period_month",
            "completed_count",
            "cancel_count",
            "issue_count",
            "on_time_rate",
            "monthly_earnings",
            "yearly_earnings",
            "rating_score",
        ]


class ClientAnalyticsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientAnalyticsSnapshot
        fields = [
            "client_id",
            "period_year",
            "period_month",
            "total_orders",
            "completed_orders",
            "yearly_completed_orders",
            "sla_breach_count",
            "sla_breach_ratio",
            "client_rating_score",
        ]
