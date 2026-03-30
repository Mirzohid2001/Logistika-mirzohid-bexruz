from django.contrib import admin

from .models import TelegramGroupConfig, TelegramMessageLog


@admin.register(TelegramMessageLog)
class TelegramMessageLogAdmin(admin.ModelAdmin):
    list_display = ("order", "chat_id", "message_id", "event", "created_at")
    search_fields = ("order__id", "chat_id", "message_id")


@admin.register(TelegramGroupConfig)
class TelegramGroupConfigAdmin(admin.ModelAdmin):
    list_display = ("group_type", "name", "chat_id", "message_thread_id", "is_active", "updated_at")
    list_filter = ("group_type", "is_active")
    search_fields = ("name", "chat_id")
