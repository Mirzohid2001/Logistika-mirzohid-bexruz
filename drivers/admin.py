from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Driver, DriverDeliveryReview, DriverVerificationAudit, Vehicle


@admin.register(DriverDeliveryReview)
class DriverDeliveryReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "driver", "stars", "recorded_by_username", "created_at", "updated_at")
    list_filter = ("stars",)
    search_fields = ("order__id", "driver__full_name", "comment", "recorded_by_username")
    raw_id_fields = ("order", "driver")
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from drivers.services import recompute_driver_rating_score

        recompute_driver_rating_score(obj.driver)


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "full_name",
        "phone",
        "telegram_user_id",
        "status",
        "verification_status",
        "verification_reason",
        "license_number",
        "license_issued_at",
        "license_expires_at",
        "license_photo_file_id",
        "license_photo_preview",
        "registration_photo_file_id",
        "registration_photo_preview",
        "registration_submitted_at",
        "verification_updated_at",
        "verification_updated_by_username",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "verification_status", "license_expires_at")
    search_fields = ("full_name", "phone", "license_number")

    readonly_fields = (
        "license_photo_preview",
        "registration_photo_preview",
    )

    def _tg_img(self, obj: Driver, file_id: str) -> str:
        if not file_id:
            return "-"
        url = reverse("driver-telegram-file", args=[str(file_id)])
        return format_html('<img src="{}" style="max-width:220px; max-height:140px; object-fit:contain;" />', url)

    @admin.display(description="License photo")
    def license_photo_preview(self, obj: Driver) -> str:
        return self._tg_img(obj, obj.license_photo_file_id)

    @admin.display(description="Registration photo")
    def registration_photo_preview(self, obj: Driver) -> str:
        return self._tg_img(obj, obj.registration_photo_file_id)


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = (
        "plate_number",
        "vehicle_type",
        "capacity_ton",
        "driver",
        "registration_document_number",
        "registration_photo_file_id",
        "registration_photo_preview",
        "calibration_expires_at",
        "tanker_document_photo_file_id",
        "tanker_document_photo_preview",
        "created_at",
        "updated_at",
    )
    search_fields = ("plate_number", "driver__full_name")

    list_filter = ("vehicle_type", "calibration_expires_at")

    readonly_fields = (
        "registration_photo_preview",
        "tanker_document_photo_preview",
    )

    def _tg_img(self, file_id: str) -> str:
        if not file_id:
            return "-"
        url = reverse("driver-telegram-file", args=[str(file_id)])
        return format_html('<img src="{}" style="max-width:220px; max-height:140px; object-fit:contain;" />', url)

    @admin.display(description="Registration photo")
    def registration_photo_preview(self, obj: Vehicle) -> str:
        return self._tg_img(obj.registration_photo_file_id)

    @admin.display(description="Tanker document photo")
    def tanker_document_photo_preview(self, obj: Vehicle) -> str:
        return self._tg_img(obj.tanker_document_photo_file_id)


@admin.register(DriverVerificationAudit)
class DriverVerificationAuditAdmin(admin.ModelAdmin):
    list_display = ("id", "driver", "action", "actor_username", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("driver__full_name", "driver__phone", "actor_username", "reason")
