from django.db import models
from django.utils import timezone

from drivers.models import Driver
from orders.models import Order


class TelegramMessageLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="telegram_logs", null=True, blank=True)
    chat_id = models.CharField(max_length=50)
    message_id = models.CharField(max_length=50)
    event = models.CharField(max_length=60)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    signature = models.CharField(max_length=255, blank=True)
    source_ip = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class CriticalActionConfirmation(models.Model):
    token = models.CharField(max_length=64, unique=True)
    action = models.CharField(max_length=40)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="critical_confirmations")
    actor_id = models.BigIntegerField()
    payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class DriverOnboardingState(models.Model):
    telegram_user_id = models.BigIntegerField(unique=True)
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="onboarding_states", null=True, blank=True)
    step = models.CharField(max_length=64, default="idle")
    payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-updated_at"]
