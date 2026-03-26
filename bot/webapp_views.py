"""Telegram Web App: haydovchi uchun marshrut xaritasi (taksi uslubi, TG ichida)."""

from __future__ import annotations

import json
import uuid
from urllib import request as urlrequest
from urllib.error import URLError

from django.core.cache import cache
from django.core import signing
from django.db import IntegrityError
from django.conf import settings
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from analytics.models import AlertEvent, AlertType
from bot.models import TelegramMessageLog
from bot.services import TRIP_MAP_WEBAPP_SIGN_SALT, _extract_coords
from dispatch.models import Assignment
from drivers.models import Driver
from orders.models import Order, OrderStatus
from orders.services import transition_order
from tracking.models import LocationPing, LocationSource
from analytics.tasks import detect_location_fraud_task, detect_route_deviation_task


def _osrm_route(lon1: float, lat1: float, lon2: float, lat2: float) -> tuple[dict | None, dict]:
    """Avtomobil yo‘li: GeoJSON geometriya + masofa/vaqt/qadamlar (OSRM)."""
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson&steps=true"
    )
    meta: dict = {"distance_m": 0, "duration_s": 0, "steps": []}
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "ShofirBot/1.0"}, method="GET")
        with urlrequest.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except (URLError, json.JSONDecodeError, TimeoutError, ValueError, OSError):
        return None, meta
    if data.get("code") != "Ok" or not data.get("routes"):
        return None, meta
    route0 = data["routes"][0]
    geom = route0.get("geometry")
    meta["distance_m"] = float(route0.get("distance") or 0)
    meta["duration_s"] = float(route0.get("duration") or 0)
    steps_out: list[dict] = []
    for leg in route0.get("legs") or []:
        for step in leg.get("steps") or []:
            m = step.get("maneuver") or {}
            steps_out.append(
                {
                    "distance_m": float(step.get("distance") or 0),
                    "duration_s": float(step.get("duration") or 0),
                    "name": (step.get("name") or "").strip(),
                    "type": m.get("type"),
                    "modifier": m.get("modifier"),
                    "maneuver_lon": (
                        float(m.get("location")[0])
                        if isinstance(m.get("location"), list) and len(m.get("location")) >= 2
                        else None
                    ),
                    "maneuver_lat": (
                        float(m.get("location")[1])
                        if isinstance(m.get("location"), list) and len(m.get("location")) >= 2
                        else None
                    ),
                }
            )
    meta["steps"] = steps_out[:24]
    return (geom if isinstance(geom, dict) else None), meta


def _trip_webapp_resolve(order_id: int, token: str) -> tuple[Order, Driver] | None:
    try:
        payload = signing.loads(token, salt=TRIP_MAP_WEBAPP_SIGN_SALT, max_age=86400 * 7)
    except signing.BadSignature:
        return None
    if int(payload.get("o", 0)) != order_id:
        return None
    tg = int(payload.get("tg", 0))
    if not tg:
        return None
    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return None
    driver = Driver.objects.filter(telegram_user_id=tg).first()
    if not driver or not Assignment.objects.filter(order=order, driver=driver).exists():
        return None
    return order, driver


def _create_ketdik_alert(order: Order, driver: Driver, message: str) -> None:
    for _ in range(8):
        try:
            AlertEvent.objects.create(
                order=order,
                driver=driver,
                alert_type=AlertType.DRIVER_KETDIK_WEBAPP,
                threshold_minutes=abs(uuid.uuid4().int) % 2_147_483_647,
                message=message[:255],
            )
            return
        except IntegrityError:
            continue


@csrf_exempt
@require_POST
def trip_map_ketdik(request, order_id: int, token: str):
    """Haydovchi «Ketdik»: ASSIGNED bo‘lsa IN_TRANSIT; admin uchun AlertEvent."""
    resolved = _trip_webapp_resolve(order_id, token)
    if not resolved:
        return JsonResponse({"ok": False, "error": "Havola yoki biriktirish yaroqsiz."}, status=403)
    order, driver = resolved
    if order.status not in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT}:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Holat «{order.get_status_display()}» — «Ketdik» faqat biriktirilgan yoki yo‘lda reys uchun.",
            },
            status=400,
        )

    started_now = False
    if order.status == OrderStatus.ASSIGNED:
        started_now = transition_order(order, OrderStatus.IN_TRANSIT, changed_by=f"webapp:{driver.full_name}")
        order.refresh_from_db()

    if started_now:
        msg = (
            f"{driver.full_name} buyurtma #{order.pk} ni marshrut xaritasidan boshladi (yo‘lga chiqdi, IN_TRANSIT)."
        )
        out_msg = "Safar tizimda boshlandi. Admin panelda xabar yuborildi. Jonli joylashuvni ruxsat qiling."
    else:
        msg = f"{driver.full_name} buyurtma #{order.pk} bo‘yicha «Ketdik» bosdi (yo‘lda, mini-ilova)."
        out_msg = "Admin panelda xabar yuborildi. Jonli joylashuvni yoqing — xarita sizni kuzatadi."

    _create_ketdik_alert(order, driver, msg)
    TelegramMessageLog.objects.create(
        order=order,
        chat_id=str(driver.telegram_user_id or ""),
        message_id="",
        event="driver_ketdik_webapp",
        payload={"driver_id": driver.pk, "started_trip": started_now},
    )

    return JsonResponse(
        {
            "ok": True,
            "started_trip": started_now,
            "message": out_msg,
            "status": order.status,
        }
    )


@csrf_exempt
@require_POST
def trip_map_live_ping(request, order_id: int, token: str):
    resolved = _trip_webapp_resolve(order_id, token)
    if not resolved:
        return JsonResponse({"ok": False, "error": "Havola yoki biriktirish yaroqsiz."}, status=403)
    order, driver = resolved
    if order.status not in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT}:
        return JsonResponse({"ok": False, "error": "Buyurtma aktiv emas."}, status=400)
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Noto'g'ri koordinata."}, status=400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return JsonResponse({"ok": False, "error": "Koordinata diapazondan tashqarida."}, status=400)
    throttle_key = f"webapp:liveping:{order.pk}:{driver.pk}"
    if not cache.add(throttle_key, "1", timeout=5):
        return JsonResponse({"ok": True, "throttled": True})
    created = LocationPing.objects.create(
        order=order,
        driver=driver,
        latitude=lat,
        longitude=lon,
        source=LocationSource.WEB,
        captured_at=timezone.now(),
    )
    try:
        detect_route_deviation_task.delay(order.pk, driver.pk, float(created.latitude), float(created.longitude))
        detect_location_fraud_task.delay(order.pk, driver.pk)
    except Exception:
        pass
    return JsonResponse({"ok": True})


@require_GET
def trip_map_webapp(request, order_id: int, token: str):
    resolved = _trip_webapp_resolve(order_id, token)
    if not resolved:
        return HttpResponseForbidden("Havola eskirgan yoki noto‘g‘ri.")
    order, driver = resolved

    from_ll = _extract_coords(str(order.from_location))
    to_ll = _extract_coords(str(order.to_location))
    if not from_ll or not to_ll:
        return render(
            request,
            "bot/trip_map_webapp.html",
            {
                "order": order,
                "error": "Buyurtmada yo‘l nuqtalari koordinatada emas.",
            },
        )

    lat1, lon1 = float(from_ll[0]), float(from_ll[1])
    lat2, lon2 = float(to_ll[0]), float(to_ll[1])
    geometry, route_meta = _osrm_route(lon1, lat1, lon2, lat2)
    trip_viewport = {"from_lat": lat1, "from_lon": lon1, "to_lat": lat2, "to_lon": lon2}
    show_ketdik = order.status == OrderStatus.ASSIGNED
    can_live_track = order.status in {OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT}
    ketdik_url = ""
    live_ping_url = ""
    if can_live_track:
        # request.build_absolute_uri() proxylarda ba'zan http qaytaradi; WebApp uchun HTTPS majburiy.
        base = (getattr(settings, "TELEGRAM_WEBAPP_BASE_URL", "") or "").strip().rstrip("/")
        ketdik_path = reverse("telegram-trip-map-ketdik", kwargs={"order_id": order.pk, "token": token})
        ping_path = reverse("telegram-trip-map-live-ping", kwargs={"order_id": order.pk, "token": token})
        if show_ketdik:
            ketdik_url = (
                f"{base}{ketdik_path}" if base else request.build_absolute_uri(ketdik_path).replace("http://", "https://", 1)
            )
        live_ping_url = f"{base}{ping_path}" if base else request.build_absolute_uri(ping_path).replace("http://", "https://", 1)

    response = render(
        request,
        "bot/trip_map_webapp.html",
        {
            "order": order,
            "trip_viewport": trip_viewport,
            "route_geometry": geometry,
            "route_meta": route_meta,
            "show_ketdik": show_ketdik,
            "ketdik_url": ketdik_url,
            "live_ping_url": live_ping_url,
            "error": None,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response
