from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Client, Order, OrderStatus


class Command(BaseCommand):
    help = "One-time cleanup for legacy orders data quality"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--default-client-name", type=str, default="Unknown Client")

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        default_client_name = options["default_client_name"]
        default_client, _ = Client.objects.get_or_create(name=default_client_name, defaults={"is_active": False})

        missing_client_qs = Order.objects.filter(client__isnull=True)
        missing_delivered_qs = Order.objects.filter(status=OrderStatus.COMPLETED, delivered_at__isnull=True)
        missing_comment_qs = Order.objects.filter(comment="")

        missing_client_count = missing_client_qs.count()
        missing_delivered_count = missing_delivered_qs.count()
        missing_comment_count = missing_comment_qs.count()

        self.stdout.write(
            f"Detected: missing_client={missing_client_count}, missing_delivered_at={missing_delivered_count}, missing_comment={missing_comment_count}"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode: no updates applied"))
            return

        updated_client = missing_client_qs.update(client=default_client)
        updated_delivered = missing_delivered_qs.update(delivered_at=timezone.now())
        updated_comment = missing_comment_qs.update(comment="Legacy data cleanup")

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleanup done: client={updated_client}, delivered_at={updated_delivered}, comment={updated_comment}"
            )
        )
