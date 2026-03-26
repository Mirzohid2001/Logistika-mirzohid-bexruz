from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings as dj_settings
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal
from decimal import InvalidOperation
import json
import re

from bot.services import driver_idle_reply_keyboard, send_chat_message, send_order_to_group
from common.permissions import WEB_OPERATION_GROUPS, WEB_PANEL_GROUPS, groups_required
from dispatch.models import Assignment, DriverOfferApproval, DriverOfferDecision, DriverOfferResponse
from bot.models import TelegramMessageLog
from dispatch.services import assign_order
from pricing.models import PriceQuote, TenderBid, TenderSession
from pricing.services import build_price_breakdown, evaluate_tender_bid, suggest_price

from .forms import ClientForm, OrderCreateForm, OrderCustodyForm
from .models import Client, Order, OrderStatus, QuantityUnit
from .quantity import quantity_to_metric_tonnes, shortage_tonnes
from .services import apply_client_contract, create_return_trip, reopen_order, split_shipment, transition_order
from tracking.models import LocationPing
from drivers.models import DriverStatus


def _shortage_penalty_points(shortage_kg: Decimal) -> int:
    value = Decimal(str(shortage_kg or 0))
    if value < Decimal("70"):
        return 0
    if value < Decimal("100"):
        return int(getattr(dj_settings, "SHORTAGE_PENALTY_POINTS_70_99", 2) or 2)
    if value < Decimal("200"):
        return int(getattr(dj_settings, "SHORTAGE_PENALTY_POINTS_100_199", 5) or 5)
    return int(getattr(dj_settings, "SHORTAGE_PENALTY_POINTS_200_PLUS", 10) or 10)


def _calculate_shortage_kg(order: Order) -> Decimal | None:
    lt = order.loaded_quantity_metric_ton
    dt = order.delivered_quantity_metric_ton
    if lt is None or dt is None:
        return None
    short_ton = lt - dt
    if short_ton <= 0:
        return Decimal("0")
    return (short_ton * Decimal("1000")).quantize(Decimal("0.001"))


def _custody_cells_for_list(
    loaded_q,
    loaded_uom: str | None,
    delivered_q,
    delivered_uom: str | None,
    density,
) -> tuple[str, str, str]:
    """Jadval uchun: yuklangan, topshirilgan, farq (t)."""
    unit_labels = dict(QuantityUnit.choices)
    l_cell, d_cell = "—", "—"
    if loaded_q is not None:
        try:
            lq = Decimal(str(loaded_q))
            u = loaded_uom or QuantityUnit.TON
            l_cell = f"{lq} {unit_labels.get(u, u)}"
        except Exception:
            l_cell = "—"
    if delivered_q is not None:
        try:
            dq = Decimal(str(delivered_q))
            u = delivered_uom or QuantityUnit.TON
            d_cell = f"{dq} {unit_labels.get(u, u)}"
        except Exception:
            d_cell = "—"
    s_cell = "—"
    try:
        lq = Decimal(str(loaded_q)) if loaded_q is not None else None
        dq = Decimal(str(delivered_q)) if delivered_q is not None else None
        dens_dec = Decimal(str(density)) if density is not None and str(density).strip() != "" else None
    except Exception:
        lq = dq = None
        dens_dec = None
    lu = loaded_uom or QuantityUnit.TON
    du = delivered_uom or QuantityUnit.TON
    lt = quantity_to_metric_tonnes(lq, lu, density_kg_per_liter=dens_dec) if lq is not None else None
    dt = quantity_to_metric_tonnes(dq, du, density_kg_per_liter=dens_dec) if dq is not None else None
    st = shortage_tonnes(lt, dt)
    if st is not None:
        if st > 0:
            s_cell = f"≈ {st} t"
        else:
            s_cell = "0"
    return l_cell, d_cell, s_cell


@staff_member_required
def order_list(request):
    try:
        qs = Order.objects.select_related("client").order_by("-created_at")
        status = (request.GET.get("status") or "").strip()
        if status in {c[0] for c in OrderStatus.choices}:
            qs = qs.filter(status=status)
        paginator = Paginator(qs, int(getattr(dj_settings, "ORDERS_LIST_PER_PAGE", 25) or 25))
        page_obj = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            "orders/list.html",
            {
                "orders": page_obj.object_list,
                "page_obj": page_obj,
                "safe_mode": False,
                "status_filter": status,
                "order_status_choices": OrderStatus.choices,
            },
        )
    except InvalidOperation:
        _repair_all_decimal_data()
        messages.warning(request, "Legacy decimal format topildi. Xavfsiz ko'rinish rejimi yoqildi.")
        return _render_order_list_safe(request)


@staff_member_required
def order_create(request):
    if request.method == "POST":
        form = OrderCreateForm(request.POST)
        if form.is_valid():
            order: Order = form.save(commit=False)
            # Operatorga qulaylik: faqat "Qayerdan/Qayerga" ni tanlagan bo'lsa,
            # marshrut + geofence-ni avtomatik generatsiya qilamiz.
            def _extract_coords(text: str) -> tuple[float, float] | None:
                if not text:
                    return None
                m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", str(text))
                if not m:
                    return None
                return float(m.group(1)), float(m.group(2))

            from_latlon = _extract_coords(order.from_location)
            to_latlon = _extract_coords(order.to_location)
            if from_latlon and to_latlon:
                from_lat, from_lon = from_latlon
                to_lat, to_lon = to_latlon

                if not order.route_polyline:
                    order.route_polyline = [{"lat": from_lat, "lon": from_lon}, {"lat": to_lat, "lon": to_lon}]

                if not order.geofence_polygon:
                    # Oddiy bbox geofence: operatorga tezkor qulaylik uchun.
                    # (Marshrut bo'yicha aniq poligon keyinroq kengaytiriladi.)
                    margin = 0.03
                    min_lat = min(from_lat, to_lat) - margin
                    max_lat = max(from_lat, to_lat) + margin
                    min_lon = min(from_lon, to_lon) - margin
                    max_lon = max(from_lon, to_lon) + margin
                    order.geofence_polygon = [
                        {"lat": min_lat, "lon": min_lon},
                        {"lat": min_lat, "lon": max_lon},
                        {"lat": max_lat, "lon": max_lon},
                        {"lat": max_lat, "lon": min_lon},
                    ]
            order.price_suggested = suggest_price(order.weight_ton)
            apply_client_contract(order, set_client_price=False)
            order.client_price = Decimal("0")
            order.save()
            breakdown = build_price_breakdown(
                distance_km=Decimal(str(order.weight_ton)) * Decimal("8"),
                weight_ton=Decimal(str(order.weight_ton)),
                wait_minutes=0,
                empty_return_km=Decimal(str(order.weight_ton)) * Decimal("2"),
            )
            PriceQuote.objects.create(
                order=order,
                distance_km=Decimal(str(order.weight_ton)) * Decimal("8"),
                base_rate=breakdown["base_rate"],
                weight_ton=Decimal(str(order.weight_ton)),
                wait_minutes=0,
                empty_return_km=Decimal(str(order.weight_ton)) * Decimal("2"),
                peak_coef=breakdown["peak_coef"],
                distance_cost=breakdown["distance_cost"],
                weight_cost=breakdown["weight_cost"],
                wait_cost=breakdown["wait_cost"],
                empty_return_cost=breakdown["empty_return_cost"],
                cargo_coef=breakdown["cargo_coef"],
                suggested_price=breakdown["suggested_price"],
                final_price=order.client_price,
                is_approved=True,
            )
            send_order_to_group(order)
            messages.success(request, f"Buyurtma #{order.pk} yaratildi.")
            return redirect("order-detail", pk=order.pk)
    else:
        form = OrderCreateForm()
    return render(request, "orders/create.html", {"form": form})


@staff_member_required
def client_pricing_preview(request):
    """
    Buyurtma yaratish sahifasida klient tanlanganda SLA (va ixtiyoriy og‘irlik) haqida ma’lumot.
    Klientdan tushum yo‘q modelda klient narxi hisoblanmaydi.
    """
    client_id_raw = str(request.GET.get("client_id") or "").strip()
    if not client_id_raw.isdigit():
        return JsonResponse({"ok": False, "error": "client_id bo'sh yoki noto'g'ri"}, status=400)

    client = get_object_or_404(Client, pk=int(client_id_raw), is_active=True)
    return JsonResponse(
        {
            "ok": True,
            "sla_minutes": client.sla_minutes,
        }
    )


@staff_member_required
def order_detail(request, pk: int):
    try:
        order = get_object_or_404(Order, pk=pk)
    except InvalidOperation:
        _repair_order_decimal_data(pk)
        try:
            order = get_object_or_404(Order, pk=pk)
            messages.warning(request, "Buyurtmadagi noto'g'ri raqam formatlari avtomatik tuzatildi.")
        except InvalidOperation:
            messages.error(
                request,
                "Buyurtma raqam maydonlarida jiddiy format xatosi bor. Avval ma'lumotlarni tozalang.",
            )
            return redirect("order-list")
    driver_responses = order.driver_responses.select_related("driver").all() if hasattr(order, "driver_responses") else []
    quote_preview = None
    try:
        quote = order.quotes.order_by("-created_at").first()
        if quote:
            quote_preview = {
                "distance_km": quote.distance_km,
                "weight_cost": quote.weight_cost,
                "wait_cost": quote.wait_cost,
                "empty_return_cost": quote.empty_return_cost,
                "peak_coef": quote.peak_coef,
                "suggested_price": quote.suggested_price,
            }
    except InvalidOperation:
        # Legacy/corrupted decimal values in sqlite can break model conversion.
        # Fallback to raw SQL text fetch so page remains available.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT distance_km, weight_cost, wait_cost, empty_return_cost, peak_coef, suggested_price
                FROM pricing_pricequote
                WHERE order_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [order.pk],
            )
            row = cursor.fetchone()
        if row:
            quote_preview = {
                "distance_km": row[0],
                "weight_cost": row[1],
                "wait_cost": row[2],
                "empty_return_cost": row[3],
                "peak_coef": row[4],
                "suggested_price": row[5],
            }
            messages.warning(
                request,
                "Tender narx ma'lumotida format muammosi topildi (legacy data). Tuzatish tavsiya etiladi.",
            )

    def _extract_coords(text: str) -> dict | None:
        if not text:
            return None
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", str(text))
        if not m:
            return None
        return {"lat": float(m.group(1)), "lon": float(m.group(2))}

    def _safe_points(points) -> list[dict]:
        out: list[dict] = []
        for p in points or []:
            if not isinstance(p, dict):
                continue
            if "lat" in p and "lon" in p:
                try:
                    out.append({"lat": float(p["lat"]), "lon": float(p["lon"])})
                except (TypeError, ValueError):
                    continue
        return out

    pickup_point = _extract_coords(order.from_location)
    dropoff_point = _extract_coords(order.to_location)

    latest_ping = (
        LocationPing.objects.filter(order=order)
        .select_related("driver")
        .order_by("-captured_at")
        .first()
    )
    live_driver_point = None
    if latest_ping and latest_ping.driver_id:
        live_driver_point = {
            "driver": latest_ping.driver.full_name,
            "lat": float(latest_ping.latitude),
            "lon": float(latest_ping.longitude),
            "captured_at": latest_ping.captured_at.isoformat(),
        }

    assignment = Assignment.objects.select_related("driver").filter(order=order).first()
    finish_request = (
        TelegramMessageLog.objects.filter(order=order, event="driver_finish_requested").order_by("-created_at").first()
    )
    finish_request_exists = bool(finish_request)

    t_dur_min = int(getattr(dj_settings, "TENDER_DURATION_MIN_MINUTES", 3) or 3)
    t_dur_max = int(getattr(dj_settings, "TENDER_DURATION_MAX_MINUTES", 10) or 10)
    t_dur_default = max(t_dur_min, min(5, t_dur_max))
    split_max = int(getattr(dj_settings, "SPLIT_SHIPMENT_MAX_PARTS", 10) or 10)

    custody_form = OrderCustodyForm(instance=order)

    return render(
        request,
        "orders/detail.html",
        {
            "order": order,
            "custody_form": custody_form,
            "quote_preview": quote_preview,
            "driver_responses": driver_responses,
            "pickup_point": pickup_point,
            "dropoff_point": dropoff_point,
            "live_driver_point": live_driver_point,
            "route_polyline_points": _safe_points(order.route_polyline),
            "geofence_polygon_points": _safe_points(order.geofence_polygon),
            "finish_request_exists": finish_request_exists,
            "assignment_driver": assignment.driver if assignment else None,
            "finish_request_created_at": finish_request.created_at if finish_request else None,
            "tender_duration_min": t_dur_min,
            "tender_duration_max": t_dur_max,
            "tender_duration_default": t_dur_default,
            "split_shipment_max_parts": split_max,
        },
    )


@staff_member_required
def order_live_location(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    max_pts = int(getattr(dj_settings, "ORDER_LIVE_TRAIL_MAX_POINTS", 400) or 400)
    max_pts = max(10, min(max_pts, 2000))
    pings = list(
        LocationPing.objects.filter(order=order)
        .order_by("-captured_at")
        .select_related("driver")[:max_pts]
    )
    if not pings:
        return JsonResponse({"ok": False})
    latest_ping = pings[0]
    trail_chrono = list(reversed(pings))
    trail = [
        {"lat": float(p.latitude), "lon": float(p.longitude), "captured_at": p.captured_at.isoformat()}
        for p in trail_chrono
    ]
    return JsonResponse(
        {
            "ok": True,
            "driver": latest_ping.driver.full_name,
            "lat": float(latest_ping.latitude),
            "lon": float(latest_ping.longitude),
            "captured_at": latest_ping.captured_at.isoformat(),
            "trail": trail,
        }
    )


@staff_member_required
def order_finish_confirm(request, pk: int):
    """
    Driver /finish_trip bosgandan keyin order darhol COMPLETED bo'lmaydi.
    Admin bu tugmani bosgandan keyin order yakunlanadi.
    """
    order = get_object_or_404(Order, pk=pk)

    if request.method != "POST":
        return redirect("order-detail", pk=order.pk)

    shortage_note = (request.POST.get("shortage_note") or "").strip()
    shortage_preview_kg = _calculate_shortage_kg(order)
    warning_kg = Decimal(str(getattr(dj_settings, "SHORTAGE_WARNING_KG", 70) or 70))
    if shortage_preview_kg is not None and shortage_preview_kg >= warning_kg and not shortage_note:
        messages.error(
            request,
            f"Kamomad {warning_kg} kg dan yuqori bo‘lsa izoh majburiy. Iltimos, «Izoh» kiriting.",
        )
        return redirect("order-detail", pk=order.pk)

    if order.status != OrderStatus.IN_TRANSIT:
        messages.warning(request, "Faqat IN_TRANSIT buyurtma yakunlanadi.")
        return redirect("order-detail", pk=order.pk)

    finish_request = (
        TelegramMessageLog.objects.filter(order=order, event="driver_finish_requested")
        .order_by("-created_at")
        .first()
    )
    if not finish_request:
        messages.warning(request, "Haydovchi tugallash so'rovini yubormagan.")
        return redirect("order-detail", pk=order.pk)

    assignment = Assignment.objects.select_related("driver").filter(order=order).first()
    driver = assignment.driver if assignment else None

    changed = False
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
        if not locked_order:
            return redirect("order-detail", pk=order.pk)

        # Re-check status under lock to avoid race conditions.
        if locked_order.status != OrderStatus.IN_TRANSIT:
            changed = False
        else:
            locked_assignment = Assignment.objects.select_for_update().select_related("driver").filter(order=locked_order).first()
            locked_driver = locked_assignment.driver if locked_assignment else None
            changed = transition_order(locked_order, OrderStatus.COMPLETED, changed_by=request.user.username)
            if changed and locked_driver:
                locked_driver.status = DriverStatus.AVAILABLE
                locked_driver.save(update_fields=["status", "updated_at"])
                driver = locked_driver

    if not changed:
        messages.warning(request, "Buyurtma yakunlanmadi (holat o'zgardi).")
        return redirect("order-detail", pk=order.pk)

    order.refresh_from_db()
    shortage_kg = _calculate_shortage_kg(order)
    if shortage_kg is not None:
        order.shortage_kg = shortage_kg
    if shortage_note:
        order.shortage_note = shortage_note
    penalty_points = 0
    if shortage_kg is not None:
        penalty_points = _shortage_penalty_points(shortage_kg)
    order.shortage_penalty_points = max(0, int(penalty_points))
    if shortage_kg is not None and shortage_kg >= warning_kg:
        order.shortage_flagged_at = timezone.now()
    order.save(
        update_fields=[
            "shortage_kg",
            "shortage_penalty_points",
            "shortage_note",
            "shortage_flagged_at",
            "updated_at",
        ]
    )

    if driver and penalty_points > 0:
        minimum = Decimal(str(getattr(dj_settings, "SHORTAGE_RATING_MIN", 0) or 0))
        current = Decimal(str(driver.rating_score or 0))
        next_rating = max(minimum, current - Decimal(str(penalty_points)))
        if next_rating != current:
            driver.rating_score = next_rating
            driver.save(update_fields=["rating_score", "updated_at"])

    if order.delivered_quantity is None:
        messages.warning(
            request,
            "Klientga topshirilgan hajm kiritilmagan — yo‘qotish nazorati uchun tavsiya etiladi.",
        )
    lt = order.loaded_quantity_metric_ton
    dt = order.delivered_quantity_metric_ton
    if lt is not None and dt is not None and lt > 0:
        short = lt - dt
        if short > 0:
            pct = (short / lt) * Decimal("100")
            if pct >= Decimal("0.5"):
                messages.warning(
                    request,
                    f"Diqqat: yuklangan va topshirilgan o‘rtasida ≈ {short} t farq "
                    f"({pct.quantize(Decimal('0.01'))}%). Tekshiruv tavsiya etiladi.",
                )

    if shortage_kg is not None and shortage_kg >= warning_kg:
        try:
            from analytics.models import AlertEvent, AlertType

            critical_kg = Decimal(str(getattr(dj_settings, "SHORTAGE_PENALTY_KG", 100) or 100))
            sev = "CRITICAL" if shortage_kg >= critical_kg else "WARNING"
            msg = (
                f"{sev}: Order #{order.pk} kamomad {shortage_kg} kg. "
                f"Izoh: {(order.shortage_note or '-').strip()[:140]}"
            )
            alert, created = AlertEvent.objects.get_or_create(
                order=order,
                alert_type=AlertType.FUEL_SHORTAGE,
                threshold_minutes=0,
                defaults={"driver": driver, "message": msg, "resolved": False},
            )
            if not created:
                fields: list[str] = []
                if driver and alert.driver_id != driver.id:
                    alert.driver = driver
                    fields.append("driver")
                if alert.message != msg:
                    alert.message = msg
                    fields.append("message")
                if alert.resolved:
                    alert.resolved = False
                    fields.append("resolved")
                if fields:
                    alert.save(update_fields=fields)
        except Exception:
            pass

    if shortage_kg is not None:
        messages.info(request, f"Kamomad auditi: {shortage_kg} kg.")
    if penalty_points > 0:
        messages.warning(
            request,
            f"Haydovchi reytingidan {penalty_points} ball tushirildi. Joriy rating: {driver.rating_score if driver else '-'}",
        )

    TelegramMessageLog.objects.create(
        order=order,
        chat_id=str(driver.telegram_user_id) if driver and driver.telegram_user_id else "",
        message_id="",
        event="driver_finish_confirmed",
        payload={"driver_id": driver.pk if driver else None, "confirmed_by": request.user.username},
    )

    if driver and driver.telegram_user_id:
        send_chat_message(
            str(driver.telegram_user_id),
            f"✅ Buyurtma #{order.pk} yakunlandi. Yangi takliflarni qabul qilishingiz mumkin.",
            reply_markup=driver_idle_reply_keyboard(),
        )

    messages.success(request, "Haydovchi tugallashi admin tomonidan tasdiqlandi.")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_custody_update(request, pk: int):
    """Yuklangan / topshirilgan hajm va zichlik (web)."""
    order = get_object_or_404(Order, pk=pk)
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    form = OrderCustodyForm(request.POST, instance=order)
    if not form.is_valid():
        messages.error(request, "Hajm formalari noto‘g‘ri to‘ldirilgan.")
        return redirect("order-detail", pk=pk)
    obj = form.save(commit=False)
    username = getattr(request.user, "username", None) or str(request.user.pk)
    now = timezone.now()
    if "loaded_quantity" in form.changed_data and obj.loaded_quantity is not None:
        obj.loaded_recorded_at = now
        obj.loaded_recorded_by = f"web:{username}"
    if "delivered_quantity" in form.changed_data and obj.delivered_quantity is not None:
        obj.delivered_recorded_at = now
        obj.delivered_recorded_by = f"web:{username}"
    obj.save()
    messages.success(request, "Hajm va zichlik ma'lumotlari saqlandi.")
    return redirect("order-detail", pk=pk)


def _repair_order_decimal_data(order_id: int) -> None:
    # SQLite legacy rows may keep invalid decimal text; CAST normalizes them.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders_order
            SET weight_ton = CASE
                    WHEN CAST(weight_ton AS REAL) > 999999.99 THEN 999999.99
                    WHEN CAST(weight_ton AS REAL) < 0 THEN 0
                    ELSE CAST(weight_ton AS REAL)
                END,
                route_deviation_threshold_km = CASE
                    WHEN CAST(route_deviation_threshold_km AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(route_deviation_threshold_km AS REAL) < 0 THEN 0
                    ELSE CAST(route_deviation_threshold_km AS REAL)
                END,
                price_suggested = CASE
                    WHEN CAST(price_suggested AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(price_suggested AS REAL) < 0 THEN 0
                    ELSE CAST(price_suggested AS REAL)
                END,
                price_final = CASE
                    WHEN CAST(price_final AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(price_final AS REAL) < 0 THEN 0
                    ELSE CAST(price_final AS REAL)
                END,
                client_price = CASE
                    WHEN CAST(client_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(client_price AS REAL) < 0 THEN 0
                    ELSE CAST(client_price AS REAL)
                END,
                driver_fee = CASE
                    WHEN CAST(driver_fee AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(driver_fee AS REAL) < 0 THEN 0
                    ELSE CAST(driver_fee AS REAL)
                END,
                fuel_cost = CASE
                    WHEN CAST(fuel_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(fuel_cost AS REAL) < 0 THEN 0
                    ELSE CAST(fuel_cost AS REAL)
                END,
                extra_cost = CASE
                    WHEN CAST(extra_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(extra_cost AS REAL) < 0 THEN 0
                    ELSE CAST(extra_cost AS REAL)
                END,
                penalty_amount = CASE
                    WHEN CAST(penalty_amount AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(penalty_amount AS REAL) < 0 THEN 0
                    ELSE CAST(penalty_amount AS REAL)
                END
            WHERE id = %s
            """,
            [order_id],
        )
        cursor.execute(
            """
            UPDATE pricing_pricequote
            SET distance_km = CASE
                    WHEN CAST(distance_km AS REAL) > 99999999.99 THEN 99999999.99
                    WHEN CAST(distance_km AS REAL) < 0 THEN 0
                    ELSE CAST(distance_km AS REAL)
                END,
                base_rate = CASE
                    WHEN CAST(base_rate AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(base_rate AS REAL) < 0 THEN 0
                    ELSE CAST(base_rate AS REAL)
                END,
                weight_ton = CASE
                    WHEN CAST(weight_ton AS REAL) > 999999.99 THEN 999999.99
                    WHEN CAST(weight_ton AS REAL) < 0 THEN 0
                    ELSE CAST(weight_ton AS REAL)
                END,
                empty_return_km = CASE
                    WHEN CAST(empty_return_km AS REAL) > 99999999.99 THEN 99999999.99
                    WHEN CAST(empty_return_km AS REAL) < 0 THEN 0
                    ELSE CAST(empty_return_km AS REAL)
                END,
                peak_coef = CASE
                    WHEN CAST(peak_coef AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(peak_coef AS REAL) < 0 THEN 0
                    ELSE CAST(peak_coef AS REAL)
                END,
                distance_cost = CASE
                    WHEN CAST(distance_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(distance_cost AS REAL) < 0 THEN 0
                    ELSE CAST(distance_cost AS REAL)
                END,
                weight_cost = CASE
                    WHEN CAST(weight_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(weight_cost AS REAL) < 0 THEN 0
                    ELSE CAST(weight_cost AS REAL)
                END,
                wait_cost = CASE
                    WHEN CAST(wait_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(wait_cost AS REAL) < 0 THEN 0
                    ELSE CAST(wait_cost AS REAL)
                END,
                empty_return_cost = CASE
                    WHEN CAST(empty_return_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(empty_return_cost AS REAL) < 0 THEN 0
                    ELSE CAST(empty_return_cost AS REAL)
                END,
                cargo_coef = CASE
                    WHEN CAST(cargo_coef AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(cargo_coef AS REAL) < 0 THEN 0
                    ELSE CAST(cargo_coef AS REAL)
                END,
                suggested_price = CASE
                    WHEN CAST(suggested_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(suggested_price AS REAL) < 0 THEN 0
                    ELSE CAST(suggested_price AS REAL)
                END,
                final_price = CASE
                    WHEN CAST(final_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(final_price AS REAL) < 0 THEN 0
                    ELSE CAST(final_price AS REAL)
                END
            WHERE order_id = %s
            """,
            [order_id],
        )


def _repair_all_decimal_data() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders_order
            SET weight_ton = CASE
                    WHEN CAST(weight_ton AS REAL) > 999999.99 THEN 999999.99
                    WHEN CAST(weight_ton AS REAL) < 0 THEN 0
                    ELSE CAST(weight_ton AS REAL)
                END,
                route_deviation_threshold_km = CASE
                    WHEN CAST(route_deviation_threshold_km AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(route_deviation_threshold_km AS REAL) < 0 THEN 0
                    ELSE CAST(route_deviation_threshold_km AS REAL)
                END,
                price_suggested = CASE
                    WHEN CAST(price_suggested AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(price_suggested AS REAL) < 0 THEN 0
                    ELSE CAST(price_suggested AS REAL)
                END,
                price_final = CASE
                    WHEN CAST(price_final AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(price_final AS REAL) < 0 THEN 0
                    ELSE CAST(price_final AS REAL)
                END,
                client_price = CASE
                    WHEN CAST(client_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(client_price AS REAL) < 0 THEN 0
                    ELSE CAST(client_price AS REAL)
                END,
                driver_fee = CASE
                    WHEN CAST(driver_fee AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(driver_fee AS REAL) < 0 THEN 0
                    ELSE CAST(driver_fee AS REAL)
                END,
                fuel_cost = CASE
                    WHEN CAST(fuel_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(fuel_cost AS REAL) < 0 THEN 0
                    ELSE CAST(fuel_cost AS REAL)
                END,
                extra_cost = CASE
                    WHEN CAST(extra_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(extra_cost AS REAL) < 0 THEN 0
                    ELSE CAST(extra_cost AS REAL)
                END,
                penalty_amount = CASE
                    WHEN CAST(penalty_amount AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(penalty_amount AS REAL) < 0 THEN 0
                    ELSE CAST(penalty_amount AS REAL)
                END
            """
        )
        cursor.execute(
            """
            UPDATE pricing_pricequote
            SET distance_km = CASE
                    WHEN CAST(distance_km AS REAL) > 99999999.99 THEN 99999999.99
                    WHEN CAST(distance_km AS REAL) < 0 THEN 0
                    ELSE CAST(distance_km AS REAL)
                END,
                base_rate = CASE
                    WHEN CAST(base_rate AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(base_rate AS REAL) < 0 THEN 0
                    ELSE CAST(base_rate AS REAL)
                END,
                weight_ton = CASE
                    WHEN CAST(weight_ton AS REAL) > 999999.99 THEN 999999.99
                    WHEN CAST(weight_ton AS REAL) < 0 THEN 0
                    ELSE CAST(weight_ton AS REAL)
                END,
                empty_return_km = CASE
                    WHEN CAST(empty_return_km AS REAL) > 99999999.99 THEN 99999999.99
                    WHEN CAST(empty_return_km AS REAL) < 0 THEN 0
                    ELSE CAST(empty_return_km AS REAL)
                END,
                peak_coef = CASE
                    WHEN CAST(peak_coef AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(peak_coef AS REAL) < 0 THEN 0
                    ELSE CAST(peak_coef AS REAL)
                END,
                distance_cost = CASE
                    WHEN CAST(distance_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(distance_cost AS REAL) < 0 THEN 0
                    ELSE CAST(distance_cost AS REAL)
                END,
                weight_cost = CASE
                    WHEN CAST(weight_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(weight_cost AS REAL) < 0 THEN 0
                    ELSE CAST(weight_cost AS REAL)
                END,
                wait_cost = CASE
                    WHEN CAST(wait_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(wait_cost AS REAL) < 0 THEN 0
                    ELSE CAST(wait_cost AS REAL)
                END,
                empty_return_cost = CASE
                    WHEN CAST(empty_return_cost AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(empty_return_cost AS REAL) < 0 THEN 0
                    ELSE CAST(empty_return_cost AS REAL)
                END,
                cargo_coef = CASE
                    WHEN CAST(cargo_coef AS REAL) > 9999.99 THEN 9999.99
                    WHEN CAST(cargo_coef AS REAL) < 0 THEN 0
                    ELSE CAST(cargo_coef AS REAL)
                END,
                suggested_price = CASE
                    WHEN CAST(suggested_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(suggested_price AS REAL) < 0 THEN 0
                    ELSE CAST(suggested_price AS REAL)
                END,
                final_price = CASE
                    WHEN CAST(final_price AS REAL) > 9999999999.99 THEN 9999999999.99
                    WHEN CAST(final_price AS REAL) < 0 THEN 0
                    ELSE CAST(final_price AS REAL)
                END
            """
        )


def _render_order_list_safe(request):
    rows = []
    status_map = dict(OrderStatus.choices)
    status_filter = (request.GET.get("status") or "").strip()
    status_ok = status_filter in {c[0] for c in OrderStatus.choices}
    with connection.cursor() as cursor:
        sql = """
            SELECT o.id, o.from_location, o.to_location, c.name, o.cargo_type, o.weight_ton,
                   o.client_price, o.driver_fee, o.fuel_cost, o.extra_cost, o.penalty_amount, o.status,
                   o.loaded_quantity, o.loaded_quantity_uom, o.delivered_quantity, o.delivered_quantity_uom,
                   o.density_kg_per_liter
            FROM orders_order o
            LEFT JOIN orders_client c ON c.id = o.client_id
        """
        params: list = []
        if status_ok:
            sql += " WHERE o.status = %s"
            params.append(status_filter)
        sql += " ORDER BY o.created_at DESC"
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            (
                oid,
                from_location,
                to_location,
                client_name,
                cargo_type,
                weight_ton,
                client_price,
                driver_fee,
                fuel_cost,
                extra_cost,
                penalty_amount,
                status,
                loaded_quantity,
                loaded_quantity_uom,
                delivered_quantity,
                delivered_quantity_uom,
                density_kg_per_liter,
            ) = row
            margin = _safe_decimal(client_price) - _safe_decimal(driver_fee) - _safe_decimal(fuel_cost) - _safe_decimal(extra_cost) - _safe_decimal(penalty_amount)
            cl, cd, cs = _custody_cells_for_list(
                loaded_quantity,
                loaded_quantity_uom,
                delivered_quantity,
                delivered_quantity_uom,
                density_kg_per_liter,
            )
            rows.append(
                {
                    "pk": oid,
                    "from_location": from_location or "",
                    "to_location": to_location or "",
                    "client_name": client_name or "-",
                    "cargo_type": cargo_type or "",
                    "weight_ton": _safe_decimal(weight_ton),
                    "client_price": _safe_decimal(client_price),
                    "driver_fee": _safe_decimal(driver_fee),
                    "gross_margin": margin,
                    "status_display": status_map.get(status, status),
                    "custody_loaded": cl,
                    "custody_delivered": cd,
                    "custody_shortage": cs,
                }
            )
    paginator = Paginator(rows, int(getattr(dj_settings, "ORDERS_LIST_PER_PAGE", 25) or 25))
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "orders/list.html",
        {
            "orders": page_obj.object_list,
            "page_obj": page_obj,
            "safe_mode": True,
            "status_filter": status_filter,
            "order_status_choices": OrderStatus.choices,
        },
    )


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


@staff_member_required
def order_reopen(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method == "POST":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked_order:
                changed = False
            else:
                changed = reopen_order(locked_order, changed_by=request.user.username)
        if changed:
            messages.success(request, f"Buyurtma #{order.pk} qayta ochildi.")
        else:
            messages.warning(request, f"Buyurtma #{order.pk} qayta ochilmadi.")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_return_trip(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method == "POST":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked_order:
                return redirect("order-detail", pk=order.pk)
            new_order = create_return_trip(locked_order, changed_by=request.user.username)
        messages.success(request, f"Qaytish reysi yaratildi: #{new_order.pk}.")
        return redirect("order-detail", pk=new_order.pk)
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_split(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method == "POST":
        parts_raw = request.POST.get("parts", "2")
        parts = int(parts_raw) if parts_raw.isdigit() else 2
        split_cap = int(getattr(dj_settings, "SPLIT_SHIPMENT_MAX_PARTS", 10) or 10)
        parts = max(2, min(parts, split_cap))
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked_order:
                children = []
            else:
                children = split_shipment(locked_order, parts=parts, changed_by=request.user.username)
        if children:
            messages.success(request, f"Buyurtma #{order.pk} {len(children)} qismga bo'lindi.")
        else:
            messages.warning(request, "Bo'lish uchun qismlar soni kamida 2 bo'lishi kerak.")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_tender_open(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method == "POST":
        duration_raw = request.POST.get("duration_minutes", "5")
        duration = int(duration_raw) if duration_raw.isdigit() else 5
        dmin = int(getattr(dj_settings, "TENDER_DURATION_MIN_MINUTES", 3) or 3)
        dmax = int(getattr(dj_settings, "TENDER_DURATION_MAX_MINUTES", 10) or 10)
        with transaction.atomic():
            locked = Order.objects.select_for_update().filter(pk=pk).first()
            if not locked:
                messages.warning(request, "Buyurtma topilmadi.")
                return redirect("order-detail", pk=order.pk)
            TenderSession.objects.create(
                order=locked,
                opened_by=request.user.username,
                duration_minutes=max(dmin, min(dmax, duration)),
            )
        messages.success(request, f"Order #{order.pk} uchun tender ochildi.")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_tender_bid(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method != "POST":
        return redirect("order-detail", pk=order.pk)
    bidder_name = request.POST.get("bidder_name", "").strip() or "Unknown bidder"
    bid_price_raw = request.POST.get("bid_price", "0").strip()
    eta_raw = request.POST.get("eta_minutes", "0").strip()
    try:
        bid_price = Decimal(bid_price_raw)
        eta = int(eta_raw)
    except Exception:
        messages.warning(request, "Bid format xato.")
        return redirect("order-detail", pk=order.pk)
    score = evaluate_tender_bid(bid_price, eta)
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(pk=pk).first()
        if not locked_order:
            messages.warning(request, "Buyurtma topilmadi.")
            return redirect("order-detail", pk=order.pk)
        session = (
            TenderSession.objects.select_for_update()
            .filter(order=locked_order, closed_at__isnull=True)
            .order_by("-opened_at")
            .first()
        )
        if not session:
            messages.warning(request, "Avval tender oching.")
            return redirect("order-detail", pk=order.pk)
        TenderBid.objects.create(
            session=session,
            bidder_name=bidder_name,
            bid_price=bid_price,
            eta_minutes=eta,
            score=score,
        )
    messages.success(request, f"Tender bid saqlandi ({bidder_name}).")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_tender_close(request, pk: int):
    order = get_object_or_404(Order, pk=pk)
    if request.method != "POST":
        return redirect("order-detail", pk=order.pk)
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(pk=pk).first()
        if not locked_order:
            messages.warning(request, "Buyurtma topilmadi.")
            return redirect("order-detail", pk=order.pk)
        session = (
            TenderSession.objects.select_for_update()
            .filter(order=locked_order, closed_at__isnull=True)
            .order_by("-opened_at")
            .first()
        )
        if not session:
            messages.warning(request, "Yopish uchun faol tender topilmadi.")
            return redirect("order-detail", pk=order.pk)
        best = session.bids.order_by("score", "eta_minutes", "bid_price").first()
        if best:
            session.auto_selected_bid = best
            locked_order.price_final = best.bid_price
            locked_order.client_price = best.bid_price
            locked_order.save(update_fields=["price_final", "client_price", "updated_at"])
        session.closed_at = timezone.now()
        session.save(update_fields=["closed_at", "auto_selected_bid"])
        messages.success(request, f"Tender yopildi. {'Golib tanlandi.' if best else 'Bid yoq.'}")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_driver_response_approve(request, pk: int, response_id: int):
    order = get_object_or_404(Order, pk=pk)
    response = get_object_or_404(DriverOfferResponse.objects.select_related("driver"), pk=response_id, order=order)
    if request.method == "POST":
        if response.decision != DriverOfferDecision.ACCEPT:
            messages.warning(request, "Faqat 'Qabul' javobini tasdiqlash mumkin.")
            return redirect("order-detail", pk=order.pk)
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked_order:
                changed = False
            else:
                locked_response = (
                    DriverOfferResponse.objects.select_for_update()
                    .select_related("driver")
                    .filter(pk=response_id, order=locked_order)
                    .first()
                )
                if not locked_response or locked_response.decision != DriverOfferDecision.ACCEPT:
                    changed = False
                else:
                    changed = assign_order(locked_order, locked_response.driver, changed_by=request.user.username)

                    if changed:
                        locked_response.approval_status = DriverOfferApproval.APPROVED
                        locked_response.reviewed_by = request.user.username
                        locked_response.reviewed_at = timezone.now()
                        locked_response.note = "Web paneldan tasdiqlandi"
                        locked_response.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "note"])
                    else:
                        locked_response.approval_status = DriverOfferApproval.DECLINED
                        locked_response.reviewed_by = request.user.username
                        locked_response.reviewed_at = timezone.now()
                        locked_response.note = "Band yoki mos emas (zanyat)"
                        locked_response.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "note"])

        # UI uchun xabarlar (transaction yakunlangandan keyin).
        if changed:
            messages.success(request, f"{response.driver.full_name} tasdiqlandi va orderga biriktirildi.")
        else:
            messages.warning(request, "Shofyor band yoki mos emas (zanyat).")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
def order_driver_response_decline(request, pk: int, response_id: int):
    order = get_object_or_404(Order, pk=pk)
    response = get_object_or_404(DriverOfferResponse, pk=response_id, order=order)
    if request.method == "POST":
        with transaction.atomic():
            locked_response = (
                DriverOfferResponse.objects.select_for_update()
                .filter(pk=response_id, order_id=order.pk)
                .first()
            )
            if locked_response:
                locked_response.approval_status = DriverOfferApproval.DECLINED
                locked_response.reviewed_by = request.user.username
                locked_response.reviewed_at = timezone.now()
                locked_response.note = "Web paneldan rad etildi"
                locked_response.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "note"])
        messages.success(request, "Qabul so'rovi rad etildi.")
    return redirect("order-detail", pk=order.pk)


@staff_member_required
@groups_required(*WEB_PANEL_GROUPS)
def client_list(request):
    clients = Client.objects.all()
    return render(request, "orders/client_list.html", {"clients": clients})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def client_create(request):
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            client = form.save()
            messages.success(request, f"Klient yaratildi: {client.name}")
            return redirect("client-list")
    else:
        form = ClientForm()
    return render(request, "orders/client_form.html", {"form": form, "mode": "create"})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def client_edit(request, pk: int):
    client = get_object_or_404(Client, pk=pk)
    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            messages.success(request, f"Klient yangilandi: {client.name}")
            return redirect("client-list")
    else:
        form = ClientForm(instance=client)
    return render(request, "orders/client_form.html", {"form": form, "mode": "edit", "client_obj": client})


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def client_archive(request, pk: int):
    client = get_object_or_404(Client, pk=pk)
    if request.method == "POST":
        client.is_active = False
        client.save(update_fields=["is_active"])
        messages.success(request, f"Klient arxivlandi: {client.name}")
    return redirect("client-list")


@staff_member_required
@groups_required(*WEB_OPERATION_GROUPS)
def client_restore(request, pk: int):
    client = get_object_or_404(Client, pk=pk)
    if request.method == "POST":
        client.is_active = True
        client.save(update_fields=["is_active"])
        messages.success(request, f"Klient qayta aktiv qilindi: {client.name}")
    return redirect("client-list")
