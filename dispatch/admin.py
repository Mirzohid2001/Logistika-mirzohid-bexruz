from django.contrib import admin

from .models import Assignment, DriverOfferResponse


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "driver", "assigned_by", "assigned_at", "note")
    search_fields = ("order__id", "driver__full_name")


@admin.register(DriverOfferResponse)
class DriverOfferResponseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order",
        "driver",
        "decision",
        "approval_status",
        "note",
        "reviewed_by",
        "reviewed_at",
        "responded_at",
    )
    search_fields = ("order__id", "driver__full_name", "reviewed_by")
    list_filter = ("decision", "approval_status")
