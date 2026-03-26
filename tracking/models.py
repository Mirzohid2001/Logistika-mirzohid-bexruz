from django.db import models

from drivers.models import Driver
from orders.models import Order


class LocationSource(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    WEB = "web", "Web"
    CHECKPOINT = "checkpoint", "Checkpoint"


class LocationPing(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="location_pings")
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="location_pings")
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)
    source = models.CharField(max_length=20, choices=LocationSource.choices)
    captured_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]
