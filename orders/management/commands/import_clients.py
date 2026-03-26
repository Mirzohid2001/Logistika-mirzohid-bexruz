import csv

from django.core.management.base import BaseCommand

from orders.models import Client


class Command(BaseCommand):
    help = "Import clients from CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        created = 0
        updated = 0
        with open(csv_path, newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                client, is_created = Client.objects.update_or_create(
                    name=name,
                    defaults={
                        "contact_name": (row.get("contact_name") or "").strip(),
                        "phone": (row.get("phone") or "").strip(),
                        "is_active": (row.get("is_active") or "true").lower() in {"1", "true", "yes"},
                    },
                )
                if is_created:
                    created += 1
                else:
                    updated += 1
        self.stdout.write(self.style.SUCCESS(f"Clients imported. created={created} updated={updated}"))
