from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
import tempfile
import os

from analytics.models import DriverPerformanceSnapshot
from dispatch.models import Assignment
from drivers.models import Driver, DriverDeliveryReview, Vehicle
from drivers.services import recompute_driver_rating_score
from orders.models import Order, OrderStatus
from tracking.models import LocationPing, LocationSource


class ImportDriversCommandTests(TestCase):
    def test_import_drivers_command(self):
        csv_content = (
            "full_name,phone,status,telegram_user_id,plate_number,vehicle_type,capacity_ton\n"
            "Driver A,+99890123,available,123456,01A777AA,truck,12.5\n"
        )
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".csv", delete=False) as temp:
            temp.write(csv_content)
            temp_path = temp.name
        try:
            call_command("import_drivers", temp_path)
            self.assertTrue(Driver.objects.filter(phone="+99890123").exists())
            self.assertTrue(Vehicle.objects.filter(plate_number="01A777AA").exists())
        finally:
            os.unlink(temp_path)


class DriverViewsTests(TestCase):
    def setUp(self):
        self.driver = Driver.objects.create(full_name="Driver A", phone="+99890111")
        self.order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="User",
            contact_phone="+99890",
            status=OrderStatus.COMPLETED,
            client_price="1200000",
            driver_fee="800000",
            delivered_at=timezone.now(),
        )
        Assignment.objects.create(order=self.order, driver=self.driver, assigned_by="dispatcher")
        DriverPerformanceSnapshot.objects.create(
            driver=self.driver,
            period_year=timezone.now().year,
            period_month=timezone.now().month,
            completed_count=3,
            cancel_count=1,
            issue_count=0,
            on_time_rate="75.00",
            monthly_earnings="2500000",
            yearly_earnings="12000000",
            rating_score="82.50",
        )
        LocationPing.objects.create(
            order=self.order,
            driver=self.driver,
            latitude="41.3100000",
            longitude="69.2400000",
            source=LocationSource.WEB,
            captured_at=timezone.now(),
        )
        self.analyst_group, _ = Group.objects.get_or_create(name="Analyst")
        self.dispatcher_group, _ = Group.objects.get_or_create(name="Dispatcher")

    def test_driver_pages_load_for_authorized_group(self):
        user = User.objects.create_user(username="staff-driver", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)

        list_response = self.client.get(reverse("driver-list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "va analitikasi")
        self.assertContains(list_response, "30 kunda tugaydi")

        detail_response = self.client.get(reverse("driver-detail", args=[self.driver.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "360")

    @override_settings(DRIVER_DOC_EXPIRY_NEAR_DAYS=14)
    def test_driver_list_shows_configurable_expiry_near_label(self):
        user = User.objects.create_user(username="staff-driver-expiry", password="x", is_staff=True)
        user.groups.add(self.analyst_group)
        self.client.force_login(user)
        response = self.client.get(reverse("driver-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "14 kunda tugaydi")

    def test_driver_pages_redirect_without_group(self):
        user = User.objects.create_user(username="staff-no-group", password="x", is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse("driver-list"))
        self.assertEqual(response.status_code, 302)

    def test_driver_crud_and_vehicle_crud(self):
        user = User.objects.create_user(username="staff-dispatch", password="x", is_staff=True)
        user.groups.add(self.dispatcher_group)
        self.client.force_login(user)

        create_response = self.client.post(
            reverse("driver-create"),
            {
                "full_name": "Driver B",
                "phone": "+99890222",
                "telegram_user_id": "999",
                "status": "available",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        created_driver = Driver.objects.get(phone="+99890222")

        edit_response = self.client.post(
            reverse("driver-edit", args=[created_driver.pk]),
            {
                "full_name": "Driver B Updated",
                "phone": "+99890222",
                "telegram_user_id": "999",
                "status": "busy",
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        created_driver.refresh_from_db()
        self.assertEqual(created_driver.full_name, "Driver B Updated")

        archive_response = self.client.post(reverse("driver-archive", args=[created_driver.pk]))
        self.assertEqual(archive_response.status_code, 302)
        created_driver.refresh_from_db()
        self.assertEqual(created_driver.status, "offline")

        restore_response = self.client.post(reverse("driver-restore", args=[created_driver.pk]))
        self.assertEqual(restore_response.status_code, 302)
        created_driver.refresh_from_db()
        self.assertEqual(created_driver.status, "available")

        vehicle_create = self.client.post(
            reverse("vehicle-create", args=[created_driver.pk]),
            {
                "plate_number": "01B123CD",
                "vehicle_type": "truck",
                "capacity_ton": "15.00",
            },
        )
        self.assertEqual(vehicle_create.status_code, 302)
        vehicle = Vehicle.objects.get(plate_number="01B123CD")

        vehicle_edit = self.client.post(
            reverse("vehicle-edit", args=[created_driver.pk, vehicle.pk]),
            {
                "plate_number": "01B123CD",
                "vehicle_type": "truck-xl",
                "capacity_ton": "16.00",
            },
        )
        self.assertEqual(vehicle_edit.status_code, 302)
        vehicle.refresh_from_db()
        self.assertEqual(vehicle.vehicle_type, "truck-xl")

        vehicle_delete = self.client.post(reverse("vehicle-delete", args=[created_driver.pk, vehicle.pk]))
        self.assertEqual(vehicle_delete.status_code, 302)
        self.assertFalse(Vehicle.objects.filter(pk=vehicle.pk).exists())


class DriverDeliveryReviewRatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="revstaff", password="x", is_staff=True)
        g, _ = Group.objects.get_or_create(name="Dispatcher")
        self.user.groups.add(g)
        self.client.force_login(self.user)
        self.driver = Driver.objects.create(
            full_name="R Driver", phone="+99890111999", rating_score=Decimal("100.00")
        )
        self.order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="U",
            contact_phone="+99890",
            status=OrderStatus.COMPLETED,
            client_price="0",
            driver_fee="100",
            delivered_at=timezone.now(),
        )
        Assignment.objects.create(order=self.order, driver=self.driver, assigned_by="dispatcher")

    def test_post_review_updates_rating(self):
        r = self.client.post(
            reverse("order-driver-review", args=[self.order.pk]),
            {"stars": "4", "comment": "Yaxshi"},
        )
        self.assertEqual(r.status_code, 302)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.rating_score, Decimal("80.00"))
        self.assertTrue(DriverDeliveryReview.objects.filter(order=self.order, stars=4).exists())

    def test_average_two_orders_two_reviews(self):
        o2 = Order.objects.create(
            from_location="C",
            to_location="D",
            cargo_type="Oil",
            weight_ton="5.00",
            pickup_time=timezone.now(),
            contact_name="U",
            contact_phone="+99891",
            status=OrderStatus.COMPLETED,
            client_price="0",
            driver_fee="50",
            delivered_at=timezone.now(),
        )
        Assignment.objects.create(order=o2, driver=self.driver, assigned_by="dispatcher")
        self.client.post(reverse("order-driver-review", args=[self.order.pk]), {"stars": "5", "comment": ""})
        self.client.post(reverse("order-driver-review", args=[o2.pk]), {"stars": "3", "comment": ""})
        self.driver.refresh_from_db()
        # (5+3)/2 = 4 → 80
        self.assertEqual(self.driver.rating_score, Decimal("80.00"))

    def test_recompute_subtracts_shortage_penalties(self):
        self.order.shortage_penalty_points = 10
        self.order.save(update_fields=["shortage_penalty_points", "updated_at"])
        DriverDeliveryReview.objects.create(order=self.order, driver=self.driver, stars=5, comment="")
        recompute_driver_rating_score(self.driver)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.rating_score, Decimal("90.00"))

    def test_recompute_none_driver_is_safe(self):
        recompute_driver_rating_score(None)

    def test_get_driver_review_aggregates_empty(self):
        from drivers.services import get_driver_review_aggregates

        self.assertEqual(get_driver_review_aggregates(None), (0, None))
        cnt, avg = get_driver_review_aggregates(self.driver)
        self.assertEqual(cnt, 0)
        self.assertIsNone(avg)

    def test_review_full_clean_rejects_wrong_driver(self):
        other = Driver.objects.create(full_name="Other D", phone="+99890111002")
        r = DriverDeliveryReview(order=self.order, driver=other, stars=3, comment="")
        with self.assertRaises(ValidationError):
            r.full_clean()

    def test_comment_too_long_rejected(self):
        r = self.client.post(
            reverse("order-driver-review", args=[self.order.pk]),
            {"stars": "5", "comment": "x" * 2001},
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(DriverDeliveryReview.objects.filter(order=self.order).exists())

    def test_non_completed_order_review_blocked(self):
        self.order.status = OrderStatus.NEW
        self.order.save(update_fields=["status", "updated_at"])
        r = self.client.post(
            reverse("order-driver-review", args=[self.order.pk]),
            {"stars": "5", "comment": ""},
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(DriverDeliveryReview.objects.filter(order=self.order).exists())
