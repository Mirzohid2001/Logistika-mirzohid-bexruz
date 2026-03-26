import csv
from decimal import Decimal

from django.core.management.base import BaseCommand

from drivers.models import Driver, DriverStatus, Vehicle


class Command(BaseCommand):
    help = "Import drivers and vehicles from CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        created = 0
        updated = 0
        with open(csv_path, newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                phone = (row.get("phone") or "").strip()
                full_name = (row.get("full_name") or "").strip()
                if not phone or not full_name:
                    continue
                status_raw = (row.get("status") or DriverStatus.OFFLINE).strip().lower()
                status = status_raw if status_raw in {DriverStatus.AVAILABLE, DriverStatus.BUSY, DriverStatus.OFFLINE} else DriverStatus.OFFLINE
                telegram_user_id_raw = (row.get("telegram_user_id") or "").strip()
                telegram_user_id = int(telegram_user_id_raw) if telegram_user_id_raw.isdigit() else None
                driver, is_created = Driver.objects.update_or_create(
                    phone=phone,
                    defaults={
                        "full_name": full_name,
                        "status": status,
                        "telegram_user_id": telegram_user_id,
                    },
                )
                capacity_raw = (row.get("capacity_ton") or "0").strip()
                try:
                    capacity = Decimal(capacity_raw)
                except Exception:
                    capacity = Decimal("0")
                plate_number = (row.get("plate_number") or "").strip()
                vehicle_type = (row.get("vehicle_type") or "truck").strip()
                if plate_number:
                    Vehicle.objects.update_or_create(
                        plate_number=plate_number,
                        defaults={
                            "driver": driver,
                            "vehicle_type": vehicle_type,
                            "capacity_ton": capacity,
                        },
                    )
                if is_created:
                    created += 1
                else:
                    updated += 1
        self.stdout.write(self.style.SUCCESS(f"Drivers imported. created={created} updated={updated}"))
