import json
from decimal import Decimal

from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.test.utils import override_settings
from unittest.mock import MagicMock, patch

from analytics.models import AlertEvent, AlertType
from bot.copy_uz import BOT_DRIVER_ONLY_NOTICE
from bot.services import (
    build_active_trip_focus_message_html,
    build_order_keyboard,
    build_order_text,
    build_start_trip_driver_message_html,
    driver_reply_keyboard_for_order,
    normalize_driver_reply_text,
    TRIP_MAP_WEBAPP_SIGN_SALT,
)
from dispatch.models import DriverOfferApproval, DriverOfferDecision, DriverOfferResponse
from bot.tasks import reverse_geocode_yandex_task, update_order_telegram_text_task
from bot.models import TelegramMessageLog
from dispatch.models import Assignment
from drivers.models import Driver, DriverStatus, Vehicle
from orders.models import Order, OrderStatus
from django.core.cache import cache
from tracking.models import LocationPing, LocationSource


@override_settings(TELEGRAM_WEBHOOK_SECRET="")
class BotWebhookTests(TestCase):
    def setUp(self) -> None:
        self.order = Order.objects.create(
            from_location="Neft Zavodi",
            to_location="Qarshi",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="Ali",
            contact_phone="+998901112233",
            status=OrderStatus.NEW,
        )

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.edit_group_message")
    @patch("bot.views.answer_callback_query")
    def test_driver_accept_callback_registers_pending_offer(self, _answer_callback_query, _edit_group_message):
        driver = Driver.objects.create(
            full_name="Group Driver",
            phone="+998900000099",
            telegram_user_id=888,
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A888AA",
            vehicle_type="large",
            capacity_ton="15.00",
        )
        payload = {
            "callback_query": {
                "id": "cb-1",
                "data": f"order:{self.order.pk}:accept",
                "from": {"id": 888, "username": "driver888"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertTrue(
            TelegramMessageLog.objects.filter(event="driver_offer_response", payload__decision="accept").exists()
        )
        row = DriverOfferResponse.objects.get(order=self.order, driver=driver)
        self.assertEqual(row.decision, DriverOfferDecision.ACCEPT)
        self.assertEqual(row.approval_status, DriverOfferApproval.PENDING)

    @patch("bot.views.edit_group_message")
    @patch("bot.views.answer_callback_query")
    def test_unregistered_telegram_user_cannot_accept_offer(self, _answer_callback_query, _edit_group_message):
        payload = {
            "callback_query": {
                "id": "cb-deny-1",
                "data": f"order:{self.order.pk}:accept",
                "from": {"id": 777, "username": "not_a_driver"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertEqual(TelegramMessageLog.objects.filter(event="callback").count(), 0)

    @override_settings(TELEGRAM_LIVE_LOCATION_SAVE_INTERVAL_SEC=0)
    def test_edited_message_live_location_saves_location_ping(self):
        """Telegram Live Location yangilanishlari edited_message orqali keladi."""
        driver = Driver.objects.create(
            full_name="Live Loc Driver",
            phone="+998900000088",
            telegram_user_id=88001,
            status=DriverStatus.BUSY,
        )
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        ts = int(timezone.now().timestamp())
        payload = {
            "update_id": 9_000_001,
            "edited_message": {
                "message_id": 501,
                "chat": {"id": 88001},
                "from": {"id": 88001, "username": "live88001"},
                "date": ts,
                "edit_date": ts + 2,
                "location": {"latitude": 41.31, "longitude": 69.28},
            },
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        ping = LocationPing.objects.filter(order=self.order, driver=driver).first()
        self.assertIsNotNone(ping)
        self.assertEqual(ping.source, LocationSource.TELEGRAM)
        self.assertAlmostEqual(float(ping.latitude), 41.31, places=4)
        self.assertAlmostEqual(float(ping.longitude), 69.28, places=4)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.edit_group_message")
    @patch("bot.views.answer_callback_query")
    def test_assign_callback_from_telegram_is_web_only(self, answer_callback_query_mock, _edit_group_message):
        driver = Driver.objects.create(
            full_name="Vali",
            phone="+998909990011",
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A900AA",
            vehicle_type="large",
            capacity_ton="15.00",
        )
        payload = {
            "callback_query": {
                "id": "cb-assign-1",
                "data": f"order:{self.order.pk}:assign:{driver.pk}",
                "from": {"id": 777, "username": "dispatcher1"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertEqual(driver.status, DriverStatus.AVAILABLE)
        self.assertFalse(Assignment.objects.filter(order=self.order).exists())
        self.assertTrue(answer_callback_query_mock.called)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.edit_chat_message")
    @patch("bot.views.answer_callback_query")
    def test_ui_home_callback_is_web_only(self, answer_callback_query_mock, _edit_chat_message):
        payload = {
            "callback_query": {
                "id": "cb-ui-1",
                "data": "ui:home",
                "from": {"id": 777, "username": "dispatcher1"},
                "message": {"chat": {"id": 777}, "message_id": 99},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TelegramMessageLog.objects.filter(event="callback").count(), 0)
        self.assertTrue(answer_callback_query_mock.called)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.edit_chat_message")
    @patch("bot.views.answer_callback_query")
    def test_ord_refresh_callback_is_web_only(self, answer_callback_query_mock, _edit_chat_message):
        payload = {
            "callback_query": {
                "id": "cb-ord-1",
                "data": f"ord:refresh:{self.order.pk}",
                "from": {"id": 777, "username": "dispatcher1"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TelegramMessageLog.objects.filter(event="callback").count(), 0)
        self.assertTrue(answer_callback_query_mock.called)

    @patch("bot.views.edit_chat_message")
    @patch("bot.views.answer_callback_query")
    def test_drv_checkpoint_callback_creates_callback_audit_log(self, _answer_callback_query, _edit_chat_message):
        driver = Driver.objects.create(
            full_name="Wizard Driver",
            phone="+998900000055",
            telegram_user_id=444,
            status=DriverStatus.BUSY,
        )
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.assertTrue(
            Assignment.objects.filter(
                driver=driver,
                order__status__in=[OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT],
            ).exists()
        )
        # Callback lock'ni tozalaymiz (agar oldingi testdan qolgan bo'lsa).
        cache.delete(f"bot:cb-lock:444:drv:checkpoint:{self.order.pk}")

        payload = {
            "callback_query": {
                "id": "cb-drv-1",
                "data": f"drv:checkpoint:{self.order.pk}",
                "from": {"id": 444, "username": "driver444"},
                "message": {"chat": {"id": 444}, "message_id": 20},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TelegramMessageLog.objects.filter(event="callback").count(), 1)
        log = TelegramMessageLog.objects.filter(event="callback").first()
        self.assertEqual(log.payload.get("action"), "checkpoint")

    @patch("bot.views.edit_group_message")
    @patch("bot.views.answer_callback_query")
    def test_finish_req_callback_keeps_in_transit_and_logs_request(self, _answer_callback_query, _edit_group_message):
        driver = Driver.objects.create(
            full_name="Jasur",
            phone="+998900000001",
            telegram_user_id=333,
            status=DriverStatus.BUSY,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["status", "updated_at"])
        payload = {
            "callback_query": {
                "id": "cb-finish-req-1",
                "data": f"order:{self.order.pk}:finish_req",
                "from": {"id": 333, "username": "jasur"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_TRANSIT)
        self.assertEqual(driver.status, DriverStatus.BUSY)
        self.assertTrue(TelegramMessageLog.objects.filter(event="driver_finish_requested").exists())

    @patch("bot.views.send_chat_message")
    def test_driver_yuklandi_command_saves_loaded_quantity(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Qty Driver",
            phone="+998900000077",
            telegram_user_id=707,
            status=DriverStatus.BUSY,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        payload = {
            "message": {
                "message_id": 71,
                "chat": {"id": 707},
                "from": {"id": 707, "username": "d707"},
                "text": "/yuklandi 10.25 tonna",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.loaded_quantity, Decimal("10.25"))
        self.assertTrue(send_chat_message_mock.called)

    @patch("bot.views.send_chat_message")
    def test_driver_commands_start_and_finish_trip(self, _send_chat_message):
        driver = Driver.objects.create(
            full_name="Sardor",
            phone="+998900000002",
            telegram_user_id=444,
            status=DriverStatus.BUSY,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        start_payload = {
            "message": {
                "message_id": 20,
                "chat": {"id": 444},
                "from": {"id": 444, "username": "driver444"},
                "text": "/start_trip",
            }
        }
        start_response = self.client.post(
            reverse("telegram-webhook"),
            data=start_payload,
            content_type="application/json",
        )
        self.assertEqual(start_response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_TRANSIT)

        finish_payload = {
            "message": {
                "message_id": 21,
                "chat": {"id": 444},
                "from": {"id": 444, "username": "driver444"},
                "text": "/finish_trip",
            }
        }
        finish_response = self.client.post(
            reverse("telegram-webhook"),
            data=finish_payload,
            content_type="application/json",
        )
        self.assertEqual(finish_response.status_code, 200)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        # Tugatish darhol COMPLETED bo'lmaydi — admin tasdiqlashi kerak.
        self.assertEqual(self.order.status, OrderStatus.IN_TRANSIT)
        self.assertEqual(driver.status, DriverStatus.BUSY)
        self.assertEqual(TelegramMessageLog.objects.filter(event="driver_command").count(), 2)

    @patch("bot.views.send_chat_message")
    def test_driver_help_command(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Help Driver",
            phone="+998900000005",
            telegram_user_id=555,
            status=DriverStatus.AVAILABLE,
        )
        payload = {
            "message": {
                "message_id": 22,
                "chat": {"id": 555},
                "from": {"id": 555, "username": "driver555"},
                "text": "/help",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send_chat_message_mock.called)
        self.assertEqual(TelegramMessageLog.objects.filter(event="driver_command", payload__driver_id=driver.pk).count(), 0)

    @patch("bot.views.send_chat_message")
    def test_driver_command_with_explicit_order_id(self, _send_chat_message):
        driver = Driver.objects.create(
            full_name="Order Driver",
            phone="+998900000006",
            telegram_user_id=666,
            status=DriverStatus.BUSY,
        )
        second_order = Order.objects.create(
            from_location="Neft Zavodi 2",
            to_location="Buxoro",
            cargo_type="Gaz",
            weight_ton="8.00",
            pickup_time=timezone.now(),
            contact_name="Vali",
            contact_phone="+998901111111",
            status=OrderStatus.ASSIGNED,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        Assignment.objects.create(order=second_order, driver=driver, assigned_by="dispatcher")
        payload = {
            "message": {
                "message_id": 23,
                "chat": {"id": 666},
                "from": {"id": 666, "username": "driver666"},
                "text": f"/start_trip {second_order.pk}",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        second_order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertEqual(second_order.status, OrderStatus.IN_TRANSIT)

    def test_normalize_driver_reply_button_maps_to_slash_command(self):
        self.assertEqual(normalize_driver_reply_text("🚛 Safarni boshlash"), "/start_trip")
        self.assertEqual(normalize_driver_reply_text("/start_trip"), "/start_trip")
        self.assertEqual(normalize_driver_reply_text("🗺 Reys xaritasi"), "/trip_map")

    @override_settings(TRIP_MAP_SHOW_YANDEX_LINKS=True)
    def test_start_trip_message_includes_yandex_route_when_flag_on(self):
        o = Order.objects.create(
            from_location="41.2, 69.2",
            to_location="39.6, 66.9",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="A",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
        )
        html = build_start_trip_driver_message_html(o)
        self.assertIn("yandex.com/maps", html)
        self.assertIn("rtext=", html)

    @override_settings(TRIP_MAP_SHOW_YANDEX_LINKS=False)
    def test_start_trip_message_default_no_yandex_links(self):
        o = Order.objects.create(
            from_location="41.2, 69.2",
            to_location="39.6, 66.9",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="A",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
        )
        html = build_start_trip_driver_message_html(o)
        self.assertNotIn("yandex.com/maps", html)

    @override_settings(TELEGRAM_WEBAPP_BASE_URL="https://example.com", TRIP_MAP_SHOW_YANDEX_LINKS=False)
    def test_start_trip_message_webapp_copy_when_configured(self):
        o = Order.objects.create(
            from_location="41.2, 69.2",
            to_location="39.6, 66.9",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="A",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
        )
        html = build_start_trip_driver_message_html(o, for_telegram_user_id=999001)
        self.assertIn("Reys xaritasi", html)
        self.assertNotIn("keyingi xabarlarda", html)

    @override_settings(TELEGRAM_WEBAPP_BASE_URL="https://example.com")
    def test_in_transit_reply_keyboard_trip_map_is_web_app(self):
        o = Order.objects.create(
            from_location="41.2, 69.2",
            to_location="39.6, 66.9",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="A",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
        )
        kb = driver_reply_keyboard_for_order(o, telegram_user_id=99001)
        trip_row = kb["keyboard"][1]
        self.assertIn("web_app", trip_row[0])
        self.assertIn("/bot/webapp/trip/", trip_row[0]["web_app"]["url"])

    @override_settings(TELEGRAM_WEBAPP_BASE_URL="https://example.com")
    def test_active_trip_focus_webapp_copy_matches_driver(self):
        o = Order.objects.create(
            from_location="41.2, 69.2",
            to_location="39.6, 66.9",
            cargo_type="Neft",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="A",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
        )
        html = build_active_trip_focus_message_html(o, for_telegram_user_id=999002)
        self.assertIn("Google Maps", html)
        self.assertNotIn("keyingi xabarlarda", html)

    def test_trip_map_ketdik_post_sets_in_transit_and_alert(self):
        driver = Driver.objects.create(
            full_name="Webapp Driver",
            phone="+998900000033",
            telegram_user_id=331122,
            status=DriverStatus.BUSY,
        )
        self.order.from_location = "41.2, 69.2"
        self.order.to_location = "41.3, 69.3"
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["from_location", "to_location", "status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="test")
        token = signing.dumps(
            {"o": self.order.pk, "tg": driver.telegram_user_id},
            salt=TRIP_MAP_WEBAPP_SIGN_SALT,
        )
        url = reverse(
            "telegram-trip-map-ketdik",
            kwargs={"order_id": self.order.pk, "token": token},
        )
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("started_trip"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_TRANSIT)
        self.assertTrue(
            AlertEvent.objects.filter(
                order=self.order, alert_type=AlertType.DRIVER_KETDIK_WEBAPP, driver=driver
            ).exists()
        )

    def test_trip_map_ketdik_when_already_in_transit_still_ok(self):
        driver = Driver.objects.create(
            full_name="Webapp Driver 2",
            phone="+998900000034",
            telegram_user_id=331123,
            status=DriverStatus.BUSY,
        )
        self.order.from_location = "41.2, 69.2"
        self.order.to_location = "41.3, 69.3"
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["from_location", "to_location", "status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="test")
        token = signing.dumps(
            {"o": self.order.pk, "tg": driver.telegram_user_id},
            salt=TRIP_MAP_WEBAPP_SIGN_SALT,
        )
        url = reverse(
            "telegram-trip-map-ketdik",
            kwargs={"order_id": self.order.pk, "token": token},
        )
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))
        self.assertFalse(r.json().get("started_trip"))
        self.assertEqual(
            AlertEvent.objects.filter(
                order=self.order, alert_type=AlertType.DRIVER_KETDIK_WEBAPP
            ).count(),
            1,
        )

    def test_trip_map_live_ping_endpoint_writes_web_ping(self):
        driver = Driver.objects.create(
            full_name="Webapp Live Driver",
            phone="+998900000035",
            telegram_user_id=331124,
            status=DriverStatus.BUSY,
        )
        self.order.from_location = "41.2, 69.2"
        self.order.to_location = "41.3, 69.3"
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["from_location", "to_location", "status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="test")
        token = signing.dumps(
            {"o": self.order.pk, "tg": driver.telegram_user_id},
            salt=TRIP_MAP_WEBAPP_SIGN_SALT,
        )
        url = reverse(
            "telegram-trip-map-live-ping",
            kwargs={"order_id": self.order.pk, "token": token},
        )
        r = self.client.post(
            url,
            data=json.dumps({"lat": 41.3111111, "lon": 69.2444444}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            LocationPing.objects.filter(order=self.order, driver=driver, source=LocationSource.WEB).exists()
        )

    def test_trip_map_webapp_hides_ketdik_when_already_in_transit(self):
        driver = Driver.objects.create(
            full_name="Webapp Driver InTransit",
            phone="+998900000036",
            telegram_user_id=331125,
            status=DriverStatus.BUSY,
        )
        self.order.from_location = "41.2, 69.2"
        self.order.to_location = "41.3, 69.3"
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["from_location", "to_location", "status", "updated_at"])
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="test")
        token = signing.dumps(
            {"o": self.order.pk, "tg": driver.telegram_user_id},
            salt=TRIP_MAP_WEBAPP_SIGN_SALT,
        )
        url = reverse(
            "telegram-trip-map-webapp",
            kwargs={"order_id": self.order.pk, "token": token},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        self.assertNotIn('id="ketdikBtn"', html)
        self.assertIn('id="trip-live-ping-url"', html)

    def test_keyboard_new_order_has_offer_buttons_no_assign_rows(self):
        keyboard = build_order_keyboard(self.order)["inline_keyboard"]
        flattened = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn(f"order:{self.order.pk}:accept", flattened)
        self.assertFalse(any(":assign:" in c for c in flattened))
        self.assertGreaterEqual(len(keyboard), 2)

    def test_keyboard_in_transit_has_finish_request_not_complete(self):
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["status", "updated_at"])
        keyboard = build_order_keyboard(self.order)["inline_keyboard"]
        flattened = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn(f"order:{self.order.pk}:finish_req", flattened)
        self.assertNotIn(f"order:{self.order.pk}:complete", flattened)
        self.assertNotIn(f"order:{self.order.pk}:accept", flattened)
        self.assertFalse(any(":assign:" in value for value in flattened))

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_orders_command_returns_list(self, send_chat_message_mock):
        payload = {
            "message": {
                "message_id": 24,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/orders",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send_chat_message_mock.called)
        sent_text = send_chat_message_mock.call_args[0][1]
        self.assertEqual(sent_text, BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_orders_command_with_status_filter(self, send_chat_message_mock):
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status", "updated_at"])
        payload = {
            "message": {
                "message_id": 25,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/orders completed",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send_chat_message_mock.called)
        sent_text = send_chat_message_mock.call_args[0][1]
        self.assertEqual(sent_text, BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_order_detail_command(self, send_chat_message_mock):
        payload = {
            "message": {
                "message_id": 26,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": f"/order {self.order.pk}",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        sent_text = send_chat_message_mock.call_args[0][1]
        self.assertEqual(sent_text, BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_assign_command(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Cmd Driver",
            phone="+998900000007",
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A901AA",
            vehicle_type="large",
            capacity_ton="20.00",
        )
        payload = {
            "message": {
                "message_id": 27,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": f"/assign {self.order.pk} {driver.pk}",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertEqual(driver.status, DriverStatus.AVAILABLE)
        self.assertFalse(Assignment.objects.filter(order=self.order).exists())
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_assign_rejects_low_capacity_driver(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Small Cmd Driver",
            phone="+998900000010",
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A902AA",
            vehicle_type="small",
            capacity_ton="2.00",
        )
        payload = {
            "message": {
                "message_id": 31,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": f"/assign {self.order.pk} {driver.pk}",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.NEW)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_orders_with_page(self, send_chat_message_mock):
        for idx in range(12):
            Order.objects.create(
                from_location=f"Zavod-{idx}",
                to_location="Urganch",
                cargo_type="Yuk",
                weight_ton="10.00",
                pickup_time=timezone.now(),
                contact_name="A",
                contact_phone="+998900000099",
                status=OrderStatus.NEW,
            )
        payload = {
            "message": {
                "message_id": 28,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/orders new 2",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_drivers_command(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Free Driver",
            phone="+998900000008",
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A333AA",
            vehicle_type="mid",
            capacity_ton="11.00",
        )
        payload = {
            "message": {
                "message_id": 29,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/drivers",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_drivers_with_busy_filter(self, send_chat_message_mock):
        busy_driver = Driver.objects.create(
            full_name="Busy List Driver",
            phone="+998900000011",
            status=DriverStatus.BUSY,
        )
        payload = {
            "message": {
                "message_id": 32,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/drivers busy",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_command_logs_created(self, send_chat_message_mock):
        payload = {
            "update_id": 33001,
            "message": {
                "message_id": 33,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/drivers",
            },
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            TelegramMessageLog.objects.filter(
                event="dispatcher_command", payload__command="/drivers", payload__reason="web_only"
            ).exists()
        )

    @override_settings(TELEGRAM_WEBHOOK_SECRET="", DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views._handle_message")
    def test_webhook_same_update_id_processed_once(self, handle_message_mock):
        cache.clear()
        payload = {
            "update_id": 424242,
            "message": {
                "message_id": 40,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/drivers",
            },
        }
        self.client.post(reverse("telegram-webhook"), data=payload, content_type="application/json")
        self.client.post(reverse("telegram-webhook"), data=payload, content_type="application/json")
        self.assertEqual(handle_message_mock.call_count, 1)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_audit_command(self, send_chat_message_mock):
        first_payload = {
            "message": {
                "message_id": 34,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/drivers",
            }
        }
        second_payload = {
            "message": {
                "message_id": 35,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/orders",
            }
        }
        self.client.post(reverse("telegram-webhook"), data=first_payload, content_type="application/json")
        self.client.post(reverse("telegram-webhook"), data=second_payload, content_type="application/json")
        audit_payload = {
            "message": {
                "message_id": 36,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/audit 5",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=audit_payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        sent_text = send_chat_message_mock.call_args[0][1]
        self.assertEqual(sent_text, BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    @patch("bot.views.edit_group_message")
    @patch("bot.views.answer_callback_query")
    def test_dispatcher_assign_callback_then_audit_command_are_web_only(
        self, _answer_callback_query, _edit_group_message, send_chat_message_mock
    ):
        driver = Driver.objects.create(
            full_name="Audit Callback Driver",
            phone="+998900000012",
            status=DriverStatus.AVAILABLE,
        )
        Vehicle.objects.create(
            driver=driver,
            plate_number="01A903AA",
            vehicle_type="large",
            capacity_ton="12.00",
        )
        callback_payload = {
            "callback_query": {
                "id": "cb-audit-1",
                "data": f"order:{self.order.pk}:assign:{driver.pk}",
                "from": {"id": 777, "username": "dispatcher1"},
                "message": {"chat": {"id": -1001}, "message_id": 9},
            }
        }
        self.client.post(reverse("telegram-webhook"), data=callback_payload, content_type="application/json")
        self.assertEqual(self.order.status, OrderStatus.NEW)
        audit_payload = {
            "message": {
                "message_id": 37,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": "/audit callbacks 5",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=audit_payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)

    @override_settings(DISPATCHER_TELEGRAM_USER_IDS=[777])
    @patch("bot.views.send_chat_message")
    def test_dispatcher_unassign_command(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Busy Driver",
            phone="+998900000009",
            status=DriverStatus.BUSY,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        payload = {
            "message": {
                "message_id": 30,
                "chat": {"id": 777},
                "from": {"id": 777, "username": "dispatcher777"},
                "text": f"/unassign {self.order.pk}",
            }
        }
        response = self.client.post(
            reverse("telegram-webhook"),
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        self.assertTrue(Assignment.objects.filter(order=self.order).exists())
        self.assertEqual(driver.status, DriverStatus.BUSY)
        self.assertEqual(self.order.status, OrderStatus.ASSIGNED)
        self.assertEqual(send_chat_message_mock.call_args[0][1], BOT_DRIVER_ONLY_NOTICE)


class TelegramOrderGeocodeUpdateTests(TestCase):
    @patch("bot.tasks.reverse_geocode_yandex_task.delay")
    @patch("urllib.request.urlopen", side_effect=AssertionError("Sync urlopen should not be called"))
    def test_reverse_geocode_is_async_and_does_not_block_send_order(self, urlopen_mock, geocode_delay_mock):
        # Cache va lockni tozalaymiz, shunda _reverse_geocode_yandex fon task'ni reja qiladi.
        cache.delete("ymap:geo:41.31:69.24")
        cache.delete("ymap:geo:lock:41.31:69.24")
        cache.delete("ymap:geo:41.35:69.30")
        cache.delete("ymap:geo:lock:41.35:69.30")

        order = Order.objects.create(
            from_location="41.31, 69.24",
            to_location="41.35, 69.30",
            cargo_type="Test cargo",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="Ali",
            contact_phone="+998901112233",
            status=OrderStatus.NEW,
        )

        text = build_order_text(order)

        # Async reja bo'ldi.
        self.assertTrue(geocode_delay_mock.called)
        # UX: manzil bo'lmasa ham lat,lon chiqariladi.
        self.assertIn("41.31, 69.24", text)
        self.assertIn("41.35, 69.30", text)
    @patch("bot.services.edit_group_message")
    def test_update_order_telegram_text_when_geocode_is_cached(self, edit_group_message_mock):
        order = Order.objects.create(
            from_location="41.31, 69.24",
            to_location="41.35, 69.30",
            cargo_type="Test cargo",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="Ali",
            contact_phone="+998901112233",
            status=OrderStatus.NEW,
        )
        chat_id = "-100999"
        message_id = "123"

        # Cachega reverse geocode matnini tayyorlab qo'yamiz.
        cache.set("ymap:geo:41.31:69.24", "From address", timeout=86400)
        cache.set("ymap:geo:41.35:69.30", "To address", timeout=86400)

        # Celery Task'ni sync rejimda run qilamiz.
        update_order_telegram_text_task.run(order.pk, chat_id, message_id, 0)

        self.assertTrue(edit_group_message_mock.called)
        kwargs = edit_group_message_mock.call_args.kwargs
        self.assertEqual(str(kwargs.get("chat_id")), chat_id)
        self.assertEqual(str(kwargs.get("message_id")), message_id)
        self.assertEqual(kwargs.get("order").pk, order.pk)

    @patch("bot.tasks.request.urlopen")
    def test_reverse_geocode_yandex_task_caches_yandex_text(self, urlopen_mock):
        cache.clear()
        payload = {
            "response": {
                "GeoObjectCollection": {
                    "featureMember": [
                        {
                            "GeoObject": {
                                "metaDataProperty": {
                                    "GeocoderMetaData": {"text": "Toshkent, Chilonzor"},
                                }
                            }
                        }
                    ]
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__.return_value = mock_resp
        ctx.__exit__.return_value = None
        urlopen_mock.return_value = ctx

        reverse_geocode_yandex_task.run("41.31", "69.24")

        self.assertEqual(cache.get("ymap:geo:41.31:69.24"), "Toshkent, Chilonzor")
