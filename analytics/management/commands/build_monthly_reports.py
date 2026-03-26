from datetime import datetime

from django.core.management.base import BaseCommand

from analytics.services import rebuild_monthly_reports


class Command(BaseCommand):
    help = "Build monthly analytics snapshots"

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=datetime.now().year)
        parser.add_argument("--month", type=int, default=datetime.now().month)

    def handle(self, *args, **options):
        rebuild_monthly_reports(options["year"], options["month"])
        self.stdout.write(self.style.SUCCESS(f"Reports ready for {options['year']}-{options['month']:02d}"))
