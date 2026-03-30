import json
from urllib import parse, request
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Telegram setWebhook — to'liq sayt asosiy URL (ngrok), yo'l: /bot/webhook/ qo'shiladi."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "base_url",
            type=str,
            help="Masalan: https://ed0c-84-54-70-176.ngrok-free.app (oxiridagi / ixtiyoriy)",
        )

    def handle(self, *args, **options) -> None:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN bo'sh.")
        base = str(options["base_url"]).strip().rstrip("/")
        if not base.startswith("https://"):
            raise CommandError("base_url https:// bilan boshlanishi kerak.")
        wh_url = f"{base}/bot/webhook/"
        secret = (settings.TELEGRAM_WEBHOOK_SECRET or "").strip()
        params = {"url": wh_url}
        if secret:
            params["secret_token"] = secret
        body = parse.urlencode(params).encode("utf-8")
        api = f"https://api.telegram.org/bot{token}/setWebhook"
        req = request.Request(
            api,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with request.urlopen(req, timeout=25) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                desc = parsed.get("description") or detail
            except (json.JSONDecodeError, TypeError):
                desc = detail or str(exc)
            raise CommandError(
                f"Telegram API {exc.code}: {desc}\n"
                "Agar secret_token xato bo'lsa: faqat A-Z, a-z, 0-9, _, - (bo'sh joy va boshqa belgilar mumkin emas)."
            ) from exc
        except URLError as exc:
            raise CommandError(f"Tarmoq xato: {exc}") from exc
        if not raw.get("ok"):
            raise CommandError(str(raw))
        self.stdout.write(self.style.SUCCESS(f"Webhook o'rnatildi: {wh_url}"))
        if secret:
            self.stdout.write("secret_token: .env dagi TELEGRAM_WEBHOOK_SECRET ishlatildi.")
        else:
            self.stdout.write(self.style.WARNING("TELEGRAM_WEBHOOK_SECRET bo'sh — header tekshiruvi o'chiq."))
        self.stdout.write("Tekshiruv: python manage.py check_telegram_webhook")
