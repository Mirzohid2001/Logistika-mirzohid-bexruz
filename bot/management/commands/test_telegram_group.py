import json
from urllib import request
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "TELEGRAM_GROUP_ID ga test xabar yuboradi — buyurtma ketmasa sababni ko‘rish uchun."

    def handle(self, *args, **options) -> None:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
        gid_raw = str(settings.TELEGRAM_GROUP_ID or "").strip()
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN bo'sh.")
        if not gid_raw:
            raise CommandError("TELEGRAM_GROUP_ID bo'sh.")

        self.stdout.write(f"GROUP_ID o‘qilgan: {repr(gid_raw)} (uzunlik {len(gid_raw)})")

        # Telegram JSON: chat_id son yoki string bo‘lishi mumkin; forumda ba’zi holatlarda farq qiladi.
        try:
            chat_id: int | str = int(gid_raw)
        except ValueError:
            chat_id = gid_raw

        body = {
            "chat_id": chat_id,
            "text": "✅ Shofir test: guruh bilan bog‘lanish ishlayapti.",
        }
        tid = getattr(settings, "TELEGRAM_GROUP_MESSAGE_THREAD_ID", None)
        if tid is not None:
            body["message_thread_id"] = tid
            self.stdout.write(f"message_thread_id (forum): {tid}")
        payload = json.dumps(body).encode("utf-8")
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        req = request.Request(
            api,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CommandError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise CommandError(f"Tarmoq xato: {exc}") from exc

        if not raw.get("ok"):
            raise CommandError(str(raw))

        self.stdout.write(self.style.SUCCESS("Xabar yuborildi. Guruhda ko‘rinishi kerak."))
        self.stdout.write("Agar bu yerda OK bo‘lsa, lekin buyurtma kelmasa: runserver ni .env o‘zgargach qayta ishga tushiring.")
