from django.db import transaction

from dispatch.models import Assignment
from drivers.models import Driver, DriverStatus
from orders.models import Order
from orders.services import transition_order


def assign_order(order: Order, driver: Driver, changed_by: str) -> bool:
    order_pk = order.pk
    driver_pk = driver.pk

    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_pk).first()
        driver = Driver.objects.select_for_update().filter(pk=driver_pk).first()
        if not order or not driver:
            return False

        busy_elsewhere = Assignment.objects.filter(
            driver=driver,
            order__status__in=["assigned", "in_transit"],
        ).exclude(order=order).exists()
        if busy_elsewhere:
            return False

        Assignment.objects.update_or_create(
            order=order,
            defaults={
                "driver": driver,
                "assigned_by": changed_by,
            },
        )
        driver.status = DriverStatus.BUSY
        driver.save(update_fields=["status", "updated_at"])
        changed = transition_order(order, "assigned", changed_by=changed_by)

    if changed and driver.telegram_user_id:
        try:
            from bot.services import (
                build_active_trip_focus_message_html,
                build_live_location_instruction,
                driver_reply_keyboard_for_order,
                send_chat_message,
                send_order_native_map_pins,
            )

            send_chat_message(
                str(driver.telegram_user_id),
                (
                    f"✅ Siz tasdiqlandingiz — Buyurtma #{order.pk}\n"
                    f"🚚 Yuk: {order.cargo_type} ({order.weight_ton} t)\n"
                    f"🧭 Yo'nalish: {order.from_location} -> {order.to_location}\n"
                    f"📞 Kontakt: {order.contact_name} {order.contact_phone}"
                ),
            )
            send_chat_message(
                str(driver.telegram_user_id),
                build_live_location_instruction(order.pk),
                parse_mode="HTML",
            )
            send_chat_message(
                str(driver.telegram_user_id),
                build_active_trip_focus_message_html(
                    order, for_telegram_user_id=driver.telegram_user_id
                ),
                parse_mode="HTML",
                reply_markup=driver_reply_keyboard_for_order(
                    order, telegram_user_id=driver.telegram_user_id or 0
                ),
                disable_web_page_preview=True,
            )
            send_order_native_map_pins(str(driver.telegram_user_id), order)
        except Exception:
            pass

    return changed
