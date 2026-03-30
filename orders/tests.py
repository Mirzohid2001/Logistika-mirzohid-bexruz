from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.management import call_command
from django.contrib.auth.models import Group, User
from django.urls import reverse
import tempfile
import os
from unittest.mock import patch

from dispatch.models import Assignment
from drivers.models import Driver, DriverStatus
from decimal import Decimal
from bot.models import TelegramMessageLog
from analytics.models import AlertEvent, AlertType

from orders.models import (
    Client,
    ContractTariff,
    Order,
    OrderExtraExpense,
    OrderSeal,
    OrderStatus,
    PaymentLedger,
    PaymentStatus,
    PaymentTerms,
    QuantityUnit,
    RevenueLedger,
)
from orders.quantity import quantity_to_metric_tonnes, shortage_tonnes
from orders.services import apply_client_contract, create_return_trip, split_shipment, transition_order


class OrderQuantityCustodyTests(TestCase):
    def test_liter_with_density_to_tonnes(self):
        t = quantity_to_metric_tonnes(Decimal("10000"), QuantityUnit.LITER, density_kg_per_liter=Decimal("0.84"))
        self.assertEqual(t, Decimal("8.4000"))

    def test_order_shortage_property(self):
        order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Diesel",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="U",
            contact_phone="+1",
            loaded_quantity=Decimal("10"),
            loaded_quantity_uom=QuantityUnit.TON,
            delivered_quantity=Decimal("9500"),
            delivered_quantity_uom=QuantityUnit.KG,
        )
        self.assertEqual(order.loaded_quantity_metric_ton, Decimal("10"))
        self.assertEqual(order.delivered_quantity_metric_ton, Decimal("9.5000"))
        self.assertEqual(order.quantity_shortage_metric_ton, Decimal("0.5000"))

    def test_delivered_liter_uses_delivered_density_then_fallback(self):
        order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Diesel",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="U",
            contact_phone="+1",
            density_kg_per_liter=Decimal("0.80"),
            delivered_quantity=Decimal("10000"),
            delivered_quantity_uom=QuantityUnit.LITER,
            delivered_density_kg_per_liter=Decimal("0.84"),
        )
        self.assertEqual(order.delivered_quantity_metric_ton, Decimal("8.4000"))
        order.delivered_density_kg_per_liter = None
        order.save(update_fields=["delivered_density_kg_per_liter"])
        self.assertEqual(order.delivered_quantity_metric_ton, Decimal("8.0000"))


class OrderFinanceTests(TestCase):
    def test_gross_margin_calculation(self):
        order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="User",
            contact_phone="+99890",
            client_price="2000000.00",
            driver_fee="1200000.00",
            fuel_cost="100000.00",
            extra_cost="50000.00",
            penalty_amount="25000.00",
        )
        self.assertEqual(str(order.gross_margin), "625000.00")


class OrderLifecycleTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="10.00",
            pickup_time=timezone.now(),
            contact_name="User",
            contact_phone="+99890",
            client_price="2000000.00",
            driver_fee="1200000.00",
        )

    def test_assigned_transition_allowed(self):
        changed = transition_order(self.order, OrderStatus.ASSIGNED, "dispatcher")
        self.assertTrue(changed)

    def test_complete_requires_financial_and_sets_delivered_at(self):
        driver = Driver.objects.create(full_name="A", phone="+99891", status=DriverStatus.AVAILABLE)
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        changed = transition_order(self.order, OrderStatus.COMPLETED, "dispatcher")
        self.assertTrue(changed)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)

    def test_transition_completed_to_issue_clears_delivered_at(self):
        driver = Driver.objects.create(full_name="A", phone="+99891", status=DriverStatus.AVAILABLE)
        # COMPLETED uchun transition_order delivered_at qo'yishi kerak.
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        transition_order(self.order, OrderStatus.COMPLETED, "dispatcher")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)

        changed = transition_order(self.order, OrderStatus.ISSUE, "dispatcher")
        self.assertTrue(changed)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.delivered_at)

    def test_transition_to_in_transit_sets_actual_start_at(self):
        driver = Driver.objects.create(full_name="A", phone="+99891", status=DriverStatus.AVAILABLE)
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.actual_start_at)

    def test_transition_in_transit_to_issue_clears_actual_start_at(self):
        driver = Driver.objects.create(full_name="A", phone="+99891", status=DriverStatus.AVAILABLE)
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.actual_start_at)

        changed = transition_order(self.order, OrderStatus.ISSUE, "dispatcher")
        self.assertTrue(changed)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.actual_start_at)

    def test_transition_issue_resets_financials_and_ledgers(self):
        driver = Driver.objects.create(full_name="A", phone="+99891", status=DriverStatus.AVAILABLE)
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")

        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])

        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        transition_order(self.order, OrderStatus.COMPLETED, "dispatcher")

        # Ledgerlar ISSUE bo'lganda yaratilishi/yangilanishi kerak.
        self.assertFalse(PaymentLedger.objects.filter(order=self.order).exists())
        self.assertFalse(RevenueLedger.objects.filter(order=self.order).exists())

        changed = transition_order(self.order, OrderStatus.ISSUE, "dispatcher")
        self.assertTrue(changed)

        self.order.refresh_from_db()
        self.assertEqual(self.order.client_price, 0)
        self.assertEqual(self.order.driver_fee, 0)

        payment_ledger = PaymentLedger.objects.get(order=self.order)
        revenue_ledger = RevenueLedger.objects.get(order=self.order)

        self.assertEqual(payment_ledger.amount, 0)
        self.assertEqual(payment_ledger.paid_amount, 0)
        self.assertEqual(payment_ledger.status, PaymentStatus.PENDING)
        self.assertIsNone(payment_ledger.paid_at)

        self.assertEqual(revenue_ledger.amount, 0)
        self.assertEqual(revenue_ledger.received_amount, 0)
        self.assertEqual(revenue_ledger.status, PaymentStatus.PENDING)
        self.assertIsNone(revenue_ledger.received_at)

    @override_settings(ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED=False)
    def test_transition_issue_keeps_financials_when_policy_off(self):
        driver = Driver.objects.create(full_name="B", phone="+99892", status=DriverStatus.AVAILABLE)
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])
        transition_order(self.order, OrderStatus.IN_TRANSIT, "dispatcher")
        transition_order(self.order, OrderStatus.COMPLETED, "dispatcher")
        self.order.refresh_from_db()
        expected_client = self.order.client_price

        self.assertTrue(transition_order(self.order, OrderStatus.ISSUE, "dispatcher"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.client_price, expected_client)
        self.assertFalse(PaymentLedger.objects.filter(order=self.order).exists())


class ImportClientsCommandTests(TestCase):
    def test_import_clients_command(self):
        csv_content = "name,contact_name,phone,is_active\nAcme,Ali,+99890,true\n"
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".csv", delete=False) as temp:
            temp.write(csv_content)
            temp_path = temp.name
        try:
            call_command("import_clients", temp_path)
            self.assertTrue(Client.objects.filter(name="Acme").exists())
        finally:
            os.unlink(temp_path)


class OrderDomainDeepeningTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Contract Client",
            payment_terms=PaymentTerms.PREPAID,
            contract_base_rate_per_ton="100000",
            contract_min_fee="900000",
        )
        self.order = Order.objects.create(
            client=self.client_obj,
            from_location="A",
            to_location="B",
            cargo_type="Oil",
            weight_ton="8.00",
            pickup_time=timezone.now(),
            contact_name="User",
            contact_phone="+99890",
            client_price="0",
            driver_fee="500000",
        )
        self.user = User.objects.create_user(username="staff", password="x", is_staff=True)
        dispatcher_group, _ = Group.objects.get_or_create(name="Dispatcher")
        self.user.groups.add(dispatcher_group)
        self.client.force_login(self.user)

    def test_apply_client_contract_uses_contract_tariff(self):
        ContractTariff.objects.create(
            client=self.client_obj,
            cargo_type="Oil",
            rate_per_ton="150000",
            min_fee="1000000",
            is_active=True,
        )
        apply_client_contract(self.order)
        self.assertEqual(self.order.payment_terms, PaymentTerms.PREPAID)
        self.assertEqual(str(self.order.client_price), "1200000.00")

    def test_create_return_trip(self):
        ret = create_return_trip(self.order, changed_by="dispatcher")
        self.assertTrue(ret.is_return_trip)
        self.assertEqual(ret.from_location, self.order.to_location)
        self.assertEqual(ret.to_location, self.order.from_location)
        self.assertEqual(ret.return_of_id, self.order.id)

    def test_split_shipment_creates_children(self):
        children = split_shipment(self.order, parts=3, changed_by="dispatcher")
        self.assertEqual(len(children), 3)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)
        self.assertTrue(all(child.parent_order_id == self.order.id for child in children))

    def test_order_reopen_endpoint(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status", "updated_at"])
        response = self.client.post(reverse("order-reopen", args=[self.order.pk]))
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)

    def test_order_return_trip_endpoint(self):
        response = self.client.post(reverse("order-return-trip", args=[self.order.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Order.objects.filter(return_of=self.order).exists())

    def test_order_split_endpoint(self):
        response = self.client.post(reverse("order-split", args=[self.order.pk]), {"parts": 2})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.filter(parent_order=self.order).count(), 2)

    def test_order_seal_add_update_delete(self):
        r = self.client.post(
            reverse("order-seal-add", args=[self.order.pk]),
            {"compartment": "Old", "seal_number_loading": "PL-999"},
        )
        self.assertEqual(r.status_code, 302)
        seal = OrderSeal.objects.get(order=self.order)
        self.assertEqual(seal.seal_number_loading, "PL-999")
        self.assertTrue((seal.loading_recorded_by or "").startswith("web:"))
        self.assertEqual((seal.seal_number_unloading or "").strip(), "")

        pfx = f"seal{seal.pk}"
        r2 = self.client.post(
            reverse("order-seal-update", args=[self.order.pk, seal.pk]),
            {f"{pfx}-seal_number_unloading": "PL-999"},
        )
        self.assertEqual(r2.status_code, 302)
        seal.refresh_from_db()
        self.assertEqual(seal.seal_number_unloading, "PL-999")
        self.assertIsNotNone(seal.unloading_recorded_at)

        r3 = self.client.post(
            reverse("order-seal-update", args=[self.order.pk, seal.pk]),
            {
                f"{pfx}-seal_number_unloading": "PL-999",
                f"{pfx}-is_broken": "on",
                f"{pfx}-broken_note": "Plomba buzilgan",
            },
        )
        self.assertEqual(r3.status_code, 302)
        seal.refresh_from_db()
        self.assertTrue(seal.is_broken)
        self.assertIsNotNone(seal.broken_at)

        r4 = self.client.post(reverse("order-seal-delete", args=[self.order.pk, seal.pk]))
        self.assertEqual(r4.status_code, 302)
        self.assertFalse(OrderSeal.objects.filter(pk=seal.pk).exists())

    def test_order_extra_expense_add_and_total(self):
        response = self.client.post(
            reverse("order-expense-add", args=[self.order.pk]),
            {
                "category": "toll",
                "amount": "125000",
                "note": "Post to'lovi",
                "incurred_at": "2026-03-30T10:30",
            },
        )
        self.assertEqual(response.status_code, 302)
        exp = OrderExtraExpense.objects.get(order=self.order)
        self.assertEqual(exp.category, "toll")
        self.assertEqual(str(exp.amount), "125000.00")
        self.order.refresh_from_db()
        self.assertEqual(str(self.order.additional_expense_total), "125000.00")

    @patch("orders.views.send_chat_message")
    def test_order_live_reminder_sends_driver_message_and_logs_event(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Live Driver",
            phone="+998901234000",
            status=DriverStatus.BUSY,
            telegram_user_id=123456789,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.save(update_fields=["status", "updated_at"])

        response = self.client.post(reverse("order-live-reminder", args=[self.order.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("order-detail", args=[self.order.pk]))
        send_chat_message_mock.assert_called_once()
        self.assertTrue(
            TelegramMessageLog.objects.filter(order=self.order, event="driver_live_reminder_manual").exists()
        )

    @patch("orders.views.send_chat_message")
    def test_order_live_reminder_skips_when_order_not_in_transit(self, send_chat_message_mock):
        driver = Driver.objects.create(
            full_name="Idle Driver",
            phone="+998901234001",
            status=DriverStatus.AVAILABLE,
            telegram_user_id=987654321,
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.ASSIGNED
        self.order.save(update_fields=["status", "updated_at"])

        response = self.client.post(reverse("order-live-reminder", args=[self.order.pk]))
        self.assertEqual(response.status_code, 302)
        send_chat_message_mock.assert_not_called()
        self.assertFalse(
            TelegramMessageLog.objects.filter(order=self.order, event="driver_live_reminder_manual").exists()
        )

    def test_client_crud_endpoints(self):
        list_response = self.client.get(reverse("client-list"))
        self.assertEqual(list_response.status_code, 200)

        create_response = self.client.post(
            reverse("client-create"),
            {
                "name": "Web Client",
                "contact_name": "Ali",
                "phone": "+998901111111",
                "sla_minutes": 100,
                "contract_base_rate_per_ton": "120000",
                "contract_min_fee": "800000",
                "payment_terms": PaymentTerms.DEFERRED,
                "is_active": True,
            },
        )
        self.assertEqual(create_response.status_code, 302)
        created = Client.objects.get(name="Web Client")

        edit_response = self.client.post(
            reverse("client-edit", args=[created.pk]),
            {
                "name": "Web Client Updated",
                "contact_name": "Ali",
                "phone": "+998901111111",
                "sla_minutes": 90,
                "contract_base_rate_per_ton": "120000",
                "contract_min_fee": "800000",
                "payment_terms": PaymentTerms.PREPAID,
                "is_active": True,
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        created.refresh_from_db()
        self.assertEqual(created.name, "Web Client Updated")

        archive_response = self.client.post(reverse("client-archive", args=[created.pk]))
        self.assertEqual(archive_response.status_code, 302)
        created.refresh_from_db()
        self.assertFalse(created.is_active)

        restore_response = self.client.post(reverse("client-restore", args=[created.pk]))
        self.assertEqual(restore_response.status_code, 302)
        created.refresh_from_db()
        self.assertTrue(created.is_active)

    def test_cleanup_orders_data_command(self):
        order = Order.objects.create(
            from_location="X",
            to_location="Y",
            cargo_type="Oil",
            weight_ton="3.00",
            pickup_time=timezone.now(),
            contact_name="User",
            contact_phone="+99890",
            status=OrderStatus.COMPLETED,
            comment="",
            delivered_at=None,
        )
        call_command("cleanup_orders_data")
        order.refresh_from_db()
        self.assertIsNotNone(order.client_id)
        self.assertIsNotNone(order.delivered_at)
        self.assertEqual(order.comment, "Legacy data cleanup")

    @override_settings(SHORTAGE_WARNING_KG=70, SHORTAGE_PENALTY_KG=100)
    def test_order_finish_confirm_requires_note_for_shortage(self):
        driver = Driver.objects.create(
            full_name="Fraud Driver",
            phone="+998901231231",
            status=DriverStatus.BUSY,
            rating_score=Decimal("95.00"),
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.loaded_quantity = Decimal("10000")
        self.order.loaded_quantity_uom = QuantityUnit.KG
        self.order.delivered_quantity = Decimal("9900")
        self.order.delivered_quantity_uom = QuantityUnit.KG
        self.order.save(
            update_fields=[
                "status",
                "loaded_quantity",
                "loaded_quantity_uom",
                "delivered_quantity",
                "delivered_quantity_uom",
                "updated_at",
            ]
        )
        TelegramMessageLog.objects.create(order=self.order, event="driver_finish_requested", chat_id="1", message_id="1")

        response = self.client.post(reverse("order-finish-confirm", args=[self.order.pk]), {"shortage_note": ""})
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_TRANSIT)

    @override_settings(
        SHORTAGE_WARNING_KG=70,
        SHORTAGE_PENALTY_KG=100,
        SHORTAGE_PENALTY_POINTS_70_99=2,
    )
    def test_order_finish_confirm_applies_rating_penalty_and_alert(self):
        driver = Driver.objects.create(
            full_name="Fraud Driver 2",
            phone="+998901231232",
            status=DriverStatus.BUSY,
            rating_score=Decimal("96.00"),
        )
        Assignment.objects.create(order=self.order, driver=driver, assigned_by="dispatcher")
        self.order.status = OrderStatus.IN_TRANSIT
        self.order.loaded_quantity = Decimal("10000")
        self.order.loaded_quantity_uom = QuantityUnit.KG
        self.order.delivered_quantity = Decimal("9900")
        self.order.delivered_quantity_uom = QuantityUnit.KG
        self.order.save(
            update_fields=[
                "status",
                "loaded_quantity",
                "loaded_quantity_uom",
                "delivered_quantity",
                "delivered_quantity_uom",
                "updated_at",
            ]
        )
        TelegramMessageLog.objects.create(order=self.order, event="driver_finish_requested", chat_id="1", message_id="2")

        response = self.client.post(
            reverse("order-finish-confirm", args=[self.order.pk]),
            {"shortage_note": "Akt bo‘yicha 100kg farq qayd etildi"},
        )
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        driver.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.order.shortage_kg, Decimal("100.000"))
        self.assertEqual(self.order.shortage_penalty_points, 5)
        # Reyting: sharh yo‘q → asos 100; kamomad jarimasi 5 ball → 95
        self.assertEqual(driver.rating_score, Decimal("95.00"))
        self.assertTrue(
            AlertEvent.objects.filter(order=self.order, alert_type=AlertType.FUEL_SHORTAGE, resolved=False).exists()
        )
