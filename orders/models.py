from django.db import models
from django.utils import timezone
from decimal import Decimal
import uuid


class QuantityUnit(models.TextChoices):
    """Yuk hajmi o‘lchov birligi (zavod / klient hujjatlari bilan moslash uchun)."""

    TON = "ton", "Tonna"
    KG = "kg", "Kg"
    LITER = "liter", "Litr"


class OrderStatus(models.TextChoices):
    NEW = "new", "New"
    OFFERED = "offered", "Offered"
    ASSIGNED = "assigned", "Assigned"
    IN_TRANSIT = "in_transit", "In Transit"
    COMPLETED = "completed", "Completed"
    CANCELED = "canceled", "Canceled"
    ISSUE = "issue", "Issue"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PARTIAL = "partial", "Partial"
    PAID = "paid", "Paid"


class PaymentTerms(models.TextChoices):
    PREPAID = "prepaid", "Prepaid"
    DEFERRED = "deferred", "Deferred"


class Client(models.Model):
    name = models.CharField(max_length=150, unique=True)
    contact_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    sla_minutes = models.PositiveIntegerField(default=120)
    contract_base_rate_per_ton = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    contract_min_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_terms = models.CharField(max_length=20, choices=PaymentTerms.choices, default=PaymentTerms.DEFERRED)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ContractTariff(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contract_tariffs")
    cargo_type = models.CharField(max_length=120, blank=True)
    from_location = models.CharField(max_length=255, blank=True)
    to_location = models.CharField(max_length=255, blank=True)
    rate_per_ton = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    min_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class Order(models.Model):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    from_location = models.CharField(max_length=255)
    to_location = models.CharField(max_length=255)
    cargo_type = models.CharField(max_length=120)
    weight_ton = models.DecimalField(max_digits=8, decimal_places=2)
    # Reja (kontrakt) — tonnada; yo‘qotish nazorati uchun quyidagilar bilan solishtiriladi.
    loaded_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    loaded_quantity_uom = models.CharField(
        max_length=10,
        choices=QuantityUnit.choices,
        default=QuantityUnit.TON,
    )
    loaded_recorded_at = models.DateTimeField(null=True, blank=True)
    loaded_recorded_by = models.CharField(max_length=120, blank=True)
    delivered_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    delivered_quantity_uom = models.CharField(
        max_length=10,
        choices=QuantityUnit.choices,
        default=QuantityUnit.TON,
    )
    delivered_recorded_at = models.DateTimeField(null=True, blank=True)
    delivered_recorded_by = models.CharField(max_length=120, blank=True)
    # Litr → massa: kg/L (masalan neft mahsuloti ~0.75–0.85). Litr kiritilganda ishlatiladi.
    density_kg_per_liter = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    pickup_time = models.DateTimeField()
    actual_start_at = models.DateTimeField(null=True, blank=True)
    sla_deadline_at = models.DateTimeField(null=True, blank=True)
    contact_name = models.CharField(max_length=120)
    contact_phone = models.CharField(max_length=30)
    comment = models.TextField(blank=True)
    route_polyline = models.JSONField(default=list, blank=True)
    geofence_polygon = models.JSONField(default=list, blank=True)
    route_deviation_threshold_km = models.DecimalField(max_digits=6, decimal_places=2, default=3)
    price_suggested = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_final = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    client_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    driver_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fuel_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    extra_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    penalty_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_terms = models.CharField(max_length=20, choices=PaymentTerms.choices, default=PaymentTerms.DEFERRED)
    is_return_trip = models.BooleanField(default=False)
    return_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="return_orders")
    parent_order = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="split_children")
    split_group_code = models.CharField(max_length=36, blank=True)
    split_index = models.PositiveIntegerField(default=0)
    split_total = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.NEW)
    delivered_at = models.DateTimeField(null=True, blank=True)
    shortage_kg = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shortage_penalty_points = models.PositiveIntegerField(default=0)
    shortage_note = models.TextField(blank=True)
    shortage_flagged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.from_location} -> {self.to_location}"

    @property
    def gross_margin(self) -> Decimal:
        return (
            Decimal(self.client_price or 0)
            - Decimal(self.driver_fee or 0)
            - Decimal(self.fuel_cost or 0)
            - Decimal(self.extra_cost or 0)
            - Decimal(self.penalty_amount or 0)
        )

    @property
    def margin_percent(self) -> Decimal:
        revenue = Decimal(self.client_price or 0)
        if revenue <= 0:
            return Decimal("0")
        return (self.gross_margin / revenue * Decimal("100")).quantize(Decimal("0.01"))

    @property
    def loaded_quantity_metric_ton(self) -> Decimal | None:
        from orders.quantity import quantity_to_metric_tonnes

        if self.loaded_quantity is None:
            return None
        return quantity_to_metric_tonnes(
            self.loaded_quantity,
            self.loaded_quantity_uom,
            density_kg_per_liter=self.density_kg_per_liter,
        )

    @property
    def delivered_quantity_metric_ton(self) -> Decimal | None:
        from orders.quantity import quantity_to_metric_tonnes

        if self.delivered_quantity is None:
            return None
        return quantity_to_metric_tonnes(
            self.delivered_quantity,
            self.delivered_quantity_uom,
            density_kg_per_liter=self.density_kg_per_liter,
        )

    @property
    def quantity_shortage_metric_ton(self) -> Decimal | None:
        """Yuklangan (fakt) minus klientga topshirilgan; ijobiy — ehtimoliy yo‘qotish."""
        from orders.quantity import shortage_tonnes

        return shortage_tonnes(self.loaded_quantity_metric_ton, self.delivered_quantity_metric_ton)

    @property
    def quantity_shortage_vs_planned_ton(self) -> Decimal | None:
        """Reja (weight_ton) minus topshirilgan; ijobiy — rejadan kam berilgan."""
        from orders.quantity import shortage_tonnes

        planned = Decimal(self.weight_ton or 0)
        return shortage_tonnes(planned, self.delivered_quantity_metric_ton)


class OrderStateLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="state_logs")
    from_status = models.CharField(max_length=20, choices=OrderStatus.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=OrderStatus.choices)
    changed_by = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class PaymentLedger(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="driver_payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    due_date = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RevenueLedger(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="client_revenues")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    received_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

