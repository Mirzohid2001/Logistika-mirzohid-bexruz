from django.db import models
from django.utils import timezone

from drivers.models import Driver
from orders.models import Order


class Assignment(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="assignment")
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="assignments")
    assigned_by = models.CharField(max_length=120, blank=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-assigned_at"]


class DriverOfferDecision(models.TextChoices):
    ACCEPT = "accept", "Qabul"
    REJECT = "reject", "Rad"
    ISSUE = "issue", "Muammo"


class DriverOfferApproval(models.TextChoices):
    PENDING = "pending", "Kutilmoqda"
    APPROVED = "approved", "Tasdiqlandi"
    DECLINED = "declined", "Rad etildi"


class DriverOfferResponse(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="driver_responses")
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="offer_responses")
    decision = models.CharField(max_length=20, choices=DriverOfferDecision.choices)
    approval_status = models.CharField(
        max_length=20,
        choices=DriverOfferApproval.choices,
        default=DriverOfferApproval.PENDING,
    )
    note = models.CharField(max_length=255, blank=True)
    reviewed_by = models.CharField(max_length=120, blank=True)
    responded_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-responded_at"]
        unique_together = ("order", "driver")
