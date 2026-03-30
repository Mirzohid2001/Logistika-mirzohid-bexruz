import json
import logging
import re
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import escape

from bot.models import TelegramGroupConfig, TelegramMessageLog
from drivers.models import Driver, DriverStatus, DriverVerificationStatus
from orders.models import Order, OrderStatus, QuantityUnit

logger = logging.getLogger(__name__)

TRIP_MAP_WEBAPP_SIGN_SALT = "shofir-trip-map"

# Reply-keyboard tugmalari: foydalanuvchi bosganda aynan shu matn yuboriladi — views da alias → buyruqqa aylantiriladi.
BTN_REPLY_GPS = "📍 GPS (bir marta)"
BTN_REPLY_START = "🚛 Safarni boshlash"
BTN_REPLY_FINISH = "📝 Tugatish so‘rovi"
BTN_REPLY_FINISH_ASCII = "📝 Tugatish so'rovi"
BTN_REPLY_FINISH_SMART = "📝 Tugatish so\u2019rovi"
BTN_REPLY_CHECKPOINT = "📌 Checkpoint"
BTN_REPLY_SUMMARY = "📊 Reys hisoboti"
BTN_REPLY_HELP = "📋 Yordam"
BTN_REPLY_WIZARD = "🧙 Tezkor menyu"
BTN_REPLY_WIZARD_TAXI = "🚕 Tezkor menyu"
BTN_REPLY_TRIP_MAP = "🗺 Reys xaritasi"
BTN_REPLY_ADD_VEHICLE = "➕ Mashina qo‘shish"


def driver_reply_button_aliases() -> dict[str, str]:
    return {
        BTN_REPLY_START: "/start_trip",
        BTN_REPLY_FINISH: "/finish_trip",
        BTN_REPLY_FINISH_ASCII: "/finish_trip",
        BTN_REPLY_FINISH_SMART: "/finish_trip",
        BTN_REPLY_CHECKPOINT: "/checkpoint",
        BTN_REPLY_SUMMARY: "/trip_summary",
        BTN_REPLY_HELP: "/help",
        BTN_REPLY_WIZARD: "/wizard",
        BTN_REPLY_WIZARD_TAXI: "/wizard",
        BTN_REPLY_TRIP_MAP: "/trip_map",
        BTN_REPLY_ADD_VEHICLE: "/add_vehicle",
    }


def normalize_driver_reply_text(text: str) -> str:
    """Chiroyli tugma bosilganda kelgan matnni /buyruq formatiga aylantiradi."""
    t = (text or "").strip()
    return driver_reply_button_aliases().get(t, t)


def normalize_telegram_command_text(text: str) -> str:
    """Tugma aliaslari, so‘ng Telegram /buyruq@BotName → /buyruq (klientlar standart qoidasi)."""
    t = normalize_driver_reply_text((text or "").strip())
    parts = t.split()
    if not parts:
        return t
    first = parts[0]
    if first.startswith("/") and "@" in first:
        parts[0] = first.split("@", 1)[0]
    return " ".join(parts)


def driver_idle_reply_keyboard() -> dict:
    """Biriktirilmagan / bo‘sh vaqt: yordam va tezkor buyruqlar."""
    return {
        "keyboard": [
            [{"text": BTN_REPLY_HELP}, {"text": BTN_REPLY_WIZARD}],
            [{"text": BTN_REPLY_ADD_VEHICLE}],
            [{"text": BTN_REPLY_SUMMARY}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def driver_assigned_reply_keyboard(*, webapp_url: str | None = None) -> dict:
    """Biriktirilgan, hali yo‘lga chiqmagan: boshlash + xarita."""
    trip_btn: dict = {"text": BTN_REPLY_TRIP_MAP}
    if webapp_url:
        trip_btn["web_app"] = {"url": webapp_url}
    rows: list[list[dict]] = [
        [{"text": BTN_REPLY_GPS, "request_location": True}],
        [{"text": BTN_REPLY_START}, trip_btn],
    ]
    rows.append([{"text": BTN_REPLY_HELP}])
    rows.insert(-1, [{"text": BTN_REPLY_ADD_VEHICLE}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def driver_in_transit_reply_keyboard(*, webapp_url: str | None = None) -> dict:
    """Yo‘lda (IN_TRANSIT): GPS, «Reys xaritasi» (HTTPS bo‘lsa Web App), tugatish."""
    trip_btn: dict = {"text": BTN_REPLY_TRIP_MAP}
    if webapp_url:
        trip_btn["web_app"] = {"url": webapp_url}
    rows: list[list[dict]] = [
        [{"text": BTN_REPLY_GPS, "request_location": True}],
        [trip_btn],
        [{"text": BTN_REPLY_FINISH}],
    ]
    rows.append([{"text": BTN_REPLY_ADD_VEHICLE}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def build_trip_map_webapp_url(order: Order, telegram_user_id: int) -> str | None:
    """Imzlangan HTTPS havola — faqat biriktirilgan haydovchi ochadi."""
    base = (getattr(settings, "TELEGRAM_WEBAPP_BASE_URL", "") or "").strip().rstrip("/")
    if not base or not telegram_user_id:
        return None
    from_ll = _extract_coords(str(order.from_location))
    to_ll = _extract_coords(str(order.to_location))
    if not from_ll or not to_ll:
        return None
    token = signing.dumps({"o": order.pk, "tg": int(telegram_user_id)}, salt=TRIP_MAP_WEBAPP_SIGN_SALT)
    return f"{base}/bot/webapp/trip/{order.pk}/{token}/"


def driver_reply_keyboard_for_order(
    order: Order | None,
    *,
    telegram_user_id: int | None = None,
) -> dict:
    """Haydovchi faol buyurtmasi holatiga qarab pastki klaviatura."""
    webapp_url = None
    if order and telegram_user_id:
        webapp_url = build_trip_map_webapp_url(order, telegram_user_id)
    if not order:
        return driver_idle_reply_keyboard()
    if order.status == OrderStatus.IN_TRANSIT:
        return driver_in_transit_reply_keyboard(webapp_url=webapp_url)
    if order.status in {OrderStatus.ASSIGNED, OrderStatus.ISSUE}:
        return driver_assigned_reply_keyboard(webapp_url=webapp_url)
    return driver_idle_reply_keyboard()


def _telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"


def _telegram_chat_id_for_api(raw: str) -> str | int:
    s = (raw or "").strip()
    if not s:
        return s
    try:
        return int(s)
    except ValueError:
        return s


def _telegram_chats_equal(a: str | int, b: str) -> bool:
    bs = (b or "").strip()
    if not bs:
        return False
    try:
        return int(str(a).strip()) == int(bs)
    except ValueError:
        return str(a).strip() == bs


def _maybe_add_group_message_thread(body: dict, *, for_configured_group: bool) -> None:
    if not for_configured_group:
        return
    tid = getattr(settings, "TELEGRAM_GROUP_MESSAGE_THREAD_ID", None)
    if tid is not None:
        body["message_thread_id"] = tid


def _resolve_group_target(group_type: str) -> tuple[str, int | None]:
    cfg = TelegramGroupConfig.objects.filter(group_type=group_type, is_active=True).first()
    if cfg:
        return str(cfg.chat_id or "").strip(), cfg.message_thread_id
    if group_type == TelegramGroupConfig.GroupType.ORDER_POST:
        return str(getattr(settings, "TELEGRAM_GROUP_ID", "") or "").strip(), getattr(
            settings, "TELEGRAM_GROUP_MESSAGE_THREAD_ID", None
        )
    if group_type == TelegramGroupConfig.GroupType.OPS_NOTIFY:
        return str(getattr(settings, "TELEGRAM_OPS_GROUP_ID", "") or "").strip(), getattr(
            settings, "TELEGRAM_OPS_GROUP_MESSAGE_THREAD_ID", None
        )
    return "", None


def send_order_to_group(order: Order) -> bool:
    target_chat_id, target_thread_id = _resolve_group_target(TelegramGroupConfig.GroupType.ORDER_POST)
    if not settings.TELEGRAM_BOT_TOKEN or not target_chat_id:
        logger.warning("send_order_to_group: TELEGRAM_BOT_TOKEN yoki order post guruhi bo'sh")
        return False
    text = build_order_text(order)
    body = {
        "chat_id": _telegram_chat_id_for_api(str(target_chat_id)),
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": build_order_keyboard(order),
    }
    if target_thread_id is not None:
        body["message_thread_id"] = target_thread_id
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("sendMessage"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("send_order_to_group HTTP %s: %s", exc.code, detail)
        return False
    except URLError as exc:
        logger.error("send_order_to_group tarmoq: %s", exc)
        return False
    if data.get("ok"):
        result = data.get("result", {})
        chat_id_value = str(result.get("chat", {}).get("id", target_chat_id))
        message_id_value = str(result.get("message_id", ""))
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=chat_id_value,
            message_id=message_id_value,
            event="order_created",
            payload=result,
        )
        # Reverse geocode async qilinadi: cache to'lgach xabarni address bilan yangilaymiz.
        if chat_id_value and message_id_value:
            try:
                from bot.tasks import update_order_telegram_text_task

                update_order_telegram_text_task.apply_async(
                    (order.pk, chat_id_value, message_id_value, 0),
                    countdown=8,
                )
            except Exception:
                pass
        return True
    logger.error("send_order_to_group Telegram ok=false: %s", data)
    return False


def send_ops_notification(
    event: str,
    *,
    order: Order | None = None,
    driver: Driver | None = None,
    note: str = "",
) -> bool:
    """
    Dispetcher/ops uchun alohida Telegram guruhga tezkor xabarnoma.
    TELEGRAM_OPS_GROUP_ID bo'sh bo'lsa jim o'tkaziladi.
    """
    ops_group_id, ops_thread_id = _resolve_group_target(TelegramGroupConfig.GroupType.OPS_NOTIFY)
    if not settings.TELEGRAM_BOT_TOKEN or not ops_group_id:
        return False

    event_titles = {
        "order_created": "🆕 Buyurtma yaratildi",
        "driver_offer_accept": "✅ Haydovchi qabul qildi",
        "driver_offer_reject": "❌ Haydovchi rad etdi",
        "driver_offer_issue": "⚠️ Haydovchi muammo yubordi",
        "trip_started": "🚛 Safar boshlandi",
        "finish_requested": "📝 Tugatish so‘rovi yuborildi",
        "driver_loaded_quantity": "🛢️ Yuklangan hajm kiritildi",
        "driver_delivered_quantity": "📤 Topshirilgan hajm kiritildi",
    }
    title = event_titles.get(event, f"ℹ️ Hodisa: {event}")
    lines = [f"<b>{title}</b>"]
    if order:
        lines.append(f"Buyurtma: <b>#{order.pk}</b>")
        lines.append(f"Holat: {escape(order.get_status_display())}")
        lines.append(f"Yo‘nalish: {escape(str(order.from_location))} → {escape(str(order.to_location))}")
    if driver:
        lines.append(f"Haydovchi: <b>{escape(driver.full_name)}</b>")
        if driver.phone:
            lines.append(f"Tel: {escape(driver.phone)}")
    if note:
        lines.append(f"Izoh: {escape(note)}")
    body = {
        "chat_id": _telegram_chat_id_for_api(ops_group_id),
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if ops_thread_id is not None:
        body["message_thread_id"] = ops_thread_id
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("sendMessage"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("send_ops_notification HTTP %s: %s", exc.code, detail)
        return False
    except URLError as exc:
        logger.error("send_ops_notification tarmoq: %s", exc)
        return False

    if data.get("ok"):
        result = data.get("result", {})
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=str(result.get("chat", {}).get("id", ops_group_id)),
            message_id=str(result.get("message_id", "")),
            event="ops_notification",
            payload={"event": event, "driver_id": driver.pk if driver else None, "note": note},
        )
        return True
    logger.error("send_ops_notification Telegram ok=false: %s", data)
    return False


def build_order_text(order: Order) -> str:
    from_location = escape(_humanize_location(order.from_location))
    to_location = escape(_humanize_location(order.to_location))
    cargo = escape(order.cargo_type)
    weight = escape(str(order.weight_ton))
    price = escape(str(order.price_suggested))
    contact = f"{escape(order.contact_name)} {escape(order.contact_phone)}"
    status = escape(str(order.get_status_display()))
    lines = [
        f"<b>📦 Buyurtma #{order.pk}</b>",
        f"📍 <b>Qayerdan:</b> {from_location}",
        f"🏁 <b>Qayerga:</b> {to_location}",
        f"📦 <b>Yuk (reja):</b> {cargo} ({weight} t)",
        f"💰 <b>Taklif narx:</b> {price}",
        f"👤 <b>Kontakt:</b> {contact}",
        f"📌 <b>Holat:</b> {status}",
    ]
    if order.loaded_quantity is not None:
        uom_label = escape(dict(QuantityUnit.choices).get(order.loaded_quantity_uom, order.loaded_quantity_uom))
        lt = order.loaded_quantity_metric_ton
        if lt is not None:
            lines.append(
                f"🛢️ <b>Yuklangan (fakt):</b> {escape(str(order.loaded_quantity))} {uom_label} "
                f"(≈ {escape(str(lt))} t)"
            )
        else:
            lines.append(
                f"🛢️ <b>Yuklangan:</b> {escape(str(order.loaded_quantity))} {uom_label} "
                f"(tonnaga aylantirish uchun <code>zichlik</code> kiriting)"
            )
    if order.delivered_quantity is not None:
        uom_d = escape(dict(QuantityUnit.choices).get(order.delivered_quantity_uom, order.delivered_quantity_uom))
        dt = order.delivered_quantity_metric_ton
        if dt is not None:
            lines.append(
                f"📤 <b>Klientga:</b> {escape(str(order.delivered_quantity))} {uom_d} (≈ {escape(str(dt))} t)"
            )
        else:
            lines.append(
                f"📤 <b>Klientga:</b> {escape(str(order.delivered_quantity))} {uom_d} "
                f"(tonnaga aylantirish uchun zichlik)"
            )
    short = order.quantity_shortage_metric_ton
    if short is not None and short > 0:
        lines.append(f"⚠️ <b>Farq (yuklangan − topshirilgan):</b> ≈ {escape(str(short))} t")
    return "\n".join(lines)


def build_order_keyboard(order: Order) -> dict:
    """Guruhdagi buyurtma xabari: 2 ustun — kichik ekranda o‘qish osonroq."""
    keyboard: list[list[dict]] = []
    if order.status in {OrderStatus.NEW, OrderStatus.OFFERED, OrderStatus.ISSUE}:
        keyboard.append(
            [
                {"text": "✅ Qabul", "callback_data": f"order:{order.pk}:accept"},
                {"text": "❌ Rad", "callback_data": f"order:{order.pk}:reject"},
            ]
        )
        keyboard.append([{"text": "⚠️ Muammo", "callback_data": f"order:{order.pk}:issue"}])
    elif order.status == OrderStatus.ASSIGNED:
        keyboard.append(
            [
                {"text": "🚛 Yo‘lga chiqish", "callback_data": f"order:{order.pk}:start"},
                {"text": "⚠️ Muammo", "callback_data": f"order:{order.pk}:issue"},
            ]
        )
    elif order.status == OrderStatus.IN_TRANSIT:
        keyboard.append(
            [
                {"text": BTN_REPLY_FINISH, "callback_data": f"order:{order.pk}:finish_req"},
                {"text": "⚠️ Muammo", "callback_data": f"order:{order.pk}:issue"},
            ]
        )

    # Haydovchini biriktirish va boshqa ofis amallari faqat web-panelda.
    return {"inline_keyboard": keyboard}


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    body = {"callback_query_id": callback_query_id}
    if text:
        body["text"] = text
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("answerCallbackQuery"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10):
        pass


def send_chat_message(
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
    *,
    disable_web_page_preview: bool = False,
) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    gid = str(settings.TELEGRAM_GROUP_ID or "").strip()
    body = {
        "chat_id": _telegram_chat_id_for_api(str(chat_id)),
        "text": text,
    }
    _maybe_add_group_message_thread(
        body, for_configured_group=_telegram_chats_equal(chat_id, gid)
    )
    if parse_mode:
        body["parse_mode"] = parse_mode
    if reply_markup:
        body["reply_markup"] = reply_markup
    if disable_web_page_preview:
        body["disable_web_page_preview"] = True
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("sendMessage"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10):
            pass
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("Telegram sendMessage HTTP %s: %s", exc.code, detail)
        raise


def _telegram_api_post(method: str, body: dict) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url(method),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10):
        pass


def send_chat_location(chat_id: str, latitude: float, longitude: float) -> None:
    """Telegram ``sendLocation`` kartochkasi; pin bosilganda ko‘pincha tashqi xarita taklif qilinadi."""
    if not chat_id:
        return
    _telegram_api_post(
        "sendLocation",
        {
            "chat_id": chat_id,
            "latitude": latitude,
            "longitude": longitude,
        },
    )


def send_order_native_map_pins(chat_id: str, order: Order) -> None:
    """Yuk olish / tushirish — alohida sendLocation pinlari.

    Agar ``TELEGRAM_WEBAPP_BASE_URL`` sozlangan bo‘lsa va haydovchi uchun imzlangan
    reys xaritasi ochilsa, pinlar yuborilmaydi: pin ustiga bosilganda Telegram odatda
    tashqi xarita (Google Maps va hokazo) taklif qiladi; marshrut TG ichida Web App orqali.
    """
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    tg_for_webapp: int | None = None
    try:
        cid = int(chat_id)
        if cid > 0:
            tg_for_webapp = cid
    except ValueError:
        pass
    if tg_for_webapp and build_trip_map_webapp_url(order, tg_for_webapp):
        return
    from_ll = _extract_coords(str(order.from_location))
    to_ll = _extract_coords(str(order.to_location))
    if not from_ll and not to_ll:
        return
    if from_ll:
        send_chat_message(
            chat_id,
            f"📍 <b>Yuk olish nuqtasi</b> — #{order.pk}",
            parse_mode="HTML",
        )
        send_chat_location(chat_id, float(from_ll[0]), float(from_ll[1]))
    if to_ll:
        send_chat_message(
            chat_id,
            f"🏁 <b>Tushirish nuqtasi</b> — #{order.pk}",
            parse_mode="HTML",
        )
        send_chat_location(chat_id, float(to_ll[0]), float(to_ll[1]))


def trip_map_show_yandex_links() -> bool:
    return bool(getattr(settings, "TRIP_MAP_SHOW_YANDEX_LINKS", False))


def edit_chat_message(
    chat_id: str,
    message_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return
    gid = str(settings.TELEGRAM_GROUP_ID or "").strip()
    body = {
        "chat_id": _telegram_chat_id_for_api(str(chat_id)),
        "message_id": int(message_id),
        "text": text,
    }
    _maybe_add_group_message_thread(
        body, for_configured_group=_telegram_chats_equal(chat_id, gid)
    )
    if parse_mode:
        body["parse_mode"] = parse_mode
    if reply_markup:
        body["reply_markup"] = reply_markup
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("editMessageText"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10):
        pass


def driver_live_location_reply_keyboard() -> dict:
    """Eski nom: biriktirilgan (ASSIGNED) payt uchun klaviatura."""
    return driver_assigned_reply_keyboard()


def _order_map_anchor_lines(order: Order) -> list[str]:
    """Yandex havolalari (<a href>) — HTML xabar ichida."""
    lines: list[str] = []
    from_ll = _extract_coords(str(order.from_location))
    to_ll = _extract_coords(str(order.to_location))
    if from_ll and to_ll:
        la, lo = from_ll
        ta, to_lon = to_ll
        route = f"https://yandex.com/maps/?rtext={la},{lo}~{ta},{to_lon}"
        lines.append(f"• <a href=\"{route}\">🧭 Marshrut (yuk olish → tushirish)</a>")
    if from_ll:
        la, lo = from_ll
        lines.append(f"• <a href=\"https://yandex.com/maps/?pt={lo},{la}&z=14\">📍 Yuk olish nuqtasi</a>")
    if to_ll:
        la, lo = to_ll
        lines.append(f"• <a href=\"https://yandex.com/maps/?pt={lo},{la}&z=14\">🏁 Tushirish nuqtasi</a>")
    if not from_ll and not to_ll:
        lines.append(
            "• Buyurtmada koordinata yo‘q — manzil matnini xaritada qidiring "
            "(from/to ga <code>lat, lon</code> kiritsangiz, marshrut paydo bo‘ladi)."
        )
    return lines


def build_active_trip_focus_message_html(
    order: Order,
    *,
    for_telegram_user_id: int | None = None,
) -> str:
    """Biriktirilgan / yo‘lda: manzillar; xarita — Web App yoki (sozlamasiz) sendLocation pinlari."""
    head = (
        "🚛 <b>Faol reys</b>"
        if order.status == OrderStatus.IN_TRANSIT
        else "📦 <b>Buyurtmangiz</b>"
    )
    webapp_ok = bool(
        for_telegram_user_id
        and build_trip_map_webapp_url(order, for_telegram_user_id)
    )
    if webapp_ok:
        map_block = [
            "",
            "<b>🗺 Marshrut</b> — Telegram <b>ichida</b> to‘liq xarita va yo‘l uchun pastdagi "
            "<b>«🗺 Reys xaritasi»</b> tugmasini bosing (mini-ilova).",
            "<i>Oddiy joylashuv pinlari yuborilmaydi: pin bosilganda Telegram ko‘pincha tashqi xaritaga "
            "(masalan, Google Maps) yo‘naltiradi.</i>",
        ]
    else:
        map_block = [
            "",
            "<b>🗺 Xarita</b> — <i>keyingi xabarlarda yuk olish va tushirish uchun joylashuv kartochkalari "
            "(pin) keladi.</i>",
            "<i>Pin ustiga bosilganda ko‘pincha oldindan ko‘rinish va tashqi xarita taklif qilinadi — "
            "bu Telegramning odatiy ishi.</i>",
            "<i>Ikki nuqta orasini xaritada qarang (marshrut chizig‘i alohida xabar bilan kelmaydi).</i>",
        ]
    lines = [
        f"{head} — #{order.pk}",
        f"📌 Holat: {escape(str(order.get_status_display()))}",
        "",
        f"<b>📍 Qayerdan:</b> {escape(str(order.from_location))}",
        f"<b>🏁 Qayerga:</b> {escape(str(order.to_location))}",
        *map_block,
    ]
    if trip_map_show_yandex_links():
        lines.extend(["", "<b>Brauzerda Yandex (ixtiyoriy):</b>"])
        lines.extend(_order_map_anchor_lines(order))
    tail = [
        "",
        "<b>📍 Jonli yo‘l:</b> 📎 → Joylashuv → <b>Jonli joylashuvni ulashish</b> (vaqt: reys tugaguncha).",
    ]
    if order.status == OrderStatus.IN_TRANSIT:
        tail.extend(
            [
                "",
                "<i>Boshqa buyurtma qabul qilinmaydi — reys tugaguncha (tizim ham bloklaydi).</i>",
            ]
        )
    else:
        tail.extend(
            [
                "",
                "<i>Yo‘lga chiqishdan keyin yangi zakazlarga qaytib bo‘lmaydi — avval ushbu reysni yakunlang.</i>",
            ]
        )
    lines.extend(tail)
    return "\n".join(lines)


def build_start_trip_driver_message_html(
    order: Order,
    *,
    for_telegram_user_id: int | None = None,
) -> str:
    """Safar boshlanganda: Web App bor bo‘lsa pin matni boshqacha; Yandex — TRIP_MAP_SHOW_YANDEX_LINKS."""
    status_disp = escape(str(order.get_status_display()))
    webapp_ok = bool(
        for_telegram_user_id
        and build_trip_map_webapp_url(order, for_telegram_user_id)
    )
    if webapp_ok:
        map_intro = (
            "<b>🗺 Marshrut</b> — pastdagi <b>«🗺 Reys xaritasi»</b> orqali Telegram ichida xarita va yo‘l chizig‘i."
        )
    else:
        map_intro = (
            "<b>🗺 Xarita</b> — <i>keyingi xabarlarda yuk olish va tushirish joylashuv kartochkalari (pin) keladi; "
            "pin bosilganda ko‘pincha tashqi xarita ochiladi.</i>"
        )
    lines = [
        f"✅ <b>Safar boshlandi</b> — buyurtma #{order.pk}",
        f"Holat: {status_disp}",
        "",
        map_intro,
    ]
    if trip_map_show_yandex_links():
        lines.extend(["", "<b>Brauzerda Yandex (ixtiyoriy):</b>"])
        lines.extend(_order_map_anchor_lines(order))
    elif not _extract_coords(str(order.from_location)) and not _extract_coords(str(order.to_location)):
        lines.append(
            "Koordinata yo‘q — buyurtmada <code>lat, lon</code> ko‘rinishida manzil kiriting, "
            "yoki ofisdan so‘rang."
        )
    lines.extend(
        [
            "",
            "<b>📍 Jonli kuzatuvni qanday yoqish</b>",
            "Telegram <b>bot orqali</b> «Jonli joylashuv» oynasini avtomatik ochib bo‘lmaydi — "
            "buni o‘zingiz yoqasiz:",
            "1) 📎 → <b>Joylashuv</b> → <b>Jonli joylashuvni ulashish</b> / <b>Share Live Location</b>",
            "2) Vaqtni <b>8 soat yoki maksimal</b> tanlang (reys tugaguncha uzilmasin)",
            "3) Telefon sozlamalarida ilova uchun <b>Joylashuv</b> ruxsatini tekshiring",
            "",
            "Bir martalik nuqta: pastdagi <b>📍 GPS (bir marta)</b> — admin uchun yo‘lda <b>jonli</b> yo‘l yaxshiroq.",
            "",
            "<i>Endi pastda faqat reysga oid tugmalar chiqadi (boshqa zakaz — reys tugaguncha emas).</i>",
        ]
    )
    return "\n".join(lines)


def build_live_location_instruction(order_id: int) -> str:
    from orders.models import Order

    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return (
            f"📍 Buyurtma #{order_id}\n\n"
            "<b>Jonli kuzatuv (taksi kabi)</b>\n"
            "1) Pastdagi «📍 GPS» yoki 📎 → Location → <b>Share Live Location</b> (reys tugaguncha)\n"
            "2) Yo‘lga chiqishda: /start_trip\n"
            "3) Tugagach: /finish_trip"
        )

    from_latlon = _extract_coords(str(order.from_location))
    to_latlon = _extract_coords(str(order.to_location))

    if from_latlon:
        lat, lon = from_latlon
        from_map_url = f"https://yandex.com/maps/?pt={lon},{lat}&z=13"
        from_part = f"{order.from_location} | Xarita: {from_map_url}"
    else:
        from_part = str(order.from_location)

    if to_latlon:
        lat, lon = to_latlon
        to_map_url = f"https://yandex.com/maps/?pt={lon},{lat}&z=13"
        to_part = f"{order.to_location} | Xarita: {to_map_url}"
    else:
        to_part = str(order.to_location)

    return (
        f"🚚 Buyurtma #{order_id}\n"
        f"Qayerdan: {from_part}\n"
        f"Qayerga: {to_part}\n\n"
        "<b>📍 Jonli kuzatuv</b> (admin webda xaritada ko‘radi)\n"
        "• Eng yaxshisi: 📎 → Location → <b>Share Live Location</b> (muddat — reys oxirigacha)\n"
        "• Yoki pastdagi «📍 GPS (bir marta)» tugmasi\n"
        "Yo‘lga chiqganda: <code>/start_trip</code>"
    )


def edit_group_message(chat_id: str, message_id: str, order: Order) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return
    body = {
        "chat_id": _telegram_chat_id_for_api(str(chat_id)),
        "message_id": int(message_id),
        "text": build_order_text(order),
        "parse_mode": "HTML",
        "reply_markup": build_order_keyboard(order),
    }
    _maybe_add_group_message_thread(body, for_configured_group=True)
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("editMessageText"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10):
        pass


def build_driver_wizard_keyboard(
    order_id: int, current_step: int = 1, *, trip_in_progress: bool = False
) -> dict:
    prev_step = max(1, current_step - 1)
    if trip_in_progress:
        return {
            "inline_keyboard": [
                [
                    {"text": "📌 Checkpoint", "callback_data": f"drv:checkpoint:{order_id}"},
                    {"text": "📊 Hisobot", "callback_data": f"drv:summary:{order_id}"},
                ],
                [{"text": "📝 Tugatish", "callback_data": f"drv:finish:{order_id}"}],
                [
                    {"text": "⬅️ Orqaga", "callback_data": f"drv:back:{order_id}:{prev_step}"},
                    {"text": "❌ Bekor qilish", "callback_data": f"drv:cancel:{order_id}"},
                ],
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "🚛 Yo‘lga chiqish", "callback_data": f"drv:start:{order_id}"},
                {"text": "📌 Checkpoint", "callback_data": f"drv:checkpoint:{order_id}"},
            ],
            [
                {"text": "📊 Hisobot", "callback_data": f"drv:summary:{order_id}"},
                {"text": "📝 Tugatish", "callback_data": f"drv:finish:{order_id}"},
            ],
            [
                {"text": "⬅️ Orqaga", "callback_data": f"drv:back:{order_id}:{prev_step}"},
                {"text": "❌ Bekor qilish", "callback_data": f"drv:cancel:{order_id}"},
            ],
        ]
    }


def _humanize_location(raw_value: str) -> str:
    value = (raw_value or "").strip()
    lat_lon = _extract_coords(value)
    if not lat_lon:
        return value
    lat, lon = lat_lon
    address = _reverse_geocode_yandex(lat, lon)
    map_url = f"https://yandex.com/maps/?pt={lon},{lat}&z=13"
    if address:
        return f"{address} ({lat}, {lon}) | Xarita: {map_url}"
    return f"{lat}, {lon} | Xarita: {map_url}"


def _extract_coords(value: str) -> tuple[str, str] | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
    if not match:
        return None
    lat = match.group(1)
    lon = match.group(2)
    return lat, lon


def _reverse_geocode_yandex(lat: str, lon: str) -> str:
    cache_key = f"ymap:geo:{lat}:{lon}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    # Telegram flow'ni sekinlashtirmaslik uchun reverse geocode ni async qilamiz.
    # Cache yo'q bo'lsa darhol "" qaytaramiz, ammo fon task keyin cache'ni to'ldiradi.
    lock_key = f"ymap:geo:lock:{lat}:{lon}"
    if cache.add(lock_key, "1", timeout=60):
        try:
            from bot.tasks import reverse_geocode_yandex_task

            reverse_geocode_yandex_task.delay(lat, lon)
        except Exception:
            # Celery ishga tushmagan bo'lsa ham flow buzilmasin.
            pass
    return ""
