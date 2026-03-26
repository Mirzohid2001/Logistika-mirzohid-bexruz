from django.core.management.base import BaseCommand
from django.utils import timezone

from analytics.tasks import (
    check_sla_escalations_task,
    monthly_report_scheduler_task,
    nightly_reconcile_task,
    rebuild_monthly_reports_task,
)


class Command(BaseCommand):
    help = "Run automation tasks once to verify worker/beat logic"

    def add_arguments(self, parser):
        parser.add_argument("--sync", action="store_true", default=False)

    def handle(self, *args, **options):
        now = timezone.now()
        year = now.year
        month = now.month
        sync = options["sync"]

        if sync:
            rebuild_monthly_reports_task(year, month)
            sla_count = check_sla_escalations_task()
            nightly_reconcile_task()
            monthly_report_scheduler_task()
            self.stdout.write(self.style.SUCCESS(f"Sync automation check done. SLA alerts created: {sla_count}"))
            return

        rebuild_monthly_reports_task.delay(year, month)
        check_sla_escalations_task.delay()
        nightly_reconcile_task.delay()
        monthly_report_scheduler_task.delay()
        self.stdout.write(self.style.SUCCESS("Async automation tasks queued"))
