import json
import logging
import re
from urllib import request
from urllib.error import URLError
from urllib.parse import quote

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _cache_key(lat: str, lon: str) -> str:
    return f"ymap:geo:{lat}:{lon}"


def _lock_key(lat: str, lon: str) -> str:
    return f"ymap:geo:lock:{lat}:{lon}"


@shared_task(bind=True, acks_late=True)
def reverse_geocode_yandex_task(self, lat: str, lon: str) -> None:
    """
    Reverse geocode ni Telegram flow'idan ajratib, sync bo'lmasligi uchun ishlatamiz.
    Cache yo'q bo'lsa manzil bo'sh string bilan qaytishi mumkin (timeout/xato holatlar).
    """
    cache_key = _cache_key(lat, lon)
    cached = cache.get(cache_key)
    if cached is not None:
        return

    lock_key = _lock_key(lat, lon)
    # Lock bor bo'lsa ham task kelishi mumkin (redis o'chgan bo'lishi mumkin), shuning uchun cache'ga tayanamiz.
    _ = cache.get(lock_key)

    url = (
        "https://geocode-maps.yandex.ru/1.x/?format=json"
        f"&geocode={quote(f'{lon},{lat}')}&results=1&lang=uz_UZ"
    )
    try:
        with request.urlopen(url, timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        collection = (
            payload.get("response", {})
            .get("GeoObjectCollection", {})
            .get("featureMember", [])
        )
        if collection:
            text = (
                collection[0]
                .get("GeoObject", {})
                .get("metaDataProperty", {})
                .get("GeocoderMetaData", {})
                .get("text", "")
            ).strip()
        else:
            text = ""

        # Bo'sh qiymatni ham cache'laymiz (tez-tez qayta so'ralmasin).
        cache.set(cache_key, text, 86400 if text else 600)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.info("reverse_geocode_yandex_task failed: %s", exc)
        cache.set(cache_key, "", 600)
    finally:
        cache.delete(lock_key)


def _extract_coords(value: str) -> tuple[str, str] | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value or "")
    if not match:
        return None
    return match.group(1), match.group(2)


@shared_task(bind=True, acks_late=True)
def update_order_telegram_text_task(self, order_id: int, chat_id: str, message_id: str, attempt: int = 0) -> None:
    """
    Reverse geocode cache to'ldirilgandan keyin Telegram guruhdagi order xabarini yangilaydi.
    """
    if not order_id or not chat_id or not message_id:
        return

    edit_lock_key = f"tg:order-edit-lock:{chat_id}:{message_id}"
    if not cache.add(edit_lock_key, "1", timeout=20):
        return

    from bot.models import TelegramMessageLog
    from orders.models import Order

    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return

    # Xabarga format berishda ishlatiladigan koordinatalarni topamiz.
    from_latlon = _extract_coords(str(order.from_location))
    to_latlon = _extract_coords(str(order.to_location))

    def _cached_address(latlon: tuple[str, str] | None) -> str | None:
        if not latlon:
            return None
        lat, lon = latlon
        return cache.get(_cache_key(lat, lon))

    coords_present = bool(from_latlon) or bool(to_latlon)
    if not coords_present:
        return

    from_addr = _cached_address(from_latlon)
    to_addr = _cached_address(to_latlon)
    # "" (bo‘sh) caching holatida ham qaytishi mumkin; manzil chiqqanini ko‘rsatish uchun truthy tekshiruv qilamiz.
    has_any_address = bool(from_addr) or bool(to_addr)

    max_attempts = 6
    if not has_any_address and attempt < max_attempts:
        # Geocode task hali tugamagan bo‘lishi mumkin: yana 5s dan keyin urinamiz.
        try:
            self.apply_async(
                (order_id, chat_id, message_id, attempt + 1),
                countdown=5,
            )
        finally:
            return
    elif not has_any_address:
        # Geocode bo‘lmaydi: xabarni o'zgartirmay qoldiramiz.
        return

    try:
        from bot.services import edit_group_message

        edit_group_message(chat_id=chat_id, message_id=message_id, order=order)
    except Exception:
        return

