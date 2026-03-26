import json
from urllib import request
from urllib.error import URLError

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Telegram getWebhookInfo — URL, oxirgi xato, kutilayotgan yangilanishlar."

    def handle(self, *args, **options) -> None:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
        if not token:
            self.stderr.write(self.style.ERROR("TELEGRAM_BOT_TOKEN bo'sh."))
            return
        url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
        try:
            with request.urlopen(url, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except URLError as exc:
            self.stderr.write(self.style.ERROR(f"Tarmoq xato: {exc}"))
            return
        except (ValueError, UnicodeDecodeError) as exc:
            self.stderr.write(self.style.ERROR(f"Javob o'qilmadi: {exc}"))
            return
        if not raw.get("ok"):
            self.stderr.write(self.style.ERROR(str(raw)))
            return
        info = raw.get("result") or {}
        wh_url = info.get("url") or "(o'rnatilmagan)"
        self.stdout.write(f"url: {wh_url}")
        self.stdout.write(f"pending_update_count: {info.get('pending_update_count', 0)}")
        err = info.get("last_error_message") or ""
        if err:
            self.stdout.write(self.style.WARNING(f"last_error_message: {err}"))
            self.stdout.write(f"last_error_date: {info.get('last_error_date')}")
        else:
            self.stdout.write(self.style.SUCCESS("last_error_message: (yo'q)"))
        local_secret = bool((settings.TELEGRAM_WEBHOOK_SECRET or "").strip())
        secret_state = "bor" if local_secret else "yo'q"
        self.stdout.write(f"Mahalliy TELEGRAM_WEBHOOK_SECRET: {secret_state}")
        self.stdout.write("")
        self.stdout.write(
            "Agar url noto'g'ri yoki last_error_message bor bo'lsa, setWebhook ni qayta o'rnating:\n"
            "  curl -s \"https://api.telegram.org/bot<TOKEN>/setWebhook\" \\\n"
            "    -d \"url=https://<SIZNING_NGROK>/bot/webhook/\" \\\n"
            "    -d \"secret_token=<.env dagi TELEGRAM_WEBHOOK_SECRET>\""
        )
