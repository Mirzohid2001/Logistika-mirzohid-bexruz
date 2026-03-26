from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from dispatch.models import Assignment
from drivers.models import Driver, DriverStatus, Vehicle
from orders.models import Client, Order, OrderStatus, PaymentTerms
from tracking.models import LocationPing, LocationSource


class Command(BaseCommand):
    help = "Seed rich demo data for local development"

    def add_arguments(self, parser):
        parser.add_argument("--clients", type=int, default=14)
        parser.add_argument("--drivers", type=int, default=28)
        parser.add_argument("--orders", type=int, default=180)
        parser.add_argument("--seed", type=int, default=2026)

    @transaction.atomic
    def handle(self, *args, **options):
        clients_count = max(1, options["clients"])
        drivers_count = max(1, options["drivers"])
        orders_count = max(1, options["orders"])
        seed = options["seed"]
        random.seed(seed)

        clients = self._ensure_clients(clients_count)
        drivers = self._ensure_drivers(drivers_count)
        orders = self._ensure_orders(orders_count, clients, drivers)
        self._seed_locations(orders)

        now = timezone.now()
        call_command("build_monthly_reports", year=now.year, month=now.month)
        call_command("reconcile_finance")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed completed: clients={len(clients)}, drivers={len(drivers)}, orders={len(orders)}"
            )
        )

    def _ensure_clients(self, count: int) -> list[Client]:
        clients: list[Client] = []
        payment_terms = [PaymentTerms.PREPAID, PaymentTerms.DEFERRED]
        for idx in range(1, count + 1):
            client, _ = Client.objects.update_or_create(
                name=f"Demo Client {idx:02d}",
                defaults={
                    "contact_name": f"Manager {idx:02d}",
                    "phone": f"+99890{idx:07d}"[-13:],
                    "sla_minutes": random.choice([90, 120, 150, 180]),
                    "contract_base_rate_per_ton": Decimal(random.choice([85000, 98000, 110000, 125000])),
                    "contract_min_fee": Decimal(random.choice([700000, 850000, 1000000, 1200000])),
                    "payment_terms": random.choice(payment_terms),
                    "is_active": True,
                },
            )
            clients.append(client)
        return clients

    def _ensure_drivers(self, count: int) -> list[Driver]:
        drivers: list[Driver] = []
        for idx in range(1, count + 1):
            phone = f"+99891{idx:07d}"[-13:]
            driver, _ = Driver.objects.update_or_create(
                phone=phone,
                defaults={
                    "full_name": f"Demo Driver {idx:02d}",
                    "telegram_user_id": 900000000 + idx,
                    "status": DriverStatus.AVAILABLE,
                },
            )
            Vehicle.objects.update_or_create(
                plate_number=f"01A{idx:03d}BC",
                defaults={
                    "driver": driver,
                    "vehicle_type": random.choice(["Truck", "Tanker", "Semi-trailer"]),
                    "capacity_ton": Decimal(random.choice([8, 10, 12, 15, 18, 22])),
                },
            )
            drivers.append(driver)
        return drivers

    def _ensure_orders(self, count: int, clients: list[Client], drivers: list[Driver]) -> list[Order]:
        now = timezone.now()
        routes = [
            ("Fargona NPZ", "Toshkent Ombor"),
            ("Buxoro Terminal", "Samarqand Depot"),
            ("Qo'qon Bazasi", "Andijon Markaz"),
            ("Navoiy Reserve", "Jizzax Storage"),
            ("Sirdaryo Hub", "Namangan Point"),
        ]
        cargo_types = ["AI-80", "AI-92", "Diesel", "Bitum", "Jet Fuel"]
        statuses = [
            OrderStatus.NEW,
            OrderStatus.ASSIGNED,
            OrderStatus.IN_TRANSIT,
            OrderStatus.COMPLETED,
            OrderStatus.COMPLETED,
            OrderStatus.COMPLETED,
            OrderStatus.CANCELED,
            OrderStatus.ISSUE,
        ]
        orders: list[Order] = []

        for idx in range(1, count + 1):
            from_location, to_location = random.choice(routes)
            client = random.choice(clients)
            weight = Decimal(random.choice([6, 8, 10, 12, 15, 20]))
            pickup_time = now - timedelta(days=random.randint(0, 120), hours=random.randint(0, 22))
            status = random.choice(statuses)
            rate = client.contract_base_rate_per_ton or Decimal("100000")
            client_price = (rate * weight).quantize(Decimal("0.01"))
            driver_fee = (client_price * Decimal(random.choice(["0.55", "0.62", "0.68"]))).quantize(Decimal("0.01"))
            delivered_at = None
            if status == OrderStatus.COMPLETED:
                delivered_at = pickup_time + timedelta(hours=random.randint(2, 10))

            order = Order.objects.create(
                client=client,
                from_location=from_location,
                to_location=to_location,
                cargo_type=random.choice(cargo_types),
                weight_ton=weight,
                pickup_time=pickup_time,
                actual_start_at=pickup_time + timedelta(minutes=random.randint(0, 40)),
                contact_name=f"Operator {idx:03d}",
                contact_phone=f"+99893{idx:07d}"[-13:],
                comment=random.choice(
                    [
                        "Normal delivery",
                        "Rush delivery",
                        "Night operation",
                        "Bridge traffic delay",
                        "Driver checkpoint requested",
                    ]
                ),
                client_price=client_price,
                driver_fee=driver_fee,
                fuel_cost=(driver_fee * Decimal("0.16")).quantize(Decimal("0.01")),
                extra_cost=(driver_fee * Decimal("0.04")).quantize(Decimal("0.01")),
                penalty_amount=Decimal("0"),
                payment_terms=client.payment_terms,
                status=status,
                delivered_at=delivered_at,
                route_polyline=[
                    {"lat": 41.31, "lon": 69.24},
                    {"lat": 41.18, "lon": 69.32},
                    {"lat": 40.95, "lon": 69.48},
                ],
                geofence_polygon=[
                    {"lat": 41.90, "lon": 68.90},
                    {"lat": 40.20, "lon": 68.90},
                    {"lat": 40.20, "lon": 70.20},
                    {"lat": 41.90, "lon": 70.20},
                ],
                route_deviation_threshold_km=Decimal("4.00"),
            )
            if status in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT, OrderStatus.COMPLETED}:
                driver = random.choice(drivers)
                Assignment.objects.update_or_create(
                    order=order,
                    defaults={
                        "driver": driver,
                        "assigned_by": "seed-script",
                    },
                )
                if status in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT}:
                    driver.status = DriverStatus.BUSY
                    driver.save(update_fields=["status", "updated_at"])
            orders.append(order)

        return orders

    def _seed_locations(self, orders: list[Order]) -> None:
        now = timezone.now()
        for order in orders:
            assignment = Assignment.objects.filter(order=order).select_related("driver").first()
            if not assignment:
                continue
            for step in range(3):
                LocationPing.objects.create(
                    order=order,
                    driver=assignment.driver,
                    latitude=Decimal("41.20") + Decimal(step) * Decimal("0.01"),
                    longitude=Decimal("69.20") + Decimal(step) * Decimal("0.01"),
                    source=random.choice([LocationSource.TELEGRAM, LocationSource.WEB]),
                    captured_at=now - timedelta(minutes=random.randint(1, 120)),
                )
