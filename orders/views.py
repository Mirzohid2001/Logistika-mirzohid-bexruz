from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings as dj_settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from decimal import Decimal
from decimal import InvalidOperation
import datetime
import json
import re

from bot.services import driver_idle_reply_keyboard, send_chat_message, send_ops_notification, send_order_to_group
from common.permissions import WEB_OPERATION_GROUPS, WEB_PANEL_GROUPS, groups_required
from dispatch.models import Assignment, DriverOfferApproval, DriverOfferDecision, DriverOfferResponse
from drivers.forms import DriverDeliveryReviewForm
from drivers.models import Driver, DriverDeliveryReview, DriverStatus
from drivers.services import get_driver_review_aggregates, recompute_driver_rating_score
from bot.models import TelegramMessageLog
from dispatch.services import assign_order
from dispatch.allocation import AllocationResult, DriverCapacity, calculate_big_order_allocation
from pricing.models import PriceQuote, TenderBid, TenderSession
from pricing.services import build_price_breakdown, evaluate_tender_bid, suggest_price

from .forms import (
    ClientForm,
    OrderCreateForm,
    OrderCustodyForm,
    OrderExtraExpenseForm,
    OrderSealAddForm,
    OrderSealUpdateForm,
)

from .models import Client, Order, OrderExtraExpense, OrderSeal, OrderStatus, QuantityUnit
from .quantity import quantity_to_metric_tonnes, shortage_tonnes
from .services import (
    apply_client_contract,
    create_return_trip,
    log_order_field_audit,
    reopen_order,
    split_shipment,
    transition_order,
)
from tracking.models import LocationPing

# Buyurtmalar ro‘yxati: filter dropdown uchun qisqa o‘zbekcha yozuvlar
ORDER_STATUS_LABELS_UZ = {
    "new": "Yangi",
    "offered": "Taklif",
    "assigned": "Biriktirilgan",
    "in_transit": "Yo‘lda",
    "completed": "Yakunlangan",
    "canceled": "Bekor",
    "issue": "Muammo",
}


def _extract_coords_for_route(text: str) -> tuple[float, float] | None:
    if not text:
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", str(text))
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _apply_route_geofence_to_order(order: Order) -> None:
    """Koordinatalar bo‘lsa marshrut va oddiy bbox geofence to‘ldiriladi."""
    from_latlon = _extract_coords_for_route(order.from_location)
    to_latlon = _extract_coords_for_route(order.to_location)
    if not from_latlon or not to_latlon:
        return
    from_lat, from_lon = from_latlon
    to_lat, to_lon = to_latlon
    if not order.route_polyline:
        order.route_polyline = [{"lat": from_lat, "lon": from_lon}, {"lat": to_lat, "lon": to_lon}]
    if not order.geofence_polygon:
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


def _save_new_order_with_quote(order: Order) -> None:
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


def _form_errors_text(form) -> str:
    parts: list[str] = []
    for field, errs in form.errors.items():
        label = "" if field == "__all__" else f"{field}: "
        for e in errs:
            parts.append(f"{label}{e}")
    return "; ".join(parts) if parts else "xato"


def _orders_preserve_get_params(request) -> str:
    p = request.GET.copy()
    p.pop("page", None)
    return p.urlencode()


def _orders_status_choices_uz():
    return [(code, ORDER_STATUS_LABELS_UZ.get(code, label)) for code, label in OrderStatus.choices]


def _web_actor_username(request) -> str:
    u = getattr(request.user, "username", None) or str(request.user.pk)
    return f"web:{u}"


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


def _format_age_short(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    hours = sec // 3600
    mins = (sec % 3600) // 60
    if mins:
        return f"{hours}h {mins}m"
    return f"{hours}h"


def _custody_cells_for_list(
    loaded_q,
    loaded_uom: str | None,
    delivered_q,
    delivered_uom: str | None,
    density,
    delivered_density=None,
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
        deliv_dens_dec = (
            Decimal(str(delivered_density))
            if delivered_density is not None and str(delivered_density).strip() != ""
            else None
        )
    except Exception:
        lq = dq = None
        dens_dec = None
        deliv_dens_dec = None
    lu = loaded_uom or QuantityUnit.TON
    du = delivered_uom or QuantityUnit.TON
    dt_density = deliv_dens_dec if deliv_dens_dec is not None else dens_dec
    lt = quantity_to_metric_tonnes(lq, lu, density_kg_per_liter=dens_dec) if lq is not None else None
    dt = quantity_to_metric_tonnes(dq, du, density_kg_per_liter=dt_density) if dq is not None else None
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
        qs = Order.objects.select_related("client", "assignment__driver").order_by("-created_at")
        preset = (request.GET.get("preset") or "").strip()
        status = (request.GET.get("status") or "").strip()
        search_q = (request.GET.get("q") or "").strip()
        date_str = (request.GET.get("date") or "").strip()
        driver_filter = (request.GET.get("driver") or "").strip()
        client_filter = (request.GET.get("client") or "").strip()
        view_mode = (request.GET.get("view") or "full").strip()
        if view_mode not in {"full", "minimal"}:
            view_mode = "full"

        if preset == "today":
            date_str = timezone.localdate().isoformat()

        active_statuses = [
            OrderStatus.NEW,
            OrderStatus.OFFERED,
            OrderStatus.ASSIGNED,
            OrderStatus.IN_TRANSIT,
        ]
        now = timezone.now()
        if preset == "delayed":
            qs = qs.filter(
                sla_deadline_at__isnull=False,
                sla_deadline_at__lt=now,
                status__in=active_statuses,
            )
        elif preset == "active" and status not in {c[0] for c in OrderStatus.choices}:
            qs = qs.filter(status__in=active_statuses)

        if date_str:
            try:
                day = datetime.date.fromisoformat(date_str)
                start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
                end = start + datetime.timedelta(days=1)
                qs = qs.filter(pickup_time__gte=start, pickup_time__lt=end)
            except ValueError:
                pass

        if status in {c[0] for c in OrderStatus.choices}:
            qs = qs.filter(status=status)

        if driver_filter.isdigit():
            qs = qs.filter(assignment__driver_id=int(driver_filter))
        if client_filter.isdigit():
            qs = qs.filter(client_id=int(client_filter))

        if search_q:
            sq = (
                Q(contact_phone__icontains=search_q)
                | Q(client__name__icontains=search_q)
                | Q(client__phone__icontains=search_q)
                | Q(assignment__driver__full_name__icontains=search_q)
                | Q(assignment__driver__phone__icontains=search_q)
                | Q(from_location__icontains=search_q)
                | Q(to_location__icontains=search_q)
            )
            if search_q.isdigit():
                sq |= Q(pk=int(search_q))
            qs = qs.filter(sq)

        paginator = Paginator(qs, int(getattr(dj_settings, "ORDERS_LIST_PER_PAGE", 25) or 25))
        page_obj = paginator.get_page(request.GET.get("page"))
        visible_orders = list(page_obj.object_list)
        visible_order_ids = [o.pk for o in visible_orders]
        latest_ping_map: dict[int, timezone.datetime] = {}
        if visible_order_ids:
            rows = (
                LocationPing.objects.filter(order_id__in=visible_order_ids)
                .order_by("order_id", "-captured_at")
                .values("order_id", "captured_at")
            )
            for row in rows.iterator(chunk_size=200):
                oid = int(row["order_id"])
                if oid not in latest_ping_map:
                    latest_ping_map[oid] = row["captured_at"]
        stale_sec = int(getattr(dj_settings, "ORDER_LIVE_STALE_SEC", 600) or 600)
        for order in visible_orders:
            cap = latest_ping_map.get(order.pk)
            order.live_ping_at = cap
            if cap is None:
                order.live_ping_age_sec = None
                order.live_ping_age_label = "—"
                order.live_ping_stale = True
            else:
                age = max(0, int((now - cap).total_seconds()))
                order.live_ping_age_sec = age
                order.live_ping_age_label = _format_age_short(age)
                order.live_ping_stale = age >= stale_sec
        drivers_qs = Driver.objects.order_by("full_name")[:800]
        clients_qs = Client.objects.filter(is_active=True).order_by("name")[:800]
        return render(
            request,
            "orders/list.html",
            {
                "orders": visible_orders,
                "page_obj": page_obj,
                "safe_mode": False,
                "status_filter": status,
                "order_status_choices": OrderStatus.choices,
                "order_status_choices_uz": _orders_status_choices_uz(),
                "order_live_stale_sec": stale_sec,
                "search_q": search_q,
                "date_filter": date_str,
                "driver_filter": driver_filter,
                "client_filter": client_filter,
                "preset": preset,
                "view_mode": view_mode,
                "request_params": _orders_preserve_get_params(request),
                "drivers_for_filter": drivers_qs,
                "clients_for_filter": clients_qs,
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
            _apply_route_geofence_to_order(order)
            _save_new_order_with_quote(order)
            if not send_order_to_group(order):
                messages.warning(
                    request,
                    "Buyurtma saqlandi, lekin Telegram guruhiga xabar yuborilmadi. "
                    ".env da TELEGRAM_BOT_TOKEN va TELEGRAM_GROUP_ID ni tekshiring; "
                    "forum guruh bo‘lsa TELEGRAM_GROUP_MESSAGE_THREAD_ID qo‘ying; "
                    "bot guruhda va xabar yuborish huquqi borligini tasdiqlang. "
                    "Diagnostika: python manage.py test_telegram_group",
                )
            send_ops_notification("order_created", order=order, note="Dispetcher tomonidan yaratildi")
            messages.success(request, f"Buyurtma #{order.pk} yaratildi.")
            return redirect("order-detail", pk=order.pk)
    else:
        initial = {}
        now = timezone.localtime()
        start = now.replace(minute=0, second=0, microsecond=0)
        pt = start + datetime.timedelta(hours=1) if now >= start else start
        initial["pickup_time"] = pt.strftime("%Y-%m-%d %H:%M")
        fl = (getattr(dj_settings, "ORDER_DEFAULT_FROM_LOCATION", "") or "").strip()
        tl = (getattr(dj_settings, "ORDER_DEFAULT_TO_LOCATION", "") or "").strip()
        cg = (getattr(dj_settings, "ORDER_DEFAULT_CARGO_TYPE", "") or "").strip()
        if fl:
            initial["from_location"] = fl
        if tl:
            initial["to_location"] = tl
        if cg:
            initial["cargo_type"] = cg
        form = OrderCreateForm(initial=initial)
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
        order = get_object_or_404(Order.objects.prefetch_related("seals", "additional_expenses"), pk=pk)
    except InvalidOperation:
        _repair_order_decimal_data(pk)
        try:
            order = get_object_or_404(Order.objects.prefetch_related("seals", "additional_expenses"), pk=pk)
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
    seal_add_form = OrderSealAddForm()
    seals_qs = order.seals.all()
    seal_rows = list(seals_qs)
    seal_broken_any = any(s.is_broken for s in seal_rows)
    seal_count = len(seal_rows)
    seal_update_forms = [(s, OrderSealUpdateForm(instance=s, prefix=f"seal{s.pk}")) for s in seal_rows]
    expense_form = OrderExtraExpenseForm()
    expenses = list(order.additional_expenses.all())

    driver_review = DriverDeliveryReview.objects.filter(order_id=order.pk).first()
    driver_review_form = DriverDeliveryReviewForm(instance=driver_review)
    can_driver_review = order.status == OrderStatus.COMPLETED and assignment is not None
    driver_review_stats = {"count": 0, "avg_stars": None}
    if assignment and assignment.driver_id:
        assignment.driver.refresh_from_db()
        rc, ra = get_driver_review_aggregates(assignment.driver)
        driver_review_stats = {"count": rc, "avg_stars": ra}

    big_alloc: AllocationResult | None = None
    try:
        big_alloc = _calculate_big_order_allocation_cached(order)
    except Exception:
        big_alloc = None

    state_logs = list(order.state_logs.order_by("-created_at")[:25])
    field_audits = list(order.field_audits.order_by("-created_at")[:40])

    return render(
        request,
        "orders/detail.html",
        {
            "order": order,
            "custody_form": custody_form,
            "seal_add_form": seal_add_form,
            "seal_broken_any": seal_broken_any,
            "seal_count": seal_count,
            "seal_update_forms": seal_update_forms,
            "expense_form": expense_form,
            "expenses": expenses,
            "driver_review": driver_review,
            "driver_review_form": driver_review_form,
            "can_driver_review": can_driver_review,
            "driver_review_stats": driver_review_stats,
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
            "big_order_allocation": big_alloc,
            "state_logs": state_logs,
            "field_audits": field_audits,
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
def order_live_reminder(request, pk: int):
    if request.method != "POST":
        return redirect("order-detail", pk=pk)

    order = get_object_or_404(Order, pk=pk)
    if order.status != OrderStatus.IN_TRANSIT:
        messages.warning(request, "Eslatma faqat yo‘ldagi (IN_TRANSIT) buyurtmada yuboriladi.")
        return redirect("order-detail", pk=pk)

    ass = Assignment.objects.select_related("driver").filter(order=order).first()
    if not ass or not ass.driver_id:
        messages.warning(request, "Bu buyurtmaga shofyor biriktirilmagan.")
        return redirect("order-detail", pk=pk)
    if not ass.driver.telegram_user_id:
        messages.warning(request, "Shofyor Telegram’ga ulanmagan. Eslatma yuborilmadi.")
        return redirect("order-detail", pk=pk)

    reminder_text = (
        f"⚠️ Buyurtma #{order.pk}: live location yangilanishi sust.\n"
        "Iltimos, Telegram’da jonli joylashuvni davom ettiring yoki qayta ulashing:\n"
        "📎 → Joylashuv → Jonli joylashuv."
    )
    send_chat_message(str(ass.driver.telegram_user_id), reminder_text)
    TelegramMessageLog.objects.create(
        order=order,
        chat_id=str(ass.driver.telegram_user_id),
        message_id="",
        event="driver_live_reminder_manual",
        payload={"driver_id": ass.driver_id, "by": _web_actor_username(request)},
    )
    messages.success(request, "Haydovchiga live location eslatmasi yuborildi.")
    return redirect("order-detail", pk=pk)


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

    if order.delivered_quantity is None:
        messages.error(
            request,
            "Klientga topshirilgan hajm kiritilmagan. Tugatishdan oldin avval <code>/topshirildi</code> yoki web-panel orqali hajmni kiriting.",
        )
        return redirect("order-detail", pk=order.pk)

    if order.delivered_quantity_uom == QuantityUnit.LITER and order.delivered_quantity_metric_ton is None:
        messages.error(
            request,
            "Litr uchun zichlik kg/L kerak: <code>/topshirildi ... litr 0.84</code> yoki web-paneldagi zichlikni kiriting.",
        )
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

    if driver:
        recompute_driver_rating_score(driver)
        driver.refresh_from_db()

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
            f"Kamomad jarimasi: {penalty_points} ball (reyting qayta hisoblandi). "
            f"Joriy reyting: {driver.rating_score if driver else '-'}",
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


_CUSTODY_AUDIT_FIELDS = (
    "loaded_quantity",
    "loaded_quantity_uom",
    "delivered_quantity",
    "delivered_quantity_uom",
    "density_kg_per_liter",
    "delivered_density_kg_per_liter",
)


@staff_member_required
def order_custody_update(request, pk: int):
    """Yuklangan / topshirilgan hajm va zichlik (web)."""
    order = get_object_or_404(Order, pk=pk)
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    before = {f: getattr(order, f) for f in _CUSTODY_AUDIT_FIELDS}
    form = OrderCustodyForm(request.POST, instance=order)
    if not form.is_valid():
        messages.error(request, "Hajm ma'lumotlari: " + _form_errors_text(form))
        return redirect("order-detail", pk=pk)
    obj = form.save(commit=False)
    username = getattr(request.user, "username", None) or str(request.user.pk)
    actor = f"web:{username}"
    now = timezone.now()
    if "loaded_quantity" in form.changed_data and obj.loaded_quantity is not None:
        obj.loaded_recorded_at = now
        obj.loaded_recorded_by = actor
    if "delivered_quantity" in form.changed_data and obj.delivered_quantity is not None:
        obj.delivered_recorded_at = now
        obj.delivered_recorded_by = actor
    obj.save()
    for fname in _CUSTODY_AUDIT_FIELDS:
        old_v = before.get(fname)
        new_v = getattr(obj, fname)
        if old_v != new_v:
            log_order_field_audit(obj, fname, old_v, new_v, actor)
    messages.success(request, "Hajm va zichlik ma'lumotlari saqlandi.")
    return redirect("order-detail", pk=pk)


@staff_member_required
def order_seal_add(request, pk: int):
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    order = get_object_or_404(Order, pk=pk)
    form = OrderSealAddForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Muhr qo‘shish: maydonlarni tekshiring.")
        return redirect("order-detail", pk=pk)
    seal = form.save(commit=False)
    seal.order = order
    seal.loading_recorded_at = timezone.now()
    seal.loading_recorded_by = _web_actor_username(request)
    seal.save()
    messages.success(request, "Muhr qo‘shildi.")
    return redirect("order-detail", pk=pk)


@staff_member_required
def order_seal_update(request, pk: int, seal_id: int):
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    order = get_object_or_404(Order, pk=pk)
    seal = get_object_or_404(OrderSeal, pk=seal_id, order=order)
    pfx = f"seal{seal.pk}"
    form = OrderSealUpdateForm(request.POST, instance=seal, prefix=pfx)
    if not form.is_valid():
        messages.error(
            request,
            "Muhrni yangilash: formani tekshiring. "
            + (form.errors.as_text() if form.errors else ""),
        )
        return redirect("order-detail", pk=pk)
    obj = form.save(commit=False)
    username = _web_actor_username(request)
    new_unl = (form.cleaned_data.get("seal_number_unloading") or "").strip()
    if "seal_number_unloading" in form.changed_data and new_unl:
        if seal.unloading_recorded_at is None:
            obj.unloading_recorded_at = timezone.now()
            obj.unloading_recorded_by = username
        elif (seal.seal_number_unloading or "").strip() != new_unl:
            obj.unloading_recorded_by = username

    if form.cleaned_data.get("is_broken"):
        if seal.broken_at is None:
            obj.broken_at = timezone.now()
            obj.broken_recorded_by = username
    else:
        obj.broken_at = None
        obj.broken_recorded_by = ""

    obj.save()
    messages.success(request, "Muhr ma’lumoti yangilandi.")
    return redirect("order-detail", pk=pk)


@staff_member_required
def order_seal_delete(request, pk: int, seal_id: int):
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    order = get_object_or_404(Order, pk=pk)
    seal = get_object_or_404(OrderSeal, pk=seal_id, order=order)
    seal.delete()
    messages.success(request, "Muhr yozuvi o‘chirildi.")
    return redirect("order-detail", pk=pk)


@staff_member_required
def order_expense_add(request, pk: int):
    if request.method != "POST":
        return redirect("order-detail", pk=pk)
    order = get_object_or_404(Order, pk=pk)
    form = OrderExtraExpenseForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Qo‘shimcha xarajat formasini tekshiring.")
        return redirect("order-detail", pk=pk)
    exp = form.save(commit=False)
    exp.order = order
    exp.recorded_by = _web_actor_username(request)
    exp.save()
    messages.success(request, "Qo‘shimcha xarajat saqlandi.")
    return redirect("order-detail", pk=pk)


@staff_member_required
def order_driver_review(request, pk: int):
    """Yakunlangan reys uchun shofyor bahosi — reyting qayta hisoblanadi."""
    if request.method != "POST":
        return redirect("order-detail", pk=pk)

    form = DriverDeliveryReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Baholash: " + form.errors.as_text().strip())
        return redirect("order-detail", pk=pk)

    uname = getattr(request.user, "username", None) or str(request.user.pk)
    stars = form.cleaned_data["stars"]
    comment = form.cleaned_data.get("comment") or ""

    try:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            if order.status != OrderStatus.COMPLETED:
                messages.error(request, "Faqat yakunlangan buyurtma uchun shofyor bahosi qoldiriladi.")
                return redirect("order-detail", pk=pk)
            ass = Assignment.objects.select_for_update().select_related("driver").filter(order=order).first()
            if not ass or not ass.driver_id:
                messages.error(request, "Bu buyurtmada biriktirilgan shofyor yo‘q.")
                return redirect("order-detail", pk=pk)
            existing = DriverDeliveryReview.objects.select_for_update().filter(order=order).first()
            if existing:
                existing.stars = stars
                existing.comment = comment
                existing.driver = ass.driver
                existing.recorded_by_username = uname
                existing.full_clean()
                existing.save()
            else:
                review = DriverDeliveryReview(
                    order=order,
                    driver=ass.driver,
                    stars=stars,
                    comment=comment,
                    recorded_by_username=uname,
                )
                review.full_clean()
                review.save()
            recompute_driver_rating_score(ass.driver)
    except ValidationError as e:
        parts: list[str] = []
        err_dict = getattr(e, "error_dict", None) or {}
        for ev in err_dict.values():
            parts.extend(str(x) for x in ev)
        if not parts:
            parts = [str(m) for m in e.messages]
        messages.error(request, "; ".join(parts) if parts else "Tasdiqlash xatosi.")
        return redirect("order-detail", pk=pk)
    except DatabaseError:
        messages.error(request, "Maʼlumotlar bazasi xatosi. Qayta urinib ko‘ring.")
        return redirect("order-detail", pk=pk)

    messages.success(request, "Shofyor bahosi saqlandi. Reyting yangilandi.")
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
                   o.density_kg_per_liter, o.delivered_density_kg_per_liter
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
                delivered_density_kg_per_liter,
            ) = row
            margin = _safe_decimal(client_price) - _safe_decimal(driver_fee) - _safe_decimal(fuel_cost) - _safe_decimal(extra_cost) - _safe_decimal(penalty_amount)
            cl, cd, cs = _custody_cells_for_list(
                loaded_quantity,
                loaded_quantity_uom,
                delivered_quantity,
                delivered_quantity_uom,
                density_kg_per_liter,
                delivered_density_kg_per_liter,
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
            "order_status_choices_uz": _orders_status_choices_uz(),
            "search_q": "",
            "date_filter": "",
            "driver_filter": "",
            "client_filter": "",
            "preset": "",
            "view_mode": "full",
            "request_params": _orders_preserve_get_params(request),
            "drivers_for_filter": [],
            "clients_for_filter": [],
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
            old_cp = locked_order.client_price
            old_pf = locked_order.price_final
            locked_order.price_final = best.bid_price
            locked_order.client_price = best.bid_price
            locked_order.save(update_fields=["price_final", "client_price", "updated_at"])
            by = getattr(request.user, "username", None) or str(request.user.pk)
            log_order_field_audit(locked_order, "client_price", old_cp, locked_order.client_price, by)
            log_order_field_audit(locked_order, "price_final", old_pf, locked_order.price_final, by)
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
            try:
                _notify_big_order_allocation_if_applicable(locked_order)
            except Exception:
                pass
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


def _notify_big_order_allocation_if_applicable(order: Order) -> None:
    from dispatch.models import Assignment

    min_ton = Decimal(str(getattr(dj_settings, "BIG_ORDER_MIN_TON", 20) or 20))
    if not order.weight_ton or Decimal(order.weight_ton) < min_ton:
        return

    qs = (
        Assignment.objects.select_related("driver")
        .filter(order=order)
    )
    drivers: list[DriverCapacity] = []
    for a in qs:
        driver = a.driver
        vehicle = driver.vehicles.order_by("-capacity_ton").first()
        if not vehicle or not vehicle.capacity_ton:
            continue
        cap_kg = (Decimal(vehicle.capacity_ton) * Decimal("1000")).quantize(Decimal("1"))
        if cap_kg <= 0:
            continue
        drivers.append(DriverCapacity(driver=driver, capacity_kg=cap_kg))

    if not drivers:
        return

    result = calculate_big_order_allocation(order, drivers=drivers)
    from bot.services import send_ops_notification

    lines: list[str] = []
    for drv, kg in result.allocations:
        lines.append(f"{drv.full_name}: {kg} kg")
    note = (
        "Katta zakaz taqsimlash (tavsiya).\n"
        + "\n".join(lines)
        + f"\nQoldiq: {result.remaining_kg} kg"
    )
    send_ops_notification("big_order_allocation", order=order, driver=None, note=note)


def _calculate_big_order_allocation_cached(order: Order) -> AllocationResult | None:
    min_ton = Decimal(str(getattr(dj_settings, "BIG_ORDER_MIN_TON", 20) or 20))
    if not order.weight_ton or Decimal(order.weight_ton) < min_ton:
        return None

    qs = Assignment.objects.select_related("driver").filter(order=order)
    drivers: list[DriverCapacity] = []
    for a in qs:
        driver = a.driver
        vehicle = driver.vehicles.order_by("-capacity_ton").first()
        if not vehicle or not vehicle.capacity_ton:
            continue
        cap_kg = (Decimal(vehicle.capacity_ton) * Decimal("1000")).quantize(Decimal("1"))
        if cap_kg <= 0:
            continue
        drivers.append(DriverCapacity(driver=driver, capacity_kg=cap_kg))

    if not drivers:
        return None

    return calculate_big_order_allocation(order, drivers=drivers)


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
