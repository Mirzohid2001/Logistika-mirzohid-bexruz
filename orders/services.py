from django.conf import settings
from django.utils import timezone
from decimal import Decimal
import uuid

from orders.models import (
    ContractTariff,
    Order,
    OrderStateLog,
    OrderStatus,
    PaymentLedger,
    PaymentStatus,
    PaymentTerms,
    RevenueLedger,
)


ACTION_TO_STATUS = {
    "accept": OrderStatus.ASSIGNED,
    "reject": OrderStatus.CANCELED,
    "issue": OrderStatus.ISSUE,
    "start": OrderStatus.IN_TRANSIT,
    "complete": OrderStatus.COMPLETED,
    "reopen": OrderStatus.ISSUE,
}

ALLOWED_TRANSITIONS = {
    OrderStatus.NEW: {OrderStatus.ASSIGNED, OrderStatus.CANCELED, OrderStatus.ISSUE},
    OrderStatus.OFFERED: {OrderStatus.ASSIGNED, OrderStatus.CANCELED, OrderStatus.ISSUE},
    OrderStatus.ASSIGNED: {OrderStatus.IN_TRANSIT, OrderStatus.CANCELED, OrderStatus.ISSUE},
    OrderStatus.IN_TRANSIT: {OrderStatus.COMPLETED, OrderStatus.ISSUE},
    OrderStatus.ISSUE: {OrderStatus.ASSIGNED, OrderStatus.CANCELED},
    OrderStatus.COMPLETED: {OrderStatus.ISSUE},
    OrderStatus.CANCELED: {OrderStatus.ISSUE},
}


def transition_order(order: Order, to_status: str, changed_by: str) -> bool:
    from_status = order.status
    if from_status == to_status:
        return False
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        return False
    delivered_at_cleared = False
    actual_start_at_cleared = False
    pricing_cleared_fields: list[str] = []
    if to_status == OrderStatus.COMPLETED:
        if order.client_price is None:
            order.client_price = Decimal(order.price_final or order.price_suggested or 0)
        if order.driver_fee is None:
            order.driver_fee = Decimal("0")
        if Decimal(order.client_price) < 0 or Decimal(order.driver_fee) < 0:
            return False
        order.delivered_at = timezone.now()
    elif to_status == OrderStatus.IN_TRANSIT:
        # Trip haqiqiy boshlanganda actual_start_at ni belgilaymiz.
        if order.actual_start_at is None:
            order.actual_start_at = timezone.now()
    elif to_status in {OrderStatus.ISSUE, OrderStatus.CANCELED}:
        # Issue/canceled holatlarida delivered_at chalkash bo'lmasligi uchun clear qilamiz.
        if order.delivered_at is not None:
            order.delivered_at = None
            delivered_at_cleared = True
        if order.actual_start_at is not None:
            order.actual_start_at = None
            actual_start_at_cleared = True
        if getattr(settings, "ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED", True):
            # Finans kontekst: delivery bo'lmagani uchun narx/fee va margin uchun ishlatiladigan qiymatlarni nolga qaytaramiz.
            # Ledgerlar ham shu holatga keltiriladi (reconcile_finance buni keyinroq tasdiqlaydi).
            if order.price_final is not None:
                order.price_final = None
                pricing_cleared_fields.append("price_final")
            if order.client_price != 0:
                order.client_price = Decimal("0")
                pricing_cleared_fields.append("client_price")
            if order.driver_fee != 0:
                order.driver_fee = Decimal("0")
                pricing_cleared_fields.append("driver_fee")
            if getattr(order, "fuel_cost", None) not in (None, 0):
                order.fuel_cost = Decimal("0")
                pricing_cleared_fields.append("fuel_cost")
            if getattr(order, "extra_cost", None) not in (None, 0):
                order.extra_cost = Decimal("0")
                pricing_cleared_fields.append("extra_cost")
            if getattr(order, "penalty_amount", None) not in (None, 0):
                order.penalty_amount = Decimal("0")
                pricing_cleared_fields.append("penalty_amount")
    order.status = to_status
    update_fields = ["status", "updated_at"]
    if to_status == OrderStatus.COMPLETED:
        update_fields.extend(["delivered_at", "client_price", "driver_fee"])
    elif delivered_at_cleared:
        update_fields.append("delivered_at")
    if actual_start_at_cleared:
        update_fields.append("actual_start_at")
    if to_status == OrderStatus.IN_TRANSIT:
        update_fields.append("actual_start_at")
    if pricing_cleared_fields:
        update_fields.extend(pricing_cleared_fields)
    order.save(update_fields=update_fields)

    # Finans ledgerlarni issue/canceled paytida "pending / 0" holatga moslaymiz (policy yoq bo'lsa).
    if to_status in {OrderStatus.ISSUE, OrderStatus.CANCELED} and getattr(
        settings, "ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED", True
    ):
        due_date = (order.delivered_at or order.pickup_time).date()

        PaymentLedger.objects.filter(order=order).update(
            amount=order.driver_fee or Decimal("0"),
            paid_amount=Decimal("0"),
            status=PaymentStatus.PENDING,
            due_date=due_date,
            paid_at=None,
            note="",
        )
        RevenueLedger.objects.filter(order=order).update(
            amount=order.client_price or Decimal("0"),
            received_amount=Decimal("0"),
            status=PaymentStatus.PENDING,
            received_at=None,
            note="",
        )

        if not PaymentLedger.objects.filter(order=order).exists():
            PaymentLedger.objects.create(
                order=order,
                amount=order.driver_fee or Decimal("0"),
                paid_amount=Decimal("0"),
                status=PaymentStatus.PENDING,
                due_date=due_date,
                paid_at=None,
                note="",
            )
        if not RevenueLedger.objects.filter(order=order).exists():
            RevenueLedger.objects.create(
                order=order,
                amount=order.client_price or Decimal("0"),
                received_amount=Decimal("0"),
                status=PaymentStatus.PENDING,
                received_at=None,
                note="",
            )
    OrderStateLog.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
    )

    # Alert lifecycle: order status o'zgarishi monitoringni to'xtatadi.
    # Assigned/In transit paytida location-based alertlar faol bo'ladi,
    # COMPLETED/CANCELED/ISSUE ga o'tganda unresolved alertlarni yopamiz.
    if to_status in {OrderStatus.COMPLETED, OrderStatus.CANCELED, OrderStatus.ISSUE}:
        try:
            from analytics.models import AlertEvent

            AlertEvent.objects.filter(order=order, resolved=False).update(resolved=True)
        except Exception:
            # Analytics app topilmasa yoki migratsiya qilinmagan bo'lsa flow buzilmasin.
            pass
    return True


def apply_client_contract(order: Order, *, set_client_price: bool = True) -> None:
    """Klient kontraktidan to‘lov sharti va (ixtiyoriy) klient narxini yozadi."""
    if not order.client:
        return
    order.payment_terms = order.client.payment_terms
    if not set_client_price:
        return
    applicable_tariff = (
        ContractTariff.objects.filter(client=order.client, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if applicable_tariff:
        weight = Decimal(str(order.weight_ton or 0))
        price = (weight * applicable_tariff.rate_per_ton).quantize(Decimal("0.01"))
        order.client_price = max(price, applicable_tariff.min_fee)
    elif order.client.contract_base_rate_per_ton > 0:
        weight = Decimal(str(order.weight_ton or 0))
        fallback_price = (weight * order.client.contract_base_rate_per_ton).quantize(Decimal("0.01"))
        order.client_price = max(fallback_price, order.client.contract_min_fee)


def reopen_order(order: Order, changed_by: str) -> bool:
    return transition_order(order, OrderStatus.ISSUE, changed_by=changed_by)


def create_return_trip(order: Order, changed_by: str) -> Order:
    return Order.objects.create(
        client=order.client,
        from_location=order.to_location,
        to_location=order.from_location,
        cargo_type=order.cargo_type,
        weight_ton=order.weight_ton,
        pickup_time=timezone.now(),
        contact_name=order.contact_name,
        contact_phone=order.contact_phone,
        comment=f"Return trip for order #{order.pk}",
        payment_terms=order.payment_terms,
        client_price=order.client_price,
        driver_fee=order.driver_fee,
        is_return_trip=True,
        return_of=order,
        status=OrderStatus.NEW,
    )


def split_shipment(order: Order, parts: int, changed_by: str) -> list[Order]:
    if parts < 2:
        return []
    split_code = str(uuid.uuid4())
    weight = Decimal(str(order.weight_ton or 0))
    part_weight = (weight / Decimal(parts)).quantize(Decimal("0.01"))
    children = []
    for idx in range(1, parts + 1):
        client_price = (Decimal(str(order.client_price or 0)) / Decimal(parts)).quantize(Decimal("0.01"))
        driver_fee = (Decimal(str(order.driver_fee or 0)) / Decimal(parts)).quantize(Decimal("0.01"))
        child = Order.objects.create(
            client=order.client,
            from_location=order.from_location,
            to_location=order.to_location,
            cargo_type=order.cargo_type,
            weight_ton=part_weight,
            pickup_time=order.pickup_time,
            contact_name=order.contact_name,
            contact_phone=order.contact_phone,
            comment=f"Split from order #{order.pk}",
            payment_terms=order.payment_terms,
            client_price=client_price,
            driver_fee=driver_fee,
            parent_order=order,
            split_group_code=split_code,
            split_index=idx,
            split_total=parts,
            status=OrderStatus.NEW,
        )
        children.append(child)
    transition_order(order, OrderStatus.ISSUE, changed_by=changed_by)
    return children
