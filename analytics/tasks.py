from datetime import datetime, timedelta
from math import atan2, cos, radians, sin, sqrt

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db.models import Q
from django.utils import timezone

from analytics.models import AlertEvent, AlertType
from bot.models import TelegramMessageLog
from analytics.services import rebuild_monthly_reports
from bot.services import send_chat_message
from dispatch.models import Assignment
from drivers.models import Driver
from orders.models import Order, OrderStatus
from tracking.models import LocationPing


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return r * c


def _point_in_polygon(lat: float, lon: float, polygon: list[dict]) -> bool:
    if len(polygon) < 3:
        return False
    x = lon
    y = lat
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi = float(polygon[i]["lon"])
        yi = float(polygon[i]["lat"])
        xj = float(polygon[j]["lon"])
        yj = float(polygon[j]["lat"])
        intersects = ((yi > y) != (yj > y)) and (x < ((xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi))
        if intersects:
            inside = not inside
        j = i
    return inside


def _min_distance_to_polyline_km(lat: float, lon: float, polyline: list[dict]) -> float:
    if len(polyline) < 2:
        return 10**9
    min_km = 10**9
    for point in polyline:
        km = _distance_km(lat, lon, float(point["lat"]), float(point["lon"]))
        if km < min_km:
            min_km = km
    return min_km


@shared_task
def rebuild_monthly_reports_task(year: int, month: int) -> None:
    rebuild_monthly_reports(year, month)
    cache.delete("ops_dashboard_v1")


@shared_task
def nightly_reconcile_task() -> None:
    call_command("reconcile_finance")


@shared_task
def monthly_report_scheduler_task() -> None:
    now = timezone.now()
    target = now.replace(day=1) - timedelta(days=1)
    rebuild_monthly_reports(target.year, target.month)


@shared_task
def check_sla_escalations_task() -> int:
    thresholds = list(getattr(settings, "SLA_ESCALATION_THRESHOLDS_MINUTES", [15, 30, 60]) or [15, 30, 60])
    now = timezone.now()
    created_count = 0
    active_orders = Order.objects.filter(status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT]).select_related("client")
    for order in active_orders:
        base = order.sla_deadline_at
        if not base:
            if not order.client:
                continue
            base = order.actual_start_at or order.pickup_time
            base = base + timedelta(minutes=order.client.sla_minutes)
        overdue = int((now - base).total_seconds() // 60)
        if overdue <= 0:
            # SLA allaqachon yo'qolgan - unresolved escalation alertlarni yopamiz.
            AlertEvent.objects.filter(order=order, alert_type=AlertType.SLA_ESCALATION, resolved=False).update(
                resolved=True
            )
            continue
        driver = Assignment.objects.filter(order=order).select_related("driver").first()
        for threshold in thresholds:
            if overdue < threshold:
                AlertEvent.objects.filter(
                    order=order,
                    alert_type=AlertType.SLA_ESCALATION,
                    threshold_minutes=threshold,
                    resolved=False,
                ).update(resolved=True)
                continue

            alert, created = AlertEvent.objects.get_or_create(
                order=order,
                alert_type=AlertType.SLA_ESCALATION,
                threshold_minutes=threshold,
                defaults={
                    "driver": driver.driver if driver else None,
                    "message": f"Order #{order.pk} SLA {threshold} daqiqadan oshdi",
                    "resolved": False,
                },
            )
            updated_fields: list[str] = []
            new_driver = driver.driver if driver else None
            if alert.driver_id != (new_driver.id if new_driver else None):
                alert.driver = new_driver
                updated_fields.append("driver")
            new_message = f"Order #{order.pk} SLA {threshold} daqiqadan oshdi"
            if alert.message != new_message:
                alert.message = new_message
                updated_fields.append("message")
            if alert.resolved:
                alert.resolved = False
                updated_fields.append("resolved")
            if updated_fields:
                alert.save(update_fields=updated_fields)

            if created:
                created_count += 1
                send_chat_message(
                    str(settings.TELEGRAM_GROUP_ID),
                    f"SLA alert: order #{order.pk} {threshold}+ min overdue",
                )
    return created_count


@shared_task
def detect_route_deviation_task(order_id: int, driver_id: int, lat: float, lon: float) -> bool:
    order = Order.objects.filter(pk=order_id).first()
    assignment = Assignment.objects.filter(order_id=order_id, driver_id=driver_id).select_related("driver").first()
    if not order:
        return False
    threshold_km = float(order.route_deviation_threshold_km or settings.ROUTE_DEVIATION_DEFAULT_THRESHOLD_KM)
    deviation = False
    message = ""
    if order.route_polyline:
        min_km = _min_distance_to_polyline_km(lat, lon, order.route_polyline)
        if min_km > threshold_km:
            deviation = True
            message = f"Order #{order.pk} route deviation: {min_km:.1f} km (threshold {threshold_km} km)"
    if order.geofence_polygon and not _point_in_polygon(lat, lon, order.geofence_polygon):
        deviation = True
        if message:
            message += "; "
        message += f"Order #{order.pk} geofence outside detected"
    if not deviation:
        # Shart bajarilmayapti: unresolved route deviation alertlarni yopamiz.
        AlertEvent.objects.filter(
            order_id=order_id,
            alert_type=AlertType.ROUTE_DEVIATION,
            threshold_minutes=0,
            resolved=False,
        ).update(resolved=True)
        return False
    alert, created = AlertEvent.objects.get_or_create(
        order=order,
        alert_type=AlertType.ROUTE_DEVIATION,
        threshold_minutes=0,
        defaults={
            "driver": assignment.driver if assignment else None,
            "message": message,
            "resolved": False,
        },
    )
    new_driver = assignment.driver if assignment else None
    fields_to_update: list[str] = []
    if not created and new_driver and alert.driver_id != new_driver.id:
        alert.driver = new_driver
        fields_to_update.append("driver")
    if message and alert.message != message:
        alert.message = message
        fields_to_update.append("message")
    if not alert.resolved:
        # deviation yana tasdiqlandi
        pass
    else:
        alert.resolved = False
        fields_to_update.append("resolved")

    if fields_to_update:
        alert.save(update_fields=fields_to_update)
    return True


@shared_task
def detect_location_fraud_task(order_id: int, driver_id: int) -> int:
    pings = list(
        LocationPing.objects.filter(order_id=order_id, driver_id=driver_id).order_by("-captured_at")[:8]
    )
    if len(pings) < 2:
        return 0
    pings.reverse()
    created = 0
    for prev, curr in zip(pings, pings[1:]):
        seconds = max(1, int((curr.captured_at - prev.captured_at).total_seconds()))
        km = _distance_km(float(prev.latitude), float(prev.longitude), float(curr.latitude), float(curr.longitude))
        speed_kmh = km / (seconds / 3600)
        if speed_kmh > settings.IMPOSSIBLE_SPEED_KMH:
            _, was_created = AlertEvent.objects.get_or_create(
                order_id=order_id,
                alert_type=AlertType.IMPOSSIBLE_SPEED,
                threshold_minutes=0,
                defaults={
                    "driver_id": driver_id,
                    "message": f"Order #{order_id} impossible speed detected: {speed_kmh:.1f} km/h",
                },
            )
            created += int(was_created)
            break
    same_point_count = 0
    idle_km = float(getattr(settings, "LOCATION_FRAUD_IDLE_DISTANCE_KM", 0.03) or 0.03)
    idle_count_need = int(getattr(settings, "LOCATION_FRAUD_IDLE_SAME_POINT_COUNT", 5) or 5)
    idle_alert_minutes = int(getattr(settings, "LOCATION_FRAUD_IDLE_ALERT_THRESHOLD_MINUTES", 60) or 60)
    for prev, curr in zip(pings, pings[1:]):
        km = _distance_km(float(prev.latitude), float(prev.longitude), float(curr.latitude), float(curr.longitude))
        if km < idle_km:
            same_point_count += 1
    if same_point_count >= idle_count_need:
        _, was_created = AlertEvent.objects.get_or_create(
            order_id=order_id,
            alert_type=AlertType.IDLE_ANOMALY,
            threshold_minutes=idle_alert_minutes,
            defaults={
                "driver_id": driver_id,
                "message": f"Order #{order_id} long idle anomaly detected",
            },
        )
        created += int(was_created)
    return created


@shared_task
def notify_driver_document_expiry_task() -> int:
    today = timezone.localdate()
    near_days = int(getattr(settings, "DRIVER_DOC_EXPIRY_NEAR_DAYS", 30) or 30)
    near_date = today + timedelta(days=near_days)
    notified = 0
    drivers = Driver.objects.prefetch_related("vehicles").all()
    for driver in drivers:
        if not driver.telegram_user_id:
            continue
        issues_near: list[str] = []
        issues_expired_admin: list[str] = []
        issues_for_driver: list[str] = []

        # Driver license
        if not driver.license_expires_at:
            # Driver ogohlantirish uchun, lekin admin "expired" alertiga kiritmaymiz.
            issues_for_driver.append("guvohnoma muddati kiritilmagan")
        elif driver.license_expires_at < today:
            issues_for_driver.append("guvohnoma muddati tugagan")
            issues_expired_admin.append("guvohnoma muddati tugagan")
        elif driver.license_expires_at <= near_date:
            issues_for_driver.append(f"guvohnoma {driver.license_expires_at} da tugaydi")
            issues_near.append(f"guvohnoma {driver.license_expires_at} da tugaydi")

        for vehicle in driver.vehicles.all():
            if not vehicle.calibration_expires_at:
                msg = f"{vehicle.plate_number}: kalibrovka muddati kiritilmagan"
                issues_for_driver.append(msg)
            elif vehicle.calibration_expires_at < today:
                msg = f"{vehicle.plate_number}: kalibrovka muddati tugagan"
                issues_for_driver.append(msg)
                issues_expired_admin.append(msg)
            elif vehicle.calibration_expires_at <= near_date:
                msg = f"{vehicle.plate_number}: kalibrovka {vehicle.calibration_expires_at} da tugaydi"
                issues_for_driver.append(msg)
                issues_near.append(msg)

        if not issues_for_driver:
            # Driver endi muammo emas (expired/near yo'q) - alertni yopamiz.
            AlertEvent.objects.filter(
                order=None,
                driver=driver,
                alert_type=AlertType.DRIVER_DOC_EXPIRED,
                resolved=False,
            ).update(resolved=True)
            continue

        # Admin ops-dashboard uchun expired hujjatlar AlertEvent.
        # (order yo'q bo'lgani uchun order=None; alert_type stats dashboard’da ko'rinadi.)
        if issues_expired_admin:
            alert, _ = AlertEvent.objects.get_or_create(
                order=None,
                driver=driver,
                alert_type=AlertType.DRIVER_DOC_EXPIRED,
                threshold_minutes=0,
                defaults={
                    "message": "⚠️ Driver hujjat(lar)i expired: " + ", ".join(issues_expired_admin[:3]),
                    "resolved": False,
                },
            )
            new_message = "⚠️ Driver hujjat(lar)i expired: " + ", ".join(issues_expired_admin[:3])
            fields_to_update: list[str] = []
            if alert.message != new_message:
                alert.message = new_message
                fields_to_update.append("message")
            if alert.resolved:
                alert.resolved = False
                fields_to_update.append("resolved")
            if fields_to_update:
                alert.save(update_fields=fields_to_update)
            cache.delete("ops_dashboard_v1")
        else:
            # Yaqinlashgan ogohlantirish bo'lishi mumkin, lekin admin alert faqat "expired" bo'lsa ochiladi.
            AlertEvent.objects.filter(
                order=None,
                driver=driver,
                alert_type=AlertType.DRIVER_DOC_EXPIRED,
                threshold_minutes=0,
                resolved=False,
            ).update(resolved=True)

        send_chat_message(
            str(driver.telegram_user_id),
            "⚠️ Hujjat ogohlantirish:\n- "
            + "\n- ".join(issues_for_driver[:6])
            + "\nYangilang, aks holda yuk qabuli bloklanadi.",
        )
        notified += 1
    return notified


@shared_task
def check_live_track_required_task() -> int:
    """Ketdikdan keyin 2 daqiqada live ping bo'lmasa alert + haydovchiga eslatma."""
    wait_sec = int(getattr(settings, "LIVE_TRACK_REQUIRED_AFTER_KETDIK_SEC", 120) or 120)
    cooldown_sec = int(getattr(settings, "LIVE_TRACK_REMINDER_COOLDOWN_SEC", 600) or 600)
    now = timezone.now()
    cutoff = now - timedelta(seconds=wait_sec)

    rows = (
        TelegramMessageLog.objects.filter(event="driver_ketdik_webapp", created_at__lte=cutoff)
        .select_related("order")
        .order_by("-created_at")[:200]
    )
    created = 0
    changed_any = False
    for row in rows:
        order = row.order
        if not order or order.status != OrderStatus.IN_TRANSIT:
            continue
        driver_id = int((row.payload or {}).get("driver_id") or 0)
        if not driver_id:
            assignment = Assignment.objects.filter(order=order).select_related("driver").first()
            driver_id = assignment.driver_id if assignment else 0
        if not driver_id:
            continue
        driver = Driver.objects.filter(pk=driver_id).first()
        if not driver:
            continue

        has_live = LocationPing.objects.filter(
            order=order,
            driver=driver,
            captured_at__gte=row.created_at,
        ).exists()
        if has_live:
            resolved_count = AlertEvent.objects.filter(
                order=order, driver=driver, alert_type=AlertType.NO_LIVE_TRACK, resolved=False
            ).update(resolved=True)
            changed_any = changed_any or bool(resolved_count)
            continue

        alert, was_created = AlertEvent.objects.get_or_create(
            order=order,
            driver=driver,
            alert_type=AlertType.NO_LIVE_TRACK,
            threshold_minutes=0,
            defaults={
                "message": f"Order #{order.pk}: Ketdikdan keyin live location hali yoqilmagan.",
                "resolved": False,
            },
        )
        if not was_created and alert.resolved:
            alert.resolved = False
            alert.save(update_fields=["resolved"])
            changed_any = True
        created += int(was_created)
        changed_any = changed_any or bool(was_created)

        cache_key = f"live-track-reminder:{order.pk}:{driver.pk}"
        if not cache.get(cache_key):
            if driver.telegram_user_id:
                send_chat_message(
                    str(driver.telegram_user_id),
                    f"⚠️ Buyurtma #{order.pk}: Jonli joylashuv hali yoqilmagan.\n"
                    "📎 → Joylashuv → Jonli joylashuvni ulashish.",
                )
            cache.set(cache_key, "1", timeout=max(60, cooldown_sec))
    if changed_any:
        cache.delete("ops_dashboard_v1")
    return created
