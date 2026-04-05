import json
from urllib import request
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Telegram: har bir command 1-32 belgi, faqat a-z, 0-9, _; tavsif ≤256 belgi.
DEFAULT_COMMANDS: list[dict[str, str]] = [
    {"command": "start", "description": "Botga ulanish va telefonni yuborish"},
    {"command": "help", "description": "Buyruqlar va yordam"},
    {"command": "start_trip", "description": "Safarni boshlash"},
    {"command": "finish_trip", "description": "Safarni tugatish so‘rovi"},
    {"command": "trip_map", "description": "Reys xaritasi"},
    {"command": "wizard", "description": "Tezkor qadamlar"},
    {"command": "add_vehicle", "description": "Qo‘shimcha mashina (2+ avto; raqam + sig‘im)"},
    {"command": "checkpoint", "description": "Oraliq eslatma"},
    {"command": "trip_summary", "description": "Qisqa hisobot"},
    {"command": "yuklandi", "description": "Yuklangan hajm (masalan tonna)"},
    {"command": "topshirildi", "description": "Topshirilgan hajm"},
    {"command": "zichlik", "description": "Zichlik kg/L (litr uchun)"},
]


class Command(BaseCommand):
    help = "Telegram setMyCommands — chatda / bosganda chiqadigan rasmiy buyruqlar ro‘yxati."

    def handle(self, *args, **options) -> None:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN bo'sh.")
        body = json.dumps({"commands": DEFAULT_COMMANDS}).encode("utf-8")
        api = f"https://api.telegram.org/bot{token}/setMyCommands"
        req = request.Request(
            api,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
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
            raise CommandError(f"Telegram API {exc.code}: {desc}") from exc
        except URLError as exc:
            raise CommandError(f"Tarmoq xato: {exc}") from exc
        if not raw.get("ok"):
            raise CommandError(str(raw))
        self.stdout.write(self.style.SUCCESS(f"setMyCommands: {len(DEFAULT_COMMANDS)} ta buyruq o'rnatildi."))
        self.stdout.write("Tekshiruv: Telegramda suhbatda / bosing — ro'yxat chiqishi kerak.")
