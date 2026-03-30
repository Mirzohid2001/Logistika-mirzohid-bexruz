from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class DriverStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    BUSY = "busy", "Busy"
    OFFLINE = "offline", "Offline"


class DriverVerificationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class Driver(models.Model):
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, unique=True)
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    license_number = models.CharField(max_length=64, blank=True)
    license_issued_at = models.DateField(null=True, blank=True)
    license_expires_at = models.DateField(null=True, blank=True)
    license_photo_file_id = models.CharField(max_length=255, blank=True)
    registration_photo_file_id = models.CharField(max_length=255, blank=True)
    verification_status = models.CharField(
        max_length=20, choices=DriverVerificationStatus.choices, default=DriverVerificationStatus.APPROVED
    )
    verification_reason = models.CharField(max_length=255, blank=True)
    verification_updated_at = models.DateTimeField(null=True, blank=True)
    verification_updated_by_username = models.CharField(max_length=150, blank=True)
    registration_submitted_at = models.DateTimeField(null=True, blank=True)
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    status = models.CharField(max_length=20, choices=DriverStatus.choices, default=DriverStatus.OFFLINE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self) -> str:
        return self.full_name


class DriverDeliveryReview(models.Model):
    """Yuk yetkazilgach shofyor haqida yulduz va matnli fikr (reyting asosida)."""

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="driver_review",
    )
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="delivery_reviews")
    stars = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1–5 yulduz",
    )
    comment = models.TextField(blank=True)
    recorded_by_username = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Shofyor yetkazib berish sharhi"
        verbose_name_plural = "Shofyor yetkazib berish sharhlari"

    def __str__(self) -> str:
        return f"#{self.order_id} {self.driver_id} ({self.stars}★)"

    def clean(self) -> None:
        super().clean()
        if not self.order_id or not self.driver_id:
            return
        from dispatch.models import Assignment

        link = Assignment.objects.filter(order_id=self.order_id).first()
        if link is None:
            raise ValidationError({"order": "Bu buyurtmada haydovchi biriktirilmagan."})
        if link.driver_id != self.driver_id:
            raise ValidationError(
                {"driver": "Tanlangan shofyor buyurtmadagi biriktirish bilan mos kelmaydi."}
            )


class Vehicle(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="vehicles")
    plate_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=80)
    capacity_ton = models.DecimalField(max_digits=8, decimal_places=2)
    registration_document_number = models.CharField(max_length=64, blank=True)
    registration_photo_file_id = models.CharField(max_length=255, blank=True)
    calibration_expires_at = models.DateField(null=True, blank=True)
    tanker_document_photo_file_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.plate_number


class DriverVerificationAuditAction(models.TextChoices):
    SUBMITTED = "submitted", "Submitted"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class DriverVerificationAudit(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="verification_audits")
    action = models.CharField(max_length=20, choices=DriverVerificationAuditAction.choices)
    actor_username = models.CharField(max_length=150, blank=True)
    actor_id = models.BigIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
