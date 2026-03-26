import json
import re
from urllib import request
from urllib.error import URLError
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import escape

from bot.models import TelegramMessageLog
from drivers.models import Driver, DriverStatus, DriverVerificationStatus
from orders.models import Order, OrderStatus, QuantityUnit

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
    }


def normalize_driver_reply_text(text: str) -> str:
    """Chiroyli tugma bosilganda kelgan matnni /buyruq formatiga aylantiradi."""
    t = (text or "").strip()
    return driver_reply_button_aliases().get(t, t)


def driver_idle_reply_keyboard() -> dict:
    """Biriktirilmagan / bo‘sh vaqt: yordam va tezkor buyruqlar."""
    return {
        "keyboard": [
            [{"text": BTN_REPLY_HELP}, {"text": BTN_REPLY_WIZARD}],
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


def send_order_to_group(order: Order) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_GROUP_ID:
        return
    text = build_order_text(order)
    body = {
        "chat_id": settings.TELEGRAM_GROUP_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": build_order_keyboard(order),
    }
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("sendMessage"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("ok"):
        result = data.get("result", {})
        chat_id_value = str(result.get("chat", {}).get("id", settings.TELEGRAM_GROUP_ID))
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

    # Haydovchini biriktirish faqat web-panelda (Telegramda dispetcher oqimi yo‘q).
    return {"inline_keyboard": keyboard}


def _get_assign_candidates(order: Order) -> list[Driver]:
    # Ro'yxatga imkon qadar ko'proq nomzod ko'rsatamiz (sig'im bo'yicha filtr).
    # Yakuniy bloklash (hujjat expired va/yoki mos emas) accept paytida `_can_driver_take_order`
    # va `_driver_has_expired_documents` ichida qilinadi.
    return list(
        Driver.objects.filter(
            verification_status=DriverVerificationStatus.APPROVED,
            status=DriverStatus.AVAILABLE,
            vehicles__capacity_ton__gte=order.weight_ton,
        )
        .distinct()
        .order_by("full_name")[:3]
    )


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
    body = {
        "chat_id": chat_id,
        "text": text,
    }
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
    with request.urlopen(req, timeout=10):
        pass


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
    body = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": text,
    }
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
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": build_order_text(order),
        "parse_mode": "HTML",
        "reply_markup": build_order_keyboard(order),
    }
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        _telegram_api_url("editMessageText"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10):
        pass


def build_dispatcher_panel_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🆕 Yangi buyurtmalar", "callback_data": "ui:orders:new:1"},
                {"text": "✅ Biriktirilgan", "callback_data": "ui:orders:assigned:1"},
            ],
            [
                {"text": "🚛 Yo‘lda", "callback_data": "ui:orders:in_transit:1"},
                {"text": "🟢 Bo‘sh haydovchilar", "callback_data": "ui:drivers:available"},
            ],
            [
                {"text": "📋 Admin buyruqlari", "callback_data": "ui:audit:dispatcher:10"},
                {"text": "🔗 Callback jurnali", "callback_data": "ui:audit:callbacks:10"},
            ],
        ]
    }


def build_pager_keyboard(mode: str, status: str, page_num: int) -> dict:
    prev_page = max(1, page_num - 1)
    next_page = page_num + 1
    return {
        "inline_keyboard": [
            [
                {"text": "⬅️ Oldingi sahifa", "callback_data": f"ui:{mode}:{status}:{prev_page}"},
                {"text": "Keyingi sahifa ➡️", "callback_data": f"ui:{mode}:{status}:{next_page}"},
            ],
            [{"text": "🏠 Bosh panel", "callback_data": "ui:home"}],
        ]
    }


def build_order_detail_keyboard(order: Order) -> dict:
    rows = [
        [
            {"text": "🔄 Ma’lumotni yangilash", "callback_data": f"ord:refresh:{order.pk}"},
            {"text": "👤 Haydovchini tanlash", "callback_data": f"ord:assign_menu:{order.pk}"},
        ]
    ]
    if order.status in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT, OrderStatus.ISSUE}:
        rows.append([{"text": "↩️ Haydovchini ajratish", "callback_data": f"ord:unassign:{order.pk}"}])
    rows.append([{"text": "🏠 Bosh panel", "callback_data": "ui:home"}])
    return {"inline_keyboard": rows}


def build_assign_candidates_keyboard(order: Order) -> dict:
    drivers = _get_assign_candidates(order)
    rows = []
    for driver in drivers:
        eta, rating = _driver_eta_and_rating(driver)
        rows.append(
            [
                {
                    "text": f"{driver.full_name} | ETA {eta}m | ⭐{rating}",
                    "callback_data": f"ord:assign:{order.pk}:{driver.pk}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Buyurtmaga qaytish", "callback_data": f"ord:refresh:{order.pk}"}])
    return {"inline_keyboard": rows}


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


def build_driver_review_keyboard(order_id: int, driver_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Tasdiqlash", "callback_data": f"review:{order_id}:{driver_id}:approve"},
                {"text": "❌ Rad etish", "callback_data": f"review:{order_id}:{driver_id}:decline"},
            ]
        ]
    }


def _driver_eta_and_rating(driver: Driver) -> tuple[int, str]:
    snap = driver.performance_snapshots.order_by("-period_year", "-period_month").first()
    if not snap:
        return 0, "0.0"
    eta = int(snap.avg_delivery_time_minutes or 0)
    rating = f"{snap.rating_score:.1f}"
    return eta, rating


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
