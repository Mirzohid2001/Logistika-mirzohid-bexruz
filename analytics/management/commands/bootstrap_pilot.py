from datetime import datetime

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Bootstrap pilot data reports after imports"

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=datetime.now().year)
        parser.add_argument("--month", type=int, default=datetime.now().month)

    def handle(self, *args, **options):
        call_command("build_monthly_reports", year=options["year"], month=options["month"])
        call_command("reconcile_finance")
        self.stdout.write(self.style.SUCCESS("Pilot bootstrap completed"))
