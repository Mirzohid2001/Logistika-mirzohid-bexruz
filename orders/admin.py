from django.contrib import admin
from django.http import HttpResponse
import csv

from .models import Client, ContractTariff, Order, OrderStateLog, PaymentLedger, RevenueLedger
from .services import create_return_trip, reopen_order, split_shipment


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "phone", "sla_minutes", "payment_terms", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "contact_name", "phone")


@admin.register(ContractTariff)
class ContractTariffAdmin(admin.ModelAdmin):
    list_display = ("client", "cargo_type", "from_location", "to_location", "rate_per_ton", "min_fee", "is_active")
    list_filter = ("is_active", "client")
    search_fields = ("client__name", "cargo_type", "from_location", "to_location")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "client",
        "from_location",
        "to_location",
        "cargo_type",
        "weight_ton",
        "pickup_time",
        "actual_start_at",
        "sla_deadline_at",
        "contact_name",
        "contact_phone",
        "comment",
        "status",
        "price_suggested",
        "price_final",
        "client_price",
        "driver_fee",
        "fuel_cost",
        "extra_cost",
        "penalty_amount",
        "payment_terms",
        "is_return_trip",
        "return_of",
        "split_group_code",
        "split_index",
        "split_total",
        "route_polyline_points",
        "geofence_polygon_points",
        "route_deviation_threshold_km",
        "gross_margin_display",
        "margin_percent",
        "delivered_at",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "cargo_type", "client", "payment_terms", "created_at")
    search_fields = ("from_location", "to_location", "contact_name", "contact_phone", "client__name")
    actions = ("export_orders_csv", "action_reopen_orders", "action_create_return_trip", "action_split_two")

    @admin.display(description="Margin")
    def gross_margin_display(self, obj: Order):
        return obj.gross_margin

    @admin.display(description="Route pts")
    def route_polyline_points(self, obj: Order):
        try:
            return len(obj.route_polyline or [])
        except Exception:
            return "-"

    @admin.display(description="Geofence pts")
    def geofence_polygon_points(self, obj: Order):
        try:
            return len(obj.geofence_polygon or [])
        except Exception:
            return "-"

    @admin.display(description="Margin %")
    def margin_percent(self, obj: Order):
        try:
            return obj.margin_percent
        except Exception:
            return "-"

    @admin.action(description="Export selected orders to CSV")
    def export_orders_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="orders_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["ID", "Client", "From", "To", "Status", "ClientPrice", "DriverFee", "GrossMargin"])
        for order in queryset:
            writer.writerow(
                [
                    order.pk,
                    order.client.name if order.client else "",
                    order.from_location,
                    order.to_location,
                    order.status,
                    order.client_price,
                    order.driver_fee,
                    order.gross_margin,
                ]
            )
        return response

    @admin.action(description="Reopen selected orders to Issue")
    def action_reopen_orders(self, request, queryset):
        for order in queryset:
            reopen_order(order, changed_by=request.user.username)

    @admin.action(description="Create return trip for selected orders")
    def action_create_return_trip(self, request, queryset):
        for order in queryset:
            create_return_trip(order, changed_by=request.user.username)

    @admin.action(description="Split selected orders into 2")
    def action_split_two(self, request, queryset):
        for order in queryset:
            split_shipment(order, parts=2, changed_by=request.user.username)


@admin.register(OrderStateLog)
class OrderStateLogAdmin(admin.ModelAdmin):
    list_display = ("order", "from_status", "to_status", "changed_by", "created_at")
    list_filter = ("to_status",)


@admin.register(PaymentLedger)
class PaymentLedgerAdmin(admin.ModelAdmin):
    list_display = ("order", "amount", "paid_amount", "status", "due_date", "paid_at")
    list_filter = ("status", "due_date")
    search_fields = ("order__id", "order__client__name")


@admin.register(RevenueLedger)
class RevenueLedgerAdmin(admin.ModelAdmin):
    list_display = ("order", "amount", "received_amount", "status", "received_at")
    list_filter = ("status", "received_at")
    search_fields = ("order__id", "order__client__name")
