from django.contrib import admin

from .models import LocationPing


@admin.register(LocationPing)
class LocationPingAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "driver", "latitude", "longitude", "source", "captured_at", "created_at")
    list_filter = ("source",)
