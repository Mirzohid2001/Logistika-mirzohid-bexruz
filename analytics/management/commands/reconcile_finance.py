from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from orders.models import Order, PaymentLedger, PaymentStatus, RevenueLedger


class Command(BaseCommand):
    help = "Revenue va payment ledgerlarni buyurtmalar bilan moslashtirish. Eslatma: klientdan tushum yo‘q modelda Revenue asosan 0."

    def handle(self, *args, **options):
        now = timezone.now()

        orders_qs = Order.objects.filter(Q(client_price__gt=0) | Q(driver_fee__gt=0)).select_related("client")

        with transaction.atomic():
            for order in orders_qs:
                due_date = (order.delivered_at or order.pickup_time).date()

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
                else:
                    PaymentLedger.objects.filter(order=order).update(amount=order.driver_fee or Decimal("0"))

                if not RevenueLedger.objects.filter(order=order).exists():
                    RevenueLedger.objects.create(
                        order=order,
                        amount=order.client_price or Decimal("0"),
                        received_amount=Decimal("0"),
                        status=PaymentStatus.PENDING,
                        received_at=None,
                        note="",
                    )
                else:
                    RevenueLedger.objects.filter(order=order).update(amount=order.client_price or Decimal("0"))

            for ledger in PaymentLedger.objects.select_related("order").all():
                amount = ledger.amount or Decimal("0")
                paid_amount = ledger.paid_amount or Decimal("0")

                if amount <= 0:
                    new_status = PaymentStatus.PENDING
                elif paid_amount >= amount:
                    new_status = PaymentStatus.PAID
                elif paid_amount > 0:
                    new_status = PaymentStatus.PARTIAL
                else:
                    new_status = PaymentStatus.PENDING

                fields_to_update: list[str] = []
                if ledger.status != new_status:
                    ledger.status = new_status
                    fields_to_update.append("status")

                if ledger.due_date is None:
                    ledger.due_date = (ledger.order.delivered_at or ledger.order.pickup_time).date()
                    fields_to_update.append("due_date")

                if new_status == PaymentStatus.PAID and ledger.paid_at is None and paid_amount > 0:
                    ledger.paid_at = now
                    fields_to_update.append("paid_at")

                if fields_to_update:
                    ledger.save(update_fields=fields_to_update)

            for ledger in RevenueLedger.objects.select_related("order").all():
                amount = ledger.amount or Decimal("0")
                received_amount = ledger.received_amount or Decimal("0")

                if amount <= 0:
                    new_status = PaymentStatus.PENDING
                elif received_amount >= amount:
                    new_status = PaymentStatus.PAID
                elif received_amount > 0:
                    new_status = PaymentStatus.PARTIAL
                else:
                    new_status = PaymentStatus.PENDING

                fields_to_update: list[str] = []
                if ledger.status != new_status:
                    ledger.status = new_status
                    fields_to_update.append("status")

                if new_status == PaymentStatus.PAID and ledger.received_at is None and received_amount > 0:
                    ledger.received_at = now
                    fields_to_update.append("received_at")

                if fields_to_update:
                    ledger.save(update_fields=fields_to_update)

        revenue_total = RevenueLedger.objects.aggregate(value=models.Sum("received_amount"))["value"] or Decimal("0")
        payment_total = PaymentLedger.objects.aggregate(value=models.Sum("paid_amount"))["value"] or Decimal("0")
        delta = revenue_total - payment_total
        self.stdout.write(self.style.SUCCESS(f"Revenue total: {revenue_total}"))
        self.stdout.write(self.style.SUCCESS(f"Payment total: {payment_total}"))
        if delta < 0:
            self.stdout.write(self.style.WARNING(f"Delta: {delta} (overpayment)"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Delta: {delta}"))
