import html
import json
import logging
import secrets
import math
import re
from hmac import compare_digest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import URLError

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone as django_timezone
from django.views.decorators.csrf import csrf_exempt

from bot.copy_uz import (
    DRIVER_HELP,
    DRIVER_HELP_IN_TRANSIT,
    DRIVER_NOT_FOUND,
    ORDER_NOT_FOUND,
    REGISTER_FIRST,
    UNKNOWN_COMMAND_DRIVER,
    WEB_ONLY_CALLBACK_ANSWER,
)
from bot.models import CriticalActionConfirmation, DriverOnboardingState, TelegramMessageLog
from bot.services import (
    answer_callback_query,
    build_active_trip_focus_message_html,
    build_driver_wizard_keyboard,
    build_start_trip_driver_message_html,
    driver_idle_reply_keyboard,
    driver_reply_keyboard_for_order,
    edit_chat_message,
    edit_group_message,
    normalize_driver_reply_text,
    normalize_telegram_command_text,
    send_chat_message,
    send_ops_notification,
    send_order_native_map_pins,
)
from dispatch.models import (
    Assignment,
    DriverOfferApproval,
    DriverOfferDecision,
    DriverOfferResponse,
)
from drivers.models import Driver, DriverStatus, DriverVerificationStatus, Vehicle
from orders.models import Order, OrderStatus, QuantityUnit
from orders.quantity import quantity_to_metric_tonnes
from orders.services import transition_order
from tracking.models import LocationPing, LocationSource
from analytics.models import AlertEvent, AlertType
from analytics.tasks import detect_location_fraud_task, detect_route_deviation_task

logger = logging.getLogger(__name__)


def _normalize_quantity_uom_token(token: str) -> str | None:
    t = (token or "").lower().strip().rstrip(".")
    if t in ("tonna", "ton", "t", "т"):
        return QuantityUnit.TON
    if t in ("kg", "кг"):
        return QuantityUnit.KG
    if t in ("litr", "litre", "l", "л", "liter"):
        return QuantityUnit.LITER
    return None


def _parse_driver_hajm_command(parts: list[str]) -> tuple[Decimal | None, str | None, Decimal | None, str | None]:
    """quantity, QuantityUnit, density (litr uchun ixtiyoriy agar buyurtmada zichlik bo‘lsa), xato matni."""
    if len(parts) < 3:
        return None, None, None, "Format: <code>/yuklandi 10.5 tonna</code> yoki <code>/topshirildi 12000 kg</code>"
    try:
        qty = Decimal(parts[1].replace(",", "."))
        if qty <= 0:
            return None, None, None, "Musbat son kiriting"
    except Exception:
        return None, None, None, "Birinchi raqam — hajm (masalan 10.5)"
    uom = _normalize_quantity_uom_token(parts[2])
    if not uom:
        return None, None, None, "O‘lchov: <code>tonna</code>, <code>kg</code> yoki <code>litr</code> (+ zichlik)"
    density = None
    if uom == QuantityUnit.LITER and len(parts) >= 4:
        try:
            density = Decimal(parts[3].replace(",", "."))
            if density <= 0:
                return None, None, None, "Zichlik 0 dan katta bo‘lsin (kg/L)"
        except Exception:
            return None, None, None, "Zichlik noto‘g‘ri (masalan 0.84)"
    return qty, uom, density, None


def _parse_driver_hajm_free_text(text: str) -> tuple[Decimal | None, str | None, Decimal | None, str | None]:
    """
    Komandasiz format:
    - 10.5 tonna
    - 12000 kg
    - 12000 litr 0.84
    """
    raw = (text or "").strip()
    if not raw:
        return None, None, None, "Hajmni yuboring: <code>12000 kg</code> yoki <code>10.5 tonna</code>."
    parts = raw.split()
    if len(parts) < 2:
        return None, None, None, "Birlikni ham yozing: <code>kg</code>, <code>tonna</code> yoki <code>litr</code>."
    qty_token = parts[0].replace(",", ".")
    try:
        qty = Decimal(qty_token)
        if qty <= 0:
            return None, None, None, "Musbat son kiriting."
    except Exception:
        return None, None, None, "Birinchi qiymat raqam bo‘lsin (masalan: <code>12000 kg</code>)."
    uom = _normalize_quantity_uom_token(parts[1])
    if not uom:
        return None, None, None, "O‘lchov: <code>tonna</code>, <code>kg</code> yoki <code>litr</code>."
    density = None
    if uom == QuantityUnit.LITER:
        if len(parts) >= 3:
            try:
                density = Decimal(parts[2].replace(",", "."))
                if density <= 0:
                    return None, None, None, "Zichlik 0 dan katta bo‘lsin (kg/L)."
            except Exception:
                return None, None, None, "Zichlik noto‘g‘ri (masalan: <code>0.84</code>)."
    return qty, uom, density, None


_ONB_FIRST_TOTAL = 5
_ONB_PHOTO_HINT = (
    "📎 <b>Clip</b> → <b>Rasm</b> yoki <b>Fotosurat</b>dan yuboring.\n"
    "Rasm aniq va to'liq ko'rinsin."
)


def _parse_capacity_kg(text: str) -> tuple[Decimal | None, str | None]:
    raw = (text or "").strip().lower().replace(",", ".")
    raw = re.sub(r"\s*kg\s*$", "", raw).strip()
    if not raw:
        return None, "Sig‘imni yozing (kg). Masalan: <code>12000</code>"
    try:
        kg = Decimal(raw)
    except Exception:
        return None, "Raqam noto‘g‘ri. Masalan: <code>12000</code> yoki <code>11500.5</code>"
    if kg <= 0:
        return None, "Musbat son kiriting (kg)."
    return kg, None


def _onb_progress_bar(current: int, total: int) -> str:
    if total <= 0:
        return ""
    width = 8
    filled = int(round(width * current / total))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled) + f"  {current}/{total}"


def _onb_first_block(current: int, emoji: str, title: str, body: str) -> str:
    bar = html.escape(_onb_progress_bar(current, _ONB_FIRST_TOTAL))
    return (
        "<b>📋 Haydovchi ro'yxatdan o'tishi</b>\n"
        f"<code>{bar}</code>\n\n"
        f"{emoji} <b>{html.escape(title)}</b>\n\n"
        f"{body}"
    )


@csrf_exempt
def webhook(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.TELEGRAM_WEBHOOK_SECRET and not compare_digest(secret, settings.TELEGRAM_WEBHOOK_SECRET):
        logger.warning(
            "telegram_webhook_403: X-Telegram-Bot-Api-Secret-Token mos emas yoki yo'q. "
            "setWebhook da secret_token .env dagi TELEGRAM_WEBHOOK_SECRET bilan bir xil bo'lishi kerak."
        )
        # Telegram qayta-qayta urinmasligi uchun 403 qaytaramiz, lekin diagnostika uchun DB'ga ham yozib qo'yamiz.
        try:
            raw = request.body.decode("utf-8")
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        callback_query = payload.get("callback_query") or {}
        user = callback_query.get("from") or {}
        TelegramMessageLog.objects.create(
            chat_id="",
            message_id="",
            event="webhook_forbidden",
            payload={
                "has_secret_header": bool(secret),
                "user_id": user.get("id"),
                "callback_data": callback_query.get("data"),
                "update_id": payload.get("update_id"),
            },
            signature="(mismatch)",
            source_ip=request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")),
        )
        return HttpResponse(status=403)
    source_ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", ""))
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Do not return 400 to Telegram to avoid endless retries on malformed payload.
        return JsonResponse({"ok": True})

    update_id = payload.get("update_id")
    dedup_key = f"tg:webhook:update:{update_id}" if isinstance(update_id, int) else None
    if dedup_key and cache.get(dedup_key):
        return JsonResponse({"ok": True})

    try:
        callback_query = payload.get("callback_query")
        if callback_query:
            _handle_callback(callback_query, signature=secret, source_ip=source_ip)
            if dedup_key:
                cache.set(dedup_key, "1", 86400)
            return JsonResponse({"ok": True})
        edited_message = payload.get("edited_message") or {}
        if edited_message:
            # Telegram Live Location yangilanishlari shu yerda keladi (Yandex Taxi kabi).
            _handle_message(edited_message, signature=secret, source_ip=source_ip)
            if dedup_key:
                cache.set(dedup_key, "1", 86400)
            return JsonResponse({"ok": True})
        message = payload.get("message") or {}
        if message:
            _handle_message(message, signature=secret, source_ip=source_ip)
            if dedup_key:
                cache.set(dedup_key, "1", 86400)
            return JsonResponse({"ok": True})
    except Exception as exc:
        callback_query = payload.get("callback_query") or {}
        callback_data = str(callback_query.get("data", ""))
        user = callback_query.get("from") or {}
        TelegramMessageLog.objects.create(
            chat_id="",
            message_id="",
            event="webhook_error",
            payload={
                "error": str(exc),
                "callback_data": callback_data,
                "user_id": user.get("id"),
            },
            signature=secret,
            source_ip=source_ip,
        )
        return JsonResponse({"ok": True})
    if dedup_key:
        cache.set(dedup_key, "1", 86400)
    return JsonResponse({"ok": True})


def _location_captured_at_from_message(message: dict | None) -> datetime:
    if not message:
        return django_timezone.now()
    ts = message.get("edit_date") or message.get("date")
    if isinstance(ts, int):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return django_timezone.now()


def _telegram_user_id_for_location_message(message: dict) -> int:
    """Telegram ba'zan edited_message da `from` qaytarmaydi; shaxsiy chatda chat.id == user id."""
    user = message.get("from") or {}
    uid = int(user.get("id", 0) or 0)
    if uid:
        return uid
    chat = message.get("chat") or {}
    if str(chat.get("type", "")).lower() == "private":
        try:
            return int(chat.get("id", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _is_live_location_telegram_event(location: dict, message: dict | None) -> bool:
    """
    Jonli joylashuv: birinchi xabarda `live_period` bo‘ladi; keyingi yangilanishlar `edited_message`.
    Bunday holatda haydovchi yuklash nuqtasidan uzoqda bo‘lishi mumkin — masofa tekshiruvini qattiq qo‘llamaymiz.
    """
    if location.get("live_period") is not None:
        return True
    if message and message.get("edit_date"):
        return True
    return False


def _save_location(telegram_user_id: int, location: dict, message: dict | None = None) -> None:
    driver = Driver.objects.filter(telegram_user_id=telegram_user_id).first()
    if not driver:
        return
    assignment = (
        Assignment.objects.select_related("order")
        .filter(driver=driver, order__status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT])
        .order_by("-assigned_at")
        .first()
    )
    if not assignment:
        return
    prev_ping = (
        LocationPing.objects.filter(order=assignment.order, driver=driver)
        .order_by("-captured_at")
        .first()
    )
    try:
        lat = float(location.get("latitude"))
        lon = float(location.get("longitude"))
    except (TypeError, ValueError):
        return
    if not _is_uzbekistan_bbox(lat, lon):
        cache_key = f"gps_warn:{driver.pk}:{assignment.order_id}"
        if cache.add(cache_key, "1", timeout=600):
            send_chat_message(
                str(driver.telegram_user_id or ""),
                "📍 Lokatsiya xato ko'rinyapti (UZ hududidan tashqari). GPS ni yoqib, qayta yuboring.",
            )
        return
    order_point = _extract_coords_text(assignment.order.from_location)
    if (
        order_point
        and prev_ping is None
        and not _is_live_location_telegram_event(location, message)
    ):
        dist = _distance_km(lat, lon, order_point[0], order_point[1])
        if dist > settings.GPS_MAX_DISTANCE_FROM_ORDER_KM:
            send_chat_message(
                str(driver.telegram_user_id or ""),
                "⚠️ Lokatsiya buyurtma nuqtasidan juda uzoq. Telefon GPS sozlamasini tekshiring.",
            )
            return
    interval = int(getattr(settings, "TELEGRAM_LIVE_LOCATION_SAVE_INTERVAL_SEC", 5) or 0)
    if interval > 0:
        throttle_key = f"tg:liveloc:{driver.pk}:{assignment.order_id}"
        if not cache.add(throttle_key, "1", timeout=interval):
            return
    captured_at = _location_captured_at_from_message(message)
    created = LocationPing.objects.create(
        order=assignment.order,
        driver=driver,
        latitude=lat,
        longitude=lon,
        source=LocationSource.TELEGRAM,
        captured_at=captured_at,
    )
    if prev_ping:
        try:
            detect_route_deviation_task.delay(
                assignment.order_id,
                driver.id,
                float(created.latitude),
                float(created.longitude),
            )
            detect_location_fraud_task.delay(assignment.order_id, driver.id)
        except Exception:
            detect_route_deviation_task(
                assignment.order_id,
                driver.id,
                float(created.latitude),
                float(created.longitude),
            )
            detect_location_fraud_task(assignment.order_id, driver.id)


def _handle_assigned_driver_group_callback(
    callback_query: dict,
    *,
    driver: Driver,
    order: Order,
    action: str,
    signature: str,
    source_ip: str,
) -> None:
    callback_id = str(callback_query.get("id", ""))
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    message_id = str(callback_query.get("message", {}).get("message_id", ""))
    username = (callback_query.get("from") or {}).get("username") or str(driver.pk)

    if not Assignment.objects.filter(order=order, driver=driver).exists():
        _safe_answer(callback_id, "Bu buyurtma sizga biriktirilmagan.")
        return

    if TelegramMessageLog.objects.filter(event="callback", payload__callback_query_id=callback_id).exists():
        _safe_answer(callback_id, "Action allaqachon bajarilgan")
        return

    if action == "finish_req":
        if order.status != OrderStatus.IN_TRANSIT:
            _safe_answer(callback_id, "Tugatish faqat yo‘lda holatida.")
            return
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=chat_id,
            message_id=message_id,
            event="driver_finish_requested",
            signature=signature,
            source_ip=source_ip,
            payload={"driver_id": driver.pk, "source": "group_callback"},
        )
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=chat_id,
            message_id=message_id,
            event="callback",
            dedupe_key=f"callback:{callback_id}",
            signature=signature,
            source_ip=source_ip,
            payload={
                "callback_query_id": callback_id,
                "action": "finish_req",
                "changed": False,
                "by": username,
                "actor_id": driver.telegram_user_id,
                "from_status": order.status,
                "to_status": order.status,
            },
        )
        _safe_answer(callback_id, "So‘rov qabul qilindi. Admin webda tasdiqlaydi.")
        return

    changed = False
    from_status = order.status

    if action == "start":
        if order.status != OrderStatus.ASSIGNED:
            _safe_answer(callback_id, f"Hozirgi holat: {order.get_status_display()}")
            return
        with transaction.atomic():
            locked = Order.objects.select_for_update().filter(pk=order.pk).first()
            changed = bool(locked) and transition_order(locked, OrderStatus.IN_TRANSIT, changed_by=driver.full_name)
            order = locked or order
    elif action == "issue":
        with transaction.atomic():
            locked = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked:
                changed = False
            else:
                changed = transition_order(locked, OrderStatus.ISSUE, changed_by=driver.full_name)
                order = locked
    else:
        _safe_answer(callback_id, "Action topilmadi")
        return

    TelegramMessageLog.objects.create(
        order=order,
        chat_id=chat_id,
        message_id=message_id,
        event="callback",
        dedupe_key=f"callback:{callback_id}",
        signature=signature,
        source_ip=source_ip,
        payload={
            "callback_query_id": callback_id,
            "action": action,
            "changed": changed,
            "by": username,
            "actor_id": driver.telegram_user_id,
            "from_status": from_status,
            "to_status": order.status,
        },
    )
    if changed:
        _safe_edit(chat_id, message_id, order)
        _safe_answer(callback_id, f"Status -> {order.get_status_display()}")
        if action == "start" and driver.telegram_user_id:
            order.refresh_from_db()
            send_chat_message(
                str(driver.telegram_user_id),
                build_start_trip_driver_message_html(
                    order, for_telegram_user_id=driver.telegram_user_id or None
                ),
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    order, telegram_user_id=driver.telegram_user_id or 0
                ),
                disable_web_page_preview=True,
            )
            send_order_native_map_pins(str(driver.telegram_user_id), order)
    else:
        _safe_answer(callback_id, "Status o'zgarmadi")


def _driver_offer_precheck_callback(
    callback_query: dict,
    *,
    driver: Driver,
    order: Order | None,
    callback_id: str,
) -> bool:
    """True = davom etish mumkin. False = allaqachon javob berilgan."""
    if driver.verification_status != DriverVerificationStatus.APPROVED:
        _safe_answer(callback_id, "Hujjatlaringiz hali tasdiqlanmadi. Admin tekshiradi.")
        send_chat_message(
            str(driver.telegram_user_id or ""),
            "⚠️ Hujjatlaringiz hali tasdiqlanmadi. Admin ko'rib chiqyapti.",
        )
        return False
    if _driver_has_expired_documents(driver):
        issues = _driver_expired_documents_issues(driver)
        today = django_timezone.localdate()
        has_actual_expired = bool(driver.license_expires_at and driver.license_expires_at < today) or any(
            v.calibration_expires_at and v.calibration_expires_at < today for v in driver.vehicles.all()
        )
        if has_actual_expired and order:
            alert, created = AlertEvent.objects.get_or_create(
                order=order,
                alert_type=AlertType.DRIVER_DOC_EXPIRED,
                threshold_minutes=0,
                defaults={
                    "driver": driver,
                    "message": "Hujjat expired: " + ", ".join(issues[:3]) if issues else "Hujjat expired",
                },
            )
            if created:
                cache.delete("ops_dashboard_v1")
        _safe_answer(callback_id, "Hujjatlar muddati tugagan. Ofis (admin) ga murojaat qiling.")
        send_chat_message(
            str(driver.telegram_user_id or ""),
            "❌ Yukni qabul qilib bo'lmaydi: hujjat muddati tugagan. Hujjatlarni yangilang.",
        )
        return False
    return True


def _handle_callback(callback_query: dict, signature: str = "", source_ip: str = "") -> None:
    callback_id = str(callback_query.get("id", ""))
    user = callback_query.get("from") or {}
    user_id = int(user.get("id", 0))
    callback_data = str(callback_query.get("data", ""))

    if callback_data.startswith(("ui:", "ord:", "review:")):
        _safe_answer(callback_id, WEB_ONLY_CALLBACK_ANSWER)
        return

    if callback_data.startswith(("drv:", "order:")):
        if not _acquire_callback_lock(user_id=user_id, callback_data=callback_data):
            _safe_answer(callback_id, "Iltimos, 2-3 soniya kuting...")
            return

    if callback_data.startswith("order:"):
        parts = callback_data.split(":")
        if len(parts) < 3 or not parts[1].isdigit():
            _safe_answer(callback_id, "Noto'g'ri action")
            return
        order_id = int(parts[1])
        action = parts[2]
        if action == "cancel":
            action = "reject"
        try:
            order = Order.objects.filter(pk=order_id).first()
        except InvalidOperation:
            _repair_order_decimals(order_id)
            order = Order.objects.filter(pk=order_id).first()
        if not order:
            _safe_answer(callback_id, "Buyurtma topilmadi")
            return

        if action in {"assign", "reassign"} or (action == "complete"):
            _safe_answer(callback_id, WEB_ONLY_CALLBACK_ANSWER)
            return

        driver = Driver.objects.filter(telegram_user_id=user_id).first()
        if not driver:
            _safe_answer(
                callback_id,
                "Siz haydovchi sifatida ro‘yxatdan o‘tmagansiz. Zakaz qabul qilish uchun avval /start bosib telefon raqamingizni yuboring.",
            )
            return

        if action in {"start", "finish_req"}:
            _handle_assigned_driver_group_callback(
                callback_query,
                driver=driver,
                order=order,
                action=action,
                signature=signature,
                source_ip=source_ip,
            )
            return

        if action == "issue":
            if order.status in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT}:
                _handle_assigned_driver_group_callback(
                    callback_query,
                    driver=driver,
                    order=order,
                    action="issue",
                    signature=signature,
                    source_ip=source_ip,
                )
                return
            if order.status in {OrderStatus.NEW, OrderStatus.OFFERED, OrderStatus.ISSUE}:
                if not _driver_offer_precheck_callback(callback_query, driver=driver, order=order, callback_id=callback_id):
                    return
                _handle_driver_offer_callback(callback_query, f"order:{order_id}:issue", driver)
                return
            _safe_answer(callback_id, f"Bu holatda Muammo tugmasi ishlamaydi: {order.get_status_display()}")
            return

        if action in {"accept", "reject"}:
            if order.status not in {OrderStatus.NEW, OrderStatus.OFFERED, OrderStatus.ISSUE}:
                _safe_answer(callback_id, f"Taklif tugmalari bu holatda ishlamaydi: {order.get_status_display()}")
                return
            if not _driver_offer_precheck_callback(callback_query, driver=driver, order=order, callback_id=callback_id):
                return
            _handle_driver_offer_callback(callback_query, f"order:{order_id}:{action}", driver)
            return

        _safe_answer(callback_id, WEB_ONLY_CALLBACK_ANSWER)
        return

    if callback_data.startswith("onb:"):
        _handle_onboarding_callback(callback_query, callback_data)
        return

    if callback_data.startswith("drv:"):
        driver = Driver.objects.filter(telegram_user_id=user_id).first()
        if not driver:
            _safe_answer(callback_id, "Siz driver sifatida bog'lanmagansiz")
            return
        _safe_answer(callback_id, "⏳ Driver wizard...")
        _handle_driver_wizard_callback(callback_query, callback_data)
        return

    _safe_answer(callback_id, "Noto'g'ri action")


def _log_ui_or_card_callback(
    callback_query: dict,
    *,
    action: str,
    order: Order | None = None,
    ok: bool = True,
    changed: bool | None = None,
) -> None:
    """
    UI/ord/drv callback’lari uchun `event="callback"` audit log.

    `_handle_callback` ichida bu log faqat `order:*` branch’da yaratiladi, shuning uchun
    ui:/ord:/drv: handler’larida ham auditga yozish kerak.
    """
    callback_id = str(callback_query.get("id", "") or "")
    if not callback_id:
        return

    if TelegramMessageLog.objects.filter(event="callback", payload__callback_query_id=callback_id).exists():
        return

    user = callback_query.get("from") or {}
    actor_id = 0
    try:
        actor_id = int(user.get("id", 0) or 0)
    except (TypeError, ValueError):
        actor_id = 0
    by = user.get("username") or str(actor_id or "unknown")

    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", "") or "")
    message_id = str(callback_query.get("message", {}).get("message_id", "") or "")

    payload: dict = {
        "callback_query_id": callback_id,
        "action": action,
        "ok": ok,
        "by": by,
        "actor_id": actor_id,
    }
    if changed is not None:
        payload["changed"] = changed

    TelegramMessageLog.objects.create(
        order=order,
        chat_id=chat_id,
        message_id=message_id,
        event="callback",
        dedupe_key=f"callback:{callback_id}",
        payload=payload,
    )


def _safe_answer(callback_id: str, text: str) -> None:
    if not callback_id:
        return
    try:
        answer_callback_query(callback_id, text=text)
    except URLError:
        return


def _safe_edit(chat_id: str, message_id: str, order: Order) -> None:
    try:
        edit_group_message(chat_id, message_id, order)
    except (URLError, ValueError):
        return


def _safe_edit_text(
    chat_id: str,
    message_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> None:
    try:
        edit_chat_message(chat_id, message_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except (URLError, ValueError):
        return


def _handle_driver_wizard_callback(callback_query: dict, callback_data: str) -> None:
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    message_id = str(callback_query.get("message", {}).get("message_id", ""))
    user = callback_query.get("from") or {}
    telegram_user_id = int(user.get("id", 0))
    if not telegram_user_id:
        return
    driver = Driver.objects.filter(telegram_user_id=telegram_user_id).first()
    if not driver:
        _safe_edit_text(chat_id, message_id, REGISTER_FIRST, parse_mode="HTML")
        return
    parts = callback_data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return
    action = parts[1]
    if action == "cancel":
        _log_ui_or_card_callback(callback_query, action=action, order=None, ok=True)
        _safe_edit_text(chat_id, message_id, "❌ <b>Wizard bekor qilindi.</b>", parse_mode="HTML")
        return
    order = _resolve_driver_order(driver, parts[2])
    if not order:
        _safe_edit_text(chat_id, message_id, "Faol buyurtma topilmadi.")
        return

    if action in {"checkpoint", "summary", "back"}:
        _log_ui_or_card_callback(callback_query, action=action, order=order, ok=True)

    if action == "start":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            changed = bool(locked_order) and transition_order(locked_order, OrderStatus.IN_TRANSIT, changed_by=driver.full_name)
            order = locked_order or order
        _log_ui_or_card_callback(callback_query, action=action, order=order, ok=True, changed=changed)
        text = "✅ Safar boshlandi." if changed else "❌ Safarni boshlab bo‘lmadi (holat mos emas)."
        step = 1
    elif action == "checkpoint":
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=chat_id,
            message_id=message_id,
            event="driver_checkpoint",
            payload={"driver_id": driver.pk, "note": "Wizard checkpoint"},
        )
        text = "✅ Checkpoint yozib olindi."
        step = 2
    elif action == "summary":
        pings_count = LocationPing.objects.filter(order=order, driver=driver).count()
        checkpoints_count = TelegramMessageLog.objects.filter(order=order, event="driver_checkpoint").count()
        text = (
            f"📊 Checkpoint: {checkpoints_count} · Lokatsiya: {pings_count} · "
            f"Holat: {order.get_status_display()}"
        )
        step = 3
    elif action == "finish":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            changed = bool(locked_order) and transition_order(locked_order, OrderStatus.COMPLETED, changed_by=driver.full_name)
            order = locked_order or order
        _log_ui_or_card_callback(callback_query, action=action, order=order, ok=True, changed=changed)
        if changed:
            driver.status = DriverStatus.AVAILABLE
            driver.save(update_fields=["status", "updated_at"])
            if driver.telegram_user_id:
                send_chat_message(
                    str(driver.telegram_user_id),
                    "✅ Reys tizimda yopildi. Yangi takliflar uchun guruhni kuzating.",
                    reply_markup=driver_idle_reply_keyboard(),
                )
        text = "✅ Safar yakunlandi." if changed else "❌ Safarni tugatib bo‘lmadi (holat mos emas)."
        step = 4
    elif action == "back":
        step = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text = "⬅️ Oldingi qadam."
    else:
        text = "Amal topilmadi."
        step = 1
    order.refresh_from_db()
    if action == "start" and changed and driver.telegram_user_id:
        send_chat_message(
            str(driver.telegram_user_id),
            build_start_trip_driver_message_html(
                order, for_telegram_user_id=driver.telegram_user_id or None
            ),
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                order, telegram_user_id=driver.telegram_user_id or 0
            ),
            disable_web_page_preview=True,
        )
        send_order_native_map_pins(str(driver.telegram_user_id), order)
    trip_in_progress = order.status == OrderStatus.IN_TRANSIT
    _safe_edit_text(
        chat_id,
        message_id,
        _build_driver_wizard_text(order.pk, step, text, order.get_status_display()),
        reply_markup=build_driver_wizard_keyboard(
            order.pk, current_step=step, trip_in_progress=trip_in_progress
        ),
        parse_mode="HTML",
    )


def _handle_driver_offer_callback(callback_query: dict, callback_data: str, driver: Driver) -> None:
    callback_id = str(callback_query.get("id", ""))
    parts = callback_data.split(":")
    if len(parts) < 3 or parts[0] != "order" or not parts[1].isdigit():
        _safe_answer(callback_id, "Action xato")
        return
    order_id = int(parts[1])
    try:
        order = Order.objects.filter(pk=order_id).first()
    except InvalidOperation:
        _repair_order_decimals(order_id)
        order = Order.objects.filter(pk=order_id).first()
    action = parts[2]
    if not order or action not in {"accept", "reject", "issue"}:
        _safe_answer(callback_id, "Order/action topilmadi")
        return
    if order.status in {OrderStatus.COMPLETED, OrderStatus.CANCELED}:
        _safe_answer(callback_id, "Bu buyurtma yakunlangan")
        return
    if order.status not in {OrderStatus.NEW, OrderStatus.OFFERED, OrderStatus.ISSUE}:
        _safe_answer(callback_id, f"Taklif tugmalari bu holatda ishlamaydi: {order.get_status_display()}")
        return

    decision = DriverOfferDecision.ACCEPT
    approval = DriverOfferApproval.PENDING
    note = ""
    if action == "reject":
        decision = DriverOfferDecision.REJECT
        approval = DriverOfferApproval.DECLINED
    elif action == "issue":
        decision = DriverOfferDecision.ISSUE
        approval = DriverOfferApproval.PENDING
        note = "Driver muammo tugmasini bosdi"

    response, _ = DriverOfferResponse.objects.update_or_create(
        order=order,
        driver=driver,
        defaults={
            "decision": decision,
            "approval_status": approval,
            "note": note,
            "responded_at": django_timezone.now(),
            "reviewed_by": "",
            "reviewed_at": None,
        },
    )

    if decision == DriverOfferDecision.ACCEPT:
        order.refresh_from_db()
        if order.status not in {OrderStatus.NEW, OrderStatus.OFFERED, OrderStatus.ISSUE}:
            response.approval_status = DriverOfferApproval.DECLINED
            response.note = "Buyurtma holati o'zgardi (taklif tugashi)"
            response.reviewed_by = "system"
            response.reviewed_at = django_timezone.now()
            response.save(update_fields=["approval_status", "note", "reviewed_by", "reviewed_at"])
            send_chat_message(
                str(driver.telegram_user_id),
                (
                    f"❌ Buyurtma #{order.pk} endi «Qabul» orqali olinmaydi "
                    f"(holat: {order.get_status_display()}). "
                    "Boshqa haydovchiga biriktirilgan yoki webdan yangilangan bo‘lishi mumkin — guruhdagi xabarni tekshiring."
                ),
            )
            _safe_answer(callback_id, "Buyurtma holati o‘zgardi")
            return
        blocking_a = (
            Assignment.objects.select_related("order")
            .filter(
                driver=driver,
                order__status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT],
            )
            .exclude(order=order)
            .order_by("-assigned_at")
            .first()
        )
        if blocking_a:
            bo = blocking_a.order
            response.approval_status = DriverOfferApproval.DECLINED
            response.note = "Driver band (zanyat)"
            response.reviewed_by = "system"
            response.reviewed_at = django_timezone.now()
            response.save(update_fields=["approval_status", "note", "reviewed_by", "reviewed_at"])
            send_chat_message(
                str(driver.telegram_user_id),
                (
                    f"❌ Avval buyurtma #{bo.pk} ni yakunlang (holat: {bo.get_status_display()}). "
                    f"Yangi taklif: #{order.pk}.\n\n"
                    "<b>Nima qilish kerak:</b>\n"
                    "• Holatni ko‘rish: <code>/trip_summary</code> yoki <code>/trip_summary "
                    f"{bo.pk}</code>\n"
                    "• Agar hali yo‘lga chiqmagan bo‘lsangiz: guruhda «Yo‘lda (safar boshlash)» yoki "
                    "<code>/start_trip</code>\n"
                    "• Yo‘lda bo‘lsangiz: <code>/finish_trip</code> — keyin <b>admin web</b>da "
                    "tugatishni tasdiqlashi kerak.\n"
                    "• Eskicha qotib qolgan bo‘lsa — dispetcher bilan bog‘laning."
                ),
                parse_mode="HTML",
            )
            _safe_answer(callback_id, "Boshqa buyurtmada band")
            return
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=str(driver.telegram_user_id or ""),
            message_id="",
            event="driver_offer_response",
            payload={"decision": "accept", "approval_status": "pending", "driver_id": driver.pk},
        )
        send_ops_notification("driver_offer_accept", order=order, driver=driver)
        _safe_answer(callback_id, "✅ So'rovingiz qabul qilindi. Admin web panelda tasdiqlaydi.")
        return
    if decision == DriverOfferDecision.REJECT:
        TelegramMessageLog.objects.create(
            order=order,
            chat_id=str(driver.telegram_user_id or ""),
            message_id="",
            event="driver_offer_response",
            payload={"decision": "reject", "approval_status": "declined", "driver_id": driver.pk},
        )
        send_ops_notification("driver_offer_reject", order=order, driver=driver)
        _safe_answer(callback_id, "✅ Rad javobi yuborildi")
        return
    TelegramMessageLog.objects.create(
        order=order,
        chat_id=str(driver.telegram_user_id or ""),
        message_id="",
        event="driver_offer_response",
        payload={"decision": "issue", "approval_status": "pending", "driver_id": driver.pk},
    )
    send_ops_notification("driver_offer_issue", order=order, driver=driver)
    _safe_answer(callback_id, "✅ Muammo signali yuborildi")


def _apply_driver_hajm(
    *,
    driver: Driver,
    order: Order,
    chat_id: str,
    message_id: str,
    loaded: bool,
    quantity: Decimal,
    uom: str,
    density: Decimal | None,
) -> tuple[bool, str]:
    """Haydovchi Telegram orqali yuklangan / topshirilgan hajmni yozadi."""
    if loaded:
        eff_density = density if density is not None else order.density_kg_per_liter
    else:
        eff_density = density if density is not None else (
            order.delivered_density_kg_per_liter or order.density_kg_per_liter
        )
    if uom == QuantityUnit.LITER and (eff_density is None or eff_density <= 0):
        return (
            False,
            "Litr uchun <code>zichlik kg/L</code> kerak: masalan "
            "<code>/topshirildi 10000 litr 0.84</code> yoki avval <code>/zichlik 0.84</code> "
            "(yuklangan uchun); topshirishda alohida zichlik webdan ham mumkin.",
        )

    ton_preview = quantity_to_metric_tonnes(quantity, uom, density_kg_per_liter=eff_density)
    if ton_preview is None:
        return False, "Hajmni tonnaga aylantirib bo‘lmadi."

    now = django_timezone.now()
    with transaction.atomic():
        locked = Order.objects.select_for_update().filter(pk=order.pk).first()
        if not locked:
            return False, "Buyurtma topilmadi"
        uf: list[str] = ["updated_at"]
        if density is not None:
            if loaded:
                locked.density_kg_per_liter = density
                uf.append("density_kg_per_liter")
            else:
                locked.delivered_density_kg_per_liter = density
                uf.append("delivered_density_kg_per_liter")
        if loaded:
            locked.loaded_quantity = quantity
            locked.loaded_quantity_uom = uom
            locked.loaded_recorded_at = now
            locked.loaded_recorded_by = f"tg:{driver.pk}"
            uf += ["loaded_quantity", "loaded_quantity_uom", "loaded_recorded_at", "loaded_recorded_by"]
        else:
            locked.delivered_quantity = quantity
            locked.delivered_quantity_uom = uom
            locked.delivered_recorded_at = now
            locked.delivered_recorded_by = f"tg:{driver.pk}"
            uf += ["delivered_quantity", "delivered_quantity_uom", "delivered_recorded_at", "delivered_recorded_by"]
        locked.save(update_fields=uf)

    TelegramMessageLog.objects.create(
        order=order,
        chat_id=chat_id,
        message_id=message_id,
        event="driver_loaded_quantity" if loaded else "driver_delivered_quantity",
        payload={
            "driver_id": driver.pk,
            "quantity": str(quantity),
            "uom": uom,
            "metric_ton": str(ton_preview),
            "density_kg_l": str(eff_density) if eff_density is not None else None,
        },
    )
    send_ops_notification(
        "driver_loaded_quantity" if loaded else "driver_delivered_quantity",
        order=order,
        driver=driver,
        note=f"{quantity} {dict(QuantityUnit.choices).get(uom, uom)}",
    )
    uom_disp = dict(QuantityUnit.choices).get(uom, uom)
    kind = "Yuklangan (fakt)" if loaded else "Topshirilgan (klient)"
    return (
        True,
        f"✅ <b>{kind}</b>\n#{order.pk}: <b>{quantity}</b> {uom_disp}\n"
        f"Tonna ekvivalenti: ≈ <b>{ton_preview}</b> t",
    )


def _release_assigned_driver(order: Order) -> None:
    assignment = Assignment.objects.filter(order=order).select_related("driver").first()
    if not assignment:
        return
    driver = assignment.driver
    driver.status = DriverStatus.AVAILABLE
    driver.save(update_fields=["status", "updated_at"])


def _handle_driver_command(message: dict) -> None:
    user = message.get("from") or {}
    telegram_user_id = user.get("id")
    if not telegram_user_id:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    raw_text = normalize_telegram_command_text(str(message.get("text", "")))
    parts = raw_text.split()
    command = parts[0] if parts else ""
    if command == "/start":
        _handle_driver_start(chat_id, int(telegram_user_id), parts)
        return
    driver = Driver.objects.filter(telegram_user_id=int(telegram_user_id)).first()
    if not driver:
        send_chat_message(chat_id, REGISTER_FIRST, parse_mode="HTML")
        return
    if command == "/help":
        ao_help = _resolve_driver_order(driver, None)
        if ao_help and ao_help.status == OrderStatus.IN_TRANSIT:
            body = DRIVER_HELP_IN_TRANSIT
        else:
            body = DRIVER_HELP
        send_chat_message(
            chat_id,
            body,
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                ao_help, telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command == "/add_vehicle":
        active_state = DriverOnboardingState.objects.filter(
            telegram_user_id=int(telegram_user_id), is_active=True
        ).first()
        if active_state and not active_state.step.startswith("add_vehicle_"):
            send_chat_message(
                chat_id,
                "Hozir hujjatlar yoki boshqa kiritish jarayoni ketayapti. Avval uni tugating — "
                "keyin qo‘shimcha mashina uchun <code>/add_vehicle</code>.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
                ),
            )
            return

        state, _ = DriverOnboardingState.objects.get_or_create(
            telegram_user_id=int(telegram_user_id),
            defaults={"driver": driver},
        )
        state.driver = driver
        state.is_active = True
        state.step = "add_vehicle_plate"
        state.payload = {}
        state.save(update_fields=["driver", "is_active", "step", "payload", "updated_at"])

        send_chat_message(
            chat_id,
            "➕ <b>Qo‘shimcha mashina</b> (birinchi avto botda ro‘yxatdan o‘tishda; ikkinchi va keyingi — shu yerda)\n\n"
            "Birinchi qadam: <b>davlat raqami</b>ni yuboring.\n"
            "Masalan: <code>80A123BC</code> (probelsiz, katta harf bilan).",
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command == "/trip_map":
        target_order = _resolve_driver_order(driver, parts[1] if len(parts) > 1 and parts[1].isdigit() else None)
        if not target_order:
            send_chat_message(
                chat_id,
                "Faol buyurtma topilmadi.",
                reply_markup=driver_idle_reply_keyboard(),
            )
            return
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            build_active_trip_focus_message_html(
                target_order, for_telegram_user_id=int(telegram_user_id)
            ),
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
            disable_web_page_preview=True,
        )
        send_order_native_map_pins(chat_id, target_order)
        send_ops_notification("trip_started", order=target_order, driver=driver)
        return
    if command == "/wizard":
        target_order = _resolve_driver_order(driver, parts[1] if len(parts) > 1 and parts[1].isdigit() else None)
        if not target_order:
            send_chat_message(
                chat_id,
                "Faol buyurtma topilmadi.",
                reply_markup=driver_idle_reply_keyboard(),
            )
            return
        target_order.refresh_from_db()
        trip_ip = target_order.status == OrderStatus.IN_TRANSIT
        if trip_ip:
            send_chat_message(
                chat_id,
                build_active_trip_focus_message_html(
                    target_order, for_telegram_user_id=int(telegram_user_id)
                )
                + "\n\n<i>Oraliq: </i><code>/checkpoint</code> · <i>qisqa hisobot: </i><code>/trip_summary</code>",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
                disable_web_page_preview=True,
            )
            send_order_native_map_pins(chat_id, target_order)
            return
        send_chat_message(
            chat_id,
            _build_driver_wizard_text(
                target_order.pk, 1, "Qadamni tanlang", target_order.get_status_display()
            ),
            reply_markup=build_driver_wizard_keyboard(target_order.pk, current_step=1),
            parse_mode="HTML",
        )
        send_chat_message(
            chat_id,
            "⬇️ Pastki tugmalar — buyurtma holatiga mos.",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command == "/zichlik":
        zrk = driver_reply_keyboard_for_order(
            _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
        )
        if len(parts) < 2:
            send_chat_message(
                chat_id,
                "Format: <code>/zichlik 0.84</code> — kg/L (keyingi litr kiritishlar uchun).",
                parse_mode="HTML",
                reply_markup=zrk,
            )
            return
        try:
            dens = Decimal(parts[1].replace(",", "."))
            if dens <= 0:
                raise ValueError
        except Exception:
            send_chat_message(
                chat_id,
                "Zichlik noto‘g‘ri (masalan 0.84).",
                parse_mode="HTML",
                reply_markup=zrk,
            )
            return
        oid = parts[2] if len(parts) > 2 and parts[2].isdigit() else None
        target_order = _resolve_driver_order(driver, oid)
        if not target_order:
            send_chat_message(chat_id, "Faol buyurtma topilmadi.", reply_markup=driver_idle_reply_keyboard())
            return
        target_order.density_kg_per_liter = dens
        target_order.save(update_fields=["density_kg_per_liter", "updated_at"])
        TelegramMessageLog.objects.create(
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            event="driver_density_set",
            payload={"driver_id": driver.pk, "density_kg_per_liter": str(dens)},
        )
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            f"✅ Buyurtma #{target_order.pk}: zichlik <b>{dens}</b> kg/L saqlandi.",
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command in ("/yuklandi", "/topshirildi"):
        qty, uom, density, err = _parse_driver_hajm_command(parts)
        if err:
            send_chat_message(
                chat_id,
                err,
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        target_order = _resolve_driver_order(driver, None)
        if not target_order:
            send_chat_message(
                chat_id,
                "Faol buyurtma topilmadi (biriktirilgan reys).",
                reply_markup=driver_idle_reply_keyboard(),
            )
            return
        ok, msg = _apply_driver_hajm(
            driver=driver,
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            loaded=(command == "/yuklandi"),
            quantity=qty,
            uom=uom,
            density=density,
        )
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            msg,
            parse_mode="HTML" if ok else "HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        if ok:
            TelegramMessageLog.objects.create(
                order=target_order,
                chat_id=chat_id,
                message_id=str(message.get("message_id", "")),
                event="driver_command",
                payload={"command": command, "driver_id": driver.pk, "changed": True},
            )
        return
    if command == "/checkpoint":
        oid = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        target_order = _resolve_driver_order(driver, oid)
        if not target_order:
            send_chat_message(
                chat_id,
                "Checkpoint uchun faol buyurtma topilmadi.",
                reply_markup=driver_idle_reply_keyboard(),
            )
            return
        if oid:
            note = " ".join(parts[2:]).strip() or "Checkpoint"
        else:
            note = " ".join(parts[1:]).strip() or "Checkpoint"
        TelegramMessageLog.objects.create(
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            event="driver_checkpoint",
            payload={"driver_id": driver.pk, "note": note},
        )
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            f"✅ Checkpoint saqlandi (buyurtma #{target_order.pk}).",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command == "/trip_summary":
        target_order = _resolve_driver_order(driver, parts[1] if len(parts) > 1 else None)
        if not target_order:
            send_chat_message(
                chat_id,
                "Trip summary uchun buyurtma topilmadi.",
                reply_markup=driver_idle_reply_keyboard(),
            )
            return
        pings_count = LocationPing.objects.filter(order=target_order, driver=driver).count()
        checkpoints_count = TelegramMessageLog.objects.filter(order=target_order, event="driver_checkpoint").count()
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            f"📊 Buyurtma #{target_order.pk}\nHolat: {target_order.get_status_display()}\nCheckpoint: {checkpoints_count}\nLokatsiya: {pings_count} ta",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    if command not in {"/start_trip", "/finish_trip"}:
        send_chat_message(
            chat_id,
            UNKNOWN_COMMAND_DRIVER,
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
            ),
        )
        return
    target_order = _resolve_driver_order(driver, parts[1] if len(parts) > 1 else None)
    if not target_order:
        send_chat_message(
            chat_id,
            "Faol buyurtma topilmadi.",
            reply_markup=driver_idle_reply_keyboard(),
        )
        return
    if command == "/start_trip":
        # Ideal oqim: avval yukni qabul qilinadi (/yuklandi), shundan keyin safar boshlanadi (/start_trip).
        active_in_transit = (
            Assignment.objects.select_related("order")
            .filter(driver=driver, order__status=OrderStatus.IN_TRANSIT)
            .order_by("-assigned_at")
            .first()
        )
        if active_in_transit and active_in_transit.order_id != target_order.pk:
            send_chat_message(
                chat_id,
                f"✅ Siz hozir yo‘ldasiz (buyurtma #{active_in_transit.order.pk}).\n"
                "Boshqa buyurtmani bosib bo‘lmaydi — avval tugating.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    active_in_transit.order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        if target_order.loaded_quantity is None:
            state, _ = DriverOnboardingState.objects.get_or_create(
                telegram_user_id=int(telegram_user_id),
                defaults={"driver": driver},
            )
            state.driver = driver
            state.is_active = True
            state.step = "await_loaded_quantity"
            state.payload = {"order_id": target_order.pk}
            state.save(update_fields=["driver", "is_active", "step", "payload", "updated_at"])
            send_chat_message(
                chat_id,
                "Yuk qancha yuklandi? Faqat qiymat yuboring.\n"
                "Masalan: <code>12000 kg</code> yoki <code>10.5 tonna</code>.\n"
                "Yuborganingizdan keyin safar avtomatik boshlanadi.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        changed = transition_order(target_order, OrderStatus.IN_TRANSIT, changed_by=driver.full_name)
        if not changed:
            send_chat_message(
                chat_id,
                f"❌ Safarni boshlab bo‘lmadi. Holat: {html.escape(target_order.get_status_display())}",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            build_start_trip_driver_message_html(
                target_order, for_telegram_user_id=int(telegram_user_id)
            ),
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
            disable_web_page_preview=True,
        )
        send_order_native_map_pins(chat_id, target_order)
    else:
        # Ideal oqim: avval topshirilgan hajm kiriydi (/topshirildi), keyin tugatish so‘rovi yuboriladi.
        active_in_transit = (
            Assignment.objects.select_related("order")
            .filter(driver=driver, order__status=OrderStatus.IN_TRANSIT)
            .order_by("-assigned_at")
            .first()
        )
        if active_in_transit and active_in_transit.order_id != target_order.pk:
            send_chat_message(
                chat_id,
                f"❌ Hozir tugatish kerak bo‘lgan reys: #{active_in_transit.order.pk}.\n"
                "Boshqa buyurtmani tugatmang — avval tugating.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    active_in_transit.order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        if target_order.status != OrderStatus.IN_TRANSIT:
            send_chat_message(
                chat_id,
                "Avval safarni boshlang: <code>/start_trip</code>.\nKeyin <code>/topshirildi ...</code> kiriting va faqat shundan keyin tugating.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        if target_order.delivered_quantity is None:
            state, _ = DriverOnboardingState.objects.get_or_create(
                telegram_user_id=int(telegram_user_id),
                defaults={"driver": driver},
            )
            state.driver = driver
            state.is_active = True
            state.step = "await_delivered_quantity"
            state.payload = {"order_id": target_order.pk}
            state.save(update_fields=["driver", "is_active", "step", "payload", "updated_at"])
            send_chat_message(
                chat_id,
                "Klientga qancha topshirildi? Faqat qiymat yuboring.\n"
                "Masalan: <code>10000 kg</code> yoki <code>12000 litr 0.84</code>.\n"
                "Yuborganingizdan keyin tugallash so‘rovi avtomatik yuboriladi.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        # Litr uchun zichlik bo‘lmasa tonnaga hisob bo‘lmaydi; shuni ham tekshiramiz.
        if (
            target_order.delivered_quantity_uom == QuantityUnit.LITER
            and target_order.delivered_quantity_metric_ton is None
        ):
            send_chat_message(
                chat_id,
                "Litr uchun <b>zichlik kg/L</b> kerak.\n"
                "Siz <code>/topshirildi ... litr</code> yuborgansiz, lekin zichlik topilmadi.\n"
                "Yechim: <code>/topshirildi ... litr 0.84</code> yoki avval <code>/zichlik 0.84</code> kiriting.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return
        # Tugatish darhol COMPLETED bo'lmaydi. Admin tasdiqlagandan keyin yakunlanadi.
        changed = False
        TelegramMessageLog.objects.create(
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            event="driver_finish_requested",
            payload={"driver_id": driver.pk},
        )
        send_ops_notification("finish_requested", order=target_order, driver=driver)
        target_order.refresh_from_db()
        send_chat_message(
            chat_id,
            f"📝 <b>Tugallash so‘rovi yuborildi</b>\n"
            f"Buyurtma #{target_order.pk} admin tasdiqlagach yakunlanadi.",
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
    TelegramMessageLog.objects.create(
        order=target_order,
        chat_id=chat_id,
        message_id=str(message.get("message_id", "")),
        event="driver_command",
        payload={
            "command": command,
            "driver_id": driver.pk,
            "changed": changed,
        },
    )


def _handle_message(message: dict, signature: str = "", source_ip: str = "") -> None:
    user = message.get("from") or {}
    telegram_user_id = int(user.get("id", 0) or 0)
    location = message.get("location")
    if not telegram_user_id and location:
        telegram_user_id = _telegram_user_id_for_location_message(message)
    if not telegram_user_id:
        return
    if location:
        _save_location(telegram_user_id, location, message)
        return
    if _handle_driver_contact_message(message):
        return
    if _handle_driver_onboarding_message(message):
        return
    if message.get("text"):
        _handle_text_message(message)


def _handle_text_message(message: dict) -> None:
    _handle_driver_command(message)


def _resolve_driver_order(driver: Driver, order_id: str | None) -> Order | None:
    queryset = Assignment.objects.select_related("order").filter(
        driver=driver,
        order__status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT],
    )
    if order_id and order_id.isdigit():
        assignment = queryset.filter(order_id=int(order_id)).first()
    else:
        # Ideal: agar haydovchida IN_TRANSIT reys bo‘lsa — uni ustun qo‘yamiz.
        # Aks holda (faqat ASSIGNED bo‘lsa) — oxirgi assign bo‘lganni qaytaramiz.
        assignment = queryset.filter(order__status=OrderStatus.IN_TRANSIT).order_by("-assigned_at").first()
        if not assignment:
            assignment = queryset.order_by("-assigned_at").first()
    if not assignment:
        return None
    return assignment.order


def _handle_driver_start(chat_id: str, telegram_user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        # Agar haydovchi allaqachon ulangan bo‘lsa, telefon so‘ramaymiz.
        connected_driver = Driver.objects.filter(telegram_user_id=telegram_user_id).first()
        if connected_driver:
            docs_ok = not _driver_has_expired_documents(connected_driver)
            should_be_available = (
                connected_driver.verification_status == DriverVerificationStatus.APPROVED and docs_ok
            )
            connected_driver.status = DriverStatus.AVAILABLE if should_be_available else DriverStatus.OFFLINE
            connected_driver.save(update_fields=["status", "updated_at"])

            if connected_driver.verification_status == DriverVerificationStatus.PENDING:
                send_chat_message(
                    chat_id,
                    "<b>⏳ Hujjatlar tekshiruvda</b>\nAdmin tasdiqlagandan keyin sizga xabar beriladi va "
                    "buyurtmalarni <b>Qabul</b> qila olasiz.",
                    parse_mode="HTML",
                )
                return
            if connected_driver.verification_status == DriverVerificationStatus.REJECTED:
                send_chat_message(
                    chat_id,
                    "<b>⛔ Hujjatlar rad etildi</b>\nAdmin tekshirgan sabab: "
                    f"{html.escape(connected_driver.verification_reason or '-')}.\n\n"
                    "Qayta topshirish uchun tugmani bosing.",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "🔄 Hujjatlarni qayta yuborish", "callback_data": "onb:reverify"}]
                        ]
                    },
                    parse_mode="HTML",
                )
                return
            if not docs_ok:
                _start_driver_onboarding(chat_id, connected_driver)
                return

            if connected_driver.verification_status != DriverVerificationStatus.APPROVED:
                send_chat_message(
                    chat_id,
                    "Holatingizni tekshirib bo‘lmadi. Administratorga murojaat qiling.",
                    parse_mode="HTML",
                )
                return

            # APPROVED + docs ok: aktiv reysga qaytamiz.
            active_link = _resolve_driver_order(connected_driver, None)
            if active_link:
                send_chat_message(
                    chat_id,
                    build_active_trip_focus_message_html(
                        active_link, for_telegram_user_id=telegram_user_id
                    ),
                    parse_mode="HTML",
                    reply_markup=driver_reply_keyboard_for_order(
                        active_link, telegram_user_id=telegram_user_id
                    ),
                    disable_web_page_preview=True,
                )
            else:
                send_chat_message(
                    chat_id,
                    "✅ Tasdiqlangansiz. Hozir faol reys yo‘q.",
                    parse_mode="HTML",
                    reply_markup=driver_idle_reply_keyboard(),
                )
            return

        send_chat_message(
            chat_id,
            "<b>📱 Botga ulanish</b>\n\nPastdagi tugmani bosing — telefon raqamingiz avtomatik yuboriladi.",
            reply_markup={
                "keyboard": [[{"text": "📱 Raqamni yuborish", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
            parse_mode="HTML",
        )
        return
    driver = _find_driver_by_phone(parts[1])
    if not driver:
        send_chat_message(chat_id, "Bu raqam bo'yicha shofyor topilmadi. Ofis (admin) ga murojaat qiling.")
        return
    driver.telegram_user_id = telegram_user_id
    docs_ok = not _driver_has_expired_documents(driver)
    should_be_available = driver.verification_status == DriverVerificationStatus.APPROVED and docs_ok
    driver.status = DriverStatus.AVAILABLE if should_be_available else DriverStatus.OFFLINE
    driver.save(update_fields=["telegram_user_id", "status", "updated_at"])
    if driver.verification_status == DriverVerificationStatus.REJECTED or not docs_ok:
        first_markup: dict = {"remove_keyboard": True}
    else:
        first_markup = driver_idle_reply_keyboard()
    send_chat_message(
        chat_id,
        f"✅ <b>Ulandi</b>\nHaydovchi: {html.escape(driver.full_name)}",
        reply_markup=first_markup,
        parse_mode="HTML",
    )
    if driver.verification_status == DriverVerificationStatus.PENDING:
        send_chat_message(
            chat_id,
            "<b>⏳ Hujjatlar tekshiruvda</b>\nAdmin tasdiqlagandan keyin sizga xabar beriladi va "
            "buyurtmalarni <b>Qabul</b> qila olasiz.",
            parse_mode="HTML",
        )
        return
    if driver.verification_status == DriverVerificationStatus.REJECTED:
        send_chat_message(
            chat_id,
            "<b>⛔ Hujjatlar rad etildi</b>\nAdmin tekshirgan sabab: "
            f"{html.escape(driver.verification_reason or '-')}.\n\n"
            "Qayta topshirish uchun tugmani bosing.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Hujjatlarni qayta yuborish", "callback_data": "onb:reverify"}]
                ]
            },
            parse_mode="HTML",
        )
        return
    if not docs_ok:
        _start_driver_onboarding(chat_id, driver)
        return

    # Approved + docs OK:
    send_chat_message(
        chat_id,
        "<b>✅ Tasdiqlangansiz</b>\nEndi buyurtmalar kelganda guruhda <b>Qabul</b> qilishingiz mumkin.\n"
        "<i>Pastdagi tugmalar:</i> yordam, tezkor menyu, reys hisoboti.",
        parse_mode="HTML",
    )
    active_link = _resolve_driver_order(driver, None)
    if active_link:
        send_chat_message(
            chat_id,
            build_active_trip_focus_message_html(
                active_link, for_telegram_user_id=telegram_user_id
            ),
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                active_link, telegram_user_id=telegram_user_id
            ),
            disable_web_page_preview=True,
        )
        send_order_native_map_pins(chat_id, active_link)


def _start_driver_onboarding(chat_id: str, driver: Driver) -> None:
    state, _ = DriverOnboardingState.objects.get_or_create(
        telegram_user_id=int(driver.telegram_user_id or 0),
        defaults={"driver": driver},
    )
    state.driver = driver
    state.is_active = True
    state.step = "onb_license_photo"
    state.payload = {}
    state.save(update_fields=["driver", "is_active", "step", "payload", "updated_at"])
    send_chat_message(
        chat_id,
        _onb_first_block(
            1,
            "🪪",
            "Haydovchilik guvohnomasi",
            _ONB_PHOTO_HINT,
        ),
        parse_mode="HTML",
    )


def _handle_driver_onboarding_message(message: dict) -> bool:
    user = message.get("from") or {}
    telegram_user_id = int(user.get("id", 0) or 0)
    if not telegram_user_id:
        return False
    state = DriverOnboardingState.objects.filter(telegram_user_id=telegram_user_id, is_active=True).first()
    if not state:
        return False
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text", "")).strip()

    if state.step in {"await_loaded_quantity", "await_delivered_quantity"}:
        driver = state.driver or Driver.objects.filter(telegram_user_id=telegram_user_id).first()
        if not driver:
            state.is_active = False
            state.step = "idle"
            state.payload = {}
            state.save(update_fields=["is_active", "step", "payload", "updated_at"])
            send_chat_message(chat_id, REGISTER_FIRST, parse_mode="HTML")
            return True
        if text.lower() in {"/cancel", "bekor", "bekor qilish"}:
            state.is_active = False
            state.step = "idle"
            state.payload = {}
            state.save(update_fields=["is_active", "step", "payload", "updated_at"])
            send_chat_message(
                chat_id,
                "Bekor qilindi.",
                reply_markup=driver_reply_keyboard_for_order(
                    _resolve_driver_order(driver, None), telegram_user_id=int(telegram_user_id)
                ),
            )
            return True

        order_id = str((state.payload or {}).get("order_id") or "")
        target_order = _resolve_driver_order(driver, order_id if order_id.isdigit() else None)
        if not target_order:
            state.is_active = False
            state.step = "idle"
            state.payload = {}
            state.save(update_fields=["is_active", "step", "payload", "updated_at"])
            send_chat_message(chat_id, "Faol buyurtma topilmadi.", reply_markup=driver_idle_reply_keyboard())
            return True

        qty, uom, density, err = _parse_driver_hajm_free_text(text)
        if err:
            send_chat_message(
                chat_id,
                err,
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return True

        loaded = state.step == "await_loaded_quantity"
        ok, msg = _apply_driver_hajm(
            driver=driver,
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            loaded=loaded,
            quantity=qty,
            uom=uom,
            density=density,
        )
        target_order.refresh_from_db()
        if not ok:
            send_chat_message(
                chat_id,
                msg,
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return True

        # Step muvaffaqiyatli: sessionni yopamiz.
        state.is_active = False
        state.step = "idle"
        state.payload = {}
        state.save(update_fields=["is_active", "step", "payload", "updated_at"])

        if loaded:
            changed = transition_order(target_order, OrderStatus.IN_TRANSIT, changed_by=driver.full_name)
            if changed:
                target_order.refresh_from_db()
                send_chat_message(
                    chat_id,
                    build_start_trip_driver_message_html(
                        target_order, for_telegram_user_id=int(telegram_user_id)
                    ),
                    parse_mode="HTML",
                    reply_markup=driver_reply_keyboard_for_order(
                        target_order, telegram_user_id=int(telegram_user_id)
                    ),
                    disable_web_page_preview=True,
                )
                send_order_native_map_pins(chat_id, target_order)
                send_ops_notification("trip_started", order=target_order, driver=driver)
                TelegramMessageLog.objects.create(
                    order=target_order,
                    chat_id=chat_id,
                    message_id=str(message.get("message_id", "")),
                    event="driver_command",
                    payload={"command": "/start_trip(auto)", "driver_id": driver.pk, "changed": True},
                )
                return True
            send_chat_message(
                chat_id,
                f"{msg}\n\n❌ Safarni boshlab bo‘lmadi. Holat: {html.escape(target_order.get_status_display())}",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return True

        # delivered flow: tugallash so‘rovini avtomatik yuboramiz
        if (
            target_order.delivered_quantity_uom == QuantityUnit.LITER
            and target_order.delivered_quantity_metric_ton is None
        ):
            send_chat_message(
                chat_id,
                msg
                + "\n\nLitr uchun <b>zichlik kg/L</b> kerak. "
                "Masalan: <code>12000 litr 0.84</code>.",
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    target_order, telegram_user_id=int(telegram_user_id)
                ),
            )
            return True

        TelegramMessageLog.objects.create(
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            event="driver_finish_requested",
            payload={"driver_id": driver.pk},
        )
        send_ops_notification("finish_requested", order=target_order, driver=driver)
        send_chat_message(
            chat_id,
            f"{msg}\n\n📝 <b>Tugallash so‘rovi yuborildi</b>\n"
            f"Buyurtma #{target_order.pk} admin tasdiqlagach yakunlanadi.",
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                target_order, telegram_user_id=int(telegram_user_id)
            ),
        )
        TelegramMessageLog.objects.create(
            order=target_order,
            chat_id=chat_id,
            message_id=str(message.get("message_id", "")),
            event="driver_command",
            payload={"command": "/finish_trip(auto)", "driver_id": driver.pk, "changed": False},
        )
        return True

    if state.step == "add_vehicle_plate":
        if not text:
            send_chat_message(chat_id, "Davlat raqamini yuboring.")
            return True
        state.payload["vehicle_plate"] = text.upper().strip()
        state.step = "add_vehicle_capacity"
        state.save(update_fields=["payload", "step", "updated_at"])
        send_chat_message(
            chat_id,
            "Endi <b>sig‘im</b>ni yuboring (tonna).\nMasalan: <code>10</code> yoki <code>8.50</code>",
            parse_mode="HTML",
        )
        return True

    if state.step == "add_vehicle_capacity":
        if not text:
            send_chat_message(chat_id, "Sig‘imni yuboring (tonna). Masalan: <code>10</code>", parse_mode="HTML")
            return True
        raw = str(text).strip().replace(",", ".")
        try:
            cap = Decimal(raw)
        except Exception:
            send_chat_message(
                chat_id,
                "Sig‘im noto‘g‘ri. Masalan: <code>10</code> yoki <code>8.50</code>",
                parse_mode="HTML",
            )
            return True
        if cap <= 0:
            send_chat_message(
                chat_id,
                "Sig‘im musbat bo‘lsin. Masalan: <code>10</code>",
                parse_mode="HTML",
            )
            return True

        driver = state.driver
        plate_value = str((state.payload or {}).get("vehicle_plate", "")).strip().upper()
        if not driver or not plate_value:
            state.is_active = False
            state.step = "idle"
            state.payload = {}
            state.save(update_fields=["is_active", "step", "payload", "updated_at"])
            send_chat_message(chat_id, "Sessiya topilmadi. /add_vehicle ni qayta ishga tushiring.")
            return True

        cap = cap.quantize(Decimal("0.01"))

        other_vehicle = Vehicle.objects.filter(plate_number=plate_value).exclude(driver_id=driver.pk).first()
        if other_vehicle:
            send_chat_message(
                chat_id,
                f"Bu raqam (<code>{plate_value}</code>) boshqa haydovchida ishlatilmoqda. Boshqasini yuboring.",
                parse_mode="HTML",
            )
            state.is_active = False
            state.step = "idle"
            state.payload = {}
            state.save(update_fields=["is_active", "step", "payload", "updated_at"])
            return True

        vehicle = driver.vehicles.filter(plate_number=plate_value).first()
        if not vehicle:
            vehicle = driver.vehicles.create(
                plate_number=plate_value,
                vehicle_type="Tanker",
                capacity_ton=cap,
            )
        else:
            vehicle.capacity_ton = cap
            vehicle.vehicle_type = "Tanker"
            vehicle.save(update_fields=["capacity_ton", "vehicle_type", "updated_at"])

        state.is_active = False
        state.step = "idle"
        state.payload = {}
        state.save(update_fields=["is_active", "step", "payload", "updated_at"])

        send_chat_message(
            chat_id,
            f"✅ Mashina saqlandi: <code>{plate_value}</code> — <b>{cap}</b> tonna.",
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                _resolve_driver_order(driver, None),
                telegram_user_id=int(telegram_user_id),
            ),
        )
        return True

    if state.step == "onb_license_photo":
        file_id = _message_best_photo_file_id(message)
        if not file_id:
            send_chat_message(chat_id, "Iltimos, haydovchilik guvohnomasi <b>rasm</b>ini yuboring.", parse_mode="HTML")
            return True
        state.payload["license_photo_file_id"] = file_id
        state.step = "onb_texpasport_photo"
        state.save(update_fields=["payload", "step", "updated_at"])
        send_chat_message(
            chat_id,
            _onb_first_block(2, "📄", "Texnika pasporti (texpasport)", _ONB_PHOTO_HINT),
            parse_mode="HTML",
        )
        return True

    if state.step == "onb_texpasport_photo":
        file_id = _message_best_photo_file_id(message)
        if not file_id:
            send_chat_message(chat_id, "Iltimos, texpasport <b>rasm</b>ini yuboring.", parse_mode="HTML")
            return True
        state.payload["texpasport_photo_file_id"] = file_id
        state.step = "onb_vehicle_front"
        state.save(update_fields=["payload", "step", "updated_at"])
        send_chat_message(
            chat_id,
            _onb_first_block(3, "🚗", "Mashina (old tomondan)", _ONB_PHOTO_HINT),
            parse_mode="HTML",
        )
        return True

    if state.step == "onb_vehicle_front":
        file_id = _message_best_photo_file_id(message)
        if not file_id:
            send_chat_message(chat_id, "Iltimos, mashina oldi <b>rasm</b>ini yuboring.", parse_mode="HTML")
            return True
        state.payload["vehicle_front_photo_file_id"] = file_id
        state.step = "onb_vehicle_rear"
        state.save(update_fields=["payload", "step", "updated_at"])
        send_chat_message(
            chat_id,
            _onb_first_block(4, "🚗", "Mashina (orqa tomondan)", _ONB_PHOTO_HINT),
            parse_mode="HTML",
        )
        return True

    if state.step == "onb_vehicle_rear":
        file_id = _message_best_photo_file_id(message)
        if not file_id:
            send_chat_message(chat_id, "Iltimos, mashina orqasi <b>rasm</b>ini yuboring.", parse_mode="HTML")
            return True
        state.payload["vehicle_rear_photo_file_id"] = file_id
        state.step = "onb_capacity_kg"
        state.save(update_fields=["payload", "step", "updated_at"])
        send_chat_message(
            chat_id,
            _onb_first_block(
                5,
                "⚖️",
                "Mashina umumiy sig‘imi",
                "Mashina qancha mahsulotni sig‘dirishini <b>kg</b>da yozing.\nMasalan: <code>12000</code> yoki <code>12000 kg</code>.",
            ),
            parse_mode="HTML",
        )
        return True

    if state.step == "onb_capacity_kg":
        kg, err = _parse_capacity_kg(text)
        if err or kg is None:
            send_chat_message(chat_id, err or "Noto‘g‘ri qiymat.", parse_mode="HTML")
            return True
        state.payload["capacity_kg"] = str(kg)
        user = message.get("from") or {}
        username = (user.get("username") or "").strip()
        with transaction.atomic():
            _apply_onboarding_data(state)
            _persist_driver_pending_verification(
                driver=state.driver,
                telegram_user_id=telegram_user_id,
                username=username,
                state=state,
            )
        send_chat_message(chat_id, _DRIVER_REGISTRATION_SAVED_HTML, parse_mode="HTML")
        return True

    return True


def _handle_onboarding_callback(callback_query: dict, callback_data: str) -> None:
    callback_id = str(callback_query.get("id", ""))
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    user = callback_query.get("from") or {}
    username = (user.get("username") or "").strip()
    telegram_user_id = int(user.get("id", 0) or 0)
    if callback_data == "onb:reverify":
        driver = Driver.objects.filter(telegram_user_id=telegram_user_id).first()
        if not driver:
            _safe_answer(callback_id, "Driver topilmadi")
            return
        state, _ = DriverOnboardingState.objects.get_or_create(
            telegram_user_id=telegram_user_id,
            defaults={"driver": driver},
        )
        state.driver = driver
        state.is_active = True
        state.step = "onb_license_photo"
        state.payload = {}
        state.save(update_fields=["driver", "is_active", "step", "payload", "updated_at"])
        if driver.status != DriverStatus.OFFLINE:
            driver.status = DriverStatus.OFFLINE
            driver.save(update_fields=["status", "updated_at"])
        send_chat_message(
            chat_id,
            _onb_first_block(1, "🪪", "Haydovchilik guvohnomasi", _ONB_PHOTO_HINT),
            parse_mode="HTML",
        )
        _safe_answer(callback_id, "Qayta topshirish boshlandi")
        return

    state = DriverOnboardingState.objects.filter(telegram_user_id=telegram_user_id, is_active=True).first()
    if not state:
        _safe_answer(callback_id, "Onboarding topilmadi")
        return
    if callback_data == "onb:add_truck":
        state.is_active = False
        state.step = "idle"
        state.save(update_fields=["is_active", "step", "updated_at"])
        send_chat_message(
            chat_id,
            "Qo'shimcha mashina uchun <code>/add_vehicle</code> buyrug'idan foydalaning.",
            parse_mode="HTML",
        )
        _safe_answer(callback_id, "OK")
        return
    if callback_data == "onb:finish":
        driver = state.driver
        _finalize_driver_registration_submission(
            driver=driver,
            telegram_user_id=telegram_user_id,
            username=username,
            chat_id=chat_id,
            state=state,
        )
        _safe_answer(callback_id, "Tugatildi")
        return
    _safe_answer(callback_id, "Noma'lum amal")


def _synthetic_vehicle_plate(driver: Driver) -> str:
    uid = int(driver.telegram_user_id or 0)
    base = f"TG{uid}"
    if len(base) > 20:
        base = "TG" + str(uid)[-17:]
    if not Vehicle.objects.filter(plate_number=base).exclude(driver_id=driver.pk).exists():
        return base
    return f"TG{uid}-{driver.pk}"[:20]


_DRIVER_REGISTRATION_SAVED_HTML = (
    "<b>✅ Hujjatlaringiz saqlandi</b>\n\n"
    "Admin ularni ko'rib chiqadi. "
    "Tasdiqlangandan keyin sizga xabar beriladi va keyin guruhdagi buyurtmalarni <b>Qabul</b> qilishingiz mumkin.\n\n"
    "<i>Qo‘shimcha mashina kerak bo‘lsa (ikkinchi va keyingi avto):</i> <code>/add_vehicle</code> "
    "— davlat raqami va sig‘im (tonna) so‘raladi."
)


def _persist_driver_pending_verification(
    *,
    driver: Driver | None,
    telegram_user_id: int,
    username: str,
    state: DriverOnboardingState,
) -> None:
    if not driver:
        return
    from drivers.models import DriverVerificationAudit, DriverVerificationAuditAction

    DriverVerificationAudit.objects.create(
        driver=driver,
        action=DriverVerificationAuditAction.SUBMITTED,
        actor_username="driver",
        actor_id=telegram_user_id,
        reason="",
        from_status=driver.verification_status,
        to_status=DriverVerificationStatus.PENDING,
        details={
            "license_expires_at": driver.license_expires_at.isoformat() if driver.license_expires_at else None,
            "vehicles_count": driver.vehicles.count(),
        },
    )
    driver.verification_status = DriverVerificationStatus.PENDING
    driver.verification_reason = ""
    driver.registration_submitted_at = django_timezone.now()
    driver.verification_updated_at = django_timezone.now()
    driver.verification_updated_by_username = username
    driver.status = DriverStatus.OFFLINE
    driver.save(
        update_fields=[
            "verification_status",
            "verification_reason",
            "registration_submitted_at",
            "verification_updated_at",
            "verification_updated_by_username",
            "status",
        ]
    )
    cache.delete("ops_dashboard_v1")
    state.is_active = False
    state.step = "done"
    state.save(update_fields=["is_active", "step", "updated_at"])


def _finalize_driver_registration_submission(
    *,
    driver: Driver | None,
    telegram_user_id: int,
    username: str,
    chat_id: str,
    state: DriverOnboardingState,
) -> None:
    _persist_driver_pending_verification(
        driver=driver,
        telegram_user_id=telegram_user_id,
        username=username,
        state=state,
    )
    send_chat_message(
        chat_id,
        _DRIVER_REGISTRATION_SAVED_HTML,
        parse_mode="HTML",
    )


def _apply_onboarding_data(state: DriverOnboardingState) -> None:
    driver = state.driver
    if not driver:
        return
    payload = state.payload or {}
    kg_raw = str(payload.get("capacity_kg", "")).strip().replace(",", ".")
    try:
        kg = Decimal(kg_raw)
    except Exception:
        kg = Decimal("0")
    capacity_ton = (kg / Decimal("1000")).quantize(Decimal("0.01"))
    if capacity_ton <= 0:
        capacity_ton = Decimal("0.01")

    driver.license_photo_file_id = str(payload.get("license_photo_file_id", "")).strip()
    driver.registration_photo_file_id = ""
    driver.license_number = ""
    driver.license_issued_at = None
    driver.license_expires_at = None
    driver.save(
        update_fields=[
            "license_photo_file_id",
            "registration_photo_file_id",
            "license_number",
            "license_issued_at",
            "license_expires_at",
            "updated_at",
        ]
    )

    driver.vehicles.all().delete()
    plate_value = _synthetic_vehicle_plate(driver)
    Vehicle.objects.create(
        driver=driver,
        plate_number=plate_value,
        vehicle_type="Tanker",
        capacity_ton=capacity_ton,
        registration_document_number="",
        registration_photo_file_id=str(payload.get("texpasport_photo_file_id", "")).strip(),
        front_photo_file_id=str(payload.get("vehicle_front_photo_file_id", "")).strip(),
        rear_photo_file_id=str(payload.get("vehicle_rear_photo_file_id", "")).strip(),
        calibration_expires_at=None,
        tanker_document_photo_file_id="",
    )


def _pick_photo_file_id(photo_items: list[dict]) -> str:
    if not photo_items:
        return ""
    best = photo_items[-1]
    return str(best.get("file_id", ""))


def _message_best_photo_file_id(message: dict) -> str:
    photos = message.get("photo") or []
    if photos:
        return _pick_photo_file_id(photos)
    doc = message.get("document") or {}
    mime = str(doc.get("mime_type", ""))
    if mime.startswith("image/"):
        return str(doc.get("file_id", ""))
    return ""


def _driver_has_expired_documents(driver: Driver) -> bool:
    today = django_timezone.localdate()
    # Admin tasdiqlagan bo'lishi kerak, shuning uchun bu yerda faqat "muddat o'tgan" holatlarini tekshiramiz.
    if driver.license_expires_at and driver.license_expires_at < today:
        return True
    vehicles = list(driver.vehicles.all())
    if not vehicles:
        return True
    for vehicle in vehicles:
        if vehicle.calibration_expires_at and vehicle.calibration_expires_at < today:
            return True
    return False


def _driver_expired_documents_issues(driver: Driver) -> list[str]:
    """
    Human-readable issues used both for driver message and admin AlertEvent.
    """
    today = django_timezone.localdate()
    issues: list[str] = []

    if driver.license_expires_at and driver.license_expires_at < today:
        issues.append(f"Guvohnoma tugagan ({driver.license_expires_at})")

    vehicles = list(driver.vehicles.all())
    if not vehicles:
        issues.append("Mashina(lar) yo'q")

    for vehicle in vehicles:
        if vehicle.calibration_expires_at and vehicle.calibration_expires_at < today:
            issues.append(f"{vehicle.plate_number}: kalibrovka tugagan ({vehicle.calibration_expires_at})")

    return issues


def _normalize_phone(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return ""

    # Uzbekistan typical formats:
    # - +9989XXXXXXXX (E.164)
    # - 9989XXXXXXXX (without '+')
    # - 9XXXXXXXX (national without country)
    # - 8XXXXXXXXX (national with leading 8)
    if digits.startswith("998"):
        return "+" + digits
    if len(digits) == 9:
        if digits.startswith("9"):
            return "+998" + digits
        if digits.startswith("8"):
            return "+998" + digits[1:]
        return "+998" + digits
    if digits.startswith("8") and len(digits) >= 10:
        return "+998" + digits[1:]
    return "+" + digits


def _phone_candidates(raw: str) -> set[str]:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return set()
    last9 = digits[-9:]
    normalized_e164 = _normalize_phone(raw)

    candidates = {normalized_e164, digits}
    if digits.startswith("998"):
        candidates.add(digits)
        candidates.add("+" + digits)
    # Try national forms (last 9 digits)
    if len(last9) == 9:
        if last9.startswith("9"):
            candidates.add("+998" + last9)
            candidates.add("998" + last9)
        if last9.startswith("8"):
            candidates.add("+998" + last9[1:])
            candidates.add("998" + last9[1:])
        candidates.add(last9)
    return {c for c in candidates if c}


def _find_driver_by_phone(raw: str) -> Driver | None:
    candidates = list(_phone_candidates(raw))
    if not candidates:
        return None
    return Driver.objects.filter(phone__in=candidates).first()


def _handle_driver_contact_message(message: dict) -> bool:
    contact = message.get("contact") or {}
    user = message.get("from") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    telegram_user_id = int(user.get("id", 0) or 0)
    if not contact or not telegram_user_id:
        return False
    contact_user_id = int(contact.get("user_id", 0) or 0)
    if contact_user_id and contact_user_id != telegram_user_id:
        send_chat_message(chat_id, "Faqat o'zingizning raqamingizni yuboring.")
        return True
    driver = _find_driver_by_phone(str(contact.get("phone_number", "")))
    if not driver:
        send_chat_message(chat_id, "Bu raqam bo'yicha shofyor topilmadi. Ofis (admin) ga murojaat qiling.")
        return True
    driver.telegram_user_id = telegram_user_id
    docs_ok = not _driver_has_expired_documents(driver)
    should_be_available = driver.verification_status == DriverVerificationStatus.APPROVED and docs_ok
    driver.status = DriverStatus.AVAILABLE if should_be_available else DriverStatus.OFFLINE
    driver.save(update_fields=["telegram_user_id", "status", "updated_at"])
    if driver.verification_status == DriverVerificationStatus.REJECTED or not docs_ok:
        contact_first_markup: dict = {"remove_keyboard": True}
    else:
        contact_first_markup = driver_idle_reply_keyboard()
    send_chat_message(
        chat_id,
        f"✅ <b>Ulandi</b>\nHaydovchi: {html.escape(driver.full_name)}",
        reply_markup=contact_first_markup,
        parse_mode="HTML",
    )
    if driver.verification_status == DriverVerificationStatus.PENDING:
        send_chat_message(
            chat_id,
            "<b>⏳ Hujjatlar tekshiruvda</b>\nAdmin tasdiqlagandan keyin sizga xabar beriladi.",
            parse_mode="HTML",
        )
        return True
    if driver.verification_status == DriverVerificationStatus.REJECTED:
        send_chat_message(
            chat_id,
            "<b>⛔ Hujjatlar rad etildi</b>\nAdmin tekshirgan sabab: "
            f"{html.escape(driver.verification_reason or '-')}.\n\n"
            "Qayta topshirish uchun tugmani bosing.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Hujjatlarni qayta yuborish", "callback_data": "onb:reverify"}]
                ]
            },
            parse_mode="HTML",
        )
        return True
    if not docs_ok:
        _start_driver_onboarding(chat_id, driver)
        return True
    send_chat_message(
        chat_id,
        "<b>✅ Tasdiqlangansiz</b>\nEndi buyurtmalar kelganda guruhda <b>Qabul</b> qilasiz.\n"
        "<i>Pastdagi tugmalar:</i> yordam, tezkor menyu, reys hisoboti.",
        parse_mode="HTML",
    )
    active_c = _resolve_driver_order(driver, None)
    if active_c:
        send_chat_message(
            chat_id,
            build_active_trip_focus_message_html(
                active_c, for_telegram_user_id=telegram_user_id
            ),
            parse_mode="HTML",
            reply_markup=driver_reply_keyboard_for_order(
                active_c, telegram_user_id=telegram_user_id
            ),
            disable_web_page_preview=True,
        )
        send_order_native_map_pins(chat_id, active_c)
    return True


def _create_critical_confirmation(action: str, order: Order, actor_id: int, payload: dict | None = None) -> str:
    token = secrets.token_urlsafe(16)
    CriticalActionConfirmation.objects.create(
        token=token,
        action=action,
        order=order,
        actor_id=actor_id,
        payload=payload or {},
        expires_at=django_timezone.now() + timedelta(minutes=10),
    )
    return token


def _build_driver_wizard_text(order_id: int, step: int, message: str, status_text: str) -> str:
    step = max(1, min(4, step))
    return (
        f"🚚 <b>Buyurtma #{order_id}</b>\n"
        f"📍 Qadam: <b>{step}/4</b>\n"
        f"📌 Holat: {html.escape(str(status_text))}\n\n"
        f"{html.escape(str(message))}"
    )


def _acquire_callback_lock(user_id: int, callback_data: str, seconds: int = 3) -> bool:
    if not user_id or not callback_data:
        return True
    key = f"bot:cb-lock:{user_id}:{callback_data}"
    return cache.add(key, "1", timeout=seconds)


def _extract_coords_text(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_uzbekistan_bbox(lat: float, lon: float) -> bool:
    return 37.0 <= lat <= 46.0 and 55.0 <= lon <= 74.0


def _repair_order_decimals(order_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders_order
            SET weight_ton = CAST(weight_ton AS REAL),
                route_deviation_threshold_km = CAST(route_deviation_threshold_km AS REAL),
                price_suggested = CAST(price_suggested AS REAL),
                price_final = CAST(price_final AS REAL),
                client_price = CAST(client_price AS REAL),
                driver_fee = CAST(driver_fee AS REAL),
                fuel_cost = CAST(fuel_cost AS REAL),
                extra_cost = CAST(extra_cost AS REAL),
                penalty_amount = CAST(penalty_amount AS REAL)
            WHERE id = %s
            """,
            [order_id],
        )
        cursor.execute(
            """
            UPDATE pricing_pricequote
            SET distance_km = CAST(distance_km AS REAL),
                base_rate = CAST(base_rate AS REAL),
                weight_ton = CAST(weight_ton AS REAL),
                empty_return_km = CAST(empty_return_km AS REAL),
                peak_coef = CAST(peak_coef AS REAL),
                distance_cost = CAST(distance_cost AS REAL),
                weight_cost = CAST(weight_cost AS REAL),
                wait_cost = CAST(wait_cost AS REAL),
                empty_return_cost = CAST(empty_return_cost AS REAL),
                cargo_coef = CAST(cargo_coef AS REAL),
                suggested_price = CAST(suggested_price AS REAL),
                final_price = CAST(final_price AS REAL)
            WHERE order_id = %s
            """,
            [order_id],
        )


def _apply_confirmation(token: str, actor_id: int, actor_name: str) -> str:
    conf = CriticalActionConfirmation.objects.filter(token=token).select_related("order").first()
    if not conf:
        return "Tasdiqlash topilmadi."
    if conf.actor_id != actor_id:
        return "Bu tasdiqlash sizga tegishli emas."
    if conf.used_at:
        return "Bu tasdiqlash allaqachon ishlatilgan."
    if conf.is_expired:
        return "Tasdiqlash muddati tugagan."
    order = conf.order
    action = conf.action
    if action == "cancel":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            changed = bool(locked_order) and transition_order(locked_order, OrderStatus.CANCELED, changed_by=actor_name)
            order = locked_order or order
        if not changed:
            return "Cancel bajarilmadi."
        result = f"Order #{order.pk} bekor qilindi."
    elif action == "reprice":
        amount_raw = str(conf.payload.get("amount", "0"))
        try:
            amount = float(amount_raw)
        except ValueError:
            return "Reprice amount xato."
        order.price_final = amount
        order.client_price = amount
        order.save(update_fields=["price_final", "client_price", "updated_at"])
        result = f"Order #{order.pk} qayta narxlandi: {amount}"
    elif action == "refund":
        amount_raw = str(conf.payload.get("amount", "0"))
        result = f"Refund request qabul qilindi: order #{order.pk}, amount={amount_raw}"
    else:
        return "Noma'lum critical action."
    conf.used_at = django_timezone.now()
    conf.save(update_fields=["used_at"])
    TelegramMessageLog.objects.create(
        order=order,
        chat_id=str(actor_id),
        message_id="",
        event="critical_confirmation_applied",
        dedupe_key=f"critical:applied:{conf.token}",
        payload={"action": action, "result": result},
    )
    return result
