from django.core.management.base import BaseCommand
from django.test import Client


class Command(BaseCommand):
    help = "GET /health/ takroriy tekshiruv (yuk smoke; lokal protsess ichida Django test client)."

    def add_arguments(self, parser):
        parser.add_argument("times", nargs="?", type=int, default=50)

    def handle(self, *args, **options):
        n = max(1, int(options["times"]))
        client = Client()
        bad = 0
        for _ in range(n):
            r = client.get("/health/")
            if r.status_code != 200:
                bad += 1
        if bad:
            self.stderr.write(self.style.ERROR(f"{bad}/{n} so‘rov 200 emas"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"{n} marta /health/ — barchasi 200"))
