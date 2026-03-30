import csv
from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase
from django.core.management import call_command
from django.utils import timezone
from django.contrib.auth.models import User
from django.urls import reverse

from analytics.models import AlertEvent, AlertType, AnalyticsSettings, ClientAnalyticsSnapshot, DriverPerformanceSnapshot, MonthlyFinanceReport
from analytics.services import rebuild_monthly_reports
from analytics.tasks import check_live_track_required_task, detect_route_deviation_task
from bot.models import TelegramMessageLog
from tracking.models import LocationPing, LocationSource
from dispatch.models import Assignment
from drivers.models import Driver, DriverStatus
from orders.models import Client, Order, OrderStatus, PaymentLedger, RevenueLedger


class AnalyticsReportTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Client")
        self.driver = Driver.objects.create(full_name="Driver A", phone="+9989000", status=DriverStatus.AVAILABLE)
        self.order = Order.objects.create(
            client=self.client_obj,
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            delivered_at=timezone.now(),
            contact_name="User",
            contact_phone="+99891",
            status=OrderStatus.COMPLETED,
            client_price="2000000.00",
            driver_fee="1200000.00",
        )
        Assignment.objects.create(order=self.order, driver=self.driver, assigned_by="dispatcher")
        self.analyst_group, _ = Group.objects.get_or_create(name="Analyst")

    def test_rebuild_monthly_reports_creates_snapshots(self):
        today = timezone.now().date()
        rebuild_monthly_reports(today.year, today.month)
        self.assertTrue(MonthlyFinanceReport.objects.filter(year=today.year, month=today.month).exists())
        self.assertTrue(ClientAnalyticsSnapshot.objects.filter(client=self.client_obj).exists())
        self.assertTrue(DriverPerformanceSnapshot.objects.filter(driver=self.driver).exists())

    def test_monthly_gross_revenue_sums_completed_only(self):
        today = timezone.now().date()
        Order.objects.create(
            client=self.client_obj,
            from_location="X",
            to_location="Y",
            cargo_type="Oil",
            weight_ton="1.00",
            pickup_time=timezone.now(),
            contact_name="U",
            contact_phone="+99892",
            status=OrderStatus.ISSUE,
            client_price="50000000.00",
            driver_fee="1000000.00",
        )
        rebuild_monthly_reports(today.year, today.month)
        report = MonthlyFinanceReport.objects.get(year=today.year, month=today.month)
        self.assertEqual(str(report.gross_revenue), "2000000.00")
        self.assertEqual(str(report.total_driver_cost), "1200000.00")

    def test_monthly_report_net_margin_includes_fuel_extra_penalty(self):
        self.order.fuel_cost = "100000.00"
        self.order.extra_cost = "50000.00"
        self.order.penalty_amount = "25000.00"
        self.order.save(update_fields=["fuel_cost", "extra_cost", "penalty_amount", "updated_at"])
        today = timezone.now().date()
        rebuild_monthly_reports(today.year, today.month)
        report = MonthlyFinanceReport.objects.get(year=today.year, month=today.month)
        self.assertEqual(str(report.total_fuel_cost), "100000.00")
        self.assertEqual(str(report.total_extra_cost), "50000.00")
        self.assertEqual(str(report.total_penalty), "25000.00")
        self.assertEqual(str(report.net_margin), "625000.00")

    def test_accounting_pnl_report_and_csv_load(self):
        today = timezone.now().date()
        rebuild_monthly_reports(today.year, today.month)
        user = User.objects.create_user(username="staff_acc", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        r1 = self.client.get(
            reverse("accounting-pnl-report"),
            {"period": "month", "year": str(today.year), "month": str(today.month)},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertContains(r1, "Haydovchilarga")
        r2 = self.client.get(
            reverse("accounting-pnl-export-csv"),
            {"period": "month", "year": str(today.year), "month": str(today.month)},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertIn("text/csv", r2["Content-Type"])

    def test_reconcile_command_runs(self):
        PaymentLedger.objects.create(order=self.order, amount="1200000", paid_amount="1000000")
        RevenueLedger.objects.create(order=self.order, amount="2000000", received_amount="1500000")
        call_command("reconcile_finance")

    def test_bootstrap_pilot_command_runs(self):
        call_command("bootstrap_pilot")

    def test_dashboard_loads_for_staff(self):
        user = User.objects.create_user(username="staff1", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        response = self.client.get(reverse("ops-dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_live_fleet_map_and_data_for_analyst(self):
        user = User.objects.create_user(username="staff_fleet", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        r_map = self.client.get(reverse("live-fleet-map"))
        self.assertEqual(r_map.status_code, 200)
        self.assertContains(r_map, "fleetLiveMap")
        r_json = self.client.get(reverse("live-fleet-data"))
        self.assertEqual(r_json.status_code, 200)
        data = r_json.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("markers", data)
        self.assertIn("missing_live", data)

    def test_live_fleet_data_contains_freshness_and_no_live_alert_flags(self):
        in_transit_order = Order.objects.create(
            client=self.client_obj,
            from_location="Tashkent",
            to_location="Samarkand",
            cargo_type="Oil",
            weight_ton="8.00",
            pickup_time=timezone.now(),
            contact_name="Ops",
            contact_phone="+99890",
            status=OrderStatus.IN_TRANSIT,
            client_price="0.00",
            driver_fee="0.00",
        )
        Assignment.objects.create(order=in_transit_order, driver=self.driver, assigned_by="dispatcher")
        LocationPing.objects.create(
            order=in_transit_order,
            driver=self.driver,
            latitude="41.3111111",
            longitude="69.2444444",
            source=LocationSource.TELEGRAM,
            captured_at=timezone.now() - timedelta(minutes=2),
        )

        missing_order = Order.objects.create(
            client=self.client_obj,
            from_location="Buxoro",
            to_location="Navoiy",
            cargo_type="Oil",
            weight_ton="7.00",
            pickup_time=timezone.now(),
            contact_name="Ops",
            contact_phone="+99891",
            status=OrderStatus.IN_TRANSIT,
            client_price="0.00",
            driver_fee="0.00",
        )
        Assignment.objects.create(order=missing_order, driver=self.driver, assigned_by="dispatcher")
        AlertEvent.objects.create(
            order=missing_order,
            driver=self.driver,
            alert_type=AlertType.NO_LIVE_TRACK,
            threshold_minutes=0,
            message="No live yet",
            resolved=False,
        )

        user = User.objects.create_user(username="staff_fleet2", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        data = self.client.get(reverse("live-fleet-data")).json()

        marker = next((m for m in data.get("markers", []) if m.get("order_id") == in_transit_order.pk), None)
        self.assertIsNotNone(marker)
        self.assertIn("age_sec", marker)
        self.assertIsInstance(marker["age_sec"], int)

        missing = next((m for m in data.get("missing_live", []) if m.get("order_id") == missing_order.pk), None)
        self.assertIsNotNone(missing)
        self.assertTrue(missing.get("no_live_alert_open"))
        self.assertGreaterEqual(int(data.get("counts", {}).get("no_live_alert_open", 0)), 1)

    def test_generate_monthly_redirects_to_dashboard(self):
        user = User.objects.create_user(username="staff2", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        response = self.client.get(reverse("generate-monthly-report"), {"year": "2026", "month": "3"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("ops-dashboard"), response.url)

    def test_clients_rating_report_loads(self):
        today = timezone.now().date()
        rebuild_monthly_reports(today.year, today.month)
        user = User.objects.create_user(username="staff3", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        response = self.client.get(
            reverse("clients-rating-report"),
            {"year": str(today.year), "month": str(today.month), "search": "Test", "top": "10", "active_only": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Klientlar reytingi")

    def test_clients_monthly_yearly_report_and_csv(self):
        today = timezone.now().date()
        rebuild_monthly_reports(today.year, today.month)
        user = User.objects.create_user(username="staff4", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        report_response = self.client.get(
            reverse("clients-monthly-yearly-report"),
            {"year": str(today.year), "month": str(today.month)},
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, "yillik kesim")

        csv_response = self.client.get(
            reverse("export-clients-yearly-report-csv"),
            {"year": str(today.year)},
        )
        self.assertEqual(csv_response.status_code, 200)
        content = csv_response.content.decode("utf-8")
        first_row = next(csv.reader(content.splitlines()))
        self.assertEqual(first_row, ["Client", "YearlyTotalOrders", "YearlyCompletedOrders", "YearlyAvgRating"])

    def test_client_360_and_pdf_and_range_export(self):
        today = timezone.now().date()
        AnalyticsSettings.objects.create(name="default", rating_completed_weight="60", rating_quality_weight="40")
        rebuild_monthly_reports(today.year, today.month)
        user = User.objects.create_user(username="staff5", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)

        detail_response = self.client.get(reverse("client-360-report", args=[self.client_obj.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "360")

        pdf_response = self.client.get(reverse("export-clients-report-pdf"), {"year": today.year, "month": today.month})
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")

        range_response = self.client.get(
            reverse("export-clients-report-csv"),
            {"from": today.isoformat(), "to": today.isoformat()},
        )
        self.assertEqual(range_response.status_code, 200)
        self.assertIn("Client", range_response.content.decode("utf-8"))

    def test_permission_denied_without_role(self):
        user = User.objects.create_user(username="staff6", password="x", is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse("clients-rating-report"))
        self.assertEqual(response.status_code, 302)

    def test_route_deviation_task_creates_alert_for_polyline(self):
        self.order.route_polyline = [{"lat": 41.31, "lon": 69.24}, {"lat": 41.35, "lon": 69.30}]
        self.order.route_deviation_threshold_km = "1.00"
        self.order.save(update_fields=["route_polyline", "route_deviation_threshold_km", "updated_at"])
        created = detect_route_deviation_task(self.order.id, self.driver.id, 41.50, 69.60)
        self.assertTrue(created)
        self.assertTrue(
            AlertEvent.objects.filter(order=self.order, alert_type=AlertType.ROUTE_DEVIATION).exists()
        )

    def test_route_deviation_task_resolves_alert_when_no_deviation(self):
        self.order.route_polyline = [{"lat": 41.31, "lon": 69.24}, {"lat": 41.35, "lon": 69.30}]
        self.order.route_deviation_threshold_km = "10.00"
        self.order.save(update_fields=["route_polyline", "route_deviation_threshold_km", "updated_at"])

        AlertEvent.objects.create(
            order=self.order,
            alert_type=AlertType.ROUTE_DEVIATION,
            threshold_minutes=0,
            driver=None,
            message="old deviation",
            resolved=False,
        )

        created = detect_route_deviation_task(self.order.id, self.driver.id, 41.33, 69.26)
        self.assertFalse(created)
        self.order.refresh_from_db()

        alert = AlertEvent.objects.get(order=self.order, alert_type=AlertType.ROUTE_DEVIATION, threshold_minutes=0)
        self.assertTrue(alert.resolved)

    def test_route_deviation_task_updates_alert_message_on_new_deviation(self):
        self.order.route_polyline = [{"lat": 41.31, "lon": 69.24}, {"lat": 41.35, "lon": 69.30}]
        self.order.route_deviation_threshold_km = "1.00"
        self.order.save(update_fields=["route_polyline", "route_deviation_threshold_km", "updated_at"])

        detect_route_deviation_task(self.order.id, self.driver.id, 41.50, 69.60)
        alert = AlertEvent.objects.get(order=self.order, alert_type=AlertType.ROUTE_DEVIATION, threshold_minutes=0)
        message_1 = alert.message

        detect_route_deviation_task(self.order.id, self.driver.id, 41.45, 69.50)
        alert.refresh_from_db()
        message_2 = alert.message

        self.assertNotEqual(message_1, message_2)

    def test_check_live_track_required_creates_alert_after_ketdik_without_ping(self):
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["status", "updated_at"])
        TelegramMessageLog.objects.create(
            order=self.order,
            chat_id=str(self.driver.telegram_user_id or ""),
            message_id="",
            event="driver_ketdik_webapp",
            payload={"driver_id": self.driver.id},
        )
        TelegramMessageLog.objects.filter(order=self.order, event="driver_ketdik_webapp").update(
            created_at=timezone.now() - timedelta(minutes=3)
        )
        created = check_live_track_required_task()
        self.assertGreaterEqual(created, 1)
        self.assertTrue(
            AlertEvent.objects.filter(order=self.order, driver=self.driver, alert_type=AlertType.NO_LIVE_TRACK).exists()
        )

    def test_check_live_track_required_resolves_when_ping_exists(self):
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["status", "updated_at"])
        log_time = timezone.now() - timedelta(minutes=3)
        TelegramMessageLog.objects.create(
            order=self.order,
            chat_id=str(self.driver.telegram_user_id or ""),
            message_id="",
            event="driver_ketdik_webapp",
            payload={"driver_id": self.driver.id},
        )
        TelegramMessageLog.objects.filter(order=self.order, event="driver_ketdik_webapp").update(created_at=log_time)
        AlertEvent.objects.create(
            order=self.order,
            driver=self.driver,
            alert_type=AlertType.NO_LIVE_TRACK,
            threshold_minutes=0,
            message="live yo'q",
            resolved=False,
        )
        LocationPing.objects.create(
            order=self.order,
            driver=self.driver,
            latitude="41.3111111",
            longitude="69.2444444",
            source=LocationSource.TELEGRAM,
            captured_at=timezone.now() - timedelta(minutes=1),
        )
        created = check_live_track_required_task()
        self.assertEqual(created, 0)
        alert = AlertEvent.objects.get(order=self.order, driver=self.driver, alert_type=AlertType.NO_LIVE_TRACK)
        self.assertTrue(alert.resolved)
