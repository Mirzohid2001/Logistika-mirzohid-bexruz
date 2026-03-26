from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from analytics.models import ClientAnalyticsSnapshot, DriverPerformanceSnapshot, MonthlyFinanceReport
from orders.models import Client, Order, PaymentLedger, RevenueLedger


class Command(BaseCommand):
    help = "Create default admin role groups (operatsiyani admin/Owner boshqaradi; Dispatcher = tarixiy nom, Owner bilan bir xil ruxsat)"

    def handle(self, *args, **options):
        owner, _ = Group.objects.get_or_create(name="Owner")
        dispatcher, _ = Group.objects.get_or_create(name="Dispatcher")
        finance, _ = Group.objects.get_or_create(name="Finance")
        analyst, _ = Group.objects.get_or_create(name="Analyst")

        order_ct = ContentType.objects.get_for_model(Order)
        client_ct = ContentType.objects.get_for_model(Client)
        payment_ct = ContentType.objects.get_for_model(PaymentLedger)
        revenue_ct = ContentType.objects.get_for_model(RevenueLedger)
        client_analytics_ct = ContentType.objects.get_for_model(ClientAnalyticsSnapshot)
        driver_analytics_ct = ContentType.objects.get_for_model(DriverPerformanceSnapshot)
        monthly_report_ct = ContentType.objects.get_for_model(MonthlyFinanceReport)

        order_perms = Permission.objects.filter(content_type=order_ct)
        client_perms = Permission.objects.filter(content_type=client_ct)
        payment_perms = Permission.objects.filter(content_type=payment_ct)
        revenue_perms = Permission.objects.filter(content_type=revenue_ct)
        analytics_perms = Permission.objects.filter(
            content_type__in=[client_analytics_ct, driver_analytics_ct, monthly_report_ct]
        )

        full_ops = order_perms | client_perms | payment_perms | revenue_perms | analytics_perms
        owner.permissions.set(full_ops)
        # Dispetcher alohida lavozim emas: admin bilan bir xil to‘liq veb-operatsiya
        dispatcher.permissions.set(full_ops)
        finance.permissions.set(
            payment_perms
            | revenue_perms
            | Permission.objects.filter(content_type=order_ct, codename__in=["view_order"])
            | Permission.objects.filter(content_type=client_ct, codename__in=["view_client"])
            | analytics_perms
        )
        analyst.permissions.set(
            Permission.objects.filter(content_type=order_ct, codename__startswith="view_")
            | Permission.objects.filter(content_type=client_ct, codename__startswith="view_")
            | analytics_perms
        )
        self.stdout.write(self.style.SUCCESS("Role groups are ready"))
