from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = "Rotate DRF API tokens for one or all staff users."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, default="", help="Rotate token only for this username")
        parser.add_argument(
            "--all-staff",
            action="store_true",
            help="Rotate tokens for all staff users",
        )

    def handle(self, *args, **options):
        username = (options.get("username") or "").strip()
        all_staff = options.get("all_staff", False)

        User = get_user_model()
        if username:
            users = User.objects.filter(username=username, is_staff=True)
        elif all_staff:
            users = User.objects.filter(is_staff=True)
        else:
            self.stdout.write(self.style.WARNING("Use --username=<name> or --all-staff"))
            return

        if not users.exists():
            self.stdout.write(self.style.WARNING("No matching staff users found."))
            return

        for user in users:
            Token.objects.filter(user=user).delete()
            token = Token.objects.create(user=user)
            self.stdout.write(self.style.SUCCESS(f"{user.username}: {token.key}"))
