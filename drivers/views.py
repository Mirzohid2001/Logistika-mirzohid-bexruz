import json
from datetime import datetime, timedelta
from urllib import request as urlrequest
from urllib.error import URLError

from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from analytics.models import DriverPerformanceSnapshot
from common.permissions import WEB_OPERATION_GROUPS, WEB_PANEL_GROUPS, groups_required
from dispatch.models import Assignment
from orders.models import OrderStatus
from tracking.models import LocationPing

from .forms import DriverForm, VehicleForm
from .models import (
    Driver,
    DriverDeliveryReview,
    DriverStatus,
    DriverVerificationAudit,
    DriverVerificationAuditAction,
    DriverVerificationStatus,
    Vehicle,
)
from .services import get_driver_review_aggregates


def _driver_doc_score(driver: Driver) -> dict:
    """
    Document completeness score for admin control.
    License: 4 items, Vehicle: 4 items per vehicle.
    """
    today = timezone.localdate()
    near_days = int(getattr(settings, "DRIVER_DOC_EXPIRY_NEAR_DAYS", 30) or 30)
    near_until = today + timedelta(days=near_days)

    # License requirements (4 items)
    license_items = [
        bool(driver.license_number),
        driver.license_issued_at is not None,
        driver.license_expires_at is not None,
        bool(driver.license_photo_file_id),
    ]
    license_present = sum(1 for x in license_items if x)
    license_total = 4

    vehicles = list(driver.vehicles.all())
    if not vehicles:
        vehicle_total = 4
        vehicle_present = 0
    else:
        vehicle_total = 0
        vehicle_present = 0
        for v in vehicles:
            vehicle_items = [
                bool(v.registration_document_number),
                bool(v.registration_photo_file_id),
                v.calibration_expires_at is not None,
                bool(v.tanker_document_photo_file_id),
            ]
            vehicle_total += 4
            vehicle_present += sum(1 for x in vehicle_items if x)

    total = license_total + vehicle_total
    present = license_present + vehicle_present
    score = int(round(100 * present / total)) if total else 0

    expired = False
    if driver.license_expires_at and driver.license_expires_at < today:
        expired = True
    for v in vehicles:
        if v.calibration_expires_at and v.calibration_expires_at < today:
            expired = True

    near = False
    if not expired:
        if driver.license_expires_at and driver.license_expires_at <= near_until:
            near = True
        for v in vehicles:
            if v.calibration_expires_at and v.calibration_expires_at <= near_until:
                near = True

    expiry_state = "expired" if expired else ("near" if near else "ok")

    missing_summary = f"Guvohnoma: {license_present}/{license_total}; Mashina: {vehicle_present}/{vehicle_total}"

    return {
        "score": score,
        "expiry_state": expiry_state,
        "missing_summary": missing_summary,
    }


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def telegram_file_preview(request, file_id: str):
    """
    Telegram `file_id` asosida rasmni server orqali proxy qilib beradi.
    Shunda admin panelda `<img>` ishlaydi va token browserga chiqmaydi.
    """
    if not file_id:
        raise Http404("file_id bo'sh")

    cache_key = f"tg_file_path:{file_id}"
    file_path = cache.get(cache_key)
    if not file_path:
        api_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        try:
            with urlrequest.urlopen(api_url, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (URLError, ValueError, UnicodeDecodeError):
            raise Http404("Telegram getFile xato")
        if not payload.get("ok"):
            raise Http404("Telegram file topilmadi")
        file_path = (payload.get("result") or {}).get("file_path")
        if not file_path:
            raise Http404("file_path topilmadi")
        cache.set(cache_key, file_path, timeout=3600)

    bytes_cache_key = f"tg_file_bytes:{file_id}"
    cached = cache.get(bytes_cache_key)
    if cached and isinstance(cached, dict) and "data" in cached and "content_type" in cached:
        return HttpResponse(cached["data"], content_type=cached["content_type"])

    file_url = f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        with urlrequest.urlopen(file_url, timeout=30) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type") or "application/octet-stream"
    except (URLError, ValueError, UnicodeDecodeError):
        raise Http404("Telegram file yuklash xato")

    # Kichik fayllar uchun qisqa cache (thumbnaillar tez-tez ochiladi).
    if data and len(data) <= 2_000_000:
        cache.set(bytes_cache_key, {"data": data, "content_type": content_type}, timeout=300)

    return HttpResponse(data, content_type=content_type)


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def driver_list(request):
    v_status = str(request.GET.get("verification_status", "all")).strip()
    doc_expiry = str(request.GET.get("expiry", "all")).strip()

    drivers = list(Driver.objects.prefetch_related("vehicles").all())
    driver_ids = [d.id for d in drivers]
    latest_snapshots = {}
    for row in DriverPerformanceSnapshot.objects.order_by("driver_id", "-period_year", "-period_month"):
        if row.driver_id not in latest_snapshots:
            latest_snapshots[row.driver_id] = row
    latest_locations = {}
    for ping in LocationPing.objects.order_by("driver_id", "-captured_at"):
        if ping.driver_id not in latest_locations:
            latest_locations[ping.driver_id] = ping

    # So'nggi baho va izohlar (har bir haydovchi uchun bitta eng so'nggi sharh)
    latest_reviews: dict[int, DriverDeliveryReview] = {}
    if driver_ids:
        for review in (
            DriverDeliveryReview.objects.filter(driver_id__in=driver_ids)
            .order_by("driver_id", "-created_at")
            .only("driver_id", "stars", "comment", "created_at")
        ):
            if review.driver_id not in latest_reviews:
                latest_reviews[review.driver_id] = review

    rows = []
    for driver in drivers:
        if v_status != "all" and driver.verification_status != v_status:
            continue
        snapshot = latest_snapshots.get(driver.id)
        location = latest_locations.get(driver.id)
        doc = _driver_doc_score(driver)
        if doc_expiry != "all" and doc["expiry_state"] != doc_expiry:
            continue
        review_count, review_avg = get_driver_review_aggregates(driver)
        last_review = latest_reviews.get(driver.id)
        rows.append(
            {
                "driver": driver,
                "snapshot": snapshot,
                "location": location,
                "vehicles": list(driver.vehicles.all()),
                "active_assignments": Assignment.objects.filter(
                    driver=driver, order__status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT]
                ).count(),
                "doc_score": doc["score"],
                "doc_expiry_state": doc["expiry_state"],
                "doc_missing_summary": doc["missing_summary"],
                "review_count": review_count,
                "review_avg": review_avg,
                "last_review": last_review,
            }
        )
    rows.sort(
        key=lambda item: (
            0
            if item["driver"].verification_status == DriverVerificationStatus.PENDING
            else (1 if item["driver"].verification_status == DriverVerificationStatus.REJECTED else 2),
            -(float(item["snapshot"].rating_score) if item["snapshot"] else 0.0),
            item["driver"].full_name,
        )
    )
    near_days = int(getattr(settings, "DRIVER_DOC_EXPIRY_NEAR_DAYS", 30) or 30)
    return render(
        request,
        "drivers/list.html",
        {"rows": rows, "driver_doc_expiry_near_days": near_days},
    )


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_create(request):
    if request.method == "POST":
        form = DriverForm(request.POST)
        if form.is_valid():
            driver = form.save()
            return redirect("driver-detail", driver_id=driver.id)
    else:
        form = DriverForm(initial={"status": DriverStatus.AVAILABLE})
    return render(request, "drivers/form.html", {"form": form, "mode": "create"})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_edit(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        form = DriverForm(request.POST, instance=driver)
        if form.is_valid():
            form.save()
            return redirect("driver-detail", driver_id=driver.id)
    else:
        form = DriverForm(instance=driver)
    return render(request, "drivers/form.html", {"form": form, "mode": "edit", "driver": driver})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_archive(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        driver.status = DriverStatus.OFFLINE
        driver.save(update_fields=["status", "updated_at"])
    return redirect("driver-list")


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_restore(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        driver.status = DriverStatus.AVAILABLE
        driver.save(update_fields=["status", "updated_at"])
    return redirect("driver-list")


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def driver_detail(request, driver_id: int):
    driver = get_object_or_404(Driver.objects.prefetch_related("vehicles"), pk=driver_id)
    snapshots = list(driver.performance_snapshots.order_by("-period_year", "-period_month")[:12])
    snapshots.reverse()
    trend = {
        "labels": [f"{row.period_year}-{row.period_month:02d}" for row in snapshots],
        "rating": [float(row.rating_score) for row in snapshots],
        "deliveries": [int(row.completed_count) for row in snapshots],
        "on_time": [float(row.on_time_rate) for row in snapshots],
    }
    top_routes = (
        driver.assignments.filter(order__isnull=False)
        .values("order__from_location", "order__to_location")
        .annotate(total=Count("id"))
        .order_by("-total", "order__from_location", "order__to_location")[:5]
    )
    issue_reasons = (
        driver.assignments.filter(order__status__in=[OrderStatus.ISSUE, OrderStatus.CANCELED])
        .values("order__comment")
        .annotate(total=Count("id"))
        .order_by("-total")[:5]
    )
    recent_locations = LocationPing.objects.filter(driver=driver).order_by("-captured_at")[:20]
    audits = list(driver.verification_audits.all()[:10])
    return render(
        request,
        "drivers/detail.html",
        {
            "driver": driver,
            "verification_audits": audits,
            "is_verification_pending": driver.verification_status == DriverVerificationStatus.PENDING,
            "trend_json": json.dumps(trend),
            "top_routes": top_routes,
            "issue_reasons": issue_reasons,
            "recent_locations": recent_locations,
            "year": datetime.now().year,
        },
    )


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_verify_approve(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        from_status = driver.verification_status
        driver.verification_status = DriverVerificationStatus.APPROVED
        driver.verification_reason = ""
        driver.verification_updated_at = timezone.now()
        driver.verification_updated_by_username = getattr(request.user, "username", "") or str(getattr(request.user, "id", ""))
        DriverVerificationAudit.objects.create(
            driver=driver,
            action=DriverVerificationAuditAction.APPROVED,
            actor_username=getattr(request.user, "username", "") or "",
            actor_id=getattr(request.user, "id", None),
            reason="",
            from_status=from_status,
            to_status=DriverVerificationStatus.APPROVED,
            details={
                "license_expires_at": driver.license_expires_at.isoformat() if driver.license_expires_at else None,
                "license_photo_file_id": driver.license_photo_file_id or "",
                "vehicles": [
                    {
                        "plate_number": v.plate_number,
                        "calibration_expires_at": v.calibration_expires_at.isoformat() if v.calibration_expires_at else None,
                        "tanker_document_photo_file_id": v.tanker_document_photo_file_id or "",
                    }
                    for v in driver.vehicles.all()
                ],
            },
        )
        driver.status = DriverStatus.AVAILABLE if driver.telegram_user_id else DriverStatus.OFFLINE
        driver.save(
            update_fields=[
                "verification_status",
                "verification_reason",
                "verification_updated_at",
                "verification_updated_by_username",
                "status",
            ]
        )
        if driver.telegram_user_id:
            from bot.services import send_chat_message

            send_chat_message(
                str(driver.telegram_user_id),
                "<b>✅ Hujjatlaringiz tasdiqlandi</b>\n\nEndi buyurtmalar kelganda guruhdagi <b>Qabul</b> tugmasini bosishingiz mumkin.",
                parse_mode="HTML",
            )
        cache.delete("ops_dashboard_v1")
    if request.method == "POST" and request.POST.get("next") == "list":
        return redirect("driver-list")
    return redirect("driver-detail", driver_id=driver.id)


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def driver_verify_reject(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        from_status = driver.verification_status
        reason = str(request.POST.get("reason", "")).strip()[:255]
        driver.verification_status = DriverVerificationStatus.REJECTED
        driver.verification_reason = reason
        driver.verification_updated_at = timezone.now()
        driver.verification_updated_by_username = getattr(request.user, "username", "") or str(getattr(request.user, "id", ""))
        DriverVerificationAudit.objects.create(
            driver=driver,
            action=DriverVerificationAuditAction.REJECTED,
            actor_username=getattr(request.user, "username", "") or "",
            actor_id=getattr(request.user, "id", None),
            reason=reason,
            from_status=from_status,
            to_status=DriverVerificationStatus.REJECTED,
            details={
                "license_expires_at": driver.license_expires_at.isoformat() if driver.license_expires_at else None,
                "vehicles": [
                    {
                        "plate_number": v.plate_number,
                        "calibration_expires_at": v.calibration_expires_at.isoformat() if v.calibration_expires_at else None,
                    }
                    for v in driver.vehicles.all()
                ],
            },
        )
        driver.status = DriverStatus.OFFLINE
        driver.save(
            update_fields=[
                "verification_status",
                "verification_reason",
                "verification_updated_at",
                "verification_updated_by_username",
                "status",
            ]
        )
        if driver.telegram_user_id:
            from bot.services import send_chat_message

            send_chat_message(
                str(driver.telegram_user_id),
                "<b>❌ Hujjatlaringiz rad etildi</b>\n\nAdmin sabab: "
                f"{reason or '—'}\n"
                "Iltimos hujjatlarni yangilang, keyin yana qayta yuboring.\n\n"
                "Qayta topshirish uchun tugmani bosing:",
                parse_mode="HTML",
                reply_markup={
                    "inline_keyboard": [[{"text": "🔄 Hujjatlarni qayta yuborish", "callback_data": "onb:reverify"}]]
                },
            )
        cache.delete("ops_dashboard_v1")
    if request.method == "POST" and request.POST.get("next") == "list":
        return redirect("driver-list")
    return redirect("driver-detail", driver_id=driver.id)


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def vehicle_create(request, driver_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == "POST":
        form = VehicleForm(request.POST)
        if form.is_valid():
            vehicle = form.save(commit=False)
            vehicle.driver = driver
            vehicle.save()
            return redirect("driver-detail", driver_id=driver.id)
    else:
        form = VehicleForm()
    return render(request, "drivers/vehicle_form.html", {"form": form, "driver": driver, "mode": "create"})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def vehicle_edit(request, driver_id: int, vehicle_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    vehicle = get_object_or_404(Vehicle, pk=vehicle_id, driver=driver)
    if request.method == "POST":
        form = VehicleForm(request.POST, instance=vehicle)
        if form.is_valid():
            form.save()
            return redirect("driver-detail", driver_id=driver.id)
    else:
        form = VehicleForm(instance=vehicle)
    return render(request, "drivers/vehicle_form.html", {"form": form, "driver": driver, "vehicle": vehicle, "mode": "edit"})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def vehicle_delete(request, driver_id: int, vehicle_id: int):
    driver = get_object_or_404(Driver, pk=driver_id)
    vehicle = get_object_or_404(Vehicle, pk=vehicle_id, driver=driver)
    if request.method == "POST":
        vehicle.delete()
    return redirect("driver-detail", driver_id=driver.id)
